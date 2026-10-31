import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from support_copilot.api import app
from support_copilot.capybara_provider import CapybaraProvider, ENDPOINT
from support_copilot.draft_provider import DraftFailure, configured_provider


def mock_response(monkeypatch, value, capture=None):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return json.dumps(value).encode()
    class Opener:
        def open(self, request, timeout):
            if capture is not None: capture.append(request)
            return Response()
    monkeypatch.setattr('support_copilot.capybara_provider.build_opener', lambda *args: Opener())


def envelope(content='{}', finish='stop'):
    return {'model': 'gemini-3.8-flash', 'choices': [{'finish_reason': finish, 'message': {'content': content}}],
            'usage': {'prompt_tokens': 200, 'completion_tokens': 40}}


def test_transport_and_usage(monkeypatch):
    captured=[];mock_response(monkeypatch,envelope(),captured)
    p=CapybaraProvider(api_key='synthetic-fixture')
    assert p.generate({'messages':[]})=='{}'
    r=captured[0];body=json.loads(r.data)
    assert r.full_url==ENDPOINT
    assert body['model']=='gemini-3.8-flash' and body['stream'] is False
    assert 'tools' not in body and body['max_tokens']==900
    assert p.last_usage=={'input_tokens':200,'output_tokens':40,'cached_tokens':0}


@pytest.mark.parametrize('finish',['length','content_filter',None])
def test_partial_answers_rejected(monkeypatch,finish):
    mock_response(monkeypatch,envelope(finish=finish))
    with pytest.raises(DraftFailure,match='incomplete'):
        CapybaraProvider(api_key='fixture').generate({})


def test_missing_key_and_wrong_model():
    for key,model in [('', 'gemini-3.8-flash'),('fixture','unapproved')]:
        with pytest.raises(DraftFailure): CapybaraProvider(api_key=key,model=model)


def test_secret_file_and_offline_default(monkeypatch,tmp_path):
    assert configured_provider() is None
    file=tmp_path/'credential';file.write_text('synthetic-fixture')
    monkeypatch.setenv('CAPYBARA_API_KEY_FILE',str(file))
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER','capybara')
    assert isinstance(configured_provider(),CapybaraProvider)


def test_oversized_request_never_sent(monkeypatch):
    calls=[];mock_response(monkeypatch,envelope(),calls)
    with pytest.raises(DraftFailure,match='oversized'):
        CapybaraProvider(api_key='fixture').generate({'text':'x'*25000})
    assert calls==[]


def test_assistant_is_read_only_and_cannot_forge_citations(monkeypatch,tmp_path):
    monkeypatch.setenv('COPILOT_OPERATOR_DB',str(tmp_path/'operator.sqlite3'))
    monkeypatch.setenv('CAPYBARA_API_KEY','synthetic-fixture')
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER','capybara')
    client=TestClient(app,client=('127.0.0.1',50000),base_url='http://127.0.0.1')
    ticket=client.post('/operator/tickets',json={'title':'Demo','request':{'kind':'report','messages':[
        {'role':'player','text':'Оплатил, баланс не изменился','attachments':0}]}}).json()
    path=f"/operator/tickets/{ticket['id']}/assistant"
    mock_response(monkeypatch,envelope(json.dumps({'answer':'Уточните время оплаты.','citations':[]})))
    response=client.post(path,json={'expected_revision':ticket['revision'],'question':'Что уточнить?'})
    assert response.status_code==200,response.text
    assert response.json()['advisory_only'] and 'решение человека' in response.json()['answer']
    after=client.get(f"/operator/tickets/{ticket['id']}").json()
    assert after==ticket
    mock_response(monkeypatch,envelope(json.dumps({'answer':'Правило','citations':[{'source_id':'77:7.7','quote':'fake'}]})))
    assert client.post(path,json={'expected_revision':ticket['revision'],'question':'Правила?'}).status_code==503
    assert client.post(path,json={'expected_revision':999,'question':'Что?'}).status_code==409


def test_assistant_requires_authentication(monkeypatch):
    monkeypatch.setenv('COPILOT_AUTH_MODE','required')
    c=TestClient(app)
    assert c.post('/operator/tickets/00000000-0000-0000-0000-000000000000/assistant',json={}).status_code==401
