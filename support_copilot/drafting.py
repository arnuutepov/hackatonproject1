"""Validate a model's grounded plan, then render only controlled language clauses."""
import json

from pydantic import ValidationError
from support_copilot.draft_provider import DraftPlan, DraftProvider, DraftFailure, FAILURE_CODES, configured_provider
from support_copilot.draft_text import TEXT
from support_copilot.knowledge import Knowledge, tokens
from support_copilot.schemas import DraftReply, TriageRequest, TriageResponse

STOP = {'правил', 'игрок', 'игрока', 'игроки', 'пункт', 'номер', 'можно', 'какой', 'такой', 'пожал', 'меня', 'этого', 'этот', 'того', 'игрово'}


def topic_for(result: TriageResponse) -> str:
    if result.category in {'report.account', 'report.payment'}:
        return result.category.split('.')[1]
    if result.category == 'complaint.admin':
        return 'appeal'
    if result.category.startswith('complaint.') and result.category != 'complaint.invalid' or result.category == 'report.complaint':
        return 'complaint'
    return {'report.help': 'bug', 'report.question': 'question'}.get(result.category, 'other')


def allowed_questions(result: TriageResponse, topic: str) -> list[str]:
    if result.lang == 'und' or result.lang not in TEXT:
        return ['preferred_language']
    specifics = {
        'payment': ['payment_details'], 'account': ['account_details'],
        'appeal': ['incident_time', 'evidence_link'],
        'complaint': ['incident_time', 'evidence_link'], 'bug': ['bug_steps'],
        'question': ['question_details'], 'other': ['question_details'],
    }[topic]
    # A game question need not demand a nickname or location.
    if topic in {'question', 'other'}:
        return specifics
    return specifics + ([] if result.extracted.server else ['server']) + ([] if result.extracted.nickname else ['nickname'])


def build_context(request: TriageRequest, result: TriageResponse, knowledge: Knowledge) -> tuple[dict, bool]:
    player = [(f'm{index}', message.text) for index, message in enumerate(request.messages) if message.role == 'player']
    selected = player if len(player) <= 6 else player[:2] + player[-4:]
    truncated = len(selected) != len(player) or any(len(text) > 1000 for _, text in selected)
    messages = [{'id': key, 'text': text[:1000]} for key, text in selected]
    rules = []
    for candidate in result.retrieved_rules:
        rule = knowledge.rules.get(candidate.source_id)
        # Unnumbered sections and weak title-only matches are not citations.
        if not rule or not rule.ref or candidate.text != rule.text:
            continue
        query_words = set(tokens(' '.join(item['text'] for item in messages))) - STOP
        if not query_words & (set(tokens(rule.text)) - STOP):
            continue
        rules.append({'source_id': rule.source_id, 'section': rule.section, 'text': rule.text[:2000]})
        if len(rules) == 3:
            break
    topic = topic_for(result)
    return dict(lang=result.lang, kind=request.kind, category=result.category, topic=topic,
                needs_human_minimum=result.needs_human or truncated,
                messages=messages, rules=rules, attachments_available=False,
                attachments_reported=bool(result.extracted.attachments_claimed),
                allowed_questions=allowed_questions(result, topic)), truncated


def validate_plan(raw: str, context: dict, knowledge: Knowledge) -> DraftPlan:
    if not isinstance(raw, str) or len(raw) > 16000:
        raise DraftFailure('draft_invalid_output')
    try:
        # JSON only: no code fences, duplicate keys, coercions, NaN, or extra fields.
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate key')
                value[key] = item
            return value
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
        plan = DraftPlan.model_validate(data)
    except (ValidationError, ValueError, TypeError):
        raise DraftFailure('draft_invalid_output') from None
    if plan.lang != context['lang']:
        raise DraftFailure('draft_wrong_language')
    if context['needs_human_minimum'] and not plan.needs_human:
        raise DraftFailure('draft_escalation_downgrade')
    if plan.action in {'human_review', 'rule_review'} and not plan.needs_human:
        raise DraftFailure('draft_escalation_downgrade')
    if plan.needs_human and plan.action == 'clarify':
        # Keep the handoff visible rather than hiding it behind a clarification.
        raise DraftFailure('draft_inconsistent_action')
    if len(plan.questions) > 3 or len(set(plan.questions)) != len(plan.questions) or not set(plan.questions) <= set(context['allowed_questions']):
        raise DraftFailure('draft_invalid_questions')
    if plan.action == 'clarify' and not plan.questions:
        raise DraftFailure('draft_missing_clarification')
    if context['lang'] == 'und' and 'preferred_language' not in plan.questions:
        raise DraftFailure('draft_missing_clarification')
    if plan.claim:
        messages = {item['id']: item['text'] for item in context['messages']}
        if not 3 <= len(plan.claim.quote.strip()) <= 500 or plan.claim.quote not in messages.get(plan.claim.message_id, ''):
            raise DraftFailure('draft_ungrounded_claim')
    if len(plan.citations) > 2 or len({item.source_id for item in plan.citations}) != len(plan.citations):
        raise DraftFailure('draft_invalid_citation')
    sources = {item['source_id']: item['text'] for item in context['rules']}
    for citation in plan.citations:
        rule = knowledge.rules.get(citation.source_id)
        if not rule or citation.source_id not in sources or not rule.ref:
            raise DraftFailure('draft_invalid_citation')
        if not 20 <= len(citation.quote.strip()) <= 600 or citation.quote not in sources[citation.source_id] or citation.quote not in rule.text:
            raise DraftFailure('draft_invalid_citation')
        # Demand actual topic-bearing overlap in the selected quote, not mere existence.
        words = set(tokens(' '.join(item['text'] for item in context['messages']))) - STOP
        if not words & (set(tokens(citation.quote)) - STOP):
            raise DraftFailure('draft_irrelevant_citation')
    if plan.action == 'rule_review' and not plan.citations:
        raise DraftFailure('draft_missing_citation')
    return plan


def render(plan: DraftPlan, result: TriageResponse, topic: str, *, provenance: str) -> DraftReply:
    text = TEXT[plan.lang]
    paragraphs = [text['hello'], text[topic], text[plan.action]]
    if result.extracted.attachments_claimed:
        paragraphs.append(text['attachments'])
    cited = [item.source_id for item in plan.citations]
    if cited:
        paragraphs.append(text['rules'].format(ids=', '.join(cited)))
    paragraphs.extend(text['questions'][question] for question in plan.questions)
    if topic in {'account', 'payment'}:
        paragraphs.append(text['security'])
    claim = plan.claim.model_dump() if plan.claim else None
    return DraftReply(lang=plan.lang, text='\n\n'.join(item for item in paragraphs if item), cited=cited,
                      action=plan.action, provenance=provenance, player_claim=claim, established_facts=[])


def add_draft(request: TriageRequest, result: TriageResponse, knowledge: Knowledge,
              provider: DraftProvider | None = None, *, use_configured_provider: bool = True) -> TriageResponse:
    context, truncated = build_context(request, result, knowledge)
    if truncated:
        result.needs_human = True
        result.reasons.append('draft_context_truncated')
    reason = 'draft_template_mode'
    if result.lang not in TEXT:
        # Preserve upstream language; mark the draft language as und explicitly.
        reason = 'draft_language_not_supported'
        context['lang'] = 'und'
        context['needs_human_minimum'] = True
        context['allowed_questions'] = ['preferred_language']
    elif result.lang == 'und':
        reason = 'draft_language_unknown'
    try:
        selected = None
        if reason not in {'draft_language_not_supported', 'draft_language_unknown'}:
            selected = provider if provider is not None else (configured_provider() if use_configured_provider else None)
        if selected is not None:
            plan = validate_plan(selected.generate(context), context, knowledge)
            result.draft_reply = render(plan, result, topic_for(result), provenance='validated_model_plan')
            result.needs_human = result.needs_human or plan.needs_human
            result.draft_available = True
            result.reasons.append('draft_model_plan_validated')
            return result
    except DraftFailure as exc:
        # A provider implementation cannot smuggle arbitrary text through its exception.
        code = str(exc)
        reason = code if code in FAILURE_CODES else 'draft_provider_error'
        result.needs_human = True
    except Exception:
        reason = 'draft_provider_error'
        result.needs_human = True
    human = result.needs_human or context['needs_human_minimum']
    questions = context['allowed_questions'][:3]
    if topic_for(result) == 'question' and context['lang'] != 'und':
        # No game-mechanics answer was validated. Avoid asking players to repeat
        # a clear question merely because our knowledge base cannot answer it.
        human = True
        questions = []
        result.reasons.append('draft_answer_sources_insufficient')
    plan = DraftPlan(lang=context['lang'], action='human_review' if human else 'clarify',
                     needs_human=human, claim=None, questions=questions, citations=[])
    result.draft_reply = render(plan, result, topic_for(result) if plan.lang != 'und' else 'other', provenance='safe_template')
    result.needs_human = human
    result.draft_available = True
    result.reasons.extend([reason, 'draft_safe_template'])
    return result
