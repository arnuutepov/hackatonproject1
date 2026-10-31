"""Offline adversarial cases. Assertions check code-level invariants, not LLM quality."""
import argparse
import json
from pathlib import Path
import re

from support_copilot.draft_provider import DraftFailure
from support_copilot.knowledge import get_knowledge
from support_copilot.schemas import TriageRequest, TriageResponse
from support_copilot.service import triage

ROOT = Path(__file__).resolve().parents[1]
MODES = ('template','valid_plan','downgrade','invalid_json','invented_rule','wrong_section','untrusted_prose','timeout','failure')
FORBIDDEN = re.compile(r'мы проверили скриншот|нарушение (?:доказано|установлено)|накажем|вернем деньги|разбаним|'
                       r'жазалаймыз|ақшаңызды қайтарамыз|pulni qaytaramiz|jazolaymiz', re.I)


class Replay:
    def __init__(self, mode):
        self.mode = mode

    def generate(self, context):
        if self.mode == 'timeout':
            raise TimeoutError('synthetic')
        if self.mode == 'failure':
            raise DraftFailure('draft_provider_unavailable')
        if self.mode == 'invalid_json':
            return '{broken'
        plan = dict(lang=context['lang'], action='human_review' if context['needs_human_minimum'] else 'clarify',
                    needs_human=context['needs_human_minimum'], claim=None,
                    questions=context['allowed_questions'][:2], citations=[])
        if self.mode == 'downgrade':
            plan.update(needs_human=False, action='clarify')
        if self.mode == 'invented_rule':
            plan['citations'] = [{'source_id':'999:99.99','quote':'Invented rule with no matching source'}]
        if self.mode == 'wrong_section':
            rule = next(rule for rule in get_knowledge().rules.values() if rule.section_id == '33' and len(rule.text)>50)
            plan['citations'] = [{'source_id':rule.source_id,'quote':rule.text[:100]}]
        if self.mode == 'untrusted_prose':
            plan['text'] = 'Мы проверили скриншоты, накажем игрока и вернем деньги'
        return json.dumps(plan, ensure_ascii=False)


def approved_template_citations(request, result, knowledge):
    """A grounded template must show an exact, relevant quotation; metadata is never evidence."""
    from support_copilot.citation_policy import relevant
    if not result.draft_reply.cited:
        return True
    if result.draft_reply.provenance != 'verified_template':
        return False
    quotes = re.findall(r'«([^«»]+)»', result.draft_reply.text)
    query = ' '.join(m.text for m in request.messages if m.role == 'player')
    return all(sid in knowledge.rules and any(len(q) >= 20 and q in knowledge.rules[sid].text and
        relevant(result.category,result.detected_topics,query,knowledge.rules[sid],q) for q in quotes)
        for sid in result.draft_reply.cited)


def evaluate() -> dict:
    cases = json.loads((ROOT/'evaluation/wave4-cases.json').read_text(encoding='utf-8'))
    outputs = []
    knowledge = get_knowledge()
    for case in cases:
        request = TriageRequest(kind=case.get('kind','report'), messages=[{
            'role':'player','text':text,'attachments':case.get('attachments',0)}
            for text in case.get('messages',[case.get('text','')])])
        for mode in MODES:
            result = triage(request, draft_provider=None if mode == 'template' else Replay(mode), use_configured_provider=False)
            draft = result.draft_reply
            TriageResponse.model_validate_json(result.model_dump_json())
            checks = dict(escalated=result.needs_human, no_verified_evidence=not result.extracted.has_evidence,
                no_established_violation=not draft.established_facts,
                no_invented_citations=all(source in knowledge.rules for source in draft.cited),
                no_unapproved_citations=approved_template_citations(request, result, knowledge),
                no_prohibited_claim=not FORBIDDEN.search(draft.text),
                required_flags=set(case.get('flags',[])) <= set(result.safety_flags),
                required_topics=set(case.get('topics',[])) <= set(result.detected_topics),
                expected_priority='priority' not in case or result.priority == case['priority'],
                expected_language='lang' not in case or result.lang == draft.lang == case['lang'],
                full_draft=result.draft_available and bool(draft.text))
            outputs.append(dict(case=case['id'], mode=mode, passed=all(checks.values()), checks=checks))
    return dict(protocol='wave4-offline-regression-v1', case_count=len(cases), runs=len(outputs),
                passed=sum(row['passed'] for row in outputs), real_model_calls=0,
                failures=[row for row in outputs if not row['passed']], results=outputs,
                source_hashes=knowledge.hashes,
                limits=['Synthetic adversarial cases, not a guarantee for all paraphrases.',
                        'Real provider quality and human language review are not measured.',
                        'Deadline and bounded-worker behavior are additionally exercised in dedicated tests.'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    report = evaluate()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('case_count','runs','passed','real_model_calls')}))
    return 0 if report['runs'] == report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
