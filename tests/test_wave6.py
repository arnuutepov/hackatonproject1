"""Operator workflow invariants: durable edits, policy, concurrency and source identity."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from support_copilot.api import app
from support_copilot.operator_store import OperatorStore, RevisionConflict


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    path = tmp_path / 'workspace.sqlite3'
    monkeypatch.setenv('COPILOT_OPERATOR_DB', str(path))
    with TestClient(app, base_url='http://127.0.0.1') as client:
        yield client, path


def create(client, *, text='Оплатил донат, деньги не пришли', title='Проверка платежа'):
    response = client.post('/operator/tickets', json={
        'title': title, 'request': {'kind': 'report', 'messages': [
            {'role': 'player', 'text': text, 'attachments': 2}]}})
    assert response.status_code == 201, response.text
    return response.json()


def analyze(client, ticket):
    response = client.post(f"/operator/tickets/{ticket['id']}/analyze", json={
        'expected_revision': ticket['revision']})
    assert response.status_code == 200, response.text
    return response.json()


def test_workspace_serves_packaged_assets_and_csp(workspace):
    client, _ = workspace
    page = client.get('/')
    assert page.status_code == 200
    assert '/assets/workspace.js' in page.text
    assert "script-src 'self'" in page.headers['content-security-policy']
    assert "'unsafe-inline'" not in page.headers['content-security-policy']
    assert page.headers['cache-control'] == 'no-store'
    for asset in ('workspace.js', 'workspace.css'):
        response = client.get('/assets/' + asset)
        assert response.status_code == 200
        assert response.headers['x-content-type-options'] == 'nosniff'


def test_empty_queue_does_not_import_real_tickets(workspace):
    client, _ = workspace
    assert client.get('/operator/tickets').json() == {'tickets': []}


def test_full_workflow_persists_original_edit_history_and_policy(workspace):
    client, path = workspace
    ticket = create(client)
    assert ticket['source'] == 'manual' and ticket['revision'] == 1
    ticket = analyze(client, ticket)
    original = ticket['analysis']['draft_reply']['text']
    assert ticket['needs_human'] is True
    direct = client.post('/triage', json=ticket['payload']).json()
    assert ticket['analysis'] == direct
    response = client.post(f"/operator/tickets/{ticket['id']}/decision", json={
        'expected_revision': ticket['revision'], 'draft': 'Правка оператора',
        'action': 'save', 'note': 'Проверить платёж'})
    assert response.status_code == 200
    saved = response.json()
    assert saved['status'] == 'saved' and saved['needs_human'] is True
    assert saved['analysis']['draft_reply']['text'] == original
    assert saved['edited_draft'] == 'Правка оператора'
    # New store and new HTTP client read the same durable state.
    restored = OperatorStore(path).get(ticket['id'])
    assert restored == saved
    with TestClient(app, base_url='http://127.0.0.1') as restarted:
        assert restarted.get(f"/operator/tickets/{ticket['id']}").json() == saved
    assert [event['kind'] for event in restored['events']] == ['created', 'analyzed', 'saved']
    assert restored['events'][-1]['detail']['before'] == original
    assert restored['events'][-1]['detail']['note'] == 'Проверить платёж'
    assert restored['knowledge_hashes']['rules']


def test_manual_escalation_is_sticky_after_save_and_reanalysis(workspace):
    client, _ = workspace
    ticket = analyze(client, create(client, text='Застрял в текстурах'))
    for action in ['escalate', 'save']:
        ticket = client.post(f"/operator/tickets/{ticket['id']}/decision", json={
            'expected_revision': ticket['revision'], 'draft': 'Проверить человеком', 'action': action}).json()
        assert ticket['status'] == 'escalated' and ticket['needs_human'] is True
    ticket = analyze(client, ticket)
    assert ticket['status'] == 'escalated' and ticket['needs_human'] is True


def test_cannot_supply_forged_analysis_or_override_human_flag(workspace):
    client, _ = workspace
    ticket = analyze(client, create(client))
    url = f"/operator/tickets/{ticket['id']}"
    assert client.post(url + '/decision', json={
        'expected_revision': ticket['revision'], 'draft': 'Без проверки',
        'action': 'save', 'needs_human': False}).status_code == 422
    assert client.post(url + '/analyze', json={
        'expected_revision': ticket['revision'], 'analysis': {'needs_human': False}}).status_code == 422
    assert client.get(url).json()['revision'] == ticket['revision']


def test_stale_write_and_analysis_do_not_destroy_newer_work(workspace):
    client, _ = workspace
    ticket = analyze(client, create(client))
    url = f"/operator/tickets/{ticket['id']}"
    body = {'expected_revision': ticket['revision'], 'draft': 'Новая версия', 'action': 'save'}
    assert client.post(url + '/decision', json=body).status_code == 200
    assert client.post(url + '/decision', json={**body, 'draft': 'Устаревшая версия'}).status_code == 409
    assert client.post(url + '/analyze', json={'expected_revision': ticket['revision']}).status_code == 409
    assert client.get(url).json()['edited_draft'] == 'Новая версия'


def test_parallel_saves_commit_exactly_once(workspace):
    client, path = workspace
    ticket = analyze(client, create(client))
    def attempt(value):
        try:
            OperatorStore(path).decide(ticket['id'], ticket['revision'], value, 'save', '')
            return 'saved'
        except RevisionConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, ['first', 'second'])) == ['conflict', 'saved']
    saved = OperatorStore(path).get(ticket['id'])
    assert len(saved['events']) == 3


def test_unanalyzed_decision_rejected(workspace):
    client, _ = workspace
    ticket = create(client)
    response = client.post(f"/operator/tickets/{ticket['id']}/decision", json={
        'expected_revision': 1, 'draft': 'Не проверено', 'action': 'save'})
    assert response.status_code == 409


def test_examples_are_idempotent_and_preserve_existing_ticket(workspace):
    client, _ = workspace
    original = create(client)
    assert client.post('/operator/demo').json()['created'] is True
    assert client.post('/operator/demo').json()['created'] is False
    tickets = client.get('/operator/tickets').json()['tickets']
    assert len(tickets) == 7
    assert sum(ticket['source'] == 'demo' for ticket in tickets) == 6
    assert client.get(f"/operator/tickets/{original['id']}").json()['title'] == original['title']


def test_same_rule_number_keeps_section_identity(workspace):
    client, _ = workspace
    first = client.get('/operator/rules/30:1.1').json()
    second = client.get('/operator/rules/32:1.1').json()
    assert first['source_id'] != second['source_id']
    assert first['text'] != second['text']
    assert first['hash'] == second['hash']
    assert client.get('/operator/rules/999:99.99').status_code == 404


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', 'http://127.0.0.1:9999'])
def test_cross_origin_mutations_blocked(workspace, origin):
    client, _ = workspace
    assert client.post('/operator/demo', headers={'origin': origin}).status_code == 403
    assert client.get('/operator/tickets').json()['tickets'] == []


def test_same_origin_mutation_allowed(workspace):
    client, _ = workspace
    assert client.post('/operator/demo', headers={'origin': 'http://127.0.0.1'}).status_code == 200
    assert client.post('/operator/demo', headers={'sec-fetch-site': 'cross-site'}).status_code == 403


def test_non_loopback_workspace_hosts_blocked(workspace):
    client, _ = workspace
    for path in ['/', '/operator/config', '/operator/tickets']:
        assert client.get(path, headers={'host': 'attacker.example'}).status_code == 403


def test_unknown_uuid_and_validation_do_not_echo_messages(workspace):
    client, _ = workspace
    assert client.get(f'/operator/tickets/{uuid4()}').status_code == 404
    response = client.post('/operator/tickets', json={'title': 'bad', 'request': {
        'kind': 'report', 'messages': [{'role': 'admin', 'text': 'PRIVATE_MARKER'}]}})
    assert response.status_code == 422
    assert 'PRIVATE_MARKER' not in response.text


def test_config_does_not_expose_provider_credentials(workspace, monkeypatch):
    client, _ = workspace
    monkeypatch.setenv('OPENAI_API_KEY', 'PRIVATE_API_MARKER')
    response = client.get('/operator/config')
    assert response.status_code == 200
    assert response.json()['delivery_available'] is False
    assert 'PRIVATE_API_MARKER' not in response.text


def test_storage_failure_is_sanitized(workspace, monkeypatch, tmp_path):
    client, _ = workspace
    monkeypatch.setenv('COPILOT_OPERATOR_DB', str(tmp_path))  # Directory cannot be a SQLite file.
    response = client.get('/operator/tickets')
    assert response.status_code == 503
    assert str(tmp_path) not in response.text


def test_user_markup_is_data_not_executable_html(workspace):
    client, _ = workspace
    ticket = create(client, title='<script>window.BAD=1</script>', text='<img src=x onerror=alert(1)>')
    restored = client.get(f"/operator/tickets/{ticket['id']}").json()
    assert restored['title'] == ticket['title']  # Browser renders these values with textContent.
    script = client.get('/assets/workspace.js').text
    assert '.innerHTML' not in script and 'insertAdjacentHTML' not in script
