"""Reproducible offline project scorecard; no silent control evaluation or external inference."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile

from support_copilot.measure import ROOT, implementation_hash
from support_copilot.quality import checked_rows, metrics, retrieval_metrics

DEV = ROOT / 'outputs/handout-2026-10-27/returned-dev-v2/dev.approved.jsonl'


@contextmanager
def offline_environment(state):
    settings = dict(COPILOT_DRAFT_PROVIDER='template', COPILOT_CATEGORY_BACKEND='rules',
                    COPILOT_VERIFIED_TEMPLATES='on', COPILOT_OPERATOR_DB=str(state / 'workspace.sqlite3'),
                    COPILOT_AUTH_DB=str(state / 'auth.sqlite3'))
    previous = {key: os.environ.get(key) for key in settings}
    os.environ.update(settings)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run(*, dev_file=DEV, http=False, passes=3, workers=4):
    from support_copilot.knowledge import get_knowledge
    from support_copilot.evaluate_drafts import evaluate as drafts
    from support_copilot.evaluate_regressions import evaluate as safety
    report = dict(protocol='core-project-scorecard-v1', created_at=datetime.now(timezone.utc).isoformat(),
                  implementation_sha256=implementation_hash(), python=platform.python_version(),
                  platform=platform.platform(), mode='offline-template-rules', model_api_calls=0,
                  independent_gold=None, blind_draft_review=None, native_kk_uz_review=None,
                  overall_jury_score=None, live_model=None, control_evaluated=False)
    with tempfile.TemporaryDirectory(prefix='oguzki-scorecard-') as temporary:
        with offline_environment(Path(temporary)):
            knowledge = get_knowledge()
            report['knowledge'] = dict(rules=len(knowledge.rules), hashes=knowledge.hashes)
            if dev_file is not None and dev_file.exists():
                report['dev'] = metrics(checked_rows(dev_file))
                report['dev_source_sha256'] = hashlib.sha256(dev_file.read_bytes()).hexdigest()
                report['dev_scope'] = 'Previously tuned human-labeled development data, not independent gold'
                checked_rows(dev_file)  # Also check frozen source hashes after inference.
            else:
                report['dev'] = None
                report['dev_scope'] = 'Private labeled development data not available or explicitly skipped'
            report['retrieval'] = retrieval_metrics()
            report['draft_safety'] = drafts()
            report['adversarial_safety'] = safety()
            if http:
                from support_copilot.benchmark import benchmark
                from support_copilot.submission_check import check, DEFAULT_INPUTS
                rows = [{'request': json.loads(line)} for file in DEFAULT_INPUTS
                        for line in file.read_text(encoding='utf-8').splitlines() if line.strip()]
                report['http'] = benchmark(passes, workers, rows=rows, dataset='handwritten-synthetic')
                report['http_cli'] = check(DEFAULT_INPUTS, 1)
            else:
                report['http'] = report['http_cli'] = None
    report['limitations'] = [
        'No single project accuracy: jury weights combine different, partly unmeasured tasks.',
        'Dev was used for tuning. Frozen control is not inferred on by this command.',
        'Retrieval covers 7 synthetic queries, not 100 independently labeled search cases.',
        'Safety replays do not measure usefulness, naturalness or live provider reliability.',
        'Offline model API cost is zero; hosting and live model billing are unmeasured.',
        'Local HTTP measurements include signed SQLite audit, not WAN or external model latency.',
    ]
    return report


def markdown(report):
    dev = report['dev']
    lines = ['# Проверка ядра Support Copilot', '',
             f"Дата UTC: {report['created_at']}", '', f"Python SHA-256: `{report['implementation_sha256']}`", '',
             '| Показатель | Значение |', '|---|---|']
    if dev:
        for field, label in [('category', 'Категория dev'), ('lang', 'Язык dev'), ('priority', 'Приоритет dev')]:
            value = dev[field]
            lines.append(f"| {label}, accuracy | {value['estimate']:.1%}; 95% CI {value['low']:.1%}–{value['high']:.1%} |")
        lines += [f"| Macro-F1 приоритета (все 4 класса) | {dev['priority_macro_f1']:.4f} |",
                  f"| Baseline большинства по kind, dev | {dev['baselines']['majority_category_per_kind_accuracy']:.1%} |",
                  f"| Эскалация, TP / FN / FP | {dev['escalation']['tp']} / {dev['escalation']['fn']} / {dev['escalation']['fp']} |",
                  f"| Доля needs_human | {dev['escalation']['needs_human_share']:.1%} |",
                  f"| Возвращённые цитаты / несуществующие ID | {dev['citations']['returned']} / {dev['citations']['invalid']} |"]
    for key, label in [('draft_safety', 'Черновики: offline safety'), ('adversarial_safety', 'Adversarial safety')]:
        value = report[key]
        lines.append(f"| {label} | {value['passed']}/{value['runs']} |")
    if report['http']:
        value = report['http']
        lines += [f"| HTTP synthetic, запросов / клиентов | {value['n']} / {value['workers']} |",
                  f"| HTTP p50 / p95 | {value['p50_ms']:.2f} / {value['p95_ms']:.2f} мс |",
                  f"| HTTP/CLI проверка | {'passed' if report['http_cli']['passed'] else 'failed'} |"]
    lines += ['| Слепая оценка черновиков / носителями KK/UZ | ожидается |',
              '| Общий балл жюри / живая модель | не измерены |', '',
              'Dev использован для настройки. Эти числа не доказывают качество на новых обращениях.', '',
              '## Ограничения', ''] + ['- ' + value for value in report['limitations']]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'outputs/project-metrics')
    parser.add_argument('--http', action='store_true', help='Also run isolated localhost HTTP/CLI and load checks')
    parser.add_argument('--no-dev', action='store_true', help='Use public synthetic examples only')
    parser.add_argument('--passes', type=int, default=3)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    result = run(dev_file=None if args.no_dev else DEV, http=args.http, passes=args.passes, workers=args.workers)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'metrics.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (args.output/'summary.md').write_text(markdown(result), encoding='utf-8')
    print(json.dumps(dict(output=str(args.output), category_dev=result['dev']['category']['estimate'] if result['dev'] else None,
                         control_evaluated=False, model_api_calls=0)))
    failed = any(result[key]['passed'] != result[key]['runs'] for key in ('draft_safety', 'adversarial_safety'))
    return 1 if failed or (result['http_cli'] and not result['http_cli']['passed']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
