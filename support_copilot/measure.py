"""Small, frozen control measurement. All inference is explicitly offline."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import time

from support_copilot.drafting import build_context, CONTEXT_LIMITS
from support_copilot.draft_provider import MAX_OUTPUT_TOKENS
from support_copilot.knowledge import get_knowledge
from support_copilot.schemas import TriageRequest
from support_copilot.service import triage
from support_copilot.category_model import backend_metadata as category_backend_metadata

ROOT = Path(__file__).resolve().parents[1]
CONTROL = 'outputs/handout-2026-10-27/control-assistant/control.assistant.jsonl'


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation_hash() -> str:
    digest=hashlib.sha256()
    for path in sorted((ROOT/'support_copilot').glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def percentile(values, quantile=.95):
    return sorted(values)[max(0,math.ceil(len(values)*quantile)-1)] if values else None


def load_control() -> list[dict]:
    frozen = json.loads((ROOT/'evaluation/frozen-inputs.json').read_text(encoding='utf-8-sig'))
    for item in frozen:
        if sha(ROOT/item['path']) != item['sha256']:
            raise ValueError('Frozen evaluation input changed')
    rows = [json.loads(line) for line in (ROOT/CONTROL).read_text(encoding='utf-8').splitlines()]
    if len(rows) != 100 or len({row['ticket_id'] for row in rows}) != len(rows):
        raise ValueError('Expected 100 distinct canonical control cases')
    return rows


def classification_metrics(actual, predicted, classes):
    if len(actual) != len(predicted):
        raise ValueError('Prediction count does not match labels')
    counts = {}
    for code in classes:
        tp = sum(a == p == code for a,p in zip(actual,predicted))
        fp = sum(a != code and p == code for a,p in zip(actual,predicted))
        fn = sum(a == code and p != code for a,p in zip(actual,predicted))
        counts[code] = dict(support=sum(a == code for a in actual),tp=tp,fp=fp,fn=fn,
                           f1=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0.0,
                           predicted_as=dict(Counter(p for a,p in zip(actual,predicted) if a == code)))
    present = [value['f1'] for value in counts.values() if value['support']]
    return dict(n=len(actual), correct=sum(a==p for a,p in zip(actual,predicted)),
                accuracy=sum(a==p for a,p in zip(actual,predicted))/len(actual) if actual else None,
                macro_f1_all=statistics.mean(value['f1'] for value in counts.values()),
                macro_f1_present=statistics.mean(present) if present else None,
                absent=[code for code,value in counts.items() if not value['support']],per_class=counts)


def measure(output: Path) -> dict:
    rows = load_control()
    knowledge = get_knowledge()
    predictions,core_ms,contexts = [],[],[]
    for row in rows:
        request = TriageRequest.model_validate(row['request'])
        start = time.perf_counter()
        result = triage(request,use_configured_provider=False,use_verified_templates=True)
        core_ms.append((time.perf_counter()-start)*1000)
        predictions.append(result)
        context,_ = build_context(request,result,knowledge)
        contexts.append(len(json.dumps(context,ensure_ascii=False,separators=(',',':'))))
    required = [(row,pred) for row,pred in zip(rows,predictions) if row['needs_human']]
    citations = [source for pred in predictions for source in pred.draft_reply.cited]
    retrieved = [rule for pred in predictions for rule in pred.retrieved_rules]
    bad = sum(source not in knowledge.rules for source in citations)
    report = dict(protocol='wave5-local-control-v1',dataset='assistant-control-silver',n=len(rows),
        labels_provenance='Codex assistant, provisional, requires independent human review; not a blind test',
        source_hashes={'control':sha(ROOT/CONTROL),**knowledge.hashes},
        code_version='wave5',implementation_sha256=implementation_hash(),python=platform.python_version(),platform=platform.platform(),
        category_backend=category_backend_metadata(),
        category=classification_metrics([r['category'] for r in rows],[p.category for p in predictions],knowledge.categories),
        priority=classification_metrics([r['priority'] for r in rows],[p.priority for p in predictions],knowledge.priorities),
        language=classification_metrics([r['lang'] for r in rows],[p.lang for p in predictions],['ru','kk','uz','en','es','pt','und']),
        handoff=dict(count=sum(p.needs_human for p in predictions),denominator=len(rows),
                     rate=sum(p.needs_human for p in predictions)/len(rows),required=len(required),
                     missed=sum(not p.needs_human for row,p in required),
                     extra=sum(p.needs_human and not row['needs_human'] for row,p in zip(rows,predictions))),
        citations=dict(returned=len(citations),invalid=bad,error_rate=bad/len(citations) if citations else None,
                       relevance=None,relevance_status='needs_human_ratings',
                       retrieval_occurrences=len(retrieved),
                       retrieval_source_errors=sum(r.source_id not in knowledge.rules or r.text != knowledge.rules[r.source_id].text for r in retrieved)),
        generation=dict(attempts=sum(p.generation.attempts for p in predictions),external_calls=0,
                        api_cost_usd=0.0,infrastructure_cost_usd=None,total_cost_usd=None,
                        live_model_cost_usd=None,live_model_cost_target_met=None,
                        routes=dict(Counter(p.draft_reply.provenance for p in predictions))),
        local_core=dict(n=len(core_ms),p50_ms=percentile(core_ms,.5),p95_ms=percentile(core_ms),max_ms=max(core_ms)),
        efficiency=dict(context_limits=CONTEXT_LIMITS,max_output_tokens=MAX_OUTPUT_TOKENS,
                        serialized_context_chars_mean=statistics.mean(contexts),
                        draft_chars_mean=statistics.mean(len(p.draft_reply.text) for p in predictions),
                        draft_chars_p95=percentile([len(p.draft_reply.text) for p in predictions]),
                        note='Characters are not token usage; no live token or cost reduction is claimed'),
        blind_review=dict(status='pending',rated_cases=0,correctness=None,usefulness=None,language_quality=None,promises=None),
        remaining_weaknesses=['Provisional AI labels, previously seen control data.',
                             'Only one KK-labelled real control case; no p0/account/payment cases.',
                             'Human correctness/usefulness/language and retrieval relevance pending.',
                             'No live model cost or latency measurement.',
                             'Heuristic classifier and conservative human-review policy.'])
    output.mkdir(parents=True,exist_ok=True)
    (output/'control-metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with (output/'control-predictions.jsonl').open('w',encoding='utf-8') as stream:
        for row,pred in zip(rows,predictions):
            stream.write(json.dumps({'ticket_id':row['ticket_id'],'response':pred.model_dump()},ensure_ascii=False)+'\n')
    load_control()  # Verify source integrity after the run too.
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    report = measure(args.output)
    print(json.dumps(dict(n=report['n'],category_accuracy=report['category']['accuracy'],
        priority_macro_f1_all=report['priority']['macro_f1_all'],priority_macro_f1_present=report['priority']['macro_f1_present'],
        language_accuracy=report['language']['accuracy'],handoff=report['handoff'],generation=report['generation']),ensure_ascii=False))


if __name__ == '__main__':
    main()
