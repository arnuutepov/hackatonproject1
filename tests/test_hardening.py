"""Real-life threats, nickname/server extraction and input-size bound on fictional texts.

Code-level regressions with a synthetic taxonomy; not a quality estimate on real players.
"""
import json

import pytest

from support_copilot import service
from support_copilot.knowledge import Knowledge
from support_copilot.schemas import TriageRequest


@pytest.fixture(autouse=True)
def synthetic_knowledge(monkeypatch, tmp_path):
    folder = tmp_path / 'knowledge'
    folder.mkdir()
    (folder / 'rules.json').write_text(json.dumps({'rules': [{'id': '30', 'category': 'Общие', 'points': [
        {'ref': '1.1', 'text': 'Оскорбления игроков запрещены'}, {'ref': '1.2', 'text': 'Читы запрещены'}]}]}),
        encoding='utf-8')
    codes = [('report', code) for code in ('payment', 'account', 'complaint', 'help', 'question', 'offtopic')]
    codes += [('complaint', code) for code in ('admin', 'cheat', 'scam', 'rmt_ads', 'insult', 'dm', 'invalid', 'other')]
    (folder / 'taxonomy.json').write_text(json.dumps({
        'version': 'synthetic', 'needs_human_always': ['report.payment', 'report.account'],
        'priorities': [{'code': f'p{level}', 'title': f'P{level}'} for level in range(4)],
        'categories': [{'code': f'{kind}.{code}', 'kind': kind, 'title': code, 'rules': []} for kind, code in codes]}),
        encoding='utf-8')
    value = Knowledge(folder)
    monkeypatch.setattr(service, 'get_knowledge', lambda: value)
    return value


def run(text, kind='complaint'):
    return service.analyze(TriageRequest(kind=kind, messages=[{'role': 'player', 'text': text}]))


# --- Real-life threats are p0 whatever the verb form ----------------------------------------------

@pytest.mark.parametrize('text', [
    'Я убью тебя в реале, сказал он',
    'Он угрожает убить меня в реальной жизни',
    'Он мне сказал что убьёт меня в реале',
    'Грозится найти меня и убить в реале',
    'He said he will kill me IRL',
    'Он пишет что приедет ко мне домой и убьёт',
    'В реале я тебя найду и изобью',
    'Mene haqiqiy hayotda o‘ldiraman dedi',
])
def test_real_life_threat_is_p0_and_human(text):
    result = run(text)
    assert result.priority == 'p0'
    assert result.needs_human


@pytest.mark.parametrize('text', [
    'Найду читера и убью его на сервере',          # in-game revenge talk
    'Он убил меня три раза подряд',                 # past tense, in game
    'Хочу убить время, в жизни такого не видел',    # idiom
    'He kills everyone, great skill in real life games',
    'Меня убили, пошёл домой спать',
    'Как убить моба в реальном времени?',
])
def test_in_game_violence_is_not_p0(text):
    assert run(text).priority != 'p0'


# --- Nickname extraction ---------------------------------------------------------------------------

@pytest.mark.parametrize('text,expected', [
    ('Ник Alex_North, сервер №12', 'Alex_North'),               # Latin form unchanged
    ('Игрок Вася_Пупкин мешает на сервере 4', 'Вася_Пупкин'),   # Cyrillic after a marker word
    ('Админ Иван_Петров забанил меня без причины', 'Иван_Петров'),
    ('ник: NightRider77 читерит', 'NightRider77'),
    ('Москва_Сити сервер, где купить дом?', None),               # a place, no marker
    ('Дмитрий_Иванов хочет вступить в семью', None),
    ('ник был плохой', None),
])
def test_nickname_extraction(text, expected):
    assert service.extract_nickname(text) == expected


# --- Server extraction -----------------------------------------------------------------------------

@pytest.mark.parametrize('text,expected', [
    ('Ник Alex_North, сервер №12', '12'),
    ('на сервере 4', '4'),
    ('Server4', '4'),
    ('server12', '12'),
    ('на 4 сервере читер', '4'),
    ('на 4-м сервере читер', '4'),
    ('с 12 сервера', '12'),
    ('serverda 7 da cheat', '7'),
    ('серверде 5 читер', '5'),
    ('сервер, где купить дом?', None),
    ('4 часа на сервере', None),
])
def test_server_extraction(text, expected):
    assert service.server_number(text) == expected


def test_conflicting_servers_in_either_word_order_are_flagged():
    result = run('Это было на сервере 4, а потом на 5 сервере')
    assert 'conflicting_observable_fields' in result.safety_flags
    assert result.extracted.server is None


def test_same_server_in_both_word_orders_is_not_a_conflict():
    result = run('На 4 сервере и на сервере 4 читер')
    assert 'conflicting_observable_fields' not in result.safety_flags
    assert result.extracted.server == '4'


# --- Input-size bound ------------------------------------------------------------------------------

def test_bound_request_limits_total_text_and_keeps_original():
    request = TriageRequest(kind='complaint', messages=[{'role': 'player', 'text': 'а' * 20000}] * 100)
    bounded, truncated = service.bound_request(request)
    assert truncated
    assert sum(len(m.text) for m in bounded.messages) <= service.MAX_ANALYSIS_CHARS
    assert len(request.messages[0].text) == 20000


def test_small_request_is_untouched():
    request = TriageRequest(kind='report', messages=[{'role': 'player', 'text': 'Привет'}])
    bounded, truncated = service.bound_request(request)
    assert bounded is request and not truncated


def test_oversized_request_is_analyzed_and_handed_to_a_human():
    request = TriageRequest(kind='report', messages=[{'role': 'player', 'text': 'Как пройти квест? ' * 1000}] * 100)
    result = service.analyze(request)
    assert result.needs_human
    assert 'input_truncated_for_analysis' in result.reasons
