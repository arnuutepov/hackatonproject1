import pytest
from fastapi.testclient import TestClient
from support_copilot.api_only import app, main
from support_copilot.schemas import TriageRequest
from support_copilot.service import triage

TOKEN = 'test-token-' + 'x' * 32


def test_api_is_isolated_token_only_and_matches_core(monkeypatch):
    monkeypatch.setenv('COPILOT_TRIAGE_TOKEN', TOKEN)
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER', 'template')
    with TestClient(app) as client:
        assert client.get('/health').status_code == 401
        headers = {'Authorization': 'Bearer ' + TOKEN}
        assert client.get('/health', headers=headers).status_code == 200
        assert client.get('/auth/login', headers=headers).status_code == 404
        payload = {'kind': 'report', 'id': 'x', 'verdict': 'private', 'messages': [
            {'text': 'Помогите, игра вылетает', 'attachments': '2', 'meta': 'private'}]}
        response = client.post('/triage', headers=headers, json=payload)
        assert response.status_code == 200
        assert response.json() == triage(TriageRequest.model_validate(payload), use_configured_provider=False).model_dump()
        assert 'private' not in response.text
        invalid = client.post('/triage', headers=headers, json={'kind': 'report', 'messages': [{'text': '  PRIVATE  ', 'role': 'admin'}]})
        assert invalid.status_code == 422 and 'PRIVATE' not in invalid.text


@pytest.mark.parametrize('token', ['short', 'я' * 32])
def test_startup_rejects_invalid_tokens(monkeypatch, capsys, token):
    monkeypatch.setenv('COPILOT_TRIAGE_TOKEN', token)
    assert main() == 2
    assert 'COPILOT_TRIAGE_TOKEN' in capsys.readouterr().err


def test_open_mode_without_token(monkeypatch):
    monkeypatch.delenv('COPILOT_TRIAGE_TOKEN', raising=False)
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER', 'template')
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        response = client.post('/triage', json={'kind': 'report', 'messages': [{'text': 'Застрял в текстурах'}]})
        assert response.status_code == 200


def test_missing_dictionary_has_concrete_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('COPILOT_TRIAGE_TOKEN', TOKEN)
    monkeypatch.setattr('support_copilot.api_only.DATA', tmp_path)
    assert main() == 2
    assert 'rules.json' in capsys.readouterr().err


def test_defaults_and_unicode_long_text(monkeypatch):
    monkeypatch.setenv('COPILOT_TRIAGE_TOKEN', TOKEN)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + TOKEN}
        for text in ['Қазақша o‘yin 😀\u200b yordam', 'я' * 20000]:
            response = client.post('/triage', headers=headers, json={'messages': [{'text': text}]})
            assert response.status_code == 200
            assert response.json()['needs_human'] is True
        for payload in ({'messages': []}, {'messages': [{'text': '   '}]},
                        {'kind': 'unrecognized', 'messages': [{'text': 'x'}]}):
            assert client.post('/triage', headers=headers, json=payload).status_code == 422
