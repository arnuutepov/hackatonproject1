"""Category selection regressions on fictional texts with a synthetic taxonomy.

These pairs pin down code-level behaviour (word boundaries, negation, multi-topic conflicts,
RU/KK/UZ/translit vocabulary). They are not a quality estimate on real players.
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


def run(text, kind='complaint', *more):
    messages = [{'role': 'player', 'text': item} for item in (text, *more)]
    return service.analyze(TriageRequest(kind=kind, messages=messages))


def category(text, kind='complaint'):
    return run(text, kind).category


# --- Word boundaries: substrings of ordinary words must not create a topic ------------------------

@pytest.mark.parametrize('text,topic', [
    ('Поскольку я новичок, не понимаю систему фракций', 'insult'),   # «оск» inside «поскольку»
    ('Где взять материалы для крафта?', 'insult'),                     # «матер» inside «материалы»
    ('Хочу прочитать правила сервера', 'cheat'),                       # «\bчит» inside «читать»
    ('Как поднять skill стрельбы', 'dm'),                              # «kill» inside «skill»
    ('Дмитрий_Иванов хочет вступить в семью', 'dm'),                   # «\bдм» inside «Дмитрий»
    ('Я сбился с пути, где автосалон?', 'dm'),                         # «сбил» inside «сбился»
    ('Текстурада тұрып қалдым, көмектесіңіз', 'dm'),                   # «ұрып» inside «тұрып»
    ('Көлікті қалай сатып аламын?', 'dm'),                             # «атып» inside «сатып»
])
def test_substrings_of_ordinary_words_do_not_match(text, topic):
    assert topic not in run(text, 'report').detected_topics


@pytest.mark.parametrize('text,expected', [
    ('Москва_Сити сервер, где купить дом?', 'report.question'),
    ('Поскольку обновление вышло, игра вылетает', 'report.help'),
])
def test_false_insult_substring_no_longer_turns_report_into_complaint(text, expected):
    assert category(text, 'report') == expected


# --- Short insult complaints, slang and transliteration ------------------------------------------

@pytest.mark.parametrize('text', [
    'Оск родных', 'упом родни', 'osk rodni', 'Обозвал меня', 'материт в чате',
    'Он написал что я лох', 'Toxic_Guy called me names', 'onamni so`kdi', 'Meni haqorat qildi',
    'Ол мені балағаттады', 'Анамды боқтады',
])
def test_short_insult_complaints_are_insult_not_invalid(text):
    assert category(text) == 'complaint.insult'


# --- DM vocabulary --------------------------------------------------------------------------------

@pytest.mark.parametrize('text', [
    'Застрелил меня у мэрии', 'Убийство без причины возле банка', 'Сбил машиной на парковке',
    'ДБ из машины', 'Ubil bez prichiny', 'Meni otib o`ldirdi', 'Ойыншы мені ұрып өлтірді',
])
def test_dm_variants(text):
    assert category(text) == 'complaint.dm'


# --- Cheat vocabulary vs. crash/texture problems --------------------------------------------------

@pytest.mark.parametrize('text', ['У него софт', 'Игрок с вх видит через стены', 'Chiter Fast_Rider',
                                  'Бессмертный, не умирает от пуль', 'Флай по карте у Sky_Man'])
def test_cheat_variants(text):
    assert category(text) == 'complaint.cheat'


@pytest.mark.parametrize('text', ['Игра вылетает при входе в гараж', 'Провалился под карту у порта',
                                  'Черный экран после загрузки', 'O`yin ishlamayapti', 'Ойын жұмыс істемейді'])
def test_crash_reports_are_help_not_cheat(text):
    result = run(text, 'report')
    assert result.category == 'report.help'
    assert 'cheat' not in result.detected_topics


# --- Scam -----------------------------------------------------------------------------------------

@pytest.mark.parametrize('text', ['Кинул на машину при обмене', 'Развел на деньги и не отдал',
                                  'Meni aldab ketdi', 'Мені алдады, ақшаны қайтармады'])
def test_scam_variants(text):
    assert category(text) == 'complaint.scam'


# --- Admin vs. offence conflicts ------------------------------------------------------------------

@pytest.mark.parametrize('text,expected', [
    # Asking staff to punish someone else is not a complaint about staff.
    ('Читер стреляет через стены, забаньте его', 'complaint.cheat'),
    ('Админ, игрок Bad_Boy оскорбляет в чате', 'complaint.insult'),
    ('Уважаемая администрация, меня убил игрок без причины', 'complaint.dm'),
    ('Админы накажите его, он материт', 'complaint.insult'),
    # The reporter contests their own punishment, or the admin is the offender.
    ('Меня забанили за читы, но я не читер', 'complaint.admin'),
    ('Не согласен с мутом за оскорбления', 'complaint.admin'),
    ('Админ оскорбил меня в репорте', 'complaint.admin'),
    ('Выдали варн ни за что', 'complaint.admin'),
])
def test_admin_conflicts(text, expected):
    assert category(text) == expected


# --- Negation -------------------------------------------------------------------------------------

def test_negated_offence_does_not_choose_category():
    assert category('Я никого не убивал, бан без причины') == 'complaint.admin'
    assert category('Он не читер, просто играет хорошо, а вот оскорбляет постоянно') == 'complaint.insult'
    assert category('Меня убил игрок') == 'complaint.dm'


# --- invalid vs other -----------------------------------------------------------------------------

@pytest.mark.parametrize('text,expected', [
    ('2.1', 'complaint.invalid'),
    ('+', 'complaint.invalid'),
    ('Как получить права?', 'complaint.invalid'),
    ('Нарушает правила', 'complaint.other'),
    ('Bad_Boy нонрп', 'complaint.other'),
    ('Bad_Boy мешал проехать', 'complaint.other'),
    ('Bad_Boy', 'complaint.invalid'),
    ('Как можно так играть, он постоянно мешает проезду у автосалона', 'complaint.other'),
])
def test_invalid_is_reserved_for_empty_or_question_like_complaints(text, expected):
    assert category(text) == expected


# --- Reports: complaint intent, plain admin mentions ----------------------------------------------

@pytest.mark.parametrize('text,expected', [
    ('Хочу пожаловаться на игрока Night_Wolf, нарушает правила', 'report.complaint'),
    ('Игрок Night_Wolf флудит, примите меры', 'report.complaint'),
    ('Админ выдал бан без причины', 'report.complaint'),
    ('Как снять бан?', 'report.complaint'),
    ('Прошу пересмотреть блокировку', 'report.complaint'),
    ('Админ, помогите, застрял в текстурах', 'report.help'),
    ('Как стать админом?', 'report.question'),
    ('Админ ответь', 'report.offtopic'),
    ('Привет всем', 'report.offtopic'),
    ('Я был афк', 'report.offtopic'),
    ('Админ дай денег', 'report.offtopic'),
])
def test_report_complaint_intent(text, expected):
    assert category(text, 'report') == expected


# --- Mandatory categories and escalation stay first -----------------------------------------------

@pytest.mark.parametrize('text,expected', [
    ('Оплатил донат, а меня еще и оскорбили', 'report.payment'),
    ('Взломали аккаунт, админ не отвечает', 'report.account'),
])
def test_payment_and_account_keep_precedence(text, expected):
    result = run(text, 'report')
    assert result.category == expected
    assert result.needs_human and result.priority == 'p0'


def test_admin_mention_keeps_escalation_even_when_category_is_not_complaint():
    result = run('Админ ответь', 'report')
    assert 'admin' in result.detected_topics
    assert result.needs_human


def test_existing_wave2_and_wave7_expectations_hold():
    for text, kind, expected, priority in [
        ('Оплатил донат, деньги списались', 'report', 'report.payment', 'p0'),
        ('Взломали аккаунт', 'report', 'report.account', 'p0'),
        ('Не согласен с наказанием', 'complaint', 'complaint.admin', 'p2'),
        ('Админ выдал бан без причины', 'report', 'report.complaint', 'p2'),
        ('Читер стреляет через стены', 'complaint', 'complaint.cheat', 'p1'),
        ('Убил без причины', 'complaint', 'complaint.dm', 'p2'),
        ('Застрял в текстурах', 'report', 'report.help', 'p2'),
        ('Как получить лицензию?', 'report', 'report.question', 'p3'),
        ('ДМ у банка', 'complaint', 'complaint.dm', 'p2'),
        ('Как купить дом у банка?', 'report', 'report.question', 'p3'),
        ('После обновления игра вылетает при входе в гараж, сервер 11.', 'report', 'report.help', 'p2'),
        ('Игрок летает по карте', 'complaint', 'complaint.cheat', 'p1'),
    ]:
        result = run(text, kind)
        assert (result.category, result.priority) == (expected, priority), text


@pytest.mark.parametrize('text,kind,expected', [
    ('Хочу пересмотреть запись', 'report', 'report.offtopic'),
    ('Как пересмотреть видеозапись?', 'report', 'report.question'),
    ('Прошу пересмотреть блокировку', 'report', 'report.complaint'),
    ('Прошу пересмотреть наказание', 'complaint', 'complaint.admin'),
])
def test_rewatching_a_recording_is_not_a_punishment_appeal(text, kind, expected):
    assert category(text, kind) == expected


@pytest.mark.parametrize('text,expected', [
    ('Вопрос по установке софта', 'report.offtopic'),
    ('Где скачать софт для компьютера?', 'report.question'),
    ('Как обновить обычный soft?', 'report.question'),
])
def test_ordinary_software_requests_are_not_cheating(text, expected):
    result = run(text, 'report')
    assert result.category == expected
    assert 'cheat' not in result.detected_topics
    assert result.priority == 'p3'


def test_software_context_does_not_hide_independent_cheat_evidence():
    result = run('Игрок скачал софт, теперь использует читы и стреляет через стены')
    assert result.category == 'complaint.cheat'
    assert result.priority == 'p1'


@pytest.mark.parametrize('text', ['Zastrial u bolnicy', 'Pomogite, zastral v stene'])
def test_transliterated_help_keeps_human_review(text):
    request = TriageRequest(kind='report', messages=[{'role': 'player', 'text': text}])
    result = service.triage(request, use_configured_provider=False)
    assert result.lang == 'ru'
    assert result.category == 'report.help'
    assert result.needs_human
    assert result.confidence_level == 'low'
    assert 'transliterated_input_requires_review' in result.reasons
    assert result.draft_reply.action == 'human_review'
