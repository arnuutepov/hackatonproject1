from fastapi.testclient import TestClient
from support_copilot.api import app


def test_chat_is_per_ticket_persistent_and_revision_checked(tmp_path, monkeypatch):
    monkeypatch.setenv('COPILOT_OPERATOR_DB', str(tmp_path / 'chat.sqlite3'))
    with TestClient(app, client=('127.0.0.1', 40000), base_url='http://127.0.0.1') as client:
        payload = {'title': 'Demo', 'request': {'kind': 'report', 'messages': [{'role': 'player', 'text': 'Помогите', 'attachments': 0}]}}
        first = client.post('/operator/tickets', json=payload).json()
        second = client.post('/operator/tickets', json=payload).json()
        url = f"/operator/tickets/{first['id']}/chat"
        msg = {'expected_revision': first['revision'], 'text': '<script>hello</script>'}
        saved = client.post(url, json=msg).json()
        assert saved['chat_messages'][0]['text'] == msg['text']
        assert client.post(url, json={**msg, 'role': 'player'}).status_code == 422
        assert client.post(url, json=msg).status_code == 409
        reply = client.post(url, json={'expected_revision': saved['revision'], 'text': 'Спасибо'}).json()
        assert [m['role'] for m in reply['chat_messages']] == ['admin', 'admin']
        assert client.get(f"/operator/tickets/{second['id']}").json()['chat_messages'] == []
        assert client.post(url, json={'expected_revision': reply['revision'], 'text': ' '}).status_code == 422
        assert client.post(url, json={'expected_revision': reply['revision'], 'text': 'x'}, headers={'Origin': 'https://example.com'}).status_code == 403
        assert reply['payload'] == first['payload']
    with TestClient(app, client=('127.0.0.1', 40000), base_url='http://127.0.0.1') as client:
        assert len(client.get(f"/operator/tickets/{first['id']}").json()['chat_messages']) == 2
