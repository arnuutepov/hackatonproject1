"""Mock adapter contract, atomic effects, retries and unchanged safety policy."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from support_copilot.api import app
from support_copilot.connector import fingerprint
from support_copilot.mock_provider import player_context
from support_copilot.operator_store import OperatorStore, RevisionConflict


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('COPILOT_OPERATOR_DB', str(tmp_path / 'workspace.sqlite3'))
    monkeypatch.delenv('COPILOT_CONNECTOR_AUDIT', raising=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield client


def ingest(client, scenario='payment_ru', **values):
    payload = {'event_id': str(uuid4()), 'scenario_id': scenario, **values}
    response = client.post('/api/v1/simulate-ticket', json=payload)
    assert response.status_code == 201, response.text
    return response.json()['ticket'], payload


def action(ticket, **values):
    return {'action_id': str(uuid4()), 'expected_revision': ticket['revision'], 'action': 'reply',
            'draft': ticket['edited_draft'], 'note': '', 'channel': 'game_chat', **values}


def send(client, ticket, payload):
    return client.post(f"/api/v1/tickets/{ticket['id']}/actions", json=payload)


def test_ingestion_enriches_without_changing_core_contract(client):
    ticket, _ = ingest(client)
    assert ticket['source'] == 'mock_connector' and ticket['revision'] == 2
    context = ticket['connector']['context']
    assert context['is_mock'] and context['provider'] == 'mock_fixture_v1'
    assert context['player_id'] == 777123
    assert len(context['profile']['transactions']) == 3
    assert 'player_id' not in ticket['payload']
    assert ticket['analysis'] == client.post('/triage', json=ticket['payload']).json()
    assert ticket['analysis']['generation']['attempts'] == 0
    assert client.get('/operator/tickets').json()['tickets'][0]['player_id'] == 777123
    assert OperatorStore().get(ticket['id']) == ticket


def test_replayed_ingestion_preserves_snapshot_and_revision(client, monkeypatch):
    ticket, payload = ingest(client)
    monkeypatch.setattr('support_copilot.connector.triage', lambda *a, **kw: pytest.fail('Replayed ingestion ran inference'))
    response = client.post('/api/v1/simulate-ticket', json=payload)
    assert response.status_code == 200
    assert response.json()['replayed'] and response.json()['ticket'] == ticket
    assert len(client.get('/operator/tickets').json()['tickets']) == 1
    assert client.post('/api/v1/simulate-ticket', json={**payload, 'channel': 'telegram'}).status_code == 409


@pytest.mark.parametrize('scenario,language', [('account_kk_injection', 'kk'), ('stuck_uz_injection', 'uz')])
def test_multilingual_injection_remains_untrusted(client, scenario, language, monkeypatch):
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER', 'responses')
    ticket, _ = ingest(client, scenario)
    result = ticket['analysis']
    assert result['lang'] == language and result['draft_reply']['lang'] == language
    assert 'instruction_override_attempt' in result['safety_flags']
    assert result['needs_human'] is True
    assert result['draft_reply']['established_facts'] == []
    assert result['generation']['attempts'] == 0 and result['generation']['api_cost_usd'] == 0


def test_unknown_player_has_no_invented_profile(client):
    ticket, _ = ingest(client, 'unknown_player')
    context = ticket['connector']['context']
    assert context['lookup_status'] == 'not_found' and context['profile'] is None
    assert '77:7.7' not in ticket['analysis']['draft_reply']['cited']


def test_custom_ticket_and_fixture_copy_are_isolated(client):
    response = client.post('/api/v1/simulate-ticket', json={
        'event_id': str(uuid4()), 'player_id': 880012, 'title': 'Свой кейс', 'channel': 'telegram',
        'request': {'kind': 'report', 'messages': [{'role': 'player', 'text': '<script>alert(1)</script> Застрял'}]}})
    assert response.status_code == 201
    ticket = response.json()['ticket']
    assert ticket['connector']['channel'] == 'telegram'
    assert ticket['payload']['messages'][0]['text'].startswith('<script>')
    context = player_context(777123)
    context['profile']['transactions'].clear()
    assert len(player_context(777123)['profile']['transactions']) == 3


@pytest.mark.parametrize('player_id', [0, -1, True, '777123', 2**53])
def test_custom_player_id_is_strict_and_browser_safe(client, player_id):
    response = client.post('/api/v1/simulate-ticket', json={
        'event_id': str(uuid4()), 'player_id': player_id, 'title': 'Custom',
        'request': {'kind': 'report', 'messages': [{'role': 'player', 'text': 'Помогите'}]}})
    assert response.status_code == 422


@pytest.mark.parametrize('change', [
    {'scenario_id': None}, {'scenario_id': 'missing'}, {'player_id': 777123}, {'title': 'Override'},
    {'channel': 'real_game'}, {'context': {'balance': 999999}}, {'event_id': 'not-a-uuid'},
])
def test_ingestion_rejects_ambiguous_or_forged_envelopes(client, change):
    payload = {'event_id': str(uuid4()), 'scenario_id': 'payment_ru', **change}
    response = client.post('/api/v1/simulate-ticket', json=payload)
    assert response.status_code == 422
    assert client.get('/operator/tickets').json()['tickets'] == []


def test_mock_reply_is_durable_and_idempotent(client):
    ticket, _ = ingest(client)
    payload = action(ticket, draft='Проверим сведения о платеже.', channel='telegram')
    response = send(client, ticket, payload)
    assert response.status_code == 200
    result = response.json()
    event, saved = result['event'], result['ticket']
    assert not result['replayed'] and result['audit']['status'] == 'written'
    assert event['mode'] == 'mock' and event['delivered'] is False
    assert event['status'] == 'simulated' and event['channel'] == 'telegram'
    assert saved['status'] == 'reply_simulated' and saved['needs_human'] is True
    assert saved['payload'] == ticket['payload'] and saved['analysis'] == ticket['analysis']
    assert saved['chat_messages'][0]['text'] == payload['draft']
    assert OperatorStore().get(ticket['id']) == saved
    replay = send(client, ticket, payload).json()
    assert replay['replayed'] and replay['ticket'] == saved
    assert len(client.get('/api/v1/outbox').json()['events']) == 1
    assert send(client, ticket, {**payload, 'draft': 'Changed'}).status_code == 409
    assert send(client, ticket, action(ticket)).status_code == 409
    assert len(OperatorStore().get(ticket['id'])['outbox']) == 1
    audit = OperatorStore().path.with_name('mock-outbox.audit.jsonl')
    assert [json.loads(line) for line in audit.read_text().splitlines()] == [event]


def test_escalation_is_sticky_and_retry_never_overwrites_newer_decision(client):
    ticket, _ = ingest(client, 'stuck_uz_injection')
    first_payload = action(ticket, action='escalate', note='Ручная проверка')
    first = send(client, ticket, first_payload).json()['ticket']
    assert first['status'] == 'escalated' and first['chat_messages'] == []
    second = send(client, first, action(first)).json()['ticket']
    assert second['status'] == 'escalated' and second['needs_human']
    assert send(client, ticket, first_payload).json()['ticket'] == second
    assert client.post(f"/operator/tickets/{ticket['id']}/workflow", json={
        'expected_revision': second['revision'], 'action': 'resolve'}).status_code == 409
    assert len(second['outbox']) == 2 and len(second['chat_messages']) == 1


def test_failed_file_export_is_recoverable_without_repeating_action(client, monkeypatch):
    ticket, _ = ingest(client)
    payload = action(ticket)
    with monkeypatch.context() as temporary:
        temporary.setattr('support_copilot.operator_store.os.replace', lambda *args: (_ for _ in ()).throw(OSError('unwritable')))
        result = send(client, ticket, payload).json()
        assert result['audit']['status'] == 'pending'
        assert len(result['ticket']['outbox']) == 1
    restored = client.get('/api/v1/outbox').json()
    assert restored['audit']['status'] == 'written' and len(restored['events']) == 1
    assert send(client, ticket, payload).json()['replayed']
    audit = OperatorStore().path.with_name('mock-outbox.audit.jsonl')
    assert len(audit.read_text().splitlines()) == 1


def test_concurrent_same_event_creates_one_case(client):
    ticket, _ = ingest(client)
    workspace = OperatorStore()
    event_id = str(uuid4())
    def submit(_):
        return workspace.ingest(event_id, 'same-input', ticket['title'], ticket['payload'], 'game_chat',
                                ticket['connector']['context'], ticket['analysis'], 1.0, ticket['knowledge_hashes'])
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(submit, range(3)))
    assert len({key for key, replay in results}) == 1
    assert sum(not replay for key, replay in results) == 1


def test_concurrent_outbound_actions_obey_revision(client):
    ticket, _ = ingest(client)
    def submit(_):
        payload = action(ticket)
        try:
            OperatorStore().mock_action(ticket['id'], payload, fingerprint(payload))
            return 'ok'
        except RevisionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == ['conflict', 'ok']
    assert len(OperatorStore().get(ticket['id'])['outbox']) == 1


def test_concurrent_identical_action_and_exports_are_deduplicated(client):
    ticket, _ = ingest(client)
    payload = action(ticket)
    def submit(_):
        workspace = OperatorStore()
        event, replay = workspace.mock_action(ticket['id'], payload, fingerprint(payload))
        assert workspace.export_mock_audit()['status'] == 'written'
        return replay
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sorted(pool.map(submit, range(3))) == [False, True, True]
    audit = OperatorStore().path.with_name('mock-outbox.audit.jsonl')
    assert len(audit.read_text().splitlines()) == 1


@pytest.mark.parametrize('change', [{'action': 'ban'}, {'draft': ' '}, {'channel': 'clickhouse'},
                                   {'needs_human': False}, {'action_id': 'bad'}])
def test_outbound_cannot_override_policy_or_execute_sanctions(client, change):
    ticket, _ = ingest(client)
    response = send(client, ticket, action(ticket, **change))
    assert response.status_code == 422
    assert OperatorStore().get(ticket['id']) == ticket


def test_connector_guard_and_validation_do_not_echo_input(client):
    for path in ('/api/v1/connector', '/api/v1/outbox'):
        assert client.get('http://untrusted.example' + path).status_code == 403
    payload = {'event_id': str(uuid4()), 'scenario_id': 'payment_ru'}
    assert client.post('/api/v1/simulate-ticket', json=payload, headers={'Origin': 'https://other.example'}).status_code == 403
    marker = 'private-input-canary'
    rejected = client.post('/api/v1/simulate-ticket', json={**payload, 'secret': marker})
    assert rejected.status_code == 422 and marker not in rejected.text
    assert client.get('/api/v1/connector').headers['cache-control'] == 'no-store'


def test_existing_database_gets_connector_tables_without_losing_cases(client):
    ticket = client.post('/operator/tickets', json={'title': 'Legacy', 'request': {
        'kind': 'report', 'messages': [{'role': 'player', 'text': 'Застрял в текстурах'}]}}).json()
    path = OperatorStore().path
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE connector_outbox')
        db.execute('DROP TABLE connector_inbox')
    restored = OperatorStore(path).get(ticket['id'])
    assert restored['payload'] == ticket['payload'] and restored['connector'] is None
    ticket = client.post(f"/operator/tickets/{ticket['id']}/analyze", json={'expected_revision': 1}).json()
    assert send(client, ticket, action(ticket)).status_code == 409


def test_ui_assets_include_connector_controls(client):
    page = client.get('/').text
    for control in ('simulate-ticket', 'simulate-dialog', 'player-context', 'mock-send', 'tab-outbox'):
        assert f'id="{control}"' in page
    assert client.get('/assets/connector-ui.js').status_code == 200
