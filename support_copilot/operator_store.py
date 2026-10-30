"""Local operator workspace. Inference and operator decisions remain separate."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


class MissingTicket(Exception):
    pass


class RevisionConflict(Exception):
    pass


class AnalysisRequired(Exception):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class OperatorStore:
    def __init__(self, path: Path | str | None = None):
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

    @staticmethod
    def _event(connection, ticket_id, kind, revision, detail):
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
            rows = connection.execute('SELECT * FROM tickets ORDER BY created_at DESC, id DESC').fetchall()
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
                'needs_human': (analysis['needs_human'] or row['status'] == 'escalated') if analysis else None,
            })
        return summaries

    def get(self, ticket_id: str) -> dict:
        with self.connect() as connection:
            row = self._ticket(connection, ticket_id)
            events = connection.execute('SELECT kind, at, revision, detail FROM events '
                                        'WHERE ticket_id = ? ORDER BY id', (ticket_id,)).fetchall()
        result = dict(row)
        for field in ('payload', 'analysis', 'knowledge_hashes'):
            result[field] = json.loads(result[field]) if result[field] else None
        result['events'] = [{**dict(event), 'detail': json.loads(event['detail'])} for event in events]
        result['needs_human'] = bool(result['analysis'] and (
            result['analysis']['needs_human'] or result['status'] == 'escalated'))
        return result

    def analyze(self, ticket_id: str, expected_revision: int, analysis: dict,
                duration_ms: float, hashes: dict) -> None:
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
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
