"""Synthetic cases: an observing administrator is not automatically the accused."""
from support_copilot.schemas import TriageRequest
from support_copilot.service import analyze
import pytest


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


@pytest.mark.parametrize('text,expected', [
    ('Bad_Boy флудит, прошу бан ему', 'complaint.other'),
    ('Bad_Boy оскорбляет меня, прошу забанить его', 'complaint.insult'),
    ('Bad_Boy читер, пожалуйста накажите игрока', 'complaint.cheat'),
    ('Меня забанили за читы, прошу снять бан', 'complaint.admin'),
    ('Не согласен с мутом за оскорбления', 'complaint.admin'),
    ('Админ выдал бан без причины', 'complaint.admin'),
    ('Меня забанили за читы, а Bad_Boy оскорбляет, прошу бан ему', 'complaint.admin'),
])
def test_third_party_sanction_request_and_appeals(text, expected):
    request = TriageRequest.model_validate({'kind': 'complaint', 'messages': [
        {'role': 'player', 'text': text}]})
    result = analyze(request)
    assert result.category == expected
    assert result.needs_human
