"""Signed hash chain over operator actions: tamper-evident on one node, not a consensus blockchain.

Blocks hold digests and decision metadata; raw ticket text stays in the workspace tables.
The chain is a journal. It records decisions and never changes them.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterable

from support_copilot.chain_signer import describe, load_signer, verifier_for

SCHEMA_VERSION = 1
ZERO_HASH = '0' * 64
GENESIS = 'genesis'
ROTATED = 'key.rotated'
RESTORED = 'chain.restored'
EVENT_TYPES = frozenset({
    'triage.completed', 'draft.sent', 'escalation.human', 'operator.login', 'operator.logout',
    'operator.created', 'operator.role_change', 'operator.password_reset', 'game.event.accepted',
    ROTATED, RESTORED,
})
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / 'outputs/operator/workspace.sqlite3'
SCHEMA = '''
    CREATE TABLE IF NOT EXISTS chain_external_events (
        event_id TEXT PRIMARY KEY, block_index INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS chain_blocks (
        idx INTEGER PRIMARY KEY, created_at TEXT NOT NULL, prev_hash TEXT NOT NULL,
        event_type TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL,
        payload_digest TEXT NOT NULL, block_hash TEXT NOT NULL, signature TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS chain_blocks_event ON chain_blocks(event_type);
'''
MIRROR_TAIL: dict[Path, tuple[int, int]] = {}  # mirror file -> (size, last index); only read under the write lock
COLUMNS = 'idx, created_at, prev_hash, event_type, actor, payload_json, payload_digest, block_hash, signature'


class ChainKeyMismatch(ValueError):
    """The configured signer is not the key the chain currently expects."""


class ChainBroken(Exception):
    pass


class ChainConnection(sqlite3.Connection):
    """Marks a transaction that appended blocks, so the mirror is written only after commit."""
    chain_dirty = False


def canonical(value) -> str:
    # ensure_ascii=False keeps RU/KK/UZ digests stable and readable.
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(text: str) -> str:
    # surrogatepass: valid text hashes as plain UTF-8; a lone surrogate cannot fail the journal.
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


def content_hash(value) -> str:
    return digest(canonical(value))


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


@dataclass(frozen=True)
class Block:
    index: int          # 0 = genesis
    created_at: str     # ISO 8601 UTC; order is defined by index, not by time
    prev_hash: str      # ZERO_HASH for genesis
    event_type: str
    actor: str
    payload_json: str   # canonical event JSON
    payload_digest: str
    block_hash: str
    signature: str

    def computed_hash(self) -> str:
        return header_hash(self.index, self.created_at, self.prev_hash, self.event_type, self.actor,
                           self.payload_digest)

    def payload(self) -> dict:
        return json.loads(self.payload_json)

    def public(self) -> dict:
        try:
            return {**asdict(self), 'payload': self.payload()}
        except ValueError:
            # A damaged row must stay visible in the journal, not hide behind an error.
            return {**asdict(self), 'payload': None}


def header_hash(index, created_at, prev_hash, event_type, actor, payload_digest) -> str:
    return digest(canonical({'index': index, 'created_at': created_at, 'prev_hash': prev_hash,
                             'event_type': event_type, 'actor': actor, 'payload_digest': payload_digest}))


def triage_payload(request: dict, analysis: dict, ticket_id: str | None = None) -> dict:
    """Decision metadata only: text is provable later through content_hash, never stored here."""
    draft = analysis['draft_reply']
    return {'ticket_id': ticket_id, 'kind': request['kind'], 'lang': analysis['lang'],
            'category': analysis['category'], 'priority': analysis['priority'],
            'needs_human': analysis['needs_human'],
            'rule_ids': sorted({rule['source_id'] for rule in analysis['retrieved_rules']} | set(draft['cited'])),
            'content_hash': content_hash(request), 'draft_hash': digest(draft['text']),
            'provenance': draft['provenance'],
            'attachments_claimed': analysis['extracted']['attachments_claimed']}


def escalation_payload(analysis: dict, ticket_id: str | None = None) -> dict:
    return {'ticket_id': ticket_id, 'source': 'safety', 'safety_flags': analysis['safety_flags'],
            'reasons': analysis['reasons']}


def failure(index, check) -> dict:
    return {'index': index, 'check': check}


class AuditChain:
    def __init__(self, path: Path | str | None = None, *, signer=None):
        self.path = Path(path or os.environ.get('COPILOT_OPERATOR_DB') or DEFAULT_DB)
        self._signer = signer

    def _artifact(self, variable: str, name: str) -> Path:
        configured = os.environ.get(variable)
        if configured:
            return Path(configured)
        return ROOT / 'outputs' / name if self.path == DEFAULT_DB else self.path.with_name(name)

    @property
    def mirror_path(self) -> Path:
        return self._artifact('COPILOT_CHAIN_MIRROR', 'chain-mirror.jsonl')

    @property
    def anchors_path(self) -> Path:
        return self._artifact('COPILOT_CHAIN_ANCHORS', 'chain-anchors.jsonl')

    @property
    def key_file(self) -> Path:
        return self.path.with_name('chain-signing.key')

    @property
    def signer(self):
        if self._signer is None:
            self._signer = load_signer(self.key_file, create=True)
        return self._signer

    @property
    def chain_id(self) -> str:
        return os.environ.get('COPILOT_CHAIN_ID', 'oguzki-support-copilot')

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10, factory=ChainConnection)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('PRAGMA busy_timeout = 10000')
        connection.executescript(SCHEMA)
        try:
            yield connection
            connection.commit()
            if connection.chain_dirty:
                self.publish()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _block(row) -> Block:
        return Block(*tuple(row))

    def _head(self, connection) -> Block | None:
        row = connection.execute('SELECT idx, created_at, prev_hash, event_type, actor, payload_json, payload_digest, block_hash, signature FROM chain_blocks ORDER BY idx DESC LIMIT 1').fetchone()
        return self._block(row) if row else None

    def _insert(self, connection, index, prev_hash, created_at, event_type, actor, payload) -> Block:
        payload_json = canonical(payload)
        payload_digest = digest(payload_json)
        block_hash = header_hash(index, created_at, prev_hash, event_type, actor, payload_digest)
        block = Block(index, created_at, prev_hash, event_type, actor, payload_json, payload_digest,
                      block_hash, self.signer.sign(block_hash))
        connection.execute('INSERT INTO chain_blocks (idx, created_at, prev_hash, event_type, actor, payload_json, payload_digest, block_hash, signature) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (block.index, block.created_at, block.prev_hash, block.event_type, block.actor,
                            block.payload_json, block.payload_digest, block.block_hash, block.signature))
        connection.chain_dirty = True
        return block

    def _genesis(self, connection) -> Block:
        now = timestamp()
        return self._insert(connection, 0, ZERO_HASH, now, GENESIS, 'system', {
            'chain_id': self.chain_id, 'schema_version': SCHEMA_VERSION, 'created_at': now,
            'key': describe(self.signer)})

    def _active_key(self, connection) -> dict:
        row = connection.execute('SELECT payload_json FROM chain_blocks WHERE event_type IN (?, ?) '
                                 'ORDER BY idx DESC LIMIT 1', (GENESIS, ROTATED)).fetchone()
        return json.loads(row['payload_json'])['key']

    def _append(self, connection, event_type, actor, payload) -> Block:
        if event_type not in EVENT_TYPES:
            raise ValueError('Unknown chain event type')
        if not connection.in_transaction:
            raise RuntimeError('Chain append requires an open write transaction')
        head = self._head(connection) or self._genesis(connection)
        if self._active_key(connection)['fingerprint'] != self.signer.fingerprint:
            raise ChainKeyMismatch('Configured signing key does not match the chain')
        # A clock stepping back must not read as tampering: time is a sanity check here.
        return self._insert(connection, head.index + 1, head.block_hash, max(timestamp(), head.created_at),
                            event_type, actor, payload)

    def genesis(self) -> Block:
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT idx, created_at, prev_hash, event_type, actor, payload_json, payload_digest, block_hash, signature FROM chain_blocks WHERE idx = 0').fetchone()
            return self._block(row) if row else self._genesis(connection)

    def append(self, event_type: str, actor: str, payload: dict, *, connection=None) -> Block:
        """Pass the caller's open transaction to commit the action and its block atomically.

        BEGIN IMMEDIATE serializes writers across threads and processes. A Python lock
        would add nothing and could deadlock against a transaction that already holds SQLite.
        """
        if connection is not None:
            return self._append(connection, event_type, actor, payload)
        with self.connect() as own:
            own.execute('BEGIN IMMEDIATE')
            return self._append(own, event_type, actor, payload)

    def append_once(self, event_id, event_type, actor, payload):
        """Idempotent bridge for committed events from a separate database."""
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            prior = connection.execute('SELECT block_index FROM chain_external_events WHERE event_id = ?', (event_id,)).fetchone()
            if prior:
                return prior['block_index']
            block = self._append(connection, event_type, actor, {**payload, 'origin_event_id': event_id})
            connection.execute('INSERT INTO chain_external_events VALUES (?, ?)', (event_id, block.index))
            return block.index

    def head(self) -> Block | None:
        with self.connect() as connection:
            return self._head(connection)

    def blocks(self, *, limit: int = 50, before: int | None = None) -> list[Block]:
        with self.connect() as connection:
            rows = connection.execute('SELECT idx, created_at, prev_hash, event_type, actor, payload_json, payload_digest, block_hash, signature FROM chain_blocks WHERE idx < ? ORDER BY idx DESC LIMIT ?',
                                      (2**62 if before is None else before, limit)).fetchall()
        return [self._block(row) for row in rows]

    def publish(self) -> dict:
        """Catch the append-only mirror up to the committed head.

        Blocks reach the file only after commit, so a rollback cannot leave a phantom line.
        A failed write never undoes the action: the next publish writes what is missing.
        """
        target = self.mirror_path
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection:
                connection.row_factory = sqlite3.Row
                # The write lock keeps concurrent publishers from interleaving file appends.
                connection.execute('BEGIN IMMEDIATE')
                target.parent.mkdir(parents=True, exist_ok=True)
                # No fsync: SQLite already holds the durable copy, and a line lost to a
                # power cut is rewritten by the next publish.
                # Size unchanged since our last write means the tail is known: reading a file
                # that was just modified is the slowest step on hosts with real-time file scanning.
                size = target.stat().st_size if target.exists() else 0
                known = MIRROR_TAIL.get(target)
                last = known[1] if known and known[0] == size else self._mirror_tail(target)
                rows = connection.execute('SELECT idx, created_at, prev_hash, event_type, actor, payload_json, payload_digest, block_hash, signature FROM chain_blocks WHERE idx > ? ORDER BY idx',
                                          (last,)).fetchall()
                if rows:
                    with open(target, 'ab') as stream:
                        for row in rows:
                            stream.write((canonical(asdict(self._block(row))) + '\n').encode('utf-8', 'surrogatepass'))
                        MIRROR_TAIL[target] = (stream.tell(), rows[-1]['idx'])
                connection.rollback()
            return {'status': 'written', 'file': target.name}
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            return {'status': 'pending', 'file': target.name}

    @staticmethod
    def _mirror_tail(target: Path) -> int:
        """Last mirrored index. A line cut short by a crash was never completed and is dropped."""
        if not target.exists():
            return -1
        with open(target, 'rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            window = 4096
            while True:
                start = max(0, size - window)
                stream.seek(start)
                tail = stream.read()
                end = tail.rfind(b'\n') + 1
                lines = tail[:end].splitlines()
                # The first line of a partial window may be cut; only a whole last line counts.
                if len(lines) >= 2 or not start:
                    break
                window *= 2
        if end != len(tail):
            with open(target, 'r+b') as stream:
                stream.truncate(start + end)
        return json.loads(lines[-1])['index'] if lines else -1

    def _rows(self, connection) -> Iterable[Block]:
        for row in connection.execute('SELECT idx, created_at, prev_hash, event_type, actor, payload_json, payload_digest, block_hash, signature FROM chain_blocks ORDER BY idx'):
            yield self._block(row)

    def _configured_key(self):
        try:
            return self._signer or load_signer(self.key_file, create=False)
        except ValueError:
            return None

    def walk(self, blocks: Iterable[Block]) -> tuple[dict, list[dict]]:
        """Check one sequence of blocks. Returns the report and the key history it proved."""
        expected_key = os.environ.get('COPILOT_CHAIN_PUBLIC_KEY')
        configured = self._configured_key()
        keys, previous, report = [], None, {'valid': True, 'head_hash': ZERO_HASH, 'height': -1,
                                            'blocks_checked': 0, 'first_failure': None}

        def broken(index, check):
            report.update(valid=False, first_failure=failure(index, check))
            return report, keys

        verifier = None
        for position, block in enumerate(blocks):
            try:
                if block.index != position:
                    return broken(position, 'index')
                if digest(block.payload_json) != block.payload_digest:
                    return broken(position, 'payload_digest')
                if block.computed_hash() != block.block_hash:
                    return broken(position, 'block_hash')
                if block.prev_hash != (previous.block_hash if previous else ZERO_HASH):
                    return broken(position, 'prev_hash')
                payload = block.payload()
                if position == 0:
                    if block.event_type != GENESIS or payload.get('chain_id') != self.chain_id \
                            or payload.get('schema_version') != SCHEMA_VERSION:
                        return broken(0, 'genesis')
                    if expected_key and payload['key'].get('public_key') != expected_key:
                        return broken(0, 'genesis_key')
                    keys.append({'from_index': 0, **payload['key']})
                    verifier = verifier_for(payload['key'])
                if verifier is None:
                    return broken(position, 'signature_key_unavailable')
                if not verifier.verify(block.block_hash, block.signature):
                    return broken(position, 'signature')
                if previous and datetime.fromisoformat(block.created_at) < datetime.fromisoformat(previous.created_at):
                    return broken(position, 'timestamp')
                if block.event_type == ROTATED:
                    # The rotation block itself is signed by the outgoing key.
                    keys.append({'from_index': position + 1, **payload['key']})
                    verifier = verifier_for(payload['key'])
            except (KeyError, TypeError, ValueError, AttributeError):
                return broken(position, 'malformed_block')
            previous = block
            report.update(head_hash=block.block_hash, height=position, blocks_checked=position + 1)
        # Re-signing the whole chain with a fresh key leaves a self-consistent chain.
        # The key this server is configured with is what exposes it.
        if previous and configured and keys[-1]['fingerprint'] != configured.fingerprint:
            return broken(previous.index, 'configured_key')
        return report, keys

    def check_mirror(self, height: int) -> dict:
        target = self.mirror_path
        if not target.exists():
            return {'status': 'absent', 'height': -1, 'failure': None}
        last, status = -1, 'match'
        with self.connect() as connection, open(target, encoding='utf-8', newline='\n') as stream:
            rows = connection.execute('SELECT idx, created_at, prev_hash, event_type, actor, payload_json, payload_digest, block_hash, signature FROM chain_blocks ORDER BY idx')
            for line in stream:
                try:
                    mirrored = json.loads(line)
                    last = mirrored['index']
                    if last > height:
                        return {'status': 'ahead', 'height': last, 'failure': failure(height + 1, 'tail_truncated')}
                    row = rows.fetchone()
                except (ValueError, KeyError, TypeError):
                    return {'status': 'corrupt', 'height': last, 'failure': failure(last + 1, 'mirror_corrupt')}
                if row is None or asdict(self._block(row)) != mirrored:
                    return {'status': 'mismatch', 'height': last, 'failure': failure(last, 'mirror_mismatch')}
        if last < height:
            status = 'pending'
        return {'status': status, 'height': last, 'failure': None}

    def _restored_heights(self, connection) -> list[int]:
        rows = connection.execute('SELECT payload_json FROM chain_blocks WHERE event_type = ?', (RESTORED,))
        return [json.loads(row['payload_json'])['restored_height'] for row in rows]

    def check_anchors(self, height: int, keys: list[dict]) -> dict:
        target = self.anchors_path
        if not target.exists():
            return {'status': 'absent', 'count': 0, 'latest_height': None, 'failure': None}
        count, latest = 0, None
        known = {key['fingerprint']: key for key in keys}
        with self.connect() as connection, open(target, encoding='utf-8') as stream:
            restored = self._restored_heights(connection)
            for line in stream:
                try:
                    anchor = json.loads(line)
                    body = {key: value for key, value in anchor.items() if key != 'signature'}
                    verifier = verifier_for(known[anchor['key_fingerprint']])
                    if verifier is None or not verifier.verify(content_hash(body), anchor['signature']):
                        raise ValueError('Invalid anchor signature')
                    index, expected = anchor['height'], anchor['head_hash']
                except (ValueError, KeyError, TypeError):
                    return {'status': 'invalid', 'count': count, 'latest_height': latest,
                            'failure': failure(count, 'anchor_signature')}
                count += 1
                latest = index
                # An operator-declared restore explains a rewound tail; a silent cut does not.
                if any(point < index for point in restored):
                    continue
                if index > height:
                    return {'status': 'ahead', 'count': count, 'latest_height': latest,
                            'failure': failure(height + 1, 'tail_truncated')}
                row = connection.execute('SELECT block_hash FROM chain_blocks WHERE idx = ?', (index,)).fetchone()
                if row is None or row['block_hash'] != expected:
                    return {'status': 'mismatch', 'count': count, 'latest_height': latest,
                            'failure': failure(index, 'anchor_mismatch')}
        return {'status': 'match' if count else 'absent', 'count': count, 'latest_height': latest, 'failure': None}

    def verify(self) -> dict:
        """Full check. Tail truncation is only visible against the mirror or an anchor."""
        with self.connect() as connection:
            report, keys = self.walk(self._rows(connection))
            head = self._head(connection)
        if head:
            # A broken walk stops early; height and head_hash still describe the stored head.
            report.update(height=head.index, head_hash=head.block_hash)
        report.update(mirror={'status': 'not_checked', 'height': None, 'failure': None},
                      anchors={'status': 'not_checked', 'count': 0, 'latest_height': None, 'failure': None})
        if report['valid']:
            report['mirror'] = self.check_mirror(report['height'])
            report['anchors'] = self.check_anchors(report['height'], keys)
            found = report['mirror']['failure'] or report['anchors']['failure']
            if found:
                report.update(valid=False, first_failure=found)
        return report

    def verify_export(self, path: Path) -> dict:
        """Check an exported chain without the database: public keys travel inside the file."""
        with open(path, encoding='utf-8') as stream:
            return self.walk(Block(**json.loads(line)) for line in stream)[0]

    def export(self, target: Path) -> dict:
        temporary = target.with_name(target.name + '.tmp')
        target.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with self.connect() as connection, open(temporary, 'w', encoding='utf-8', newline='\n') as stream:
            for block in self._rows(connection):
                stream.write(canonical(asdict(block)) + '\n')
                count += 1
        os.replace(temporary, target)
        return {'file': str(target), 'blocks': count}

    def checkpoint(self) -> dict:
        """Write a signed anchor. A broken chain is refused: anchoring it would bless the damage."""
        report = self.verify()
        if not report['valid'] or report['height'] < 0:
            raise ChainBroken()
        body = {'chain_id': self.chain_id, 'height': report['height'], 'head_hash': report['head_hash'],
                'created_at': timestamp(), 'key_fingerprint': self.signer.fingerprint}
        anchor = {**body, 'signature': self.signer.sign(content_hash(body))}
        target = self.anchors_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, 'a', encoding='utf-8', newline='\n') as stream:
            stream.write(canonical(anchor) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        return anchor

    def rotate(self, new_key, reason: str) -> Block:
        """Hand over to a new key. The outgoing key signs the block that names its successor."""
        block = self.append(ROTATED, 'system', {'key': describe(new_key), 'reason': reason})
        self._signer = new_key
        return block

    def record_restore(self, reason: str) -> Block:
        """Declare a restored backup, so missing blocks read as an event and not as an attack."""
        with self.connect() as connection:
            report, _ = self.walk(self._rows(connection))
        if not report['valid'] or report['height'] < 0:
            raise ChainBroken()
        mirror = self.mirror_path
        lost = self._mirror_tail(mirror)
        if mirror.exists():
            # The old file is evidence of what was lost; it stays beside the new mirror.
            os.replace(mirror, mirror.with_name(f'{mirror.stem}.pre-restore-{report["height"]}{mirror.suffix}'))
        return self.append(RESTORED, 'system', {
            'restored_height': report['height'], 'restored_head': report['head_hash'],
            'mirror_height': lost, 'missing_blocks': max(0, lost - report['height']), 'reason': reason})
