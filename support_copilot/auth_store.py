"""Local operator identities and revocable sessions; no default credentials."""
from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time
from threading import BoundedSemaphore
from uuid import uuid4

from support_copilot.audit_chain import AuditChain

ROLES = frozenset({'agent', 'lead', 'admin'})
SESSION_SECONDS = 8 * 60 * 60
PASSWORD_SLOTS = BoundedSemaphore(2)


class LoginBusy(Exception):
    pass


def hash_password(password: str) -> str:
    if not 15 <= len(password) <= 256:
        raise ValueError('Password must contain 15..256 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1, maxmem=256*1024*1024)
    return f'scrypt${salt.hex()}${digest.hex()}'


def check_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split('$')
        if algorithm != 'scrypt':
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**17, r=8, p=1,
                                maxmem=256*1024*1024).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthStore:
    def __init__(self):
        self.path = Path(os.environ.get('COPILOT_AUTH_DB') or Path(__file__).resolve().parents[1] / 'outputs/operator/auth.sqlite3')

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        self.path.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE IF NOT EXISTS operators (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS sessions (
                digest TEXT PRIMARY KEY, operator_id TEXT NOT NULL, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_limits (
                key TEXT PRIMARY KEY, bucket INTEGER NOT NULL, count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS game_nonces (
                key_id TEXT NOT NULL, nonce TEXT NOT NULL, at REAL NOT NULL,
                PRIMARY KEY (key_id, nonce)
            );
            CREATE TABLE IF NOT EXISTS identity_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL
            );
        ''')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'username', 'role', 'active')}

    def create_user(self, username: str, password: str, role: str, actor='bootstrap') -> dict:
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{3,64}', username) or role not in ROLES:
            raise ValueError('Invalid username or role')
        encoded = hash_password(password)
        with self.connect() as db:
            key = str(uuid4())
            db.execute('INSERT INTO operators (id, username, password_hash, role) VALUES (?, ?, ?, ?)',
                       (key, username, encoded, role))
            db.execute('INSERT INTO identity_audit (at, actor, action, subject) VALUES (?, ?, ?, ?)',
                       (time.time(), actor, 'create_operator', key))
            # Separate database: appended before this commit, so a chain failure rolls the action back.
            AuditChain().append('operator.created', actor, {'id': key, 'role': role})
            return {'id': key, 'username': username, 'role': role, 'active': 1}

    def users(self) -> list[dict]:
        with self.connect() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM operators ORDER BY username')]

    def update_user(self, key: str, role: str, active: bool, actor: str):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM operators WHERE id = ?', (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row['role'] == 'admin' and row['active'] and (role != 'admin' or not active):
                count = db.execute("SELECT COUNT(*) FROM operators WHERE role = 'admin' AND active = 1").fetchone()[0]
                if count <= 1:
                    raise ValueError('Cannot remove the last active admin')
            db.execute('UPDATE operators SET role = ?, active = ? WHERE id = ?', (role, int(active), key))
            db.execute('DELETE FROM sessions WHERE operator_id = ?', (key,))
            db.execute('INSERT INTO identity_audit (at, actor, action, subject) VALUES (?, ?, ?, ?)',
                       (time.time(), actor, 'update_operator', key))
            AuditChain().append('operator.role_change', actor, {
                'id': key, 'role': role, 'active': bool(active),
                'previous_role': row['role'], 'previous_active': bool(row['active'])})

    def reset_password(self, username: str, password: str):
        encoded = hash_password(password)
        with self.connect() as db:
            row = db.execute('SELECT id FROM operators WHERE username = ?', (username,)).fetchone()
            if row is None:
                raise ValueError('Operator not found')
            db.execute('UPDATE operators SET password_hash = ? WHERE id = ?', (encoded, row['id']))
            db.execute('DELETE FROM sessions WHERE operator_id = ?', (row['id'],))
            db.execute('INSERT INTO identity_audit (at, actor, action, subject) VALUES (?, ?, ?, ?)',
                       (time.time(), 'local_cli', 'reset_password', row['id']))
            AuditChain().append('operator.password_reset', 'local_cli', {'id': row['id']})

    def reserve_login(self, username: str, peer: str) -> bool:
        bucket = int(time.time() // 60)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM auth_limits WHERE bucket < ?', (bucket - 2,))
            for key, limit in (('user:' + username.casefold(), 10), ('peer:' + peer, 30)):
                row = db.execute('SELECT bucket, count FROM auth_limits WHERE key = ?', (key,)).fetchone()
                if row and row['bucket'] == bucket and row['count'] >= limit:
                    return False
            for key in ('user:' + username.casefold(), 'peer:' + peer):
                db.execute('INSERT INTO auth_limits VALUES (?, ?, 1) ON CONFLICT(key) DO UPDATE SET '
                           'count = CASE WHEN bucket = excluded.bucket THEN count + 1 ELSE 1 END, bucket = excluded.bucket',
                           (key, bucket))
            return True

    def login(self, username: str, password: str) -> tuple[str, dict] | None:
        with self.connect() as db:
            row = db.execute('SELECT * FROM operators WHERE username = ?', (username,)).fetchone()
        # Same expensive KDF for unknown and disabled users: no cheap existence oracle.
        dummy = 'scrypt$' + '00' * 16 + '$' + '00' * 64
        if not PASSWORD_SLOTS.acquire(blocking=False):
            raise LoginBusy()
        try:
            valid = check_password(password, row['password_hash'] if row else dummy)
        finally:
            PASSWORD_SLOTS.release()
        if not valid or not row or not row['active']:
            return None
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM sessions WHERE expires_at < ?', (time.time(),))
            # Re-read under a write lock: disable/reset cannot race a successful login.
            latest = db.execute('SELECT * FROM operators WHERE id = ?', (row['id'],)).fetchone()
            if not latest['active'] or latest['password_hash'] != row['password_hash']:
                return None
            db.execute('INSERT INTO sessions VALUES (?, ?, ?)', (token_hash(token), row['id'], time.time() + SESSION_SECONDS))
            db.execute('INSERT INTO identity_audit (at, actor, action, subject) VALUES (?, ?, ?, ?)',
                       (time.time(), row['id'], 'login', row['id']))
            AuditChain().append('operator.login', row['id'], {'id': row['id'], 'role': latest['role']})
        return token, self.public(latest)

    def session(self, token: str) -> dict | None:
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute('SELECT operators.* FROM sessions JOIN operators ON operators.id = sessions.operator_id '
                             'WHERE digest = ? AND expires_at > ? AND active = 1', (token_hash(token), time.time())).fetchone()
            return self.public(row) if row else None

    def logout(self, token: str, everywhere=False):
        with self.connect() as db:
            row = db.execute('SELECT sessions.operator_id, operators.role FROM sessions '
                             'JOIN operators ON operators.id = sessions.operator_id WHERE digest = ?',
                             (token_hash(token),)).fetchone()
            if row:
                if everywhere:
                    db.execute('DELETE FROM sessions WHERE operator_id = ?', (row['operator_id'],))
                else:
                    db.execute('DELETE FROM sessions WHERE digest = ?', (token_hash(token),))
                db.execute('INSERT INTO identity_audit (at, actor, action, subject) VALUES (?, ?, ?, ?)',
                           (time.time(), row['operator_id'], 'logout_all' if everywhere else 'logout', row['operator_id']))
                AuditChain().append('operator.logout', row['operator_id'], {
                    'id': row['operator_id'], 'role': row['role'], 'everywhere': everywhere})

    def consume_nonce(self, key_id: str, nonce: str, at: float) -> bool:
        with self.connect() as db:
            db.execute('DELETE FROM game_nonces WHERE at < ?', (time.time() - 600,))
            try:
                db.execute('INSERT INTO game_nonces VALUES (?, ?, ?)', (key_id, nonce, at))
            except sqlite3.IntegrityError:
                return False
            return True
