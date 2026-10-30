"""Exact-match, code-reviewed support workflows. No invented game instructions."""
import re
from support_copilot.knowledge import normalize
from support_copilot.schemas import DraftReply, TriageRequest, TriageResponse
from support_copilot.draft_text import TEXT

MATCHES = {
    'застрял в текстурах': ('ru','stuck_ru'),
    'застрял в текстуре': ('ru','stuck_ru'),
    'текстурада тұрып қалдым': ('kk','stuck_kk'),
    "o'yinda qotib qoldim yordam bering": ('uz','stuck_uz'),
    'вы видите скриншоты': ('ru','attachment_visibility_ru'),
    'вы просмотрели скриншоты': ('ru','attachment_visibility_ru'),
}


def match_template(request: TriageRequest, result: TriageResponse) -> str | None:
    if len(request.messages) != 1 or request.messages[0].role != 'player' or result.safety_flags:
        return None
    key = normalize(request.messages[0].text).translate(str.maketrans({char:"'" for char in 'ʻ’‘ʼ'}))
    key = re.sub(r'[!?.,;:]','',key)
    key = ' '.join(key.split())
    item = MATCHES.get(key)
    if not item or result.lang != item[0] or request.kind != 'report':
        return None
    return item[1]


def reply(template: str, result: TriageResponse) -> DraftReply:
    text = TEXT[result.lang]
    if template.startswith('attachment_visibility'):
        paragraphs = ['Содержимое скриншотов здесь недоступно и не проверялось.',
                      'Если запись нужна для жалобы, укажите ссылку, которую сможет проверить сотрудник поддержки.']
        if result.needs_human:
            paragraphs.append(text['human_review'])
    else:
        paragraphs = [text['bug'],text['questions']['bug_steps']]
        if result.extracted.server is None:
            paragraphs.append(text['questions']['server'])
        if result.needs_human:
            paragraphs.append(text['human_review'])
    return DraftReply(lang=result.lang,text='\n\n'.join(paragraphs),cited=[],
                      action='human_review' if result.needs_human else 'clarify',provenance='verified_template')
