"""Conservative runtime risk checks, independent of model and category selection."""
from dataclasses import dataclass
import re
import unicodedata

from support_copilot.schemas import TriageRequest

_LATIN_TO_CYR = str.maketrans('aceopxy', 'асеорху')
_CYR_TO_LATIN = str.maketrans('асеорху', 'aceopxy')


def safety_view(text: str) -> str:
    """Matching view only; do not repair Kazakh or alter stored/displayed originals."""
    text = unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е')
    text = ''.join(char for char in text if unicodedata.category(char) != 'Cf')
    text = text.translate(str.maketrans({char: "'" for char in 'ʻ’‘ʼ'}))
    def confusable(match):
        word = match.group()
        cyr = len(re.findall(r'[а-я]', word))
        latin = len(re.findall('[a-z]', word))
        if cyr and latin:
            return word.translate(_LATIN_TO_CYR if cyr >= latin else _CYR_TO_LATIN)
        return word
    return re.sub(r'[^\W\d_]+', confusable, text)


RISK = {
    'account': re.compile(r'взлом|аккаунт|акк\b|учет(?:к|н)|доступ|парол|не могу войти|'
                          r'пропал[^\n]{0,50}(?:машин|имущество)|hacked|account|akkaunt|password|login|'
                          r'hisob(?:im|ga|ni)|ҳисоб|бұз(?:ды|ылды)|бузып|кіре алмай|кире алмай|'
                          r'lost[^\n]{0,20}access|cannot log|can.t log|compromised|'
                          r'(?:украл|взлом|увел)[^\n]{0,30}профил'),
    'payment': re.compile(r'донат|плат[е]?ж|оплат|списал|пополнени|payment|paid|donat|refund|chargeback|billing|transaction|'
                          r"to'?lov|төлем|толем|төле(?:дім|ді|нді)|ақша[^\n]{0,25}түсп|(?:карт[а-я]*|банк[а-я]*)[^\n]{0,35}(?:снял|сняли|спис|деньг)|"
                          r'куплен[^\n]{0,20}\bгк\b|pul[^\n]{0,20}(?:tushma|yechil)'),
    'admin': re.compile(r'админ|обжал|апелляц|разбан|не согласен|наказан|'
                        r'\bбан(?:а|у|ом|е|ы|ить|ил[аи]?|ят|ен|или)?\b|забан|'
                        r'\bмут(?:а|ом|е|ы|ить|ил[аи]?|ят)?\b|unban|appeal|admin|ban berd|'
                        r'жаза[^\n]{0,25}(?:келісп|кайта|қайта)|jazoni|\bban(?:ned)?\b|unmute|blokla|блокиров|suspend|бұғат'),
}
INJECTION = re.compile(
    r'игнорир[^\n]{0,60}(?:инструкц|правил|систем)|ignore[^\n]{0,60}(?:instructions|system|rules)|'
    r'needs_human[\s"\x27:=]+false|<(?:\|)?system|\[system\]|'
    r'нұсқау[^\n]{0,30}елеме|ko.rsatmalarni[^\n]{0,35}(?:qoldir|unut)|'
    r'(?:category|priority)[\s"\x27:=]+(?:report\.|complaint\.|p[0-3])')

CITATION_BLOCKERS = frozenset({'contradictory_messages', 'conflicting_observable_fields',
    'instruction_override_attempt', 'mixed_language_requires_review', 'suspected_kazakh_encoding',
    'mixed_sensitive_topics', 'reported_context_contains_risk'})


@dataclass(frozen=True)
class SafetyAssessment:
    topics: frozenset[str]
    flags: frozenset[str]


def language_flags(text: str) -> set[str]:
    view = unicodedata.normalize('NFC', text).casefold()
    flags = set()
    if re.search(r'ќ|кјмектес|к[оө]мектес[иї]н|ќалай|аккаунтќ', view):
        flags.add('suspected_kazakh_encoding')
    markers = {
        'ru': bool(re.search(r'\b(?:помогите|пожалуйста|как|где|меня|оплатил|застрял|получить)\b', view)),
        'kk': bool(re.search(r'[әңөұүһі]|\b(?:қалай|көмектес|калай|комектесиниз)', view)),
        'uz': bool(re.search(r'[ўҳ]|\b(?:yordam|menga|iltimos|qanday|qayer|bering|salom)\b', view)),
        'en': bool(re.search(r'\b(?:please|hello|where|how|my|help)\b', view)),
    }
    if sum(markers.values()) >= 2:
        flags.add('mixed_language_requires_review')
    return flags


def assess(request: TriageRequest) -> SafetyAssessment:
    all_views = [safety_view(message.text) for message in request.messages]
    players = [view for view, message in zip(all_views, request.messages) if message.role == 'player']
    others = [view for view, message in zip(all_views, request.messages) if message.role != 'player']
    text = '\n'.join(players)
    topics = {topic for topic, pattern in RISK.items() if pattern.search(text)}
    flags = language_flags('\n'.join(message.text for message in request.messages if message.role == 'player'))
    if len(topics) >= 2:
        flags.add('mixed_sensitive_topics')
    if any(pattern.search(view) for view in others for pattern in RISK.values()):
        flags.add('reported_context_contains_risk')
    if any(INJECTION.search(view) for view in all_views):
        flags.add('instruction_override_attempt')
    if len(players) >= 2 and re.search(r'ошибся|ошиблась|отзываю жалоб|не было|не убивал|не платил|не оплачивал', text):
        flags.add('contradictory_messages')
    # Opposing claims are uncertainty even when they come from different roles.
    joined = '\n'.join(all_views)
    for positive, negative in [
        (r'оскорбил|оскорбили', r'не оскорб|оскорблений не было'),
        (r'\bубил\b|\bубили\b', r'не убивал|не убил'),
        (r'\bоплатил\b|\bзаплатил\b', r'не платил|не оплачивал')]:
        if re.search(positive, joined) and re.search(negative, joined):
            flags.add('contradictory_messages')
    servers = re.findall(r'(?:сервер|server)\s*(?:№|#|:)?\s*(\d{1,3})\b', text)
    if len(set(servers)) > 1:
        flags.add('conflicting_observable_fields')
    raw = '\n'.join(message.text for message in request.messages if message.role == 'player').casefold()
    if topics and any(pattern.search(text) and not pattern.search(raw) for pattern in RISK.values()):
        flags.add('obfuscated_risk_text')
    return SafetyAssessment(frozenset(topics), frozenset(flags))
