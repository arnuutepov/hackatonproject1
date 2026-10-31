"""Synthetic cases: an observing administrator is not automatically the accused."""
from support_copilot.schemas import TriageRequest
from support_copilot.service import analyze, triage
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


@pytest.mark.parametrize('text,expected', [
    ('Персонаж не двигается после гаража', 'report.help'),
    ('Машина не едет после загрузки', 'report.help'),
    ('Подскажите стоимость лицензии', 'report.question'),
    ('Скажите мне цену машины', 'report.question'),
    ('Админ дай денег', 'report.offtopic'),
    ('Хочу пересмотреть запись', 'report.offtopic'),
    ('Оплатил донат, персонаж не двигается', 'report.payment'),
    ('Не могу войти в аккаунт, подскажите стоимость лицензии', 'report.account'),
    ('Подскажите как обжаловать бан', 'report.complaint'),
])
def test_report_help_and_question_with_sensitive_precedence(text, expected):
    request = TriageRequest.model_validate({'kind': 'report', 'messages': [
        {'role': 'player', 'text': text}]})
    result = triage(request, use_configured_provider=False)
    assert result.category == expected
    if expected in {'report.payment', 'report.account', 'report.complaint', 'report.question'}:
        assert result.needs_human


@pytest.mark.parametrize('text,kind,expected', [
    ('Он Mатерит в чате', 'complaint', 'complaint.insult'),
    ('Он 0скорбил меня', 'complaint', 'complaint.insult'),
    ('Сколько длится мут за флуд?', 'report', 'report.question'),
    ('Меня замутили, сколько длится мут за флуд?', 'report', 'report.complaint'),
    ('Bad_Boy флудит, он должен быть наказан', 'complaint', 'complaint.other'),
])
def test_remaining_defects_keep_original_and_human_review(text, kind, expected):
    request = TriageRequest.model_validate({'kind': kind, 'messages': [
        {'role': 'player', 'text': text}]})
    result = triage(request, use_configured_provider=False)
    assert result.category == expected
    assert result.needs_human
    assert request.messages[0].text == text


def test_matching_view_does_not_rewrite_player_identifiers():
    from support_copilot.safety import safety_view
    assert safety_view('Ник Player_0123 и 0123') == 'ник player_0123 и 0123'


@pytest.mark.parametrize('text', ['Персонаж не двигается после гаража', 'Машина не едет после загрузки'])
def test_movement_problem_is_not_assumed_resolved(text):
    request = TriageRequest.model_validate({'kind': 'report', 'messages': [{'text': text}]})
    result = triage(request, use_configured_provider=False)
    assert result.category == 'report.help'
    assert result.needs_human
    assert 'movement_issue_requires_review' in result.reasons
