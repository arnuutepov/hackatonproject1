"""Wave 7 fixes found while running the demo on new synthetic examples."""
import json
from pathlib import Path

from support_copilot.schemas import TriageRequest
from support_copilot.service import triage
from support_copilot.submission_check import DEMO_CASES, invariants
from support_copilot.knowledge import get_knowledge

ROOT = Path(__file__).resolve().parents[1]


def run(text, kind='report'):
    return triage(TriageRequest(kind=kind, messages=[{'role': 'player', 'text': text}]),
                  use_configured_provider=False)


def test_crash_is_not_cheating():
    result = run('После обновления игра вылетает при входе в гараж, сервер 11.')
    assert 'cheat' not in result.detected_topics
    assert result.category == 'report.help'
    assert run('Игрок летает по карте', 'complaint').category == 'complaint.cheat'


def test_server_number_in_russian_case_forms():
    for text in ('Меня оскорбили на сервере 4', 'Игрок с сервера 4 оскорбил', 'Сервер №4, оскорбили'):
        assert run(text, 'complaint').extracted.server == '4'


def test_inflected_server_conflict_still_detected():
    request = TriageRequest(kind='report', messages=[
        {'role': 'player', 'text': 'Застрял на сервере 12'}, {'role': 'player', 'text': 'Нет, на сервере 15'}])
    result = triage(request, use_configured_provider=False)
    assert result.extracted.server is None
    assert 'conflicting_observable_fields' in result.safety_flags
    assert result.needs_human


def test_demo_scenarios_and_invariants():
    rules = set(get_knowledge().rules)
    lines = (ROOT / 'examples/wave7_demo.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(lines) == len(DEMO_CASES)
    for line, (name, _, expectation) in zip(lines, DEMO_CASES):
        response = json.loads(triage(TriageRequest.model_validate_json(line),
                                     use_configured_provider=False).model_dump_json())
        assert all(expectation(response).values()), (name, expectation(response))
        assert all(invariants(response, rules).values()), name


def test_new_examples_keep_safety_invariants():
    rules = set(get_knowledge().rules)
    for line in (ROOT / 'examples/wave7_new.jsonl').read_text(encoding='utf-8').splitlines():
        response = json.loads(triage(TriageRequest.model_validate_json(line),
                                     use_configured_provider=False).model_dump_json())
        assert all(invariants(response, rules).values()), line
