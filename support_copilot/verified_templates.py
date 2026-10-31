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


def grounded_complaint(request, result, knowledge):
    """Quote a relevant prohibition as a candidate for human review, never as a verdict."""
    from support_copilot.rule_question import RULES_SHA256
    from support_copilot.drafting import build_context, validate_plan, render, topic_for
    from support_copilot.draft_provider import DraftFailure
    import json
    if (len(request.messages) != 1 or request.messages[0].role != 'player' or
        result.lang != 'ru' or result.safety_flags or not result.needs_human or
        result.category not in {'complaint.insult','complaint.cheat','complaint.dm','complaint.scam','complaint.rmt_ads'} or
        any(r.status != 'resolved' for r in result.reference_resolutions) or
        knowledge.hashes.get('rules') != RULES_SHA256):
        return None
    context, truncated = build_context(request, result, knowledge)
    if truncated:
        return None
    for source in context['rules']:
        if not source['source_id'].startswith('30:'):
            continue
        if result.reference_resolutions and any(source['source_id'] not in r.candidates for r in result.reference_resolutions):
            continue
        # Only quote a complete source sentence before any separate sanction list.
        quote = re.split(r'(?<=[.!?])\s+', source['text'].split('|',1)[0], maxsplit=1)[0].strip()
        if (not quote.endswith(('.', '!', '?')) or not 20 <= len(quote) <= 600 or
            not re.search('запрещ', normalize(quote)) or
            re.search(r'наказ|\bбан\w*|\bмут\w*|получит|вернем|возврат|разбан', normalize(quote))):
            continue
        plan = dict(lang='ru', action='rule_review', needs_human=True, claim=None,
                    questions=context['allowed_questions'][:2],
                    citations=[dict(source_id=source['source_id'], quote=quote)])
        try:
            approved = validate_plan(json.dumps(plan, ensure_ascii=False), context, knowledge)
        except DraftFailure:
            continue
        draft = render(approved, result, topic_for(result), provenance='verified_template')
        draft.text += '\n\n«' + quote + '»'
        return draft
    return None
