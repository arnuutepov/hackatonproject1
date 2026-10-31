"""Verify the IdP token independently of proxy headers; MFA is mandatory."""
from functools import lru_cache
import hashlib
import ipaddress
import json
import math
import os
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
import jwt


@lru_cache(maxsize=4)
def jwks_client(url):
    return jwt.PyJWKClient(url, cache_jwk_set=True, lifespan=300, timeout=5)


def identity(request: Request) -> dict:
    issuer = os.environ.get('COPILOT_OIDC_ISSUER', '')
    audience = os.environ.get('COPILOT_OIDC_CLIENT_ID', '')
    jwks = os.environ.get('COPILOT_OIDC_JWKS_URL', '')
    origin = os.environ.get('COPILOT_PUBLIC_ORIGIN', '')
    try:
        for url in (issuer, jwks, origin):
            parts = urlsplit(url)
            if parts.scheme != 'https' or not parts.hostname or parts.username or parts.fragment:
                raise ValueError()
        if not audience:
            raise ValueError()
        mapping = json.loads(os.environ.get('COPILOT_OIDC_GROUP_ROLES', '{}'))
        if not mapping or not isinstance(mapping, dict) or any(role not in {'agent', 'lead', 'admin'} for role in mapping.values()):
            raise ValueError()
        cidrs = [ipaddress.ip_network(item.strip()) for item in os.environ.get('COPILOT_SSO_PROXY_CIDRS', '').split(',') if item.strip()]
        if not cidrs:
            raise ValueError()
        acrs = json.loads(os.environ.get('COPILOT_OIDC_MFA_ACRS', '[]'))
        if not isinstance(acrs, list) or any(not isinstance(value, str) for value in acrs):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(503, 'Invalid SSO configuration') from None
    try:
        peer = ipaddress.ip_address(request.client.host)
    except (ValueError, AttributeError):
        raise HTTPException(403, 'Trusted SSO proxy required') from None
    if not any(peer in network for network in cidrs):
        raise HTTPException(403, 'Trusted SSO proxy required')
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer ') or len(header) > 32768:
        raise HTTPException(401, 'SSO token required')
    token = header[7:]
    try:
        key = jwks_client(jwks).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=['RS256'], issuer=issuer, audience=audience,
                            options={'require': ['iss', 'aud', 'sub', 'exp', 'iat', 'auth_time']}, leeway=15)
    except jwt.PyJWTError:
        raise HTTPException(401, 'Invalid SSO token') from None
    if (isinstance(claims.get('aud'), list) and len(claims['aud']) > 1 and claims.get('azp') != audience) or (
            'azp' in claims and claims['azp'] != audience):
        raise HTTPException(401, 'Invalid SSO authorized party')
    now = time.time()
    if not isinstance(claims['sub'], str) or not claims['sub'] or len(claims['sub']) > 256:
        raise HTTPException(401, 'Invalid SSO subject')
    if any(type(claims[name]) not in (int, float) or not math.isfinite(claims[name]) for name in ('iat', 'auth_time', 'exp')):
        raise HTTPException(401, 'Invalid SSO time')
    if now - claims['iat'] > 900 or now - claims['auth_time'] > 8*3600 or claims['auth_time'] > now + 15:
        raise HTTPException(403, 'Fresh SSO authentication required')
    amr = claims.get('amr', [])
    mfa = (isinstance(amr, list) and 'mfa' in amr) or (bool(acrs) and claims.get('acr') in acrs)
    if not mfa:
        raise HTTPException(403, 'MFA assurance required')
    groups = claims.get('groups', [])
    if not isinstance(groups, list) or any(not isinstance(value, str) for value in groups):
        raise HTTPException(403, 'SSO group membership required')
    roles = {mapping[group] for group in groups if group in mapping}
    if not roles:
        raise HTTPException(403, 'Operator group required')
    if 'email' in claims and claims.get('email_verified') is not True:
        raise HTTPException(403, 'Verified SSO email required')
    role = next(value for value in ('admin', 'lead', 'agent') if value in roles)
    subject = hashlib.sha256((issuer + '\0' + claims['sub']).encode()).hexdigest()
    username = claims.get('email') or claims.get('preferred_username') or claims['sub']
    return {'id': 'oidc:' + subject, 'username': str(username)[:256], 'role': role,
            'active': 1, 'auth_method': 'oidc', 'mfa': True}
