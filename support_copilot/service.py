"""Shared inference entry point: Wave 2 analysis plus guarded Wave 3 drafts."""
import logging
import re
import os
import sqlite3
from support_copilot.audit_chain import AuditChain, escalation_payload, triage_payload
from support_copilot.knowledge import get_knowledge, normalize
from support_copilot.schemas import DraftReply, Extracted, TriageRequest, TriageResponse
from support_copilot.safety import assess, safety_view

logger = logging.getLogger('uvicorn.error')

# Transparent dev heuristics. Admin outcomes and control labels are not inputs.
TOPICS = {
    'payment': r'донат|плат[её]ж|оплат|списал|пополнени|payment|paid|donat|to[ʻ’\x27]?lov|төлем',
    'account': r'взлом|аккаунт|акк\b|доступ|парол|не могу войти|пропал[^\n]{0,80}(?:машин|имущество)|hacked|account|akkaunt|password|login',
    'admin': r'админ|обжал|апелляц|разбан|не согласен|незаконн|\bбан(?:а|у|ом|е|ы|ить|ил[аи]?|ят|ен|или)?\b|забан|\bмут(?:а|ом|е|ы|ить|ил[аи]?|ят)?\b|наказан|unban|appeal|admin|ban berd',
    'cheat': r'\bчит|\bаим|\baimbot|\bcheat|(?<!вы)летает|сквозь стен|через стен',
    'scam': r'обман|мошенн|скам|украл|scam|aldadi|алдап',
    'rmt_ads': r'реклам|реал(?:ьные|ьных)? день|прода[её]т[^\n]{0,80}акк|rmt|reklama',
    'insult': r'оск|оскорб|обзыв|матер|токсич|расизм|insult|so[ʻ’\x27]?k|haqorat|балағат',
    'dm': r'\bдм|\bdm\b|убил|убива|стреля|давит|задавил|таран|kill|otdi|o[ʻ’\x27]?ldir|өлтір',
    'help': r'застр|баг|текстур|вылет|лагает|телепорт|stuck|bug|qotib|тұрып қал',
    'question': r'\bкак\b|\bгде\b|\bможно\b|\bсколько\b|\bзачем\b|\bhow\b|\bwhere\b|qanday|qayer|қалай|қайда|каким',
}
# Russian case forms: «на сервере 4», «с сервера 12».
SERVER = re.compile(r'(?:сервер(?:а|е|у|ом)?|server)\s*(?:№|#|:)?\s*(\d{1,3})\b', re.I)
PATTERNS = {key: re.compile(value, re.I) for key, value in TOPICS.items()}


from support_copilot.language_id import language


def analyze(request: TriageRequest) -> TriageResponse:
    knowledge = get_knowledge()
    # Reported player's quotes do not establish the reporter's intent.
    text = '\n'.join(message.text for message in request.messages if message.role == 'player')
    assessment = assess(request)
    t = safety_view(text)
    lang, language_note = language(t)
    topics = {key for key, pattern in PATTERNS.items() if pattern.search(t)}
    topics.update(assessment.topics)
    if assessment.flags & {'mixed_language_requires_review', 'suspected_kazakh_encoding'}:
        lang, language_note = 'und', None
    category, certainty = None, 0.0
    if request.kind == 'report':
        for topic, code in [('payment', 'payment'), ('account', 'account'), ('admin', 'complaint'),
                            ('cheat', 'complaint'), ('scam', 'complaint'), ('rmt_ads', 'complaint'),
                            ('insult', 'complaint'), ('dm', 'complaint'), ('help', 'help'), ('question', 'question')]:
            if topic in topics:
                category, certainty = 'report.' + code, .65
                break
        category = category or 'report.offtopic'
    else:
        for topic in ('admin', 'cheat', 'scam', 'rmt_ads', 'insult', 'dm'):
            if topic in topics:
                category, certainty = 'complaint.' + topic, .65
                break
        if category is None:
            category = 'complaint.invalid' if len(re.findall(r'[^\W\d_]+', t)) < 4 or 'question' in topics else 'complaint.other'
    if category not in knowledge.categories or knowledge.categories[category]['kind'] != request.kind:
        raise ValueError('Category incompatible with taxonomy')
    critical = bool(topics & {'payment', 'account'}) or bool(re.search(
        r'массов[^\n]{0,100}баг|(?:все|всех)[^\n]{0,100}вылета|mass[^\n]{0,100}bug|угро[зж][^\n]{0,100}(?:жизн|реал)|убью[^\n]{0,100}(?:реал|жизн)|kill you[^\n]{0,100}real', t))
    if critical:
        priority = 'p0'
    elif topics & {'cheat', 'rmt_ads'} or ('scam' in topics and re.search(r'миллион|млн|крупн|million', t)):
        priority = 'p1'
    elif category in {'complaint.invalid', 'report.question', 'report.offtopic'}:
        priority = 'p3'
    else:
        priority = 'p2'
    references = []
    for match in re.finditer(r'(?<![\w.])(?:(\d{2}):|(ЖБ)\s*)?(\d{1,2}\.\d+(?:\.\d+)?)(?![\w.])', text, re.I):
        section_id, prefix, ref = match.groups()
        reference = f'{section_id}:{ref}' if section_id else ('ЖБ ' + ref if prefix else ref)
        if reference not in references:
            references.append(reference)
    resolutions = []
    for reference in references:
        candidates = knowledge.resolve(reference)
        resolutions.append(dict(reference=reference, status='resolved' if len(candidates) == 1 else 'ambiguous' if candidates else 'missing', candidates=candidates))
    for alias in re.findall(r'\b(?:GZ|ГЗ|ЗЗ)\b', text, re.I):
        if alias.upper() not in references:
            references.append(alias.upper())
            resolutions.append(dict(reference=alias.upper(), status='ambiguous', candidates=knowledge.resolve(alias)))
    incomplete = bool(assessment.flags) or certainty == 0 or lang == 'und' or any(item['status'] != 'resolved' for item in resolutions)
    human = (bool(assessment.flags) or critical or category in knowledge.human_always or request.kind == 'complaint'
             or category == 'report.complaint' or 'admin' in topics or incomplete)
    reasons = ['wave_2_lexical_baseline', 'confidence_is_uncalibrated', 'draft_generator_not_connected',
               'duplicate_scope_within_ticket']
    if knowledge.taxonomy.get('version', '').startswith('draft'):
        reasons.append('taxonomy_is_draft')
    if language_note:
        reasons.append(language_note)
    if critical or category in knowledge.human_always or 'admin' in topics or request.kind == 'complaint' or category == 'report.complaint':
        reasons.append('human_review_required_no_personal_action')
    if incomplete:
        reasons.append('insufficient_context_or_ambiguous_reference')
    reasons.extend(sorted(assessment.flags))
    attachments = sum(message.attachments for message in request.messages)
    if attachments:
        reasons.append('attachments_reported_not_inspected')
    nickname = re.search(r'\b[A-Za-z][A-Za-z0-9]{1,24}_[A-Za-z][A-Za-z0-9_]{1,24}\b', text)
    server = re.search(SERVER, text)
    notices = {
        'ru': 'Автоматический черновик пока недоступен.',
        'kk': 'Автоматты жауап жобасы әзірге қолжетімсіз.',
        'uz': 'Avtomatik javob loyihasi hozircha mavjud emas.',
        'en': 'An automatic draft is not available yet.',
        'es': 'El borrador automático aún no está disponible.',
        'pt': 'O rascunho automático ainda não está disponível.',
        'und': 'Автоматический черновик пока недоступен.',
    }
    from support_copilot.dedup import repeated_messages
    duplicate = repeated_messages(request.messages)
    if duplicate:
        reasons.append('repeated_player_message_simhash')
    return TriageResponse(
        lang=lang, category=category, priority=priority, is_spam_or_duplicate=duplicate,
        extracted=Extracted(nickname=nickname.group() if nickname else None, rule_refs=references,
                            server=server.group(1) if server and 'conflicting_observable_fields' not in assessment.flags else None, attachments_claimed=attachments,
                            has_evidence=False),
        draft_reply=DraftReply(lang=lang, text=notices[lang]), needs_human=human,
        confidence=certainty if not incomplete else min(certainty, .25), reasons=reasons,
        confidence_level='low' if incomplete or certainty < .4 else 'medium',
        retrieved_rules=knowledge.search(text), reference_resolutions=resolutions,
        detected_topics=sorted(topics), safety_flags=sorted(assessment.flags),
    )


def triage(request: TriageRequest, *, draft_provider=None, use_configured_provider: bool = True,
           use_verified_templates: bool | None = None) -> TriageResponse:
    from support_copilot.drafting import add_draft
    result = analyze(request)
    result.reasons.remove('draft_generator_not_connected')
    # Explicitly injected providers exercise their path; normal API/CLI requests
    # can skip generation entirely for narrow, verified support workflows.
    templates = os.environ.get('COPILOT_VERIFIED_TEMPLATES','on') == 'on' if use_verified_templates is None else use_verified_templates
    if draft_provider is None and templates:
        from support_copilot.verified_templates import match_template, reply
        template = match_template(request,result)
        if template:
            result.draft_reply = reply(template,result)
            result.draft_available = True
            result.reasons.append('verified_template:'+template)
            return result
    return add_draft(request, result, get_knowledge(), provider=draft_provider,
                     use_configured_provider=use_configured_provider)


def record_triage(request: dict, analysis: dict, actor: str) -> None:
    """Journal a stateless /triage answer. The chain records decisions and never gates them.

    Ticket flows write their block inside the ticket transaction instead. Offline batches
    (make predict, evaluations) take no action and stay out of the journal.
    """
    chain = AuditChain()
    try:
        with chain.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            chain.append('triage.completed', actor, triage_payload(request, analysis), connection=connection)
            if analysis['needs_human']:
                chain.append('escalation.human', actor, escalation_payload(analysis), connection=connection)
    except (sqlite3.Error, OSError, ValueError):
        logger.warning('Audit chain unavailable: /triage answered without a block')
