"""Known defects of the rule core, pinned as minimal invented positive/negative pairs.

Each pair: the first text shows the rule working, the second a minimal change it gets wrong.
Remaining defects are xfail(strict=True); fixed cases are ordinary tests. A strict xfail
turns green-to-red when another defect is fixed, so it can be promoted to a normal test.
Texts are invented for this file and were not taken from dev/control tickets or their aggregates.
"""
import pytest

from support_copilot import service
from support_copilot.safety import assess, safety_view
from support_copilot.schemas import TriageRequest


def category(text, kind='complaint'):
    request = TriageRequest(kind=kind, messages=[{'role': 'player', 'text': text}])
    view = safety_view(text)
    topics = {key for key in service.PATTERNS if next(service._topic_matches(key, view), None) is not None}
    topics |= assess(request).topics
    return service.choose_category(kind, view, topics)[0]


@pytest.mark.parametrize('kind,text,expected', [
    ('complaint', 'DB https://example.com/evidence', 'complaint.dm'),
    ('report', 'DB', 'report.complaint'),
    ('report', 'Не работает DB сервера', 'report.help'),
    ('complaint', "Noto‘g‘ri ban berilgan", 'complaint.admin'),
    ('report', "Noto'g'ri blok qo'yilgan", 'report.complaint'),
])
def test_game_shorthand_and_uzbek_appeal(kind, text, expected):
    assert category(text, kind) == expected


@pytest.mark.parametrize('kind,text,expected', [
    ('complaint', 'Игрок Non RP нарушает ролевую ситуацию', 'complaint.dm'),
    ('complaint', 'Он ведет себя нон-рп', 'complaint.dm'),
    ('report', 'Он NonRP нарушает правила', 'report.complaint'),
    ('report', 'Что такое Non RP?', 'report.question'),
    ('report', 'Что значит DB?', 'report.question'),
    ('complaint', 'В присутствии админа игрок убил меня', 'complaint.dm'),
    ('complaint', 'На глазах у модератора игрок оскорбил меня', 'complaint.insult'),
    ('complaint', 'В присутствии админа другой админ оскорбил меня', 'complaint.admin'),
    ('complaint', 'Администратор оскорбил меня', 'complaint.admin'),
    ('complaint', 'Моя организация nonprofit ждет ответа', 'complaint.other'),
])
def test_taxonomy_rp_scope_and_administrator_witness(kind, text, expected):
    assert category(text, kind) == expected


DEFECTS = [
    # D1. Any admin mention next to an offence makes the admin the offender (ADMIN_ACTOR on staff_view).
    ('admin_witness', 'complaint', 'Bad_Boy оскорбил меня в чате', 'Bad_Boy оскорбил меня в чате, админ это видел',
     'complaint.insult'),
    # D2. Context-free APPEAL words («несправедливо», «ошибочно») win over a clear offence.
    ('bare_appeal_word', 'complaint', 'Bad_Boy убил меня без причины у банка',
     'Несправедливо: Bad_Boy убил меня без причины у банка', 'complaint.dm'),
    ('bare_appeal_word_typo', 'complaint', 'Bad_Boy убил меня у банка',
     'Ошибочно указал сервер, Bad_Boy убил меня у банка', 'complaint.dm'),
    # D3. Asking to punish someone else is read as an admin complaint when phrased outside STAFF_ADDRESS.
    ('punish_request', 'complaint', 'Bad_Boy флудит в чате, примите меры', 'Bad_Boy флудит в чате, прошу бан ему',
     'complaint.other'),
    ('punish_passive', 'complaint', 'Bad_Boy флудит в чате, примите меры', 'Bad_Boy флудит в чате, он должен быть наказан',
     'complaint.other'),
    # D4. Reports: questions without как/где/можно/сколько are offtopic; a trailing «?» is ignored.
    ('question_without_keyword', 'report', 'Как получить лицензию?', 'Подскажите стоимость лицензии', 'report.question'),
    ('question_mark_only', 'report', 'Где будет обновление?', 'Когда будет обновление?', 'report.question'),
    # D5. Help needs one of a closed list of «не + verb» forms.
    ('help_closed_verb_list', 'report', 'Не открывается меню настроек', 'Персонаж не двигается после гаража', 'report.help'),
    # D6. A rule question that names a punishment becomes a complaint (PUNISHMENT anywhere).
    ('rule_question_with_punishment', 'report', 'Как получить права на мотоцикл?', 'Сколько длится мут за флуд?',
     'report.question'),
    # D7. Mixed-script obfuscation beyond a/c/e/o/p/x/y defeats the dictionaries (safety_view map).
    ('confusable_latin_m', 'complaint', 'Он материт в чате', 'Он Mатерит в чате', 'complaint.insult'),
    ('digit_for_letter', 'complaint', 'Он оскорбил меня', 'Он 0скорбил меня', 'complaint.insult'),
]


@pytest.mark.parametrize('name,kind,working,_broken,expected', DEFECTS, ids=[d[0] for d in DEFECTS])
def test_rule_handles_the_working_half_of_the_pair(name, kind, working, _broken, expected):
    assert category(working, kind) == expected


FIXED = {'admin_witness', 'bare_appeal_word', 'bare_appeal_word_typo',
         'punish_request', 'question_without_keyword', 'help_closed_verb_list',
         'punish_passive', 'question_mark_only', 'rule_question_with_punishment',
         'confusable_latin_m', 'digit_for_letter'}


@pytest.mark.parametrize('name,kind,_working,broken,expected', [
    pytest.param(*case, id=case[0], marks=[] if case[0] in FIXED else
                 [pytest.mark.xfail(strict=True, reason='Known rule defect')])
    for case in DEFECTS
])
def test_rule_defect_on_the_minimal_change(name, kind, _working, broken, expected):
    assert category(broken, kind) == expected


def test_nickname_plus_greeting_is_not_a_complaint_description_known_gap():
    # D8 (documented, ambiguous policy): a nickname plus any word is enough for complaint.other.
    assert category('Bad_Boy') == 'complaint.invalid'
    assert category('Bad_Boy привет') == 'complaint.other'


def test_negated_payment_or_account_still_takes_mandatory_category_by_design():
    # Not a defect to fix in the category layer: over-escalation of personal risk is the safety policy.
    assert category('Это не взлом аккаунта, как сменить ник?', 'report') == 'report.account'
