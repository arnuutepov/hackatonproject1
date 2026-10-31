"""Local operator workspace. Inference and operator decisions remain separate."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4


class MissingTicket(Exception):
    pass


class RevisionConflict(Exception):
    pass


class AnalysisRequired(Exception):
    pass


class HumanReviewRequired(Exception):
    pass


class ConnectorRequired(Exception):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class OperatorStore:
    def __init__(self, path: Path | str | None = None, *, actor='system'):
        self.actor = actor
        self.path = Path(path or os.environ.get('COPILOT_OPERATOR_DB') or (
            Path(__file__).resolve().parent.parent / 'outputs/operator/workspace.sqlite3'))

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL,
                payload TEXT NOT NULL, analysis TEXT, edited_draft TEXT,
                status TEXT NOT NULL DEFAULT 'new', revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                analysis_ms REAL, knowledge_hashes TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL REFERENCES tickets(id),
                kind TEXT NOT NULL, at TEXT NOT NULL, revision INTEGER NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assignments (
                ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
                operator TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL REFERENCES tickets(id),
                role TEXT NOT NULL CHECK(role IN ('player', 'admin')),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connector_inbox (
                event_id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(id),
                fingerprint TEXT NOT NULL, channel TEXT NOT NULL, context TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connector_outbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL UNIQUE,
                ticket_id TEXT NOT NULL REFERENCES tickets(id),
                fingerprint TEXT NOT NULL, event TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS human_reviews (
                ticket_id TEXT PRIMARY KEY REFERENCES tickets(id),
                operator TEXT NOT NULL, note TEXT NOT NULL, at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS game_messages (
                event_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id),
                fingerprint TEXT NOT NULL
            );
        ''')
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _ticket(connection, ticket_id):
        row = connection.execute('SELECT * FROM tickets WHERE id = ?', (ticket_id,)).fetchone()
        if row is None:
            raise MissingTicket()
        return row

    @staticmethod
    def _revision(row, expected_revision):
        if row['revision'] != expected_revision:
            raise RevisionConflict()

    def _event(self, connection, ticket_id, kind, revision, detail):
        detail = {**detail, 'actor': self.actor}
        connection.execute('INSERT INTO events (ticket_id, kind, at, revision, detail) VALUES (?, ?, ?, ?, ?)',
                           (ticket_id, kind, timestamp(), revision, json.dumps(detail, ensure_ascii=False)))

    def _create(self, connection, title, payload, source):
        ticket_id, now = str(uuid4()), timestamp()
        connection.execute('INSERT INTO tickets (id, title, source, payload, created_at, updated_at) '
                           'VALUES (?, ?, ?, ?, ?, ?)',
                           (ticket_id, title, source, json.dumps(payload, ensure_ascii=False), now, now))
        self._event(connection, ticket_id, 'created', 1, {'source': source})
        return ticket_id

    def create(self, title: str, payload: dict, *, source='manual') -> str:
        with self.connect() as connection:
            return self._create(connection, title, payload, source)

    def seed(self, examples: list[dict]) -> bool:
        # One transaction makes concurrent "add examples" clicks idempotent.
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute("SELECT 1 FROM tickets WHERE source = 'demo' LIMIT 1").fetchone():
                return False
            for example in examples:
                self._create(connection, example['title'], example['request'], 'demo')
            return True

    def list(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute('SELECT tickets.*, assignments.operator AS assigned_to, '
                                      'human_reviews.operator AS reviewed_by, '
                                      'connector_inbox.context AS connector_context FROM tickets '
                                      'LEFT JOIN assignments ON tickets.id = assignments.ticket_id '
                                      'LEFT JOIN human_reviews ON tickets.id = human_reviews.ticket_id '
                                      'LEFT JOIN connector_inbox ON tickets.id = connector_inbox.ticket_id '
                                      'ORDER BY created_at DESC, id DESC').fetchall()
        summaries = []
        for row in rows:
            analysis = json.loads(row['analysis']) if row['analysis'] else None
            payload = json.loads(row['payload'])
            summaries.append({
                'id': row['id'], 'title': row['title'], 'source': row['source'],
                'status': row['status'], 'revision': row['revision'],
                'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'kind': payload['kind'], 'preview': payload['messages'][0]['text'][:160],
                'lang': analysis['lang'] if analysis else None,
                'priority': analysis['priority'] if analysis else None,
                'category': analysis['category'] if analysis else None,
                'needs_human': ((analysis['needs_human'] or row['status'] == 'escalated') and
                                not (row['status'] == 'resolved' and row['reviewed_by'])) if analysis else None,
                'assigned_to': row['assigned_to'],
                'mentioned_nickname': analysis['extracted']['nickname'] if analysis else None,
                'player_id': json.loads(row['connector_context'])['player_id'] if row['connector_context'] else None,
            })
        return summaries

    def get(self, ticket_id: str) -> dict:
        with self.connect() as connection:
            row = self._ticket(connection, ticket_id)
            events = connection.execute('SELECT kind, at, revision, detail FROM events '
                                        'WHERE ticket_id = ? ORDER BY id', (ticket_id,)).fetchall()
            assignment = connection.execute('SELECT operator FROM assignments WHERE ticket_id = ?', (ticket_id,)).fetchone()
            review = connection.execute('SELECT operator, note, at FROM human_reviews WHERE ticket_id = ?', (ticket_id,)).fetchone()
            chat = connection.execute('SELECT id, role, text, created_at FROM chat_messages WHERE ticket_id = ? ORDER BY id', (ticket_id,)).fetchall()
            inbox = connection.execute('SELECT event_id, channel, context FROM connector_inbox WHERE ticket_id = ?',
                                       (ticket_id,)).fetchone()
            outbox = connection.execute('SELECT event FROM connector_outbox WHERE ticket_id = ? ORDER BY sequence',
                                        (ticket_id,)).fetchall()
        result = dict(row)
        result['human_review'] = dict(review) if review else None
        result['connector'] = ({'mode': 'game' if row['source'] == 'game_connector' else 'mock', 'event_id': inbox['event_id'], 'channel': inbox['channel'],
                                'context': json.loads(inbox['context'])} if inbox else None)
        result['outbox'] = [json.loads(item['event']) for item in outbox]
        result['chat_messages'] = [dict(message) for message in chat]
        result['assigned_to'] = assignment['operator'] if assignment else None
        for field in ('payload', 'analysis', 'knowledge_hashes'):
            result[field] = json.loads(result[field]) if result[field] else None
        result['events'] = [{**dict(event), 'detail': json.loads(event['detail'])} for event in events]
        result['needs_human'] = bool(result['analysis'] and (
            result['analysis']['needs_human'] or result['status'] == 'escalated') and
            not (result['status'] == 'resolved' and review))
        return result

    def add_chat_message(self, ticket_id: str, expected_revision: int, role: str, text: str) -> None:
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self._ticket(connection, ticket_id)
            self._revision(row, expected_revision)
            now = timestamp()
            connection.execute('INSERT INTO chat_messages (ticket_id, role, text, created_at) VALUES (?, ?, ?, ?)',
                               (ticket_id, role, text, now))
            revision = expected_revision + 1
            connection.execute('UPDATE tickets SET revision = ?, updated_at = ? WHERE id = ?',
                               (revision, now, ticket_id))
            self._event(connection, ticket_id, 'chat_message', revision, {'role': role})

    def workflow(self, ticket_id: str, expected_revision: int, action: str,
                 *, allow_human_resolution=False, note='') -> None:
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self._ticket(connection, ticket_id)
            self._revision(row, expected_revision)
            analysis = json.loads(row['analysis']) if row['analysis'] else None
            status = row['status']
            if action == 'resolve':
                if not analysis:
                    raise AnalysisRequired()
                if analysis['needs_human'] or status == 'escalated':
                    if not allow_human_resolution or not note.strip():
                        raise HumanReviewRequired()
                    connection.execute('INSERT OR REPLACE INTO human_reviews VALUES (?, ?, ?, ?)',
                                       (ticket_id, self.actor, note, timestamp()))
                status = 'resolved'
            elif action == 'reopen':
                if status != 'resolved':
                    raise RevisionConflict()
                status = 'analyzed'
                connection.execute('DELETE FROM human_reviews WHERE ticket_id = ?', (ticket_id,))
            elif action == 'take':
                connection.execute('INSERT OR REPLACE INTO assignments VALUES (?, ?)',
                                   (ticket_id, self.actor))
            elif action == 'release':
                connection.execute('DELETE FROM assignments WHERE ticket_id = ?', (ticket_id,))
            else:
                raise ValueError('Unknown workflow action')
            revision = expected_revision + 1
            connection.execute('UPDATE tickets SET status = ?, revision = ?, updated_at = ? WHERE id = ?',
                               (status, revision, timestamp(), ticket_id))
            self._event(connection, ticket_id, action, revision, {'local_only': True, 'note': note,
                         'manual_review_completed': action == 'resolve' and allow_human_resolution and bool(note.strip())})

    def analyze(self, ticket_id: str, expected_revision: int, analysis: dict,
                duration_ms: float, hashes: dict) -> None:
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._analyze(connection, ticket_id, expected_revision, analysis, duration_ms, hashes)

    def _analyze(self, connection, ticket_id, expected_revision, analysis, duration_ms, hashes):
        connection.execute('DELETE FROM human_reviews WHERE ticket_id = ?', (ticket_id,))
        row = self._ticket(connection, ticket_id)
        self._revision(row, expected_revision)
        revision = expected_revision + 1
        # Escalation is sticky; a new analysis never clears a human decision.
        status = 'escalated' if row['status'] == 'escalated' else 'analyzed'
        connection.execute('UPDATE tickets SET analysis = ?, edited_draft = ?, status = ?, '
                           'revision = ?, updated_at = ?, analysis_ms = ?, knowledge_hashes = ? WHERE id = ?',
                           (json.dumps(analysis, ensure_ascii=False), analysis['draft_reply']['text'],
                            status, revision, timestamp(), duration_ms, json.dumps(hashes), ticket_id))
        self._event(connection, ticket_id, 'analyzed', revision, {
            'draft': analysis['draft_reply']['text'], 'needs_human': analysis['needs_human'],
            'provenance': analysis['draft_reply']['provenance'], 'knowledge_hashes': hashes,
            'analysis_ms': duration_ms,
        })

    def inbox_replay(self, event_id: str, fingerprint: str) -> str | None:
        with self.connect() as connection:
            return self._inbox_replay(connection, event_id, fingerprint)

    @staticmethod
    def _inbox_replay(connection, event_id, fingerprint):
        existing = connection.execute('SELECT ticket_id, fingerprint FROM connector_inbox WHERE event_id = ?',
                                      (event_id,)).fetchone()
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise RevisionConflict()
            return existing['ticket_id']
        return None

    def ingest(self, event_id: str, fingerprint: str, title: str, payload: dict, channel: str,
               context: dict, analysis: dict, duration_ms: float, hashes: dict, *, source='mock_connector') -> tuple[str, bool]:
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = self._inbox_replay(connection, event_id, fingerprint)
            if existing:
                return existing, True
            ticket_id = self._create(connection, title, payload, source)
            connection.execute('INSERT INTO connector_inbox VALUES (?, ?, ?, ?, ?)',
                               (event_id, ticket_id, fingerprint, channel, json.dumps(context, ensure_ascii=False)))
            self._analyze(connection, ticket_id, 1, analysis, duration_ms, hashes)
            return ticket_id, False

    def mock_action(self, ticket_id: str, payload: dict, fingerprint: str) -> tuple[dict, bool]:
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute('SELECT * FROM connector_outbox WHERE action_id = ?',
                                          (payload['action_id'],)).fetchone()
            if existing:
                if existing['ticket_id'] != ticket_id or existing['fingerprint'] != fingerprint:
                    raise RevisionConflict()
                return json.loads(existing['event']), True
            row = self._ticket(connection, ticket_id)
            self._revision(row, payload['expected_revision'])
            if not row['analysis']:
                raise AnalysisRequired()
            inbox = connection.execute('SELECT context FROM connector_inbox WHERE ticket_id = ?',
                                       (ticket_id,)).fetchone()
            if not inbox:
                raise ConnectorRequired()
            context = json.loads(inbox['context'])
            action = payload['action']
            if action not in {'reply', 'escalate'}:
                raise ValueError('Unknown mock action')
            revision = row['revision'] + 1
            status = 'escalated' if action == 'escalate' or row['status'] == 'escalated' else 'reply_simulated'
            connection.execute('UPDATE tickets SET edited_draft = ?, status = ?, revision = ?, updated_at = ? WHERE id = ?',
                               (payload['draft'], status, revision, timestamp(), ticket_id))
            event = {'action_id': payload['action_id'], 'ticket_id': ticket_id, 'revision': revision,
                     'at': timestamp(), 'mode': 'mock', 'status': 'simulated', 'delivered': False,
                     'player_id': context['player_id'], 'channel': payload['channel'], 'action': action,
                     'actor': self.actor, 'draft': payload['draft'], 'note': payload['note'],
                     'needs_human': json.loads(row['analysis'])['needs_human'] or status == 'escalated'}
            connection.execute('INSERT INTO connector_outbox (action_id, ticket_id, fingerprint, event) VALUES (?, ?, ?, ?)',
                               (payload['action_id'], ticket_id, fingerprint, json.dumps(event, ensure_ascii=False)))
            if action == 'reply':
                connection.execute('INSERT INTO chat_messages (ticket_id, role, text, created_at) VALUES (?, ?, ?, ?)',
                                   (ticket_id, 'admin', payload['draft'], event['at']))
            self._event(connection, ticket_id, 'mock_' + action, revision, event)
            return event, False

    def outbox(self) -> list[dict]:
        with self.connect() as connection:
            return [json.loads(row['event']) for row in connection.execute(
                'SELECT event FROM connector_outbox ORDER BY sequence DESC LIMIT 100')]

    def game_message(self, ticket_id, event_id, fingerprint, player_id, text):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute('SELECT * FROM game_messages WHERE event_id = ?', (event_id,)).fetchone()
            if existing:
                if existing['ticket_id'] != ticket_id or existing['fingerprint'] != fingerprint:
                    raise RevisionConflict()
                return True
            row = self._ticket(connection, ticket_id)
            inbox = connection.execute('SELECT context FROM connector_inbox WHERE ticket_id = ?', (ticket_id,)).fetchone()
            context = json.loads(inbox['context']) if inbox else {}
            if (row['source'] != 'game_connector' or context.get('player_id') != player_id
                    or context.get('server_actor') != self.actor):
                raise ConnectorRequired()
            payload = json.loads(row['payload'])
            if len(payload['messages']) >= 100:
                raise RevisionConflict()
            payload['messages'].append({'role': 'player', 'text': text, 'attachments': 0})
            revision = row['revision'] + 1
            now = timestamp()
            status = 'escalated' if row['status'] == 'escalated' else 'new'
            connection.execute('UPDATE tickets SET payload = ?, analysis = NULL, edited_draft = NULL, '
                               'status = ?, revision = ?, updated_at = ?, analysis_ms = NULL, knowledge_hashes = NULL WHERE id = ?',
                               (json.dumps(payload, ensure_ascii=False), status, revision, now, ticket_id))
            connection.execute('DELETE FROM human_reviews WHERE ticket_id = ?', (ticket_id,))
            connection.execute('INSERT INTO chat_messages (ticket_id, role, text, created_at) VALUES (?, ?, ?, ?)',
                               (ticket_id, 'player', text, now))
            connection.execute('INSERT INTO game_messages VALUES (?, ?, ?)', (event_id, ticket_id, fingerprint))
            self._event(connection, ticket_id, 'game_message', revision, {'role': 'player', 'event_id': event_id})
            return False

    def export_mock_audit(self) -> dict:
        """Rebuild the JSONL projection atomically; SQLite is the authoritative audit.

        Serializing exporters on the DB prevents an older snapshot overwriting a newer one.
        A crash/failed file write never undoes or repeats the durable operator action.
        """
        target = Path(os.environ.get('COPILOT_CONNECTOR_AUDIT') or self.path.with_name('mock-outbox.audit.jsonl'))
        temporary = None
        try:
            with self.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                                 prefix='.mock-audit-', delete=False) as stream:
                    temporary = Path(stream.name)
                    for row in connection.execute('SELECT event FROM connector_outbox ORDER BY sequence'):
                        stream.write(row['event'] + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            return {'status': 'written', 'file': target.name, 'source': 'sqlite_outbox'}
        except OSError:
            return {'status': 'pending', 'file': target.name, 'source': 'sqlite_outbox'}
        finally:
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)

    def decide(self, ticket_id: str, expected_revision: int, draft: str, action: str, note: str) -> None:
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = self._ticket(connection, ticket_id)
            self._revision(row, expected_revision)
            if not row['analysis']:
                raise AnalysisRequired()
            revision = expected_revision + 1
            status = 'escalated' if action == 'escalate' or row['status'] == 'escalated' else 'saved'
            connection.execute('UPDATE tickets SET edited_draft = ?, status = ?, revision = ?, '
                               'updated_at = ? WHERE id = ?', (draft, status, revision, timestamp(), ticket_id))
            self._event(connection, ticket_id, 'escalated' if action == 'escalate' else 'saved', revision, {
                'before': row['edited_draft'], 'draft': draft, 'note': note,
                'needs_human': json.loads(row['analysis'])['needs_human'] or status == 'escalated',
            })
