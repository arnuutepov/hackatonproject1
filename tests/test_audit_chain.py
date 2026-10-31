"""Audit chain: every stored field is covered, and every claim about detection is exercised."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from support_copilot import chain_cli, connector, game_ingestion, operator, service
from support_copilot.api import app
from support_copilot.audit_chain import (
    AuditChain, Block, ChainBroken, ChainKeyMismatch, ZERO_HASH, canonical, content_hash, digest, header_hash,
    triage_payload)
from support_copilot.auth_store import AuthStore, hash_password
from support_copilot.chain_signer import Ed25519Signer, HmacSigner, SignerConfigError, load_signer
from support_copilot.knowledge import Knowledge
from support_copilot.operator_store import OperatorStore, RevisionConflict

PASSWORD = 'fictional password for isolated tests'
ORIGIN = {'Origin': 'https://localhost'}
SECRET_TEXT = 'Секретная жалоба: Менің аккаунтымды бұзды, yordam bering'
REQUEST = {'kind': 'report', 'messages': [{'role': 'player', 'text': SECRET_TEXT, 'attachments': 2}]}
ANALYSIS = {
    'lang': 'ru', 'category': 'report.account', 'priority': 'p0', 'needs_human': True,
    'extracted': {'attachments_claimed': 2, 'server': '7'}, 'retrieved_rules': [{'source_id': '30:1.1'}],
    'draft_reply': {'text': 'Черновик ответа', 'cited': ['30:1.2'], 'provenance': 'safe_template'},
    'safety_flags': ['account_takeover'], 'reasons': ['human_review_required_no_personal_action'],
}


def fill(chain, count=5):
    for number in range(count):
        chain.append('operator.login', f'operator-{number}', {'id': f'operator-{number}', 'role': 'agent'})


def rows_sql(chain, statement, parameters=()):
    with closing(sqlite3.connect(chain.path)) as connection:
        connection.execute(statement, parameters)
        connection.commit()


@pytest.fixture
def chain(tmp_path):
    value = AuditChain(tmp_path / 'workspace.sqlite3')
    fill(value)
    return value


@pytest.fixture
def knowledge(monkeypatch, tmp_path):
    folder = tmp_path / 'knowledge'
    folder.mkdir()
    (folder / 'rules.json').write_text(json.dumps({'rules': [{'id': '30', 'category': 'Общие', 'points': [
        {'ref': '1.1', 'text': 'Оскорбления игроков запрещены'}, {'ref': '1.2', 'text': 'Читы запрещены'}]}]}),
        encoding='utf-8')
    codes = [('report', code) for code in ('payment', 'account', 'complaint', 'help', 'question', 'offtopic')]
    codes += [('complaint', code) for code in ('admin', 'cheat', 'scam', 'rmt_ads', 'insult', 'dm', 'invalid', 'other')]
    (folder / 'taxonomy.json').write_text(json.dumps({
        'version': 'synthetic', 'needs_human_always': ['report.payment', 'report.account'],
        'priorities': [{'code': f'p{level}', 'title': f'P{level}'} for level in range(4)],
        'categories': [{'code': f'{kind}.{code}', 'kind': kind, 'title': code, 'rules': []} for kind, code in codes]}),
        encoding='utf-8')
    value = Knowledge(folder)
    for module in (service, operator, connector, game_ingestion, sys.modules['support_copilot.api']):
        monkeypatch.setattr(module, 'get_knowledge', lambda: value)
    return value


@pytest.fixture
def secured(monkeypatch, knowledge):
    monkeypatch.setenv('COPILOT_AUTH_MODE', 'required')
    store = AuthStore()
    encoded = hash_password(PASSWORD)
    with store.connect() as db:
        for role in ('agent', 'admin'):
            db.execute('INSERT INTO operators VALUES (?, ?, ?, ?, 1)', (str(uuid4()), role, encoded, role))
    with TestClient(app, base_url='https://localhost', client=('127.0.0.1', 40000)) as client:
        yield client


def login(client, name):
    response = client.post('/auth/login', json={'username': name, 'password': PASSWORD}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()['operator']


def events(chain=None):
    return [block.event_type for block in reversed((chain or AuditChain()).blocks(limit=200))]


def test_hash_is_deterministic_for_multilingual_payload():
    first = {'ru': 'жалоба', 'kk': 'шағым әңғөұүі', 'uz': "o'yinchi shikoyat qildi ўқғҳ"}
    second = dict(reversed(list(first.items())))
    assert canonical(first) == canonical(second)
    assert content_hash(first) == content_hash(second)
    assert 'шағым әңғөұүі' in canonical(first)
    assert digest(canonical(first)) == content_hash(first)
    assert header_hash(1, 't', ZERO_HASH, 'e', 'a', 'd') == header_hash(1, 't', ZERO_HASH, 'e', 'a', 'd')


def test_digest_of_lone_surrogate_cannot_fail_the_journal():
    assert len(digest('\ud800')) == 64


def test_genesis_records_chain_identity_and_is_idempotent(tmp_path):
    chain = AuditChain(tmp_path / 'db.sqlite3')
    assert chain.head() is None
    genesis = chain.genesis()
    assert chain.genesis() == genesis
    payload = genesis.payload()
    assert (genesis.index, genesis.prev_hash, genesis.event_type) == (0, ZERO_HASH, 'genesis')
    assert payload['chain_id'] == 'oguzki-support-copilot'
    assert payload['schema_version'] == 1
    assert payload['key']['fingerprint'] == chain.signer.fingerprint


def test_fresh_chain_is_valid_and_each_block_links_to_the_previous(chain):
    report = chain.verify()
    assert report['valid'] is True
    assert report['first_failure'] is None
    assert report['height'] == 5
    assert report['blocks_checked'] == 6
    assert report['mirror'] == {'status': 'match', 'height': 5, 'failure': None}
    blocks = chain.blocks()
    assert report['head_hash'] == blocks[0].block_hash
    assert all(newer.prev_hash == older.block_hash for newer, older in zip(blocks, blocks[1:]))


def test_empty_chain_is_not_an_alarm(tmp_path):
    report = AuditChain(tmp_path / 'db.sqlite3').verify()
    assert report['valid'] is True
    assert report['height'] == -1


@pytest.mark.parametrize('column, value, check', [
    ('payload_json', '{"id":"operator-1","role":"admin"}', 'payload_digest'),
    ('payload_digest', 'a' * 64, 'payload_digest'),
    ('block_hash', 'b' * 64, 'block_hash'),
    ('prev_hash', 'c' * 64, 'block_hash'),
    ('signature', 'd' * 128, 'signature'),
    ('signature', 'not hex', 'signature'),
    ('actor', 'someone-else', 'block_hash'),
    ('created_at', '2000-01-01T00:00:00+00:00', 'block_hash'),
    ('event_type', 'operator.logout', 'block_hash'),
])
def test_every_field_mutation_names_the_block(chain, column, value, check):
    rows_sql(chain, f'UPDATE chain_blocks SET {column} = ? WHERE idx = 3', (value,))
    report = chain.verify()
    assert report['valid'] is False
    assert report['first_failure'] == {'index': 3, 'check': check}


def test_broken_report_still_describes_the_stored_head(chain):
    head = chain.head()
    rows_sql(chain, "UPDATE chain_blocks SET actor = 'x' WHERE idx = 2")
    report = chain.verify()
    assert (report['height'], report['head_hash']) == (head.index, head.block_hash)
    assert report['blocks_checked'] == 2


def test_deleted_middle_block_is_an_index_gap(chain):
    rows_sql(chain, 'DELETE FROM chain_blocks WHERE idx = 2')
    assert chain.verify()['first_failure'] == {'index': 2, 'check': 'index'}


def test_inserted_block_is_rejected(chain):
    with closing(sqlite3.connect(chain.path)) as connection:
        connection.execute('UPDATE chain_blocks SET idx = idx + 1 WHERE idx >= 3 AND idx = (SELECT MAX(idx) FROM chain_blocks)')
        connection.execute('UPDATE chain_blocks SET idx = 5 WHERE idx = 4')
        connection.execute('UPDATE chain_blocks SET idx = 4 WHERE idx = 3')
        connection.execute('INSERT INTO chain_blocks SELECT 3, created_at, prev_hash, event_type, actor, payload_json, '
                           'payload_digest, block_hash, signature FROM chain_blocks WHERE idx = 2')
        connection.commit()
    report = chain.verify()
    assert report['valid'] is False
    assert report['first_failure']['index'] == 3


def test_reordered_blocks_are_rejected(chain):
    with closing(sqlite3.connect(chain.path)) as connection:
        connection.execute('UPDATE chain_blocks SET idx = -1 WHERE idx = 2')
        connection.execute('UPDATE chain_blocks SET idx = 2 WHERE idx = 3')
        connection.execute('UPDATE chain_blocks SET idx = 3 WHERE idx = -1')
        connection.commit()
    assert chain.verify()['first_failure'] == {'index': 2, 'check': 'block_hash'}


def test_damaged_payload_never_crashes_the_check_or_the_listing(chain):
    rows_sql(chain, "UPDATE chain_blocks SET payload_json = 'not json' WHERE idx = 2")
    assert chain.verify()['first_failure'] == {'index': 2, 'check': 'payload_digest'}
    assert [block.public()['payload'] for block in chain.blocks(limit=4)][3] is None


def forged(chain, signer):
    """Rewrite block 3, then recompute every hash after it as an attacker with the database would."""
    with closing(sqlite3.connect(chain.path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute('SELECT * FROM chain_blocks ORDER BY idx')]
        previous = rows[2]['block_hash']
        for row in rows[3:]:
            if row['idx'] == 3:
                row['payload_json'] = canonical({'id': 'forged', 'role': 'admin'})
                row['payload_digest'] = digest(row['payload_json'])
            row['prev_hash'] = previous
            row['block_hash'] = header_hash(row['idx'], row['created_at'], previous, row['event_type'], row['actor'],
                                            row['payload_digest'])
            if signer:
                row['signature'] = signer.sign(row['block_hash'])
            previous = row['block_hash']
            connection.execute('UPDATE chain_blocks SET payload_json = ?, payload_digest = ?, prev_hash = ?, '
                               'block_hash = ?, signature = ? WHERE idx = ?',
                               (row['payload_json'], row['payload_digest'], row['prev_hash'], row['block_hash'],
                                row['signature'], row['idx']))
        connection.commit()


def test_consistent_forgery_without_the_signing_key_fails_on_signature(chain):
    forged(chain, None)
    assert chain.verify()['first_failure'] == {'index': 3, 'check': 'signature'}


def test_forgery_signed_with_another_key_fails_on_signature(chain):
    forged(chain, Ed25519Signer(b'\x07' * 32))
    assert chain.verify()['first_failure'] == {'index': 3, 'check': 'signature'}


def rebuild_with(chain, signer):
    """A whole replacement chain under a fresh key: internally perfect, only the key differs."""
    fake = AuditChain(chain.path.with_name('fake.sqlite3'), signer=signer)
    fill(fake)
    chain.path.unlink()
    fake.path.rename(chain.path)


def test_chain_rebuilt_under_a_new_key_is_rejected_by_configured_key(chain):
    chain.mirror_path.unlink()
    rebuild_with(chain, Ed25519Signer(b'\x09' * 32))
    report = AuditChain(chain.path).verify()
    assert report['first_failure'] == {'index': 5, 'check': 'configured_key'}


def test_pinned_public_key_rejects_a_rebuilt_genesis_for_any_verifier(chain, monkeypatch):
    monkeypatch.setenv('COPILOT_CHAIN_PUBLIC_KEY', chain.signer.public_key)
    chain.mirror_path.unlink()
    rebuild_with(chain, Ed25519Signer(b'\x09' * 32))
    assert AuditChain(chain.path).verify()['first_failure'] == {'index': 0, 'check': 'genesis_key'}


def test_wrong_chain_id_is_rejected(chain, monkeypatch):
    monkeypatch.setenv('COPILOT_CHAIN_ID', 'other-chain')
    assert chain.verify()['first_failure'] == {'index': 0, 'check': 'genesis'}


def test_tail_truncation_is_invisible_to_the_chain_but_caught_by_the_mirror(chain):
    rows_sql(chain, 'DELETE FROM chain_blocks WHERE idx > 3')
    with chain.connect() as connection:
        walked, _ = chain.walk(chain._rows(connection))
    assert walked['valid'] is True
    report = chain.verify()
    assert report['valid'] is False
    assert report['first_failure'] == {'index': 4, 'check': 'tail_truncated'}


def test_tail_truncation_is_caught_by_a_signed_anchor_when_the_mirror_is_gone(chain):
    chain.checkpoint()
    rows_sql(chain, 'DELETE FROM chain_blocks WHERE idx > 3')
    chain.mirror_path.unlink()
    assert chain.verify()['first_failure'] == {'index': 4, 'check': 'tail_truncated'}


def test_truncated_then_regrown_chain_cannot_match_the_mirror(chain):
    rows_sql(chain, 'DELETE FROM chain_blocks WHERE idx > 3')
    fill(chain, 4)
    assert chain.verify()['first_failure'] == {'index': 4, 'check': 'mirror_mismatch'}


def test_edited_mirror_line_is_detected(chain):
    lines = chain.mirror_path.read_text(encoding='utf-8').splitlines()
    lines[2] = lines[2].replace('operator-1', 'operator-9')
    chain.mirror_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert chain.verify()['first_failure'] == {'index': 2, 'check': 'mirror_mismatch'}


def test_edited_or_foreign_anchor_is_detected(chain):
    anchor = chain.checkpoint()
    assert chain.verify()['anchors']['status'] == 'match'
    chain.anchors_path.write_text(json.dumps({**anchor, 'height': 2}) + '\n', encoding='utf-8')
    assert chain.verify()['first_failure'] == {'index': 0, 'check': 'anchor_signature'}
    foreign = {**anchor, 'key_fingerprint': 'f' * 64}
    chain.anchors_path.write_text(json.dumps(foreign) + '\n', encoding='utf-8')
    assert chain.verify()['first_failure']['check'] == 'anchor_signature'


def test_anchor_refuses_a_broken_chain(chain):
    rows_sql(chain, "UPDATE chain_blocks SET actor = 'x' WHERE idx = 1")
    with pytest.raises(ChainBroken):
        chain.checkpoint()
    assert not chain.anchors_path.exists()


def test_parallel_appends_leave_no_index_gaps(tmp_path):
    path = tmp_path / 'db.sqlite3'
    AuditChain(path).genesis()

    def write(number):
        return AuditChain(path).append('operator.login', 'worker', {'id': number, 'role': 'agent'}).index

    with ThreadPoolExecutor(8) as pool:
        indexes = list(pool.map(write, range(60)))
    assert sorted(indexes) == list(range(1, 61))
    report = AuditChain(path).verify()
    assert report['valid'] is True
    assert report['height'] == 60
    assert report['mirror']['status'] == 'match'


def test_rollback_leaves_neither_block_nor_mirror_line(tmp_path):
    store = OperatorStore(tmp_path / 'db.sqlite3')
    key = store.create('Тест', REQUEST)
    store.analyze(key, 1, ANALYSIS, 1.0, {})
    height = store.chain.head().index
    with pytest.raises(RevisionConflict):
        store.analyze(key, 1, ANALYSIS, 1.0, {})
    with pytest.raises(RuntimeError):
        with store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            store._record(connection, 'operator.login', {'id': 'x', 'role': 'agent'})
            raise RuntimeError('action failed after its block')
    assert store.chain.head().index == height
    assert store.chain.verify()['mirror'] == {'status': 'match', 'height': height, 'failure': None}


def test_append_outside_a_transaction_is_refused(tmp_path):
    store = OperatorStore(tmp_path / 'db.sqlite3')
    with pytest.raises(RuntimeError):
        with store.connect() as connection:
            store.chain.append('operator.login', 'x', {}, connection=connection)


def test_unknown_event_type_is_refused(chain):
    with pytest.raises(ValueError):
        chain.append('genesis', 'attacker', {})


def test_failed_mirror_write_never_undoes_the_action_and_catches_up(tmp_path, monkeypatch):
    blocked = tmp_path / 'blocked'
    blocked.mkdir()
    monkeypatch.setenv('COPILOT_CHAIN_MIRROR', str(blocked))
    chain = AuditChain(tmp_path / 'db.sqlite3')
    fill(chain, 2)
    assert chain.head().index == 2
    assert chain.publish()['status'] == 'pending'
    monkeypatch.setenv('COPILOT_CHAIN_MIRROR', str(tmp_path / 'late.jsonl'))
    assert chain.publish()['status'] == 'written'
    assert chain.verify()['mirror'] == {'status': 'match', 'height': 2, 'failure': None}


def test_mirror_behind_is_pending_and_not_an_alarm(chain):
    lines = chain.mirror_path.read_text(encoding='utf-8').splitlines()
    chain.mirror_path.write_text('\n'.join(lines[:3]) + '\n', encoding='utf-8')
    report = chain.verify()
    assert report['valid'] is True
    assert report['mirror'] == {'status': 'pending', 'height': 2, 'failure': None}


def test_crash_cut_mirror_line_is_dropped_and_rewritten(chain):
    data = chain.mirror_path.read_bytes()
    chain.mirror_path.write_bytes(data[:-20])
    chain.publish()
    assert chain.mirror_path.read_bytes() == data


def test_key_rotation_is_signed_by_the_old_key_and_followed_by_the_new(chain):
    new_key = Ed25519Signer(b'\x11' * 32)
    block = chain.rotate(new_key, 'плановая ротация')
    assert block.event_type == 'key.rotated'
    fill(chain, 2)
    anchor = chain.checkpoint()
    assert anchor['key_fingerprint'] == new_key.fingerprint
    assert chain.verify()['valid'] is True


def test_rotation_block_forged_with_the_new_key_is_rejected(chain):
    old = chain.signer
    chain.rotate(Ed25519Signer(b'\x11' * 32), 'test')
    assert old.verify(chain.head().block_hash, chain.head().signature)
    forged(chain, Ed25519Signer(b'\x11' * 32))
    assert chain.verify()['first_failure'] == {'index': 3, 'check': 'signature'}


def test_writing_with_a_key_the_chain_does_not_expect_is_refused(chain):
    other = AuditChain(chain.path, signer=Ed25519Signer(b'\x22' * 32))
    with pytest.raises(ChainKeyMismatch):
        other.append('operator.login', 'x', {})
    assert chain.head().index == 5


def test_hmac_provider_needs_the_secret_to_verify(tmp_path, monkeypatch):
    secret = 'h' * 32
    monkeypatch.setenv('COPILOT_CHAIN_SIGNER', 'hmac')
    monkeypatch.setenv('COPILOT_CHAIN_SECRET', secret)
    chain = AuditChain(tmp_path / 'db.sqlite3')
    fill(chain, 2)
    assert chain.signer.algorithm == 'hmac-sha256'
    assert secret not in chain.genesis().payload_json
    assert chain.genesis().payload()['key']['public_key'] is None
    assert chain.verify()['valid'] is True
    monkeypatch.delenv('COPILOT_CHAIN_SECRET')
    monkeypatch.setenv('COPILOT_CHAIN_SIGNER', 'ed25519')
    assert AuditChain(chain.path).verify()['first_failure'] == {'index': 0, 'check': 'signature_key_unavailable'}


def test_hmac_rotation_keeps_old_blocks_verifiable_with_the_previous_secret(tmp_path, monkeypatch):
    old, new = 'o' * 32, 'n' * 32
    monkeypatch.setenv('COPILOT_CHAIN_SIGNER', 'hmac')
    monkeypatch.setenv('COPILOT_CHAIN_SECRET', old)
    chain = AuditChain(tmp_path / 'db.sqlite3')
    fill(chain, 1)
    chain.rotate(HmacSigner(new.encode()), 'test')
    monkeypatch.setenv('COPILOT_CHAIN_SECRET', new)
    monkeypatch.setenv('COPILOT_CHAIN_PREVIOUS_SECRETS', old)
    fill(chain, 1)
    assert AuditChain(chain.path).verify()['valid'] is True


def test_signer_configuration_errors_are_explicit(tmp_path, monkeypatch):
    with pytest.raises(SignerConfigError):
        HmacSigner(b'short')
    monkeypatch.setenv('COPILOT_CHAIN_SIGNER', 'none')
    with pytest.raises(SignerConfigError):
        load_signer(tmp_path / 'key', create=True)
    monkeypatch.setenv('COPILOT_CHAIN_SIGNER', 'hmac')
    with pytest.raises(SignerConfigError):
        load_signer(tmp_path / 'key', create=True)


def test_verification_never_generates_a_key(tmp_path):
    chain = AuditChain(tmp_path / 'db.sqlite3')
    chain.verify()
    assert not chain.key_file.exists()


def test_restore_is_declared_and_explains_the_rewound_tail(chain):
    chain.checkpoint()
    rows_sql(chain, 'DELETE FROM chain_blocks WHERE idx > 3')
    block = chain.record_restore('Восстановление из ночного бэкапа')
    payload = block.payload()
    assert block.event_type == 'chain.restored'
    assert (payload['restored_height'], payload['mirror_height'], payload['missing_blocks']) == (3, 5, 2)
    assert chain.mirror_path.with_name('chain-mirror.pre-restore-3.jsonl').exists()
    report = chain.verify()
    assert report['valid'] is True
    assert report['mirror']['status'] == 'match'


def test_restore_refuses_a_chain_that_does_not_verify(chain):
    rows_sql(chain, "UPDATE chain_blocks SET actor = 'x' WHERE idx = 1")
    with pytest.raises(ChainBroken):
        chain.record_restore('test')


def test_export_verifies_with_public_data_only(chain, tmp_path):
    target = tmp_path / 'exported.jsonl'
    assert chain.export(target)['blocks'] == 6
    chain.key_file.unlink()
    outsider = AuditChain(tmp_path / 'other' / 'empty.sqlite3')
    assert outsider.verify_export(target)['valid'] is True
    lines = target.read_text(encoding='utf-8').splitlines()
    damaged = json.loads(lines[2])
    damaged['actor'] = 'someone-else'
    lines[2] = json.dumps(damaged)
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert outsider.verify_export(target)['first_failure'] == {'index': 2, 'check': 'block_hash'}


def test_ticket_journal_is_atomic_and_proves_text_without_storing_it(tmp_path):
    store = OperatorStore(tmp_path / 'db.sqlite3', actor='operator-1')
    key = store.create('Тест', REQUEST)
    store.analyze(key, 1, ANALYSIS, 1.0, {})
    blocks = {block.event_type: block for block in store.chain.blocks()}
    assert set(blocks) == {'genesis', 'triage.completed', 'escalation.human'}
    payload = blocks['triage.completed'].payload()
    assert blocks['triage.completed'].actor == 'operator-1'
    assert payload['content_hash'] == content_hash(REQUEST)
    assert payload['draft_hash'] == digest('Черновик ответа')
    assert payload['rule_ids'] == ['30:1.1', '30:1.2']
    assert payload['attachments_claimed'] == 2
    assert blocks['escalation.human'].payload()['safety_flags'] == ['account_takeover']
    assert store.get(key)['chain_block'] == blocks['triage.completed'].index
    stored = store.chain.mirror_path.read_text(encoding='utf-8')
    assert SECRET_TEXT not in stored
    assert 'Черновик' not in stored


def test_triage_payload_omits_escalation_free_text():
    payload = triage_payload(REQUEST, ANALYSIS, 'ticket')
    assert SECRET_TEXT not in canonical(payload)


def test_operator_journey_reaches_the_chain_end_to_end(secured):
    assert secured.get('/audit/verify').status_code == 401
    assert secured.get('/audit/blocks').status_code == 401
    assert secured.post('/audit/checkpoint', headers=ORIGIN).status_code == 401
    principal = login(secured, 'agent')
    ingest = secured.post('/api/v1/simulate-ticket', json={'event_id': str(uuid4()), 'scenario_id': 'payment_ru'},
                          headers=ORIGIN)
    assert ingest.status_code == 201, ingest.text
    ticket = ingest.json()['ticket']
    assert ticket['chain_block'] is not None
    action = {'action_id': str(uuid4()), 'expected_revision': ticket['revision'], 'action': 'reply',
              'draft': 'Текст ответа оператора', 'note': '', 'channel': 'game_chat'}
    assert secured.post(f"/api/v1/tickets/{ticket['id']}/actions", json=action, headers=ORIGIN).status_code == 200
    assert secured.post(f"/api/v1/tickets/{ticket['id']}/actions", json=action, headers=ORIGIN).json()['replayed']
    updated = secured.get(f"/operator/tickets/{ticket['id']}").json()
    assert secured.post(f"/api/v1/tickets/{ticket['id']}/actions", json={
        **action, 'action_id': str(uuid4()), 'expected_revision': updated['revision'], 'action': 'escalate',
        'note': 'Нужен старший'}, headers=ORIGIN).status_code == 200
    report = secured.get('/audit/verify').json()
    assert report['valid'] is True
    body = secured.get('/audit/blocks', params={'limit': 50}).json()
    kinds = [block['event_type'] for block in reversed(body['blocks'])]
    assert kinds == ['genesis', 'operator.login', 'triage.completed', 'escalation.human', 'draft.sent', 'escalation.human']
    assert body['height'] == report['height']
    sent = body['blocks'][1]
    assert sent['actor'] == principal['id']
    assert sent['payload']['draft_hash'] == digest('Текст ответа оператора')
    assert sent['payload']['delivered'] is False
    assert sent['payload']['player_id']
    assert secured.get('/audit/blocks', params={'limit': 500}).status_code == 422
    assert [block['index'] for block in secured.get('/audit/blocks', params={'limit': 2, 'before': 3}).json()['blocks']] == [2, 1]
    assert secured.post('/audit/checkpoint', headers=ORIGIN).status_code == 403
    assert not AuditChain().anchors_path.exists()
    assert secured.post('/auth/logout', headers=ORIGIN).status_code == 200
    assert events()[-1] == 'operator.logout'


def test_admin_actions_are_chained_and_only_admin_anchors(secured):
    admin = login(secured, 'admin')
    created = secured.post('/auth/operators', json={'username': 'newcomer', 'password': PASSWORD, 'role': 'agent'},
                           headers=ORIGIN).json()
    assert secured.patch(f"/auth/operators/{created['id']}", json={'role': 'lead', 'active': True},
                         headers=ORIGIN).status_code == 200
    AuthStore().reset_password('newcomer', PASSWORD)
    anchor = secured.post('/audit/checkpoint', headers=ORIGIN)
    assert anchor.status_code == 201, anchor.text
    assert anchor.json()['height'] == 4
    blocks = {block.event_type: block for block in AuditChain().blocks()}
    assert blocks['operator.created'].actor == admin['id']
    assert {k:v for k,v in blocks['operator.created'].payload().items() if k != 'origin_event_id'} == {'id': created['id'], 'role': 'agent'}
    assert blocks['operator.role_change'].payload()['previous_role'] == 'agent'
    assert blocks['operator.role_change'].payload()['role'] == 'lead'
    assert blocks['operator.password_reset'].actor == 'local_cli'
    assert PASSWORD not in AuditChain().mirror_path.read_text(encoding='utf-8')
    assert secured.get('/audit/verify').json()['anchors']['status'] == 'match'


def test_auth_outbox_survives_chain_failure_and_replays_once(tmp_path, monkeypatch):
    AuditChain().genesis()
    monkeypatch.setenv('COPILOT_CHAIN_PRIVATE_KEY', (b'\x33' * 32).hex())
    created=AuthStore().create_user('pending_user', PASSWORD, 'agent')
    assert created['id']
    with AuthStore().connect() as db:
        assert db.execute('SELECT count(*) FROM identity_chain_outbox WHERE delivered=0').fetchone()[0]==1
    assert AuditChain().head().index==0
    monkeypatch.delenv('COPILOT_CHAIN_PRIVATE_KEY')
    AuthStore().users()  # Any later transaction retries committed outbox rows.
    assert AuditChain().head().index==1
    with AuthStore().connect() as db:
        row=db.execute('SELECT * FROM identity_chain_outbox').fetchone()
        AuditChain().append_once(row['event_id'],row['event_type'],row['actor'],__import__('json').loads(row['payload']))
    assert AuditChain().head().index==1


def test_signed_game_event_is_chained_with_its_hash(knowledge):
    store = OperatorStore(actor='game:test')
    analysis = service.triage(service.TriageRequest.model_validate(REQUEST), use_configured_provider=False)
    key, replayed = store.ingest(str(uuid4()), 'a' * 64, 'Игра', REQUEST, 'in_game', {'player_id': 7, 'server_actor': 'game:test'},
                                 analysis.model_dump(), 1.0, knowledge.hashes, source='game_connector')
    assert replayed is False
    assert store.game_message(key, str(uuid4()), 'b' * 64, 7, 'ещё сообщение') is False
    hashes = [block.payload()['event_hash'] for block in store.chain.blocks() if block.event_type == 'game.event.accepted']
    assert hashes == ['b' * 64, 'a' * 64]
    assert store.chain.verify()['valid'] is True


def test_stateless_triage_is_chained_and_survives_a_journal_outage(knowledge, monkeypatch):
    monkeypatch.setenv('COPILOT_AUTH_MODE', 'local')
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 40000)) as client:
        first = client.post('/triage', json=REQUEST)
        assert first.status_code == 200
        assert events() == ['genesis', 'triage.completed', 'escalation.human']
        assert AuditChain().blocks()[1].actor == 'local_operator'
        monkeypatch.setenv('COPILOT_CHAIN_PRIVATE_KEY', (b'\x33' * 32).hex())
        second = client.post('/triage', json=REQUEST)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(events()) == 3


def test_offline_batch_stays_out_of_the_journal(knowledge):
    service.triage(service.TriageRequest.model_validate(REQUEST), use_configured_provider=False)
    assert not Path(AuditChain().path).exists()


def test_tamper_demo_is_gated_and_breaks_verification(chain, monkeypatch, capsys):
    monkeypatch.setenv('COPILOT_OPERATOR_DB', str(chain.path))
    monkeypatch.setattr(sys, 'argv', ['chain', 'tamper', '--index', '2'])
    assert chain_cli.main() == 2
    assert chain.verify()['valid'] is True
    monkeypatch.setenv('COPILOT_CHAIN_TAMPER_DEMO', '1')
    assert chain_cli.main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, 'argv', ['chain', 'verify'])
    assert chain_cli.main() == 1
    assert json.loads(capsys.readouterr().out)['first_failure'] == {'index': 2, 'check': 'payload_digest'}


def test_cli_anchor_writes_the_git_witness_and_verify_checks_it(chain, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('COPILOT_OPERATOR_DB', str(chain.path))
    document = tmp_path / 'wave-8.md'
    monkeypatch.setattr(sys, 'argv', ['chain', 'anchor', '--wave-doc', str(document)])
    assert chain_cli.main() == 0
    head = chain.head()
    assert head.block_hash in document.read_text(encoding='utf-8')
    capsys.readouterr()
    monkeypatch.setattr(sys, 'argv', ['chain', 'verify', '--expect', f'{head.index}:{head.block_hash}'])
    assert chain_cli.main() == 0
    monkeypatch.setattr(sys, 'argv', ['chain', 'verify', '--expect', f'{head.index}:{"0" * 64}'])
    assert chain_cli.main() == 1


def test_cli_rotation_writes_a_new_key_and_prints_no_secret(chain, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('COPILOT_OPERATOR_DB', str(chain.path))
    target = tmp_path / 'next.key'
    monkeypatch.setattr(sys, 'argv', ['chain', 'rotate', '--out', str(target), '--reason', 'плановая'])
    assert chain_cli.main() == 0
    seed = target.read_text(encoding='ascii').strip()
    assert seed not in capsys.readouterr().out
    monkeypatch.setenv('COPILOT_CHAIN_KEY_FILE', str(target))
    fill(AuditChain(chain.path), 1)
    assert AuditChain(chain.path).verify()['valid'] is True
    assert chain_cli.main() == 2


def test_block_is_immutable(chain):
    block = chain.head()
    assert isinstance(block, Block)
    assert replace(block, actor='x').computed_hash() != block.block_hash
    with pytest.raises(AttributeError):
        block.actor = 'x'


def test_chain_module_is_visible_in_the_operator_panel(secured):
    login(secured, 'agent')
    page = secured.get('/')
    assert '/assets/audit-chain.js' in page.text
    assert 'data-view="chain"' in page.text
    assert secured.get('/assets/audit-chain.js').status_code == 200
