"""Reproducible evaluation: confidence intervals, retrieval, honest escalation and two-rater packets."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import statistics
import time

from support_copilot.measure import classification_metrics, implementation_hash, percentile
from support_copilot.knowledge import get_knowledge
from support_copilot.schemas import TriageRequest
from support_copilot.service import triage

ROOT=Path(__file__).resolve().parents[1]
FIELDS=('lang','category','priority','needs_human')


def bootstrap(values, repeats=2000, seed=20261031):
    if not values:return None
    rng=random.Random(seed)
    samples=sorted(statistics.mean(rng.choices(values,k=len(values))) for _ in range(repeats))
    return {'estimate':statistics.mean(values),'low':samples[int(.025*repeats)],
            'high':samples[min(repeats-1,int(.975*repeats))],'method':'percentile_case_bootstrap',
            'repeats':repeats,'seed':seed,'n':len(values)}


def kappa(a,b):
    if not a or len(a)!=len(b):raise ValueError('Aligned complete ratings required')
    n=len(a);ca,cb=Counter(a),Counter(b)
    observed=sum(x==y for x,y in zip(a,b))/n
    expected=sum(ca[c]*cb[c] for c in ca.keys()|cb.keys())/(n*n)
    return {'n':n,'agreement':observed,'kappa':(observed-expected)/(1-expected) if expected<1 else None,
            'undefined_reason':'constant_identical_labels' if expected==1 else None}


def retrieval_metrics():
    k=get_knowledge();cases=json.loads((ROOT/'tests/retrieval_cases.json').read_text())
    report={}
    for mode in ('legacy','hybrid'):
        values=[]
        for case in cases:
            expected=set(case['relevant']);results=k.search(case['query'],mode=mode)
            ids=[r['source_id'] for r in results]
            hits=len(set(ids)&expected)
            ranks=[i+1 for i,sid in enumerate(ids) if sid in expected]
            values.append({'recall_at_5':hits/len(expected),'precision_at_5':hits/5,
                           'mrr':1/min(ranks) if ranks else 0,'hit_at_5':bool(hits)})
        report[mode]={metric:bootstrap([v[metric] for v in values]) for metric in values[0]}
    report['scope']='7 existing synthetic rule-vocabulary queries; not independently judged live relevance'
    return report


def checked_rows(path):
    frozen=json.loads((ROOT/'evaluation/frozen-inputs.json').read_text(encoding='utf-8-sig'))
    for item in frozen:
        if hashlib.sha256((ROOT/item['path']).read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Frozen source changed')
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines()]


def metrics(rows):
    if not rows:
        raise ValueError('Evaluation requires at least one labeled case')
    predictions, timings = [], []
    for row in rows:
        request = TriageRequest.model_validate(row['request'])
        start = time.perf_counter()
        predictions.append(triage(request, use_configured_provider=False))
        timings.append((time.perf_counter() - start) * 1000)
    report={f:bootstrap([getattr(p,f)==r[f] for p,r in zip(predictions,rows)]) for f in ('lang','category','priority')}
    report['language_by_class']={lang:{'n':sum(r['lang']==lang for r in rows),
        'accuracy':bootstrap([p.lang==r['lang'] for p,r in zip(predictions,rows) if r['lang']==lang])}
        for lang in sorted({r['lang'] for r in rows})}
    tp=sum(p.needs_human and r['needs_human'] for p,r in zip(predictions,rows))
    fp=sum(p.needs_human and not r['needs_human'] for p,r in zip(predictions,rows))
    fn=sum(not p.needs_human and r['needs_human'] for p,r in zip(predictions,rows))
    report['escalation']={'tp':tp,'fp':fp,'fn':fn,'precision':tp/(tp+fp) if tp+fp else None,
        'recall':tp/(tp+fn) if tp+fn else None,'needs_human_share':sum(p.needs_human for p in predictions)/len(rows)}
    report['confidence']={'calibrated':False,'levels':{level:{'n':sum(p.confidence_level==level for p in predictions),
        'category_accuracy':bootstrap([p.category==r['category'] for p,r in zip(predictions,rows) if p.confidence_level==level])}
        for level in ('low','medium','high')}}
    knowledge = get_knowledge()
    report['classification'] = {field: classification_metrics([r[field] for r in rows],
        [getattr(p, field) for p in predictions], classes) for field, classes in (
        ('category', knowledge.categories), ('priority', knowledge.priorities),
        ('lang', ['ru', 'kk', 'uz', 'en', 'es', 'pt', 'und']))}
    report['category_macro_f1']=report['classification']['category']['macro_f1_all']
    report['priority_macro_f1']=report['classification']['priority']['macro_f1_all']
    # These constants are fitted on the evaluated sample: descriptive baselines, not held-out estimates.
    majority = Counter(r['category'] for r in rows).most_common(1)[0][0]
    by_kind = {kind: Counter(r['category'] for r in rows if r['request']['kind'] == kind).most_common(1)[0][0]
               for kind in {r['request']['kind'] for r in rows}}
    report['baselines'] = dict(scope='majority constants fitted on this sample; not independent',
        majority_category_accuracy=sum(r['category'] == majority for r in rows) / len(rows),
        majority_category_per_kind_accuracy=sum(r['category'] == by_kind[r['request']['kind']] for r in rows) / len(rows),
        always_escalate=dict(recall=1.0 if tp+fn else None, precision=sum(r['needs_human'] for r in rows)/len(rows),
                             needs_human_share=1.0))
    cited = [sid for p in predictions for sid in p.draft_reply.cited]
    report['citations'] = dict(returned=len(cited), invalid=sum(sid not in knowledge.rules for sid in cited),
        error_rate=sum(sid not in knowledge.rules for sid in cited)/len(cited) if cited else None,
        cases_with_citations=sum(bool(p.draft_reply.cited) for p in predictions),
        semantic_relevance=None, semantic_relevance_status='independent_human_ratings_pending',
        retrieval_source_errors=sum(rule.source_id not in knowledge.rules or rule.text != knowledge.rules[rule.source_id].text
                                    for p in predictions for rule in p.retrieved_rules))
    report['core_latency'] = dict(p50_ms=percentile(timings,.5), p95_ms=percentile(timings),
                                  n=len(timings), scope='offline local core, not HTTP or live model')
    report['draft_routes'] = dict(Counter(p.draft_reply.provenance for p in predictions))
    return report


def prepare_gold(rows, directory, count=40):
    for name in ('rater-a.json','rater-b.json'):
        file=directory/name
        if file.exists():
            existing=json.loads(file.read_text())
            if existing.get('rater_id') or any(any(value is not None for key,value in row.items() if key!='case_id') for row in existing.get('ratings',[])):
                raise ValueError('Refusing to overwrite existing human ratings; choose a new output directory')
    if not 30<=count<=50 or len(rows)<count:raise ValueError('Gold packet requires 30..50 cases')
    # Stratify language, then fill by a deterministic hash. This is already-seen dev, not blind gold.
    ordered=sorted(rows,key=lambda r:hashlib.sha256(r['ticket_id'].encode()).hexdigest())
    chosen=[]
    for lang in sorted({r['lang'] for r in rows}):chosen.extend([r for r in ordered if r['lang']==lang][:min(5,count//7)])
    ids={r['ticket_id'] for r in chosen}
    chosen+=( [r for r in ordered if r['ticket_id'] not in ids][:count-len(chosen)] )
    directory.mkdir(parents=True,exist_ok=True)
    directory.chmod(0o700)
    from support_copilot.privacy import redact_tree
    cases=[]
    for row in chosen:
        prediction=triage(TriageRequest.model_validate(row['request']),use_configured_provider=False)
        cases.append({'case_id':hashlib.sha256(row['ticket_id'].encode()).hexdigest(),
            'request':redact_tree(row['request']),'draft':redact_tree({'text':prediction.draft_reply.text})['text'],
            'cited_sources':[{'source_id':sid,'text':get_knowledge().rules[sid].text} for sid in prediction.draft_reply.cited]})
    (directory/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2))
    for name in ('rater-a','rater-b'):
        (directory/(name+'.json')).write_text(json.dumps({'rater_id':'','independent':False,'ratings':[
            {'case_id':c['case_id'],**dict.fromkeys(FIELDS), 'draft_quality_1_to_5':None, 'citations_supported':None,'no_unfounded_promises':None} for c in cases]},indent=2))
    (directory/'protocol.json').write_text(json.dumps({'n':len(cases),'status':'pending_two_independent_humans',
        'source':'previously_seen_human_dev_not_blind_holdout','automatic_gold_claim':False},indent=2))
    for file in directory.iterdir():
        if file.is_file():file.chmod(0o600)
    return {'n':len(cases),'kappa':None,'status':'pending_two_independent_humans'}


def score_gold(packet,a,b):
    cases=json.loads((packet/'cases.json').read_text());ids=[r['case_id'] for r in cases]
    kinds={r['case_id']:r['request']['kind'] for r in cases}
    ratings=[]
    for file in (a,b):
        value=json.loads(file.read_text())
        if not value.get('rater_id') or value.get('independent') is not True:raise ValueError('Independent human rater metadata required')
        mapping={r['case_id']:r for r in value['ratings']}
        if len(mapping)!=len(value['ratings']) or set(mapping)!=set(ids):raise ValueError('Rater IDs mismatch/duplicate')
        for cid in ids:
            r=mapping[cid]
            if not set(FIELDS)<=r.keys():raise ValueError('Incomplete labels')
            if r['lang'] not in ('ru','kk','uz','en','es','pt','und') or r['category'] not in get_knowledge().categories or r['priority'] not in ('p0','p1','p2','p3') or type(r['needs_human']) is not bool:
                raise ValueError('Incomplete or invalid labels')
            if get_knowledge().categories[r['category']]['kind']!=kinds[cid]:raise ValueError('Category kind mismatch')
            if type(r.get('draft_quality_1_to_5')) is not int or not 1<=r['draft_quality_1_to_5']<=5 or type(r.get('citations_supported')) is not bool or type(r.get('no_unfounded_promises')) is not bool:
                raise ValueError('Draft quality ratings required')
        ratings.append((value['rater_id'],mapping))
    if ratings[0][0]==ratings[1][0]:raise ValueError('Distinct raters required')
    return {f:kappa([ratings[0][1][cid][f] for cid in ids],[ratings[1][1][cid][f] for cid in ids]) for f in (*FIELDS,'draft_quality_1_to_5','citations_supported','no_unfounded_promises')}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['report','prepare-gold','score-gold'])
    p.add_argument('--output',type=Path,required=True);p.add_argument('--packet',type=Path)
    p.add_argument('--rater-a',type=Path);p.add_argument('--rater-b',type=Path)
    args=p.parse_args()
    dev=ROOT/'outputs/handout-2026-10-27/returned-dev-v2/dev.approved.jsonl'
    if args.command=='prepare-gold':result=prepare_gold(checked_rows(dev),args.output)
    elif args.command=='score-gold':result=score_gold(args.packet,args.rater_a,args.rater_b)
    else:
        control=ROOT/'outputs/handout-2026-10-27/control-assistant/control.assistant.jsonl'
        from support_copilot.category_model import backend_metadata
        result={'implementation_sha256':implementation_hash(),'source_hashes':get_knowledge().hashes,'category_backend':backend_metadata(),
                'dev':metrics(checked_rows(dev)),'control_silver':metrics(checked_rows(control)),
                'retrieval':retrieval_metrics(),'gold_kappa':None,
                'limitations':['Dev was used for tuning; bootstrap interval is conditional on this selected sample.',
                'Control labels are assistant silver; not independent human gold.','Gold ratings still pending.']}
    if args.command!='prepare-gold':
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result if args.command=='prepare-gold' else {'output':str(args.output)}))

if __name__=='__main__':main()
