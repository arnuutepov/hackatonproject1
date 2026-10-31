"""Wave 7 submission check: one offline command for a fresh machine.

Runs the same inputs through `make predict` (CLI) and a real localhost uvicorn
`/triage`, checks the contract, safety invariants, HTTP/CLI equality, latency,
model-API cost and the five demo scenarios. No external model is called, no
organizer dataset is read: only the local rules.json and taxonomy.json.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'examples/wave7_demo.jsonl'
DEFAULT_INPUTS = [DEMO, ROOT / 'examples/wave7_new.jsonl', ROOT / 'examples/wave3_synthetic.jsonl',
                  ROOT / 'examples/synthetic_tickets.jsonl']
SERVICE_TOKEN = secrets.token_urlsafe(32)
P95_LIMIT_MS = 5000
COST_LIMIT_USD = 0.002
FORBIDDEN = re.compile(r'накажем|наказан[аоы]? будет|верн[её]м деньги|разбаним|мы проверили скриншот|'
                       r'нарушение (?:доказано|установлено)|уже забанен|жазалаймыз|ақшаңызды қайтарамыз|'
                       r'pulni qaytaramiz|jazolaymiz|we will punish|we will refund|we checked the screenshots', re.I)

# What a reviewer should see in each demo scenario, in examples/wave7_demo.jsonl order.
DEMO_CASES = [
    # No verified FAQ source exists, so the draft honestly hands the question to staff (Wave 3 policy).
    ('simple_question', 'Простой вопрос: p3, без выдуманного ответа — передача сотруднику', lambda r: {
        'lang_ru': r['lang'] == 'ru',
        'category_question': r['category'] == 'report.question',
        'priority_p3': r['priority'] == 'p3',
        'no_safety_flags': r['safety_flags'] == [],
        'no_invented_answer': 'draft_answer_sources_insufficient' in r['reasons'] and r['draft_reply']['cited'] == [],
    }),
    ('complaint', 'Жалоба с вложением: человек, вложения не просмотрены', lambda r: {
        'complaint_category': r['category'].startswith('complaint.'),
        'needs_human': r['needs_human'] is True,
        'nickname_extracted': r['extracted']['nickname'] == 'Night_Rider',
        'server_extracted': r['extracted']['server'] == '4',
        'attachments_not_inspected': r['extracted']['has_evidence'] is False
            and 'attachments_reported_not_inspected' in r['reasons'],
    }),
    ('kazakh', 'Казахский текст: ответ на казахском', lambda r: {
        'lang_kk': r['lang'] == 'kk',
        'draft_kk': r['draft_reply']['lang'] == 'kk',
    }),
    ('mandatory_escalation', 'Платёж: p0 и обязательная передача человеку', lambda r: {
        'payment_topic': 'payment' in r['detected_topics'],
        'priority_p0': r['priority'] == 'p0',
        'needs_human': r['needs_human'] is True,
    }),
    ('wrong_rule_reference', 'Несуществующий пункт: не цитируется, нужен человек', lambda r: {
        'reference_missing': any(item['reference'] == '77:7.7' and item['status'] == 'missing'
                                 for item in r['reference_resolutions']),
        'not_cited': '77:7.7' not in r['draft_reply']['cited'],
        'needs_human': r['needs_human'] is True,
    }),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values, quantile=.95):
    import math
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)] if values else None


def offline_env() -> dict:
    return {**os.environ, 'COPILOT_DRAFT_PROVIDER': 'template', 'COPILOT_VERIFIED_TEMPLATES': 'on',
            'PYTHONUTF8': '1', 'COPILOT_AUTH_MODE': 'required', 'COPILOT_TRIAGE_TOKEN': SERVICE_TOKEN}


def source_status() -> dict:
    frozen = json.loads((ROOT / 'evaluation/frozen-inputs.json').read_text(encoding='utf-8-sig'))
    status = {}
    for item in frozen:
        if not item['path'].startswith('data/'):
            continue  # dev/control labels are not needed for submission checks
        path = ROOT / item['path']
        status[Path(item['path']).name] = ('missing' if not path.exists()
                                           else 'match' if sha(path) == item['sha256'] else 'changed')
    return status


def invariants(response: dict, rules: set) -> dict:
    draft = response['draft_reply']
    return {
        'draft_available': response['draft_available'] and bool(draft['text'].strip()),
        'draft_language_matches': draft['lang'] == response['lang'],
        'citations_exist': all(source in rules for source in draft['cited']),
        'no_unsupported_promises': not FORBIDDEN.search(draft['text']),
        'no_established_violation': draft['established_facts'] == [],
        'no_inspected_evidence': response['extracted']['has_evidence'] is False,
        'no_model_call': response['generation']['attempts'] == 0,
        'zero_api_cost': response['generation']['api_cost_usd'] == 0.0
                         and response['generation']['cost_status'] == 'no_api_calls',
    }


def run_cli(source: Path, workdir: Path) -> list[dict]:
    destination = workdir / (source.stem + '.pred.jsonl')
    command = [sys.executable, '-m', 'support_copilot.cli', '--input', str(source), '--output', str(destination)]
    done = subprocess.run(command, cwd=ROOT, env=offline_env(), capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f'make predict failed for {source.name}: {done.stderr.strip()}')
    lines = destination.read_text(encoding='utf-8').splitlines()
    inputs = source.read_text(encoding='utf-8').splitlines()
    if len(lines) != len(inputs):
        raise RuntimeError(f'{source.name}: {len(inputs)} inputs but {len(lines)} predictions')
    return [json.loads(line) for line in lines]


def start_server():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    started = time.perf_counter()
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'support_copilot.api:app', '--host', '127.0.0.1',
                                '--port', str(port), '--no-access-log', '--no-proxy-headers'], cwd=ROOT, env=offline_env(),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    base = f'http://127.0.0.1:{port}'
    while True:
        try:
            with urlopen(Request(base + '/health', headers={'Authorization': 'Bearer ' + SERVICE_TOKEN}), timeout=1) as response:
                health = json.load(response)
            return process, base, health, (time.perf_counter() - started) * 1000
        except (URLError, TimeoutError, ConnectionError):
            if process.poll() is not None or time.perf_counter() - started > 15:
                process.terminate()
                raise RuntimeError('Server did not start: check rules.json/taxonomy.json and dependencies') from None
            time.sleep(.05)


def post(base: str, path: str, payload: bytes, content_type='application/json'):
    start = time.perf_counter()
    try:
        with urlopen(Request(base + path, data=payload, headers={'Content-Type': content_type, 'Authorization': 'Bearer ' + SERVICE_TOKEN}), timeout=10) as response:
            status, body = response.status, json.load(response)
    except Exception as exc:  # urllib raises HTTPError for 4xx
        status, body = getattr(exc, 'code', None), json.loads(exc.read()) if hasattr(exc, 'read') else None
    return status, body, (time.perf_counter() - start) * 1000


def check(inputs: list[Path], passes: int) -> dict:
    from support_copilot.knowledge import get_knowledge
    rules = set(get_knowledge().rules)
    report = dict(protocol='wave7-submission-check-v1', python=platform.python_version(), platform=platform.platform(),
                  sources=source_status(), model_api_calls=0, files=[], failures=[])
    with tempfile.TemporaryDirectory(prefix='wave7-') as tmp:
        cli = {path: run_cli(path, Path(tmp)) for path in inputs}
    process, base, health, startup = start_server()
    try:
        report['health'] = health
        report['cold_startup_ms'] = round(startup, 1)
        latencies = []
        for path in inputs:
            requests = path.read_text(encoding='utf-8').splitlines()
            entry = dict(file=str(path.relative_to(ROOT)), cases=len(requests), http_equals_cli=0, invariant_failures=[])
            for _ in range(passes):
                for number, (line, expected) in enumerate(zip(requests, cli[path]), 1):
                    status, body, elapsed = post(base, '/triage', line.encode('utf-8'))
                    latencies.append(elapsed)
                    if status != 200:
                        report['failures'].append(f'{path.name}:{number}: HTTP {status}')
                        continue
                    if _ == 0:
                        entry['http_equals_cli'] += body == expected
                        bad = [name for name, ok in invariants(body, rules).items() if not ok]
                        if bad:
                            entry['invariant_failures'].append(dict(line=number, failed=bad))
            if entry['http_equals_cli'] != len(requests):
                report['failures'].append(f'{path.name}: HTTP and CLI differ on '
                                          f'{len(requests) - entry["http_equals_cli"]} lines')
            report['failures'].extend(f'{path.name}:{item["line"]}: {",".join(item["failed"])}'
                                      for item in entry['invariant_failures'])
            report['files'].append(entry)
        # Contract errors must be JSON 422 that does not echo the request.
        secret = 'canary-input-must-not-echo'
        status, body, _ = post(base, '/triage', json.dumps({'kind': 'report', 'messages': [], 'x': secret}).encode())
        report['invalid_request'] = dict(status=status, echoes_input=secret in json.dumps(body))
        if status != 422 or secret in json.dumps(body):
            report['failures'].append('invalid request is not a clean JSON 422')
        report['latency'] = dict(n=len(latencies), passes=passes, p50_ms=round(percentile(latencies, .5), 2),
                                 p95_ms=round(percentile(latencies), 2), max_ms=round(max(latencies), 2),
                                 p95_limit_ms=P95_LIMIT_MS, p95_target_met=percentile(latencies) <= P95_LIMIT_MS,
                                 scope='sequential localhost HTTP, offline templates; not live-model or WAN latency')
        if not report['latency']['p95_target_met']:
            report['failures'].append('p95 above 5 s')
    finally:
        process.terminate()
        process.wait(timeout=5)
    responses = cli[DEMO] if DEMO in cli else []
    report['demo'] = []
    for (key, title, expectation), response in zip(DEMO_CASES, responses):
        checks = expectation(response)
        report['demo'].append(dict(scenario=key, title=title, passed=all(checks.values()), checks=checks,
                                   lang=response['lang'], category=response['category'],
                                   priority=response['priority'], needs_human=response['needs_human'],
                                   cited=response['draft_reply']['cited'],
                                   provenance=response['draft_reply']['provenance'],
                                   draft=response['draft_reply']['text']))
        if not all(checks.values()):
            report['failures'].append(f'demo {key}: ' + ','.join(k for k, v in checks.items() if not v))
    report['cost'] = dict(model_api_calls=0, api_cost_usd_per_ticket=0.0, target_usd=COST_LIMIT_USD,
                          target_met_offline=True, live_model_cost='not measured; provider disabled by default')
    if any(value != 'match' for value in report['sources'].values()):
        report['failures'].append(f'knowledge sources: {report["sources"]}')
    report['passed'] = not report['failures']
    return report


def demo_markdown(report: dict) -> str:
    lines = ['# Wave 7 — демонстрация', '', 'Сгенерировано `make submission-check`; модель не вызывалась.', '']
    requests = DEMO.read_text(encoding='utf-8').splitlines()
    for number, (item, raw) in enumerate(zip(report['demo'], requests), 1):
        message = json.loads(raw)['messages'][0]['text']
        mark = 'OK' if item['passed'] else 'FAIL'
        lines += [f'## {number}. {item["title"]} — {mark}', '', f'> {message}', '',
                  f'lang `{item["lang"]}` · category `{item["category"]}` · priority `{item["priority"]}` · '
                  f'needs_human `{str(item["needs_human"]).lower()}` · cited `{item["cited"]}` · '
                  f'draft `{item["provenance"]}`', '', '```text', item['draft'], '```', '']
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description='Offline Wave 7 submission check')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/wave-7')
    parser.add_argument('--input', type=Path, action='append', help='Extra inference-contract JSONL (repeatable)')
    parser.add_argument('--passes', type=int, default=3, choices=range(1, 11))
    args = parser.parse_args()
    inputs = DEFAULT_INPUTS + [path.resolve() for path in args.input or []]
    try:
        report = check(inputs, args.passes)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'Submission check failed: {exc}', file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'submission-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                                       encoding='utf-8')
    (args.output / 'demo.md').write_text(demo_markdown(report), encoding='utf-8')
    print(json.dumps(dict(passed=report['passed'], sources=report['sources'], latency=report.get('latency'),
                          demo={d['scenario']: d['passed'] for d in report['demo']},
                          failures=report['failures']), ensure_ascii=False, indent=1))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
