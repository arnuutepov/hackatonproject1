"""Real required-mode checks; legacy workflow tests explicitly opt into local mode."""
import hashlib
import hmac
import json
import secrets
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from support_copilot.api import app
from support_copilot.auth_store import AuthStore, check_password, hash_password
from support_copilot.security import COOKIE, PUBLIC

PASSWORD = 'fictional password for isolated tests'
ORIGIN = {'Origin': 'https://localhost'}


@pytest.fixture(scope='module')
def encoded():
    return hash_password(PASSWORD)


@pytest.fixture
def secured(monkeypatch, tmp_path, encoded):
    monkeypatch.setenv('COPILOT_AUTH_MODE', 'required')
    monkeypatch.setenv('COPILOT_OPERATOR_DB', str(tmp_path / 'workspace.sqlite3'))
    monkeypatch.delenv('COPILOT_INSECURE_LOCAL_HTTP', raising=False)
    store = AuthStore()
    with store.connect() as db:
        for role in ('agent', 'lead', 'admin'):
            db.execute('INSERT INTO operators VALUES (?, ?, ?, ?, 1)', (str(uuid4()), role, encoded, role))
    with TestClient(app, base_url='https://localhost', client=('127.0.0.1', 40000)) as client:
        yield client


def login(client, name='agent'):
    result = client.post('/auth/login', json={'username': name, 'password': PASSWORD}, headers=ORIGIN)
    assert result.status_code == 200, result.text
    return result.json()['operator']


def ticket(client, text='Оплатил донат, деньги не пришли'):
    response = client.post('/operator/tickets', json={'title': 'Security fixture', 'request': {
        'kind': 'report', 'messages': [{'role': 'player', 'text': text}]}}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


def analyze(client, value):
    response = client.post(f"/operator/tickets/{value['id']}/analyze", json={
        'expected_revision': value['revision']}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()


def test_every_documented_route_is_closed_without_session(secured):
    paths = app.openapi()['paths']
    for path, operations in paths.items():
        for method in operations:
            if method.upper() not in {'GET', 'POST', 'PATCH', 'PUT', 'DELETE', 'OPTIONS'}:
                continue
            if (method.upper(), path) in PUBLIC:
                continue
            target = path
            for name in ('ticket_id', 'operator_id'):
                target = target.replace('{' + name + '}', str(uuid4()))
            target = target.replace('{source_id}', '30:1.1')
            response = secured.request(method.upper(), target)
            assert response.status_code == 401, (method, path, response.status_code)
    for path in ('/', '/assets/workspace.js', '/docs', '/openapi.json', '/redoc'):
        assert secured.get(path).status_code == 401


def test_future_route_without_dependencies_is_still_closed(secured):
    async def unguarded():
        return {'secret': 'never public'}
    app.add_api_route('/future-security-fixture', unguarded, methods=['GET'])
    try:
        assert secured.get('/future-security-fixture').status_code == 401
    finally:
        app.router.routes.pop()
        app.openapi_schema = None


def test_host_and_forwarded_headers_are_not_identity(secured):
    assert secured.get('/operator/tickets', headers={'Host': 'localhost'}).status_code == 401
    assert secured.get('/operator/tickets', headers={'X-Forwarded-For': '127.0.0.1', 'X-Operator-Role': 'admin'}).status_code == 401
    assert secured.get('/operator/tickets', headers={'Authorization': 'Bearer player-token'}).status_code == 401


def test_login_cookie_rotation_logout_and_session_hashes(secured):
    user = login(secured)
    cookie = secured.cookies.get(COOKIE)
    assert cookie and user['role'] == 'agent'
    response = secured.post('/auth/login', json={'username': 'agent', 'password': PASSWORD}, headers=ORIGIN)
    header = response.headers['set-cookie'].lower()
    assert 'httponly' in header and 'secure' in header and 'samesite=strict' in header
    assert cookie != secured.cookies.get(COOKIE)
    assert AuthStore().session(cookie) is None
    with AuthStore().connect() as db:
        row = db.execute('SELECT digest FROM sessions').fetchone()
        assert row['digest'] != secured.cookies.get(COOKIE)
    assert secured.get('/auth/me').json()['operator']['id'] == user['id']
    assert secured.post('/auth/logout', headers=ORIGIN).status_code == 200
    assert secured.get('/operator/tickets').status_code == 401


def test_expired_and_disabled_sessions_are_rejected(secured):
    user = login(secured)
    with AuthStore().connect() as db:
        db.execute('UPDATE sessions SET expires_at = ?', (time.time() - 1,))
    assert secured.get('/operator/tickets').status_code == 401
    login(secured)
    AuthStore().update_user(user['id'], 'agent', False, 'fixture-admin')
    assert secured.get('/operator/tickets').status_code == 401


def test_mutations_require_origin_even_with_cookie(secured):
    login(secured)
    assert secured.post('/operator/demo').status_code == 403
    assert secured.post('/operator/demo', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert secured.post('/operator/demo', headers={**ORIGIN, 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert secured.post('/operator/demo', headers=ORIGIN).status_code == 200
    assert secured.post('/auth/logout').status_code == 403


def test_scoped_triage_token_cannot_read_operator_data(secured, monkeypatch):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv('COPILOT_TRIAGE_TOKEN', token)
    headers = {'Authorization': 'Bearer ' + token}
    assert secured.post('/triage', json={'kind': 'report', 'messages': [{'role': 'player', 'text': 'Помогите'}]}, headers=headers).status_code == 200
    assert secured.get('/health', headers=headers).status_code == 200
    for path in ('/operator/tickets', '/api/v1/outbox', '/auth/operators'):
        assert secured.get(path, headers=headers).status_code == 403
    assert secured.post('/api/v1/simulate-ticket', json={}, headers=headers).status_code == 403


def test_agent_cannot_manage_users_or_forge_chat_role(secured):
    login(secured)
    assert secured.get('/auth/operators').status_code == 403
    assert secured.post('/auth/operators', json={}, headers=ORIGIN).status_code == 403
    item = ticket(secured)
    url = f"/operator/tickets/{item['id']}/chat"
    assert secured.post(url, json={'expected_revision': 1, 'role': 'admin', 'text': 'Impersonation'}, headers=ORIGIN).status_code == 422
    assert secured.post(url, json={'expected_revision': 1, 'role': 'player', 'text': 'Impersonation'}, headers=ORIGIN).status_code == 422
    result = secured.post(url, json={'expected_revision': 1, 'text': 'Operator message'}, headers=ORIGIN).json()
    assert result['chat_messages'][0]['role'] == 'admin'
    assert result['events'][-1]['detail']['actor'] == secured.get('/auth/me').json()['operator']['id']


def test_assignments_and_decisions_record_actual_actor(secured):
    user = login(secured)
    item = analyze(secured, ticket(secured))
    result = secured.post(f"/operator/tickets/{item['id']}/workflow", json={
        'expected_revision': item['revision'], 'action': 'take'}, headers=ORIGIN).json()
    assert result['assigned_to'] == user['id']
    assert all(event['detail']['actor'] == user['id'] for event in result['events'])
    response = secured.post(f"/operator/tickets/{item['id']}/decision", json={
        'expected_revision': result['revision'], 'draft': result['edited_draft'], 'action': 'escalate'}, headers=ORIGIN)
    assert response.json()['events'][-1]['detail']['actor'] == user['id']


def test_only_senior_can_record_human_review_and_reanalysis_invalidates_it(secured):
    login(secured)
    item = analyze(secured, ticket(secured))
    url = f"/operator/tickets/{item['id']}/workflow"
    payload = {'expected_revision': item['revision'], 'action': 'resolve', 'note': 'Verified by staff'}
    assert secured.post(url, json=payload, headers=ORIGIN).status_code == 409
    user = login(secured, 'lead')
    assert secured.post(url, json={**payload, 'note': ''}, headers=ORIGIN).status_code == 409
    result = secured.post(url, json=payload, headers=ORIGIN).json()
    assert result['status'] == 'resolved' and result['needs_human'] is False
    assert result['analysis']['needs_human'] is True
    assert result['human_review']['operator'] == user['id']
    new = analyze(secured, result)
    assert new['needs_human'] is True and new['human_review'] is None


def test_admin_update_revokes_sessions_and_last_admin_is_preserved(secured):
    agent = login(secured)
    agent_token = secured.cookies.get(COOKIE)
    admin = login(secured, 'admin')
    users = secured.get('/auth/operators').json()['operators']
    assert all('password_hash' not in user for user in users)
    assert secured.patch('/auth/operators/' + agent['id'], json={'role': 'lead', 'active': False}, headers=ORIGIN).status_code == 200
    assert AuthStore().session(agent_token) is None
    assert secured.patch('/auth/operators/' + admin['id'], json={'role': 'agent', 'active': True}, headers=ORIGIN).status_code == 409


def test_password_hash_and_reset_are_not_plaintext(secured, encoded):
    assert PASSWORD not in encoded and check_password(PASSWORD, encoded)
    assert not check_password('incorrect password fixture', encoded)
    login(secured)
    token = secured.cookies.get(COOKIE)
    AuthStore().reset_password('agent', PASSWORD + ' changed')
    assert AuthStore().session(token) is None


def test_login_rate_limit_is_persistent_and_errors_are_generic(secured, monkeypatch):
    # Keep attempts in one documented fixed-minute bucket, including near minute boundaries.
    monkeypatch.setattr("support_copilot.auth_store.time.time", lambda: 1793430000.0)
    for _ in range(10):
        assert secured.post('/auth/login', json={'username': 'unknown', 'password': PASSWORD}, headers=ORIGIN).status_code == 401
    result = secured.post('/auth/login', json={'username': 'unknown', 'password': PASSWORD}, headers=ORIGIN)
    assert result.status_code == 429
    assert PASSWORD not in result.text


def test_local_mode_checks_actual_peer_not_spoofed_host(secured, monkeypatch):
    monkeypatch.setenv('COPILOT_AUTH_MODE', 'local')
    with TestClient(app, base_url='http://localhost', client=('203.0.113.9', 40000)) as remote:
        assert remote.get('/operator/tickets', headers={'Host': 'localhost', 'X-Forwarded-For': '127.0.0.1'}).status_code == 403


def test_http_login_requires_explicit_local_opt_in(secured, monkeypatch):
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 40000)) as client:
        body = {'username': 'agent', 'password': PASSWORD}
        headers = {'Origin': 'http://127.0.0.1'}
        assert client.post('/auth/login', json=body, headers=headers).status_code == 403
        monkeypatch.setenv('COPILOT_INSECURE_LOCAL_HTTP', '1')
        assert client.post('/auth/login', json=body, headers=headers).status_code == 200
        assert client.get('/operator/tickets').status_code == 200


GAME_PATH = '/api/v1/game/tickets'


def envelope():
    return {'event_id': str(uuid4()), 'player_id': 777123, 'title': 'Signed fixture', 'channel': 'in_game',
            'request': {'kind': 'report', 'messages': [{'role': 'player', 'text': 'Застрял в текстурах'}]}}


def signed(client, monkeypatch, payload, *, path=GAME_PATH, stamp=None, nonce=None, key_id='fixture-game'):
    secret = 'fictional-hmac-fixture-' + 'x' * 32
    monkeypatch.setenv('COPILOT_GAME_HMAC_KEYS', json.dumps({key_id: secret}))
    body = json.dumps(payload, ensure_ascii=False).encode()
    at, nonce = str(int(time.time()) if stamp is None else stamp), nonce or str(uuid4())
    canonical = f'v1\nPOST\n{path}\n{at}\n{nonce}\n{hashlib.sha256(body).hexdigest()}'.encode()
    headers = {'Content-Type': 'application/json', 'X-Game-Key-Id': key_id, 'X-Game-Timestamp': at,
               'X-Game-Nonce': nonce, 'X-Game-Signature': 'v1=' + hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()}
    return body, headers


def test_signed_ingress_is_separate_idempotent_and_actor_bound(secured, monkeypatch):
    payload = envelope()
    body, headers = signed(secured, monkeypatch, payload)
    response = secured.post(GAME_PATH, content=body, headers=headers)
    assert response.status_code == 201, response.text
    key = response.json()['ticket_id']
    assert 'analysis' not in response.json()
    assert secured.post(GAME_PATH, content=body, headers=headers).status_code == 409
    fresh_body, fresh_headers = signed(secured, monkeypatch, payload)
    assert secured.post(GAME_PATH, content=fresh_body, headers=fresh_headers).json()['replayed']
    login(secured)
    item = secured.get('/operator/tickets/' + key).json()
    assert item['source'] == 'game_connector' and item['connector']['context']['is_mock'] is False
    assert item['events'][0]['detail']['actor'] == 'game:fixture-game'
    assert item['analysis']['generation']['attempts'] == 0
    assert secured.get('/operator/tickets', headers=headers).status_code == 200  # session, not HMAC headers
    secured.cookies.clear()
    assert secured.get('/operator/tickets', headers=headers).status_code == 401


@pytest.mark.parametrize('change', ['tamper', 'expired', 'path', 'missing', 'bad-secret'])
def test_forged_game_events_are_rejected_before_creation(secured, monkeypatch, change):
    body, headers = signed(secured, monkeypatch, envelope(), stamp=int(time.time()) - 1000 if change == 'expired' else None)
    if change == 'tamper': body += b' '
    if change == 'path': headers['X-Game-Signature'] = 'v1=' + '0' * 64
    if change == 'missing': headers = {'Content-Type': 'application/json'}
    if change == 'bad-secret': monkeypatch.setenv('COPILOT_GAME_HMAC_KEYS', '{}')
    assert secured.post(GAME_PATH, content=body, headers=headers).status_code == 401
    login(secured)
    assert secured.get('/operator/tickets').json()['tickets'] == []


def test_only_signed_game_server_can_add_player_message_and_invalidate_analysis(secured, monkeypatch):
    body, headers = signed(secured, monkeypatch, envelope())
    key = secured.post(GAME_PATH, content=body, headers=headers).json()['ticket_id']
    path = GAME_PATH + '/' + key + '/messages'
    message = {'event_id': str(uuid4()), 'player_id': 777123, 'text': 'Теперь застрял на сервере 4'}
    body, headers = signed(secured, monkeypatch, message, path=path)
    assert secured.post(path, content=body, headers=headers).status_code == 200
    again_body, again_headers = signed(secured, monkeypatch, message, path=path)
    assert secured.post(path, content=again_body, headers=again_headers).json()['replayed']
    wrong, wrong_headers = signed(secured, monkeypatch, {**message, 'event_id': str(uuid4()), 'player_id': 123}, path=path)
    assert secured.post(path, content=wrong, headers=wrong_headers).status_code == 409
    login(secured)
    item = secured.get('/operator/tickets/' + key).json()
    assert item['analysis'] is None and item['revision'] == 3
    assert item['chat_messages'][0]['role'] == 'player'
    assert item['payload']['messages'][-1]['text'] == message['text']
    assert item['events'][-1]['detail']['actor'] == 'game:fixture-game'


def test_default_configuration_does_not_enable_local_bypass(secured, monkeypatch):
    monkeypatch.delenv('COPILOT_AUTH_MODE')
    assert secured.get('/operator/tickets', headers={'Host': 'localhost'}).status_code == 401
    assert secured.get('/').status_code == 401
    assert 'Вход оператора' in secured.get('/').text
    assert secured.get('/auth/login').status_code == 200


def test_revoke_all_sessions(secured):
    login(secured)
    first = secured.cookies.get(COOKIE)
    # A separate client represents another browser; login rotation only revokes its own prior cookie.
    with TestClient(app, base_url='https://localhost', client=('127.0.0.1', 40001)) as other:
        login(other)
        second = other.cookies.get(COOKIE)
        assert AuthStore().session(first) is not None
        assert secured.post('/auth/logout-all', headers=ORIGIN).status_code == 200
        assert AuthStore().session(first) is None and AuthStore().session(second) is None


def test_agent_cannot_take_another_operators_assignment(secured, encoded):
    user = login(secured)
    item = ticket(secured)
    claimed = secured.post(f"/operator/tickets/{item['id']}/workflow", json={
        'expected_revision': 1, 'action': 'take'}, headers=ORIGIN).json()
    with AuthStore().connect() as db:
        db.execute('INSERT INTO operators VALUES (?, ?, ?, ?, 1)', (str(uuid4()), 'other-agent', encoded, 'agent'))
    login(secured, 'other-agent')
    assert secured.post(f"/operator/tickets/{item['id']}/workflow", json={
        'expected_revision': claimed['revision'], 'action': 'take'}, headers=ORIGIN).status_code == 403
    assert secured.get('/operator/tickets/' + item['id']).json()['assigned_to'] == user['id']


def test_admin_can_create_user_and_no_password_is_echoed(secured):
    login(secured, 'admin')
    response = secured.post('/auth/operators', json={'username': 'new-agent', 'password': PASSWORD, 'role': 'agent'}, headers=ORIGIN)
    assert response.status_code == 201 and PASSWORD not in response.text and 'password_hash' not in response.text
    invalid = secured.post('/auth/operators', json={'username': 'bad', 'password': 'secret-marker', 'role': 'admin'}, headers=ORIGIN)
    assert invalid.status_code == 422 and 'secret-marker' not in invalid.text
    assert secured.post('/auth/operators', json={'username': 'new-agent', 'password': PASSWORD, 'role': 'agent'}, headers=ORIGIN).status_code == 409


def test_mock_outbox_uses_session_actor_and_is_not_public(secured):
    user = login(secured)
    response = secured.post('/api/v1/simulate-ticket', json={'event_id': str(uuid4()), 'scenario_id': 'payment_ru'}, headers=ORIGIN)
    item = response.json()['ticket']
    result = secured.post('/api/v1/tickets/' + item['id'] + '/actions', json={
        'action_id': str(uuid4()), 'expected_revision': item['revision'], 'action': 'reply',
        'draft': item['edited_draft']}, headers=ORIGIN)
    assert result.status_code == 200
    assert result.json()['event']['actor'] == user['id']
    secured.cookies.clear()
    assert secured.get('/api/v1/outbox').status_code == 401


def test_player_cannot_use_operator_cookie_to_bypass_game_signature(secured):
    login(secured)
    assert secured.post(GAME_PATH, json=envelope(), headers=ORIGIN).status_code == 401


def test_invalid_game_configuration_fails_closed(secured, monkeypatch):
    monkeypatch.setenv('COPILOT_GAME_HMAC_KEYS', 'not-json')
    assert secured.post(GAME_PATH, json=envelope()).status_code == 401


def test_nonce_persists_and_signature_is_bound_to_path_and_body(secured, monkeypatch):
    payload = envelope()
    nonce = str(uuid4())
    body, headers = signed(secured, monkeypatch, payload, nonce=nonce)
    assert secured.post(GAME_PATH, content=body, headers=headers).status_code == 201
    assert not AuthStore().consume_nonce('fixture-game', nonce, time.time())
    different_body, different_headers = signed(secured, monkeypatch, payload, path=GAME_PATH + '/wrong')
    assert secured.post(GAME_PATH, content=different_body, headers=different_headers).status_code == 401


def test_another_game_key_cannot_append_to_first_keys_player(secured, monkeypatch):
    body, headers = signed(secured, monkeypatch, envelope())
    key = secured.post(GAME_PATH, content=body, headers=headers).json()['ticket_id']
    path = GAME_PATH + '/' + key + '/messages'
    message = {'event_id': str(uuid4()), 'player_id': 777123, 'text': 'Must not cross server boundary'}
    body, headers = signed(secured, monkeypatch, message, path=path, key_id='other-server')
    assert secured.post(path, content=body, headers=headers).status_code == 409


def test_hmac_endpoint_rejects_oversized_body_before_json_parsing(secured, monkeypatch):
    body, headers = signed(secured, monkeypatch, envelope())
    assert secured.post(GAME_PATH, content=b'x' * 2_100_001, headers=headers).status_code == 413


def test_real_uvicorn_enforces_session_hmac_and_logout(secured, monkeypatch):
    """Exercise the actual TCP/ASGI boundary, not only TestClient dependency execution."""
    import os
    import socket
    import subprocess
    import sys
    import httpx
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    body, game_headers = signed(secured, monkeypatch, envelope())
    env = {**os.environ, 'COPILOT_AUTH_MODE': 'required', 'COPILOT_INSECURE_LOCAL_HTTP': '1',
           'COPILOT_DRAFT_PROVIDER': 'template'}
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'support_copilot.api:app',
        '--host', '127.0.0.1', '--port', str(port), '--no-proxy-headers', '--no-access-log'],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}'
    try:
        with httpx.Client(base_url=base, timeout=5) as client:
            started = time.monotonic()
            while True:
                try:
                    if client.get('/auth/login').status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if process.poll() is not None or time.monotonic() - started > 10:
                    pytest.fail('Security smoke server failed to start')
                time.sleep(.05)
            assert client.get('/operator/tickets', headers={'Host': 'localhost'}).status_code == 401
            assert client.post('/triage', json={}).status_code == 401
            result = client.post(GAME_PATH, content=body, headers=game_headers)
            assert result.status_code == 201
            key = result.json()['ticket_id']
            assert client.post(GAME_PATH, content=body, headers=game_headers).status_code == 409
            origin = {'Origin': base}
            assert client.post('/auth/login', json={'username': 'agent', 'password': PASSWORD}, headers=origin).status_code == 200
            item = client.get('/operator/tickets/' + key).json()
            assert item['source'] == 'game_connector'
            assert client.post('/operator/demo').status_code == 403
            assert client.post('/auth/logout', headers=origin).status_code == 200
            assert client.get('/operator/tickets').status_code == 401
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_non_operator_identity_is_forbidden_even_with_a_valid_session(secured, encoded):
    from support_copilot.auth_store import token_hash
    token, key = secrets.token_urlsafe(32), str(uuid4())
    with AuthStore().connect() as db:
        db.execute('INSERT INTO operators VALUES (?, ?, ?, ?, 1)', (key, 'player-fixture', encoded, 'player'))
        db.execute('INSERT INTO sessions VALUES (?, ?, ?)', (token_hash(token), key, time.time() + 60))
    secured.cookies.set(COOKIE, token)
    assert secured.get('/operator/tickets').status_code == 403
    assert secured.get('/api/v1/outbox').status_code == 403
