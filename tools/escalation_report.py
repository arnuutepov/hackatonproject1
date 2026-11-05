"""Offline comparison of two escalation policies on the frozen dev and control labels.

Policy "always": every complaint is handed to a person (the default of the service).
Policy "gated": complaints are handed over only when a safety gate, a mandatory category, a critical
topic, an ambiguity or an incomplete analysis says so (COPILOT_COMPLAINTS_ALWAYS_HUMAN=off).

The report shows precision/recall/share of needs_human for both, lists every missed handoff (false
negative) with its labelled and predicted category, and shows how the human labels relate to the
taxonomy's `needs_human_always` categories. It never prints ticket text. Inference is local, template
mode, no external calls. Nothing here changes the default behaviour of the service.

    .venv/bin/python tools/escalation_report.py --output outputs/escalation/report.json
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ['COPILOT_DRAFT_PROVIDER'] = 'template'
os.environ.pop('COPILOT_CATEGORY_BACKEND', None)

from support_copilot.knowledge import get_knowledge  # noqa: E402
from support_copilot.quality import checked_rows  # noqa: E402
from support_copilot.schemas import TriageRequest  # noqa: E402
from support_copilot.service import triage  # noqa: E402

SETS = {
    'dev': 'outputs/handout-2026-10-27/returned-dev-v2/dev.approved.jsonl',
    'control_silver': 'outputs/handout-2026-10-27/control-assistant/control.assistant.jsonl',
}


def predict(rows, always: bool):
    os.environ['COPILOT_COMPLAINTS_ALWAYS_HUMAN'] = 'on' if always else 'off'
    return [triage(TriageRequest.model_validate(row['request']), use_configured_provider=False) for row in rows]


def score(rows, predictions):
    tp = sum(p.needs_human and r['needs_human'] for p, r in zip(predictions, rows))
    fp = sum(p.needs_human and not r['needs_human'] for p, r in zip(predictions, rows))
    fn = sum(not p.needs_human and r['needs_human'] for p, r in zip(predictions, rows))
    tn = len(rows) - tp - fp - fn
    return dict(tp=tp, fp=fp, fn=fn, tn=tn, precision=tp / (tp + fp) if tp + fp else None,
                recall=tp / (tp + fn) if tp + fn else None, accuracy=(tp + tn) / len(rows),
                share=sum(p.needs_human for p in predictions) / len(rows))


def misses(rows, predictions):
    return [dict(ticket_id=r.get('ticket_id'), kind=r['request']['kind'], label_category=r['category'],
                 label_priority=r['priority'], predicted_category=p.category, predicted_priority=p.priority,
                 reasons=[x for x in p.reasons if not x.startswith(('wave_2', 'confidence_is', 'duplicate_scope'))])
            for p, r in zip(predictions, rows) if r['needs_human'] and not p.needs_human]


def label_structure(rows, human_always):
    """How do the human needs_human labels relate to the taxonomy's mandatory categories?"""
    table = Counter((r['needs_human'], r['category'] in human_always) for r in rows)
    by_kind = Counter((r['request']['kind'], r['needs_human']) for r in rows)
    by_priority = Counter((r['priority'], r['needs_human']) for r in rows)
    return {'needs_human_vs_category_in_needs_human_always': {f'needs_human={a}, category_in_list={b}': n for (a, b), n in sorted(table.items())},
            'kind_vs_needs_human': {f'{a}, needs_human={b}': n for (a, b), n in sorted(by_kind.items())},
            'priority_vs_needs_human': {f'{a}, needs_human={b}': n for (a, b), n in sorted(by_priority.items())}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    human_always = get_knowledge().human_always
    report = {'note': 'dev was used for tuning; control is assistant silver. Do not tune the gate on these numbers.'}
    for name, relative in SETS.items():
        rows = checked_rows(ROOT / relative)
        always, gated = predict(rows, True), predict(rows, False)
        report[name] = dict(n=len(rows), always=score(rows, always), gated=score(rows, gated),
                            gated_missed_handoffs=misses(rows, gated),
                            labels=label_structure(rows, human_always))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for name in SETS:
        item = report[name]
        print(f"{name}: n={item['n']}")
        for policy in ('always', 'gated'):
            s = item[policy]
            precision = 'n/a' if s['precision'] is None else f"{s['precision']:.2f}"
            recall = 'n/a' if s['recall'] is None else f"{s['recall']:.2f}"
            print(f"  {policy:7} precision={precision} recall={recall} share={s['share']:.2f} FN={s['fn']} FP={s['fp']}")
        print(f"  gated missed handoffs: {len(item['gated_missed_handoffs'])}")
    print('full report:', args.output)


if __name__ == '__main__':
    main()
