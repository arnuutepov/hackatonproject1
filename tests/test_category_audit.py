"""Synthetic cases: an observing administrator is not automatically the accused."""
from support_copilot.schemas import TriageRequest
from support_copilot.service import analyze


def test_admin_witness_does_not_override_player_insult():
    request = TriageRequest.model_validate({'kind': 'complaint', 'messages': [
        {'role': 'player', 'text': 'Bad_Boy оскорбил меня в чате, админ это видел'}]})
    result = analyze(request)
    assert result.category == 'complaint.insult'
    assert result.needs_human


def test_unfairness_does_not_turn_player_complaint_into_appeal():
    request = TriageRequest.model_validate({'kind': 'complaint', 'messages': [
        {'role': 'player', 'text': 'Несправедливо: Bad_Boy убил меня без причины у банка'}]})
    result = analyze(request)
    assert result.category == 'complaint.dm'
    assert result.needs_human
