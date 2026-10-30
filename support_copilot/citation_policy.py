"""Conservative relevance gate, distinct from ID lookup and exact quotation."""
import re

from support_copilot.knowledge import Rule, normalize, tokens

STOP = {'правил','правила','игрок','игрока','игроки','пункт','номер','можно','какой','такой',
        'пожал','меня','этого','этот','того','игрово','сервер','данный','сообще','обраще','являет','находи'}
SUBJECTS = {
    'insult': r'оскорб|расизм|ксенофоб|токсич|матер',
    'cheat': r'\bчит|макрос|автоклик|скрипт|сторонн[^.]{0,45}(?:обеспеч|программ|\bпо\b)',
    'dm': r'убийств|убив|урон|deathmatch|\bdm\b|\bдм\b',
    'scam': r'мошеннич|обман|украден|краж',
    'rmt_ads': r'реклам|реальн[^.]{0,25}день|продаж[^.]{0,25}аккаунт',
    'account': r'аккаунт|учетн|доступ|парол',
    'payment': r'донат|платеж|оплат|платежн',
    'admin': r'обжал|апелляц|жалоб[^.]{0,50}админ|админ[^.]{0,50}жалоб',
}
SECTION_CONTEXT = {
    '31': r'жалоб|обжал|доказатель|апелляц', '32': r'зелен|\bgz\b|\bгз\b|\bзз\b',
    '33': r'лидер|заместит', '34': r'полиц|мвд|фсб|армия|мчс|больниц|госорган',
    '35': r'департамент|гос[^.]{0,20}волн', '36': r'семейн[^.]{0,25}меропр',
    '40': r'\bопг\b|банд|мафи', '41': r'бизнес', '42': r'фургон',
}


def relevant(category: str, detected_topics: list[str], query: str, rule: Rule, quote: str | None = None) -> bool:
    if not rule.ref:
        return False
    q, body = normalize(query), normalize(quote if quote is not None else rule.text)
    if rule.section_id != '30':
        requirement = SECTION_CONTEXT.get(rule.section_id)
        if not requirement or not re.search(requirement, q):
            return False
    topics = set(detected_topics) & SUBJECTS.keys()
    suffix = category.split('.')[-1]
    if suffix in SUBJECTS:
        topics = {suffix}
    # A generic common word or copied point number does not establish relevance.
    if not topics or not any(re.search(SUBJECTS[topic], body) for topic in topics):
        return False
    words = set(tokens(q)) - STOP
    return bool(words & (set(tokens(body)) - STOP))
