"""Offline cryptographic acceptance tests. No organizer data or external IdP needed."""
import json
import time
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
import pytest

from support_copilot.api import app

ISSUER = 'https://identity.example.invalid/realm'
AUDIENCE = 'oguzki-test-client'


@pytest.fixture(scope='module')
def keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, other


@pytest.fixture
def sso(monkeypatch, keys):
    monkeypatch.setenv('COPILOT_AUTH_MODE', 'oidc')
    monkeypatch.setenv('COPILOT_OIDC_ISSUER', ISSUER)
    monkeypatch.setenv('COPILOT_OIDC_CLIENT_ID', AUDIENCE)
    monkeypatch.setenv('COPILOT_OIDC_JWKS_URL', ISSUER + '/jwks')
    monkeypatch.setenv('COPILOT_PUBLIC_ORIGIN', 'https://support.example.invalid')
    monkeypatch.setenv('COPILOT_OIDC_GROUP_ROLES', json.dumps({'support-agent': 'agent', 'support-lead': 'lead', 'support-admin': 'admin'}))
    monkeypatch.setenv('COPILOT_OIDC_MFA_ACRS', '["urn:oguzki:mfa"]')
    monkeypatch.setenv('COPILOT_SSO_PROXY_CIDRS', '203.0.113.7/32')
    class Client:
        def get_signing_key_from_jwt(self, token):
            if jwt.get_unverified_header(token).get('kid') != 'fixture':
                raise jwt.PyJWKClientError('Unknown fixture key')
            return SimpleNamespace(key=keys[0].public_key())
    monkeypatch.setattr('support_copilot.oidc_identity.jwks_client', lambda url: Client())
    with TestClient(app, base_url='http://internal-backend', client=('203.0.113.7', 40000)) as client:
        yield client


def claims(**changes):
    now = int(time.time())
    return {'iss': ISSUER, 'aud': AUDIENCE, 'sub': 'fixture-operator', 'iat': now, 'exp': now + 300,
            'auth_time': now, 'amr': ['pwd', 'mfa'], 'groups': ['support-agent'],
            'email': 'agent@example.invalid', 'email_verified': True, **changes}


def headers(keys, payload=None, key_index=0, **overrides):
    token = jwt.encode(payload or claims(), keys[key_index], algorithm='RS256', headers={'kid': 'fixture'})
    return {'Authorization': 'Bearer ' + token, **overrides}


def test_verified_mfa_operator_and_stable_subject(sso, keys):
    response = sso.get('/auth/me', headers=headers(keys))
    assert response.status_code == 200
    operator = response.json()['operator']
    assert operator['role'] == 'agent' and operator['mfa'] and operator['auth_method'] == 'oidc'
    assert operator['id'].startswith('oidc:')
    changed = sso.get('/auth/me', headers=headers(keys, claims(email='renamed@example.invalid'))).json()['operator']
    assert changed['id'] == operator['id']


@pytest.mark.parametrize('changes,status', [
    ({'iss': 'https://other.example.invalid'}, 401), ({'aud': 'other-client'}, 401),
    ({'exp': int(time.time()) - 60}, 401), ({'iat': int(time.time()) + 60}, 401),
    ({'iat': int(time.time()) - 1000}, 403), ({'auth_time': int(time.time()) - 30000}, 403),
    ({'auth_time': int(time.time()) + 60}, 403), ({'amr': ['pwd']}, 403),
    ({'amr': ['pwd'], 'acr': 'password-only'}, 403), ({'groups': ['players']}, 403),
    ({'groups': 'support-admin'}, 403), ({'email_verified': False}, 403),
    ({'sub': ''}, 401), ({'azp': 'other-client'}, 401),
    ({'aud': [AUDIENCE, 'another']}, 401), ({'auth_time': float('nan')}, 401),
])
def test_identity_claims_fail_closed(sso, keys, changes, status):
    response = sso.get('/auth/me', headers=headers(keys, claims(**changes)))
    assert response.status_code == status, response.text


def test_claim_role_does_not_override_group_mapping(sso, keys):
    result = sso.get('/auth/me', headers=headers(keys, claims(role='admin'))).json()['operator']
    assert result['role'] == 'agent'
    result = sso.get('/auth/me', headers=headers(keys, claims(groups=['support-agent', 'support-lead']))).json()['operator']
    assert result['role'] == 'lead'


def test_explicit_mfa_acr_is_accepted(sso, keys):
    assert sso.get('/auth/me', headers=headers(keys, claims(amr=['pwd'], acr='urn:oguzki:mfa'))).status_code == 200


def test_wrong_key_and_missing_claim_are_rejected(sso, keys):
    assert sso.get('/auth/me', headers=headers(keys, key_index=1)).status_code == 401
    payload = claims()
    del payload['auth_time']
    assert sso.get('/auth/me', headers=headers(keys, payload)).status_code == 401
    token = jwt.encode(claims(), None, algorithm='none', headers={'kid': 'fixture'})
    assert sso.get('/auth/me', headers={'Authorization': 'Bearer ' + token}).status_code == 401


def test_proxy_headers_and_local_cookie_are_not_identity(sso):
    sso.cookies.set('oguzki_session', 'not-an-oidc-token')
    assert sso.get('/auth/me', headers={'X-Forwarded-User': 'admin', 'X-Forwarded-Groups': 'support-admin'}).status_code == 401


def test_native_login_and_native_user_management_are_disabled(sso, keys):
    assert sso.post('/auth/login', json={'username': 'admin', 'password': 'fixture password long'}).status_code == 403
    result = sso.get('/auth/login', follow_redirects=False)
    assert result.status_code == 303 and result.headers['location'].startswith('/oauth2/start')
    assert sso.get('/auth/operators', headers=headers(keys, claims(groups=['support-admin']))).status_code == 403


def test_sso_mutations_require_public_origin(sso, keys):
    assert sso.post('/auth/logout', headers=headers(keys)).status_code == 403
    result = sso.post('/auth/logout', headers=headers(keys, Origin='https://support.example.invalid'))
    assert result.status_code == 200 and result.json()['redirect'].startswith('/oauth2/sign_out')


def test_untrusted_peer_cannot_inject_even_a_valid_token(sso, keys):
    with TestClient(app, base_url='http://internal-backend', client=('203.0.113.99', 40000)) as other:
        assert other.get('/auth/me', headers=headers(keys)).status_code == 403


def test_missing_oidc_configuration_does_not_fall_back_to_local(sso, keys, monkeypatch):
    monkeypatch.delenv('COPILOT_OIDC_GROUP_ROLES')
    assert sso.get('/auth/me', headers=headers(keys)).status_code == 503


def test_cross_origin_isolation_headers(sso, keys):
    response = sso.get('/auth/me', headers=headers(keys))
    assert response.headers['cross-origin-opener-policy'] == 'same-origin'
    assert response.headers['cross-origin-resource-policy'] == 'same-origin'
    assert 'microphone=()' in response.headers['permissions-policy']
