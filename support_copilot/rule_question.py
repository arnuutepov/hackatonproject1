"""Narrow, source-verified answer to a general rule question; never a verdict."""
import re
from support_copilot.citation_policy import relevant
from support_copilot.knowledge import normalize
from support_copilot.schemas import DraftReply

# Frozen organizer rules; any update requires a fresh review before auto-candidate.
RULES_SHA256 = '80cd4f920f36283214fad3a748314f51560d1e1c5ee62a00d9214b9c22353be4'
SOURCE_ID = '30:2.1'
QUESTION = re.compile(r'^(?:можно|разрешено) ли оскорблять (?:других )?игроков\s*\??$')


def matches(text):
    return bool(QUESTION.fullmatch(' '.join(normalize(text).split())))


def answer(request, result, knowledge):
    if (request.kind != 'report' or len(request.messages) != 1 or
        request.messages[0].role != 'player' or request.messages[0].attachments or
        not matches(request.messages[0].text) or result.category != 'report.question' or
        result.lang != 'ru' or result.needs_human or result.safety_flags or
        result.reference_resolutions or knowledge.hashes.get('rules') != RULES_SHA256):
        return None
    rule = knowledge.rules.get(SOURCE_ID)
    if not rule or not rule.ref:
        return None
    # Take an exact sentence preceding any separate sanction list. Do not
    # embed organizer rules in Git or generate a penalty claim.
    quote = rule.text.split('|', 1)[0].split('.', 1)[0].strip() + '.'
    if not 20 <= len(quote) <= 600 or not re.search(r'оскорб', normalize(quote)):
        return None
    if not re.search(r'запрещ', normalize(quote)) or not relevant(
            result.category, result.detected_topics, request.messages[0].text, rule, quote):
        return None
    return DraftReply(lang='ru', text=f'Нет. {quote}\n\nПункт правил: {SOURCE_ID}. '
                      'Окончательное решение остаётся за оператором. '
                      'Если вопрос касается конкретной ситуации, уточните обстоятельства.', cited=[SOURCE_ID],
                      action='clarify', provenance='verified_template')
