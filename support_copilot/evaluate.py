"""Reproducible dev evaluation; never reads control examples or prints ticket text."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time

from support_copilot.knowledge import get_knowledge
from support_copilot.schemas import TriageRequest
from support_copilot.service import triage

ROOT = Path(__file__).resolve().parent.parent


def evaluate() -> dict:
    frozen = json.loads((ROOT / 'evaluation/frozen-inputs.json').read_text(encoding='utf-8-sig'))
    for item in frozen:
        if hashlib.sha256((ROOT / item['path']).read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Frozen source changed; evaluation refused')
    source = ROOT / 'outputs/handout-2026-10-27/returned-dev-v2/dev.approved.jsonl'
    rows = [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines()]
    k = get_knowledge()
    predictions, times = [], []
    for row in rows:
        request = TriageRequest.model_validate(row['request'])
        start = time.perf_counter()
        predictions.append(triage(request))
        times.append((time.perf_counter() - start) * 1000)
    n = len(rows)
    def accuracy(field):
        return sum(getattr(p, field) == row[field] for row, p in zip(rows, predictions)) / n
    classes = {}
    for code in k.categories:
        selected = [(row, p) for row, p in zip(rows, predictions) if row['category'] == code]
        classes[code] = {'support': len(selected), 'correct': sum(p.category == code for row, p in selected),
                         'predicted_as': dict(Counter(p.category for row, p in selected))}
    priority = {}
    for code in k.priorities:
        tp = sum(row['priority'] == code and p.priority == code for row, p in zip(rows, predictions))
        fp = sum(row['priority'] != code and p.priority == code for row, p in zip(rows, predictions))
        fn = sum(row['priority'] == code and p.priority != code for row, p in zip(rows, predictions))
        support = sum(row['priority'] == code for row in rows)
        priority[code] = dict(support=support, tp=tp, fp=fp, fn=fn, f1=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0.0)
    required = [(row, p) for row, p in zip(rows, predictions) if row['needs_human']]
    retrieval = []
    for case in json.loads((ROOT / 'tests/retrieval_cases.json').read_text(encoding='utf-8')):
        results = k.search(case['query'])
        ids = {item['source_id'] for item in results}
        hits = len(ids & set(case['relevant']))
        retrieval.append(dict(query=case['query'], expected=case['relevant'], returned=[r['source_id'] for r in results],
                              hit_at_5=bool(hits), precision_at_5=hits/5,
                              source_integrity=all(r['text'] == k.rules[r['source_id']].text for r in results)))
    return dict(protocol='wave2-baseline-v1', dataset='human-dev-approved', n=n,
        taxonomy_hashes=k.hashes, category_accuracy=accuracy('category'), category_by_class=classes,
        priority_by_class=priority, priority_macro_f1_all=sum(v['f1'] for v in priority.values())/4,
        priority_macro_f1_present=sum(v['f1'] for v in priority.values() if v['support'])/sum(bool(v['support']) for v in priority.values()),
        absent_priorities=[key for key, value in priority.items() if not value['support']],
        language_accuracy=accuracy('lang'),
        escalation=dict(required=len(required), missed=sum(not p.needs_human for row, p in required),
                        predicted_human=sum(p.needs_human for p in predictions),
                        false_positives=sum(p.needs_human and not row['needs_human'] for row, p in zip(rows, predictions))),
        retrieval_synthetic=dict(n=len(retrieval), hit_at_5=sum(r['hit_at_5'] for r in retrieval)/len(retrieval),
                                 precision_at_5=sum(r['precision_at_5'] for r in retrieval)/len(retrieval), cases=retrieval),
        warm_core_p95_ms=sorted(times)[int(.95*n)-1],
        limitations=['Dev is small and not representative; no blind control claim.',
                     'Synthetic retrieval uses rule-vocabulary queries, not real search relevance labels.',
                     'No dev p0/account/payment cases; synthetic tests cover selected scenarios only.',
                     'Latency measures warmed local core, not HTTP production p95.',
                     'No paid model calls; hosting cost and production cost not measured.'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ['n', 'category_accuracy', 'priority_macro_f1_all', 'priority_macro_f1_present', 'language_accuracy', 'escalation']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
