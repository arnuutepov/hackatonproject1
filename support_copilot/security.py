"""Deny-by-default HTTP boundary. Host/Origin are not authentication."""
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import StringConstraints
from starlette.concurrency import run_in_threadpool

from support_copilot.auth_store import AuthStore, LoginBusy, ROLES, SESSION_SECONDS
from support_copilot.schemas import ContractModel

COOKIE = 'oguzki_session'
STATIC = Path(__file__).with_name('static')
PUBLIC = {('GET', '/auth/login'), ('POST', '/auth/login'),
          ('GET', '/auth/login.css'), ('GET', '/auth/login.js')}
GAME_PATH = re.compile(r'^/api/v1/game/tickets(?:/[0-9a-fA-F-]{36}/messages)?$')


def loopback(request: Request) -> bool:
    try:
        return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        return False


def check_origin(request: Request, required=True):
    expected = os.environ.get('COPILOT_PUBLIC_ORIGIN') or f'{request.url.scheme}://{request.url.netloc}'
    origin = request.headers.get('origin')
    if (required and not origin) or (origin is not None and origin != expected):
        raise HTTPException(403, 'Same-origin request required')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Cross-site request rejected')


def insecure_local_http(request: Request) -> bool:
    return (os.environ.get('COPILOT_INSECURE_LOCAL_HTTP') == '1' and loopback(request)
            and request.url.hostname in {'127.0.0.1', 'localhost', '::1'})


def operator(request: Request) -> dict:
    principal = getattr(request.state, 'operator', None)
    if principal is None:
        raise HTTPException(401, 'Operator session required')
    return principal


def require_roles(*roles):
    def permission(principal=Depends(operator)):
        if principal['role'] not in roles:
            raise HTTPException(403, 'Insufficient operator role')
        return principal
    return permission


async def verify_game(request: Request) -> bytes:
    if request.method != 'POST' or request.url.query:
        raise HTTPException(401, 'Invalid game signature')
    key_id = request.headers.get('x-game-key-id', '')
    nonce = request.headers.get('x-game-nonce', '')
    at = request.headers.get('x-game-timestamp', '')
    signature = request.headers.get('x-game-signature', '')
    try:
        keys = json.loads(os.environ.get('COPILOT_GAME_HMAC_KEYS', '{}'))
        secret = keys.get(key_id) if isinstance(keys, dict) else None
        stamp = int(at)
        UUID(nonce)
    except (ValueError, TypeError):
        raise HTTPException(401, 'Invalid game signature') from None
    if not isinstance(secret, str) or len(secret.encode()) < 32:
        raise HTTPException(401, 'Invalid game signature')
    if not re.fullmatch(r'v1=[0-9a-f]{64}', signature) or len(nonce) > 36:
        raise HTTPException(401, 'Invalid game signature')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', key_id) or abs(time.time() - stamp) > 300:
        raise HTTPException(401, 'Invalid game signature')
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 2_100_000:
            raise HTTPException(413, 'Game event too large')
        chunks.append(chunk)
    body = b''.join(chunks)
    canonical = f'v1\nPOST\n{request.url.path}\n{at}\n{nonce}\n{hashlib.sha256(body).hexdigest()}'.encode()
    expected = 'v1=' + hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, 'Invalid game signature')
    if not await run_in_threadpool(AuthStore().consume_nonce, key_id, nonce, stamp):
        raise HTTPException(409, 'Game nonce already used')
    request.state.game_actor = 'game:' + key_id
    return body


class SecurityMiddleware:
    """Every route, including future mounts/docs, requires identity unless explicitly public."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 4401})
            return
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        body = None
        try:
            mode = os.environ.get('COPILOT_AUTH_MODE', 'required')
            if mode not in {'required', 'local', 'oidc'}:
                raise HTTPException(503, 'Invalid authentication configuration')
            public = (request.method, request.url.path) in PUBLIC
            if public:
                if mode == 'oidc' and request.url.path == '/auth/login':
                    if request.method == 'POST':
                        raise HTTPException(403, 'Password login disabled in SSO mode')
                    from fastapi.responses import RedirectResponse
                    await RedirectResponse('/oauth2/start?rd=%2F', status_code=303)(scope, receive, send)
                    return
            elif GAME_PATH.fullmatch(request.url.path):
                body = await verify_game(request)
            elif mode == 'oidc':
                from support_copilot.oidc_identity import identity
                request.state.operator = await run_in_threadpool(identity, request)
                if request.url.path.startswith('/auth/operators'):
                    raise HTTPException(403, 'Manage staff in the organizational identity provider')
                if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                    check_origin(request)
            elif mode == 'local':
                # Explicit debugging mode still checks the TCP peer, not a client-supplied Host.
                if not loopback(request) or request.url.hostname not in {'127.0.0.1', 'localhost', '::1'}:
                    raise HTTPException(403, 'Local development requires a loopback peer and host')
                if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                    check_origin(request, required=False)
                request.state.operator = {'id': 'local_operator', 'username': 'local_operator',
                                          'role': 'agent', 'active': 1, 'local_development': True}
            else:
                configured = os.environ.get('COPILOT_TRIAGE_TOKEN', '')
                supplied = request.headers.get('authorization', '')
                service = bool(len(configured) >= 32 and supplied.isascii() and configured.isascii()
                               and hmac.compare_digest(supplied, 'Bearer ' + configured))
                if service:
                    if request.url.path not in {'/triage', '/health'}:
                        raise HTTPException(403, 'Service token is restricted to triage and health')
                else:
                    principal = await run_in_threadpool(AuthStore().session, request.cookies.get(COOKIE, ''))
                    if principal is None:
                        raise HTTPException(401, 'Operator session required')
                    if principal['role'] not in ROLES:
                        raise HTTPException(403, 'Operator role required')
                    if request.url.scheme != 'https' and not insecure_local_http(request):
                        raise HTTPException(403, 'HTTPS required for operator sessions')
                    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                        check_origin(request)
                    request.state.operator = principal
        except HTTPException as exc:
            if exc.status_code == 401 and request.method == 'GET' and request.url.path == '/':
                response = FileResponse(STATIC / 'login.html', status_code=401)
            else:
                response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
            await response(scope, receive, send)
            return
        except (sqlite3.Error, OSError):
            await JSONResponse({'detail': 'Authentication storage unavailable'}, status_code=503)(scope, receive, send)
            return
        if body is not None:
            sent = False
            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                return await receive()
            await self.app(scope, replay, send)
        else:
            await self.app(scope, receive, send)


router = APIRouter(prefix='/auth')
Username = Annotated[str, StringConstraints(pattern=r'^[a-zA-Z0-9_.-]{3,64}$')]
Password = Annotated[str, StringConstraints(min_length=15, max_length=256)]


class Login(ContractModel):
    username: Username
    password: Password


class NewOperator(Login):
    role: Literal['agent', 'lead', 'admin']


class UpdateOperator(ContractModel):
    role: Literal['agent', 'lead', 'admin']
    active: bool


@router.get('/login', include_in_schema=False)
def login_page():
    return FileResponse(STATIC / 'login.html')


@router.get('/login.css', include_in_schema=False)
def login_css():
    return FileResponse(STATIC / 'login.css')


@router.get('/login.js', include_in_schema=False)
def login_js():
    return FileResponse(STATIC / 'login.js')


@router.post('/login')
def login(payload: Login, request: Request, response: Response):
    check_origin(request)
    if request.url.scheme != 'https' and not insecure_local_http(request):
        raise HTTPException(403, 'HTTPS required; explicit loopback HTTP opt-in is available for development')
    db = AuthStore()
    if not db.reserve_login(payload.username, request.client.host if request.client else 'unknown'):
        raise HTTPException(429, 'Too many login attempts; retry later')
    try:
        result = db.login(payload.username, payload.password)
    except LoginBusy:
        raise HTTPException(429, 'Authentication busy; retry later') from None
    if result is None:
        raise HTTPException(401, 'Invalid credentials')
    db.logout(request.cookies.get(COOKIE, ''))
    token, principal = result
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=not insecure_local_http(request),
                        samesite='strict', path='/')
    return {'operator': principal}


@router.get('/me')
def me(principal=Depends(operator)):
    return {'operator': principal}


@router.post('/logout')
def logout(request: Request, response: Response, principal=Depends(operator)):
    if principal.get('auth_method') == 'oidc':
        return {'logged_out': False, 'redirect': '/oauth2/sign_out?rd=%2Fauth%2Flogin'}
    AuthStore().logout(request.cookies.get(COOKIE, ''))
    response.delete_cookie(COOKIE, path='/', secure=not insecure_local_http(request), httponly=True, samesite='strict')
    return {'logged_out': True}


@router.post('/logout-all')
def logout_all(request: Request, response: Response, principal=Depends(operator)):
    if principal.get('auth_method') == 'oidc':
        raise HTTPException(403, 'Revoke organizational sessions in the identity provider')
    AuthStore().logout(request.cookies.get(COOKIE, ''), everywhere=True)
    response.delete_cookie(COOKIE, path='/', secure=not insecure_local_http(request), httponly=True, samesite='strict')
    return {'logged_out': True}


@router.get('/operators')
def users(principal=Depends(require_roles('admin'))):
    return {'operators': AuthStore().users()}


@router.post('/operators', status_code=201)
def create_user(payload: NewOperator, principal=Depends(require_roles('admin'))):
    try:
        return AuthStore().create_user(payload.username, payload.password, payload.role, principal['id'])
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Username already exists') from None


@router.patch('/operators/{operator_id}')
def update_user(operator_id: UUID, payload: UpdateOperator, principal=Depends(require_roles('admin'))):
    try:
        AuthStore().update_user(str(operator_id), payload.role, payload.active, principal['id'])
    except KeyError:
        raise HTTPException(404, 'Operator not found') from None
    except ValueError:
        raise HTTPException(409, 'Cannot remove the last active admin') from None
    return {'updated': True, 'sessions_revoked': True}
