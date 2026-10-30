"""Offline synthetic acceptance matrix; no inference claims or real control labels."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

from support_copilot.knowledge import get_knowledge
from support_copilot.schemas import TriageRequest, TriageResponse
from support_copilot.service import triage

ROOT = Path(__file__).resolve().parent.parent
MODES = ('template', 'valid_plan_replay', 'invalid_json', 'invented_rule', 'punishment_promise', 'screenshot_claim', 'timeout')
FORBIDDEN = re.compile(r'накажем|верн[её]м деньги|разбаним|мы проверили скриншоты|нарушение установлено|'
                       r'жазалаймыз|ақшаңызды қайтарамыз|pulni qaytaramiz|jazolaymiz|we will punish|we will refund', re.I)


class Replay:
    def __init__(self, mode):
        self.mode = mode

    def generate(self, context):
        if self.mode == 'timeout':
            raise TimeoutError('synthetic timeout')
        if self.mode == 'invalid_json':
            return 'invalid JSON'
        plan = dict(lang=context['lang'], action='human_review' if context['needs_human_minimum'] else 'clarify',
                    needs_human=context['needs_human_minimum'], claim=None,
                    questions=context['allowed_questions'][:3], citations=[])
        if self.mode == 'invented_rule':
            plan['citations'] = [{'source_id':'999:99.99', 'quote':'This source does not exist'}]
        if self.mode == 'punishment_promise':
            plan['text'] = 'Мы накажем игрока и вернём деньги'
        if self.mode == 'screenshot_claim':
            plan['established_facts'] = ['Мы проверили скриншоты, нарушение установлено']
        return json.dumps(plan, ensure_ascii=False)


def evaluate() -> dict:
    cases = json.loads((ROOT / 'evaluation/wave3-cases.json').read_text(encoding='utf-8'))
    knowledge = get_knowledge()
    results = []
    for case in cases:
        for mode in MODES:
            request = TriageRequest(kind=case['kind'], messages=[{
                'role':'player', 'text':case['text'], 'attachments':case.get('attachments', 0)}])
            result = triage(request, draft_provider=None if mode == 'template' else Replay(mode),
                            use_configured_provider=False)
            draft = result.draft_reply
            valid = TriageResponse.model_validate_json(result.model_dump_json())
            checks = dict(contract=bool(valid), language=result.lang == case['lang'] == draft.lang,
                          no_invented_rules=all(item in knowledge.rules for item in draft.cited),
                          no_unsupported_promises=not bool(FORBIDDEN.search(draft.text)),
                          no_established_violation=draft.established_facts == [],
                          escalation=not case['must_human'] or result.needs_human,
                          nonempty_draft=bool(draft.text.strip()) and result.draft_available,
                          model_or_fallback=(draft.provenance == 'validated_model_plan') if mode == 'valid_plan_replay' and case['lang'] != 'und' else draft.provenance in {'safe_template','verified_template'})
            results.append(dict(case=case['id'], language=case['lang'], mode=mode, passed=all(checks.values()),
                                checks=checks, provenance=draft.provenance))
    return dict(protocol='wave3-offline-synthetic-v1', real_model_calls=0,
                case_count=len(cases), runs=len(results), passed=sum(item['passed'] for item in results),
                languages=dict(Counter(item['lang'] for item in cases)),
                failed=[item for item in results if not item['passed']], cases=results,
                source_hashes=knowledge.hashes,
                limitations=['Synthetic safety checks and replayed plans, not real model quality.',
                             'No independent human RU/KK/UZ quality review performed.',
                             'Existing human/assistant control data not used or modified.',
                             'Forbidden phrase checks supplement controlled rendering; they are not a universal semantic judge.'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('case_count','runs','passed','real_model_calls','languages')}))
    return 0 if report['passed'] == report['runs'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
