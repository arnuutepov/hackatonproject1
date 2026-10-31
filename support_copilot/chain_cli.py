"""Owner CLI for the audit chain. Key material is never printed."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time

from support_copilot.audit_chain import (
    AuditChain, ChainBroken, ChainConnection, ROOT, canonical, content_hash, digest, header_hash)
from support_copilot.chain_signer import HmacSigner, describe, write_new_key


def show(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def verify(args) -> int:
    chain = AuditChain()
    report = chain.verify_export(args.export) if args.export else chain.verify()
    if args.expect and report['valid']:
        height, expected = args.expect.split(':', 1)
        actual = next(iter(chain.blocks(limit=1, before=int(height) + 1)), None)
        if actual is None or actual.index != int(height) or actual.block_hash != expected:
            report.update(valid=False, first_failure={'index': int(height), 'check': 'expected_anchor'})
    show(report)
    return 0 if report['valid'] else 1


def export(args) -> int:
    show(AuditChain().export(args.output))
    return 0


def anchor(args) -> int:
    try:
        value = AuditChain().checkpoint()
    except ChainBroken:
        print('Chain is broken or empty: anchor refused.', file=sys.stderr)
        return 1
    if args.wave_doc:
        # Git is the external witness: commit this line after it is written.
        with open(args.wave_doc, 'a', encoding='utf-8', newline='\n') as stream:
            stream.write(f'| {value["created_at"]} | {value["height"]} | `{value["head_hash"]}` |\n')
    show(value)
    return 0


def rotate(args) -> int:
    chain = AuditChain()
    if args.out.exists():
        print('Refusing to overwrite an existing key file.', file=sys.stderr)
        return 2
    if os.environ.get('COPILOT_CHAIN_SIGNER') == 'hmac':
        secret = os.urandom(32).hex()
        descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
            stream.write(secret + '\n')
        new_key = HmacSigner(secret.encode())
    else:
        new_key = write_new_key(args.out)
    block = chain.rotate(new_key, args.reason)
    show({'block': block.index, 'new_key': describe(new_key), 'key_file': str(args.out)})
    print('Install the new key before the next write. HMAC: move the old secret to '
          'COPILOT_CHAIN_PREVIOUS_SECRETS.', file=sys.stderr)
    return 0


def restored(args) -> int:
    try:
        show(AuditChain().record_restore(args.reason).public())
    except ChainBroken:
        print('Restored chain does not verify: nothing recorded.', file=sys.stderr)
        return 1
    return 0


def tamper(args) -> int:
    """Demo only: mutate one stored block, as an attacker with database access would."""
    if os.environ.get('COPILOT_CHAIN_TAMPER_DEMO') != '1':
        print('Set COPILOT_CHAIN_TAMPER_DEMO=1 to corrupt this chain on purpose.', file=sys.stderr)
        return 2
    chain = AuditChain()
    head = chain.head()
    if head is None or head.index < 1:
        print('Nothing to tamper with: the chain has no event blocks.', file=sys.stderr)
        return 1
    index = args.index if args.index is not None else max(1, head.index // 2)
    with closing(sqlite3.connect(chain.path)) as connection:
        row = connection.execute('SELECT payload_json FROM chain_blocks WHERE idx = ?', (index,)).fetchone()
        if row is None:
            print('Block not found.', file=sys.stderr)
            return 1
        payload = {**json.loads(row[0]), 'tampered': True}
        connection.execute('UPDATE chain_blocks SET payload_json = ? WHERE idx = ?', (canonical(payload), index))
        connection.commit()
    print(f'Block #{index} payload changed directly in {chain.path.name}. Run verify.')
    return 0


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def bench(args) -> int:
    """Separate the block itself from the durable commit: SQLite fsync dominates the total."""
    payload = {'ticket_id': None, 'content_hash': content_hash('Шағым: сервер 7'), 'category': 'complaint.dm',
               'priority': 'p2', 'needs_human': True, 'rule_ids': ['30:1.1', '30:1.2']}
    with tempfile.TemporaryDirectory(dir=ROOT if args.on_project_disk else None, ignore_cleanup_errors=True) as folder:
        database = Path(folder) / 'bench.sqlite3'
        os.environ['COPILOT_CHAIN_MIRROR'] = str(Path(folder) / 'mirror.jsonl')
        chain = AuditChain(database)
        chain.genesis()
        in_transaction, committed, published = [], [], []
        for _ in range(args.n):
            with closing(sqlite3.connect(database, timeout=10, isolation_level=None,
                                         factory=ChainConnection)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute('BEGIN IMMEDIATE')
                start = time.perf_counter()
                chain.append('triage.completed', 'bench', payload, connection=connection)
                in_transaction.append(time.perf_counter() - start)
                start = time.perf_counter()
                connection.execute('COMMIT')
                committed.append(time.perf_counter() - start)
            start = time.perf_counter()
            chain.publish()
            published.append(time.perf_counter() - start)
        started = time.perf_counter()
        report = chain.verify()
        verify_seconds = time.perf_counter() - started
    os.environ.pop('COPILOT_CHAIN_MIRROR', None)
    micro = lambda values, share: round(percentile(values, share) * 1e6, 1)
    # Disk is the whole story past this line: hashing and signing alone stay in microseconds.
    block_only = []
    for _ in range(args.n):
        start = time.perf_counter()
        text = canonical(payload)
        chain.signer.sign(header_hash(1, 'now', '0' * 64, 'triage.completed', 'bench', digest(text)))
        block_only.append(time.perf_counter() - start)
    show({'n': args.n, 'signer': chain.signer.algorithm, 'valid': report['valid'],
          'hash_and_sign_us': {'p50': micro(block_only, .5), 'p95': micro(block_only, .95)},
          'append_in_transaction_us': {'p50': micro(in_transaction, .5), 'p95': micro(in_transaction, .95)},
          'commit_us': {'p50': micro(committed, .5), 'p95': micro(committed, .95)},
          'mirror_us': {'p50': micro(published, .5), 'p95': micro(published, .95)},
          'mean_total_ms': round(statistics.mean(map(sum, zip(in_transaction, committed, published))) * 1e3, 3),
          'verify_blocks_per_second': round(args.n / verify_seconds)})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('verify')
    command.add_argument('--export', type=Path, help='check an exported file instead of the database')
    command.add_argument('--expect', help='HEIGHT:HASH from a Git or wave-document anchor')
    command.set_defaults(run=verify)
    command = commands.add_parser('export')
    command.add_argument('--output', type=Path, default=ROOT / 'outputs/chain-export.jsonl')
    command.set_defaults(run=export)
    command = commands.add_parser('anchor')
    command.add_argument('--wave-doc', type=Path, help='also append the head to this tracked document')
    command.set_defaults(run=anchor)
    command = commands.add_parser('rotate')
    command.add_argument('--out', type=Path, required=True, help='new key file; must not exist')
    command.add_argument('--reason', required=True)
    command.set_defaults(run=rotate)
    command = commands.add_parser('restored')
    command.add_argument('--reason', required=True)
    command.set_defaults(run=restored)
    command = commands.add_parser('tamper')
    command.add_argument('--index', type=int)
    command.set_defaults(run=tamper)
    command = commands.add_parser('bench')
    command.add_argument('--n', type=int, default=2000)
    command.add_argument('--on-project-disk', action='store_true')
    command.set_defaults(run=bench)
    args = parser.parse_args()
    return args.run(args)


if __name__ == '__main__':
    sys.exit(main())
