"""Persistent assignment and closure cannot bypass mandatory human review."""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from support_copilot.api import app
from support_copilot.operator_store import OperatorStore, RevisionConflict


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('COPILOT_OPERATOR_DB', str(tmp_path / 'operator.sqlite3'))
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield client


def create(client, text='Застрял в текстурах'):
    return client.post('/operator/tickets', json={'title': 'Test', 'request': {
        'kind': 'report', 'messages': [{'role': 'player', 'text': text, 'attachments': 0}]}}).json()


def action(client, ticket, value):
    return client.post(f"/operator/tickets/{ticket['id']}/workflow", json={
        'expected_revision': ticket['revision'], 'action': value})


def analyzed(client, ticket):
    return client.post(f"/operator/tickets/{ticket['id']}/analyze", json={
        'expected_revision': ticket['revision']}).json()


def test_assignment_survives_new_store_and_stale_client(client):
    first = create(client)
    taken = action(client, first, 'take').json()
    assert taken['assigned_to'] == 'local_operator'
    assert OperatorStore().get(first['id'])['assigned_to'] == 'local_operator'
    assert action(client, first, 'release').status_code == 409
    assert client.get('/operator/tickets').json()['tickets'][0]['assigned_to'] == 'local_operator'
    released = action(client, taken, 'release').json()
    assert released['assigned_to'] is None
    assert [event['kind'] for event in released['events']] == ['created', 'take', 'release']


def test_resolution_requires_analysis_and_can_reopen(client):
    ticket = create(client)
    assert action(client, ticket, 'resolve').status_code == 409
    ticket = analyzed(client, ticket)
    assert ticket['needs_human'] is False
    resolved = action(client, ticket, 'resolve').json()
    assert resolved['status'] == 'resolved'
    assert resolved['analysis'] == ticket['analysis']
    assert action(client, ticket, 'reopen').status_code == 409
    reopened = action(client, resolved, 'reopen').json()
    assert reopened['status'] == 'analyzed'
    assert reopened['edited_draft'] == resolved['edited_draft']


@pytest.mark.parametrize('text', ['Оплатил донат, деньги не пришли', 'Мой аккаунт взломали', 'Хочу обжаловать бан'])
def test_mandatory_review_cannot_be_resolved(client, text):
    ticket = analyzed(client, create(client, text))
    ticket = action(client, ticket, 'take').json()
    assert ticket['needs_human'] is True
    assert action(client, ticket, 'resolve').status_code == 409
    unchanged = client.get(f"/operator/tickets/{ticket['id']}").json()
    assert unchanged['revision'] == ticket['revision']
    assert unchanged['needs_human'] is True


def test_manual_escalation_survives_assignment_and_blocks_resolution(client):
    ticket = analyzed(client, create(client))
    ticket = client.post(f"/operator/tickets/{ticket['id']}/decision", json={
        'expected_revision': ticket['revision'], 'draft': ticket['edited_draft'],
        'action': 'escalate', 'note': 'Check manually'}).json()
    ticket = action(client, ticket, 'take').json()
    ticket = action(client, ticket, 'release').json()
    assert ticket['status'] == 'escalated' and ticket['needs_human']
    assert action(client, ticket, 'resolve').status_code == 409
    assert action(client, ticket, 'reopen').status_code == 409


def test_concurrent_assignment_obeys_revision(client):
    ticket = create(client)
    def claim():
        try:
            OperatorStore().workflow(ticket['id'], ticket['revision'], 'take')
            return 'ok'
        except RevisionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == ['conflict', 'ok']


def test_workflow_rejects_sanctions_and_policy_override(client):
    ticket = create(client)
    assert action(client, ticket, 'ban').status_code == 422
    assert client.post(f"/operator/tickets/{ticket['id']}/workflow", json={
        'expected_revision': 1, 'action': 'resolve', 'needs_human': False}).status_code == 422
    assert client.post(f"/operator/tickets/{ticket['id']}/workflow", json={
        'expected_revision': 1, 'action': 'take'}, headers={'Origin': 'https://example.com'}).status_code == 403
