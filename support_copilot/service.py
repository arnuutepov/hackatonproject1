"""Shared inference entry point: Wave 2 analysis plus guarded Wave 3 drafts."""
import re
from support_copilot.knowledge import get_knowledge, normalize
from support_copilot.schemas import DraftReply, Extracted, TriageRequest, TriageResponse

# Transparent dev heuristics. Admin outcomes and control labels are not inputs.
TOPICS = {
    'payment': r'донат|плат[её]ж|оплат|списал|пополнени|payment|paid|donat|to[ʻ’\x27]?lov|төлем',
    'account': r'взлом|аккаунт|акк\b|доступ|парол|не могу войти|пропал.*(?:машин|имущество)|hacked|account|akkaunt|password|login',
    'admin': r'админ|обжал|апелляц|разбан|не согласен|незаконн|\bбан(?:а|у|ом|е|ы|ить|ил[аи]?|ят|ен|или)?\b|забан|\bмут(?:а|ом|е|ы|ить|ил[аи]?|ят)?\b|наказан|unban|appeal|admin|ban berd',
    'cheat': r'\bчит|\bаим|\baimbot|\bcheat|летает|сквозь стен|через стен',
    'scam': r'обман|мошенн|скам|украл|scam|aldadi|алдап',
    'rmt_ads': r'реклам|реал(?:ьные|ьных)? день|прода[её]т.*акк|rmt|reklama',
    'insult': r'оск|оскорб|обзыв|матер|токсич|расизм|insult|so[ʻ’\x27]?k|haqorat|балағат',
    'dm': r'\bдм|\bdm\b|убил|убива|стреля|давит|задавил|таран|kill|otdi|o[ʻ’\x27]?ldir|өлтір',
    'help': r'застр|баг|текстур|вылет|лагает|телепорт|stuck|bug|qotib|тұрып қал',
    'question': r'\bкак\b|\bгде\b|\bможно\b|\bсколько\b|\bзачем\b|\bhow\b|\bwhere\b|qanday|qayer|қалай|қайда|каким',
}
PATTERNS = {key: re.compile(value, re.I) for key, value in TOPICS.items()}


def language(text: str) -> tuple[str, str | None]:
    t = normalize(text)
    if re.search(r'[їєґ]', t) or re.search(r'\b(будь ласка|мене|чому|гравець)\b', t):
        return 'und', 'recognized_uk_outside_contract'
    if re.search(r'[әғқңөұүһі]', t):
        return 'kk', None
    if re.search(r'[ўқғҳ]', t) or re.search(r"\b(salom|yordam|menga|iltimos|qanday|qayer|nima|men|uchun|bilan|bering|yo[ʻ’']q)\b", t):
        return 'uz', None
    if re.search(r'[а-я]', t):
        return 'ru', None
    vocab = {
        'en': {'hello', 'help', 'please', 'my', 'how', 'where', 'why', 'stuck', 'account', 'payment', 'player', 'hacked'},
        'es': {'hola', 'ayuda', 'por', 'favor', 'cuenta', 'pago', 'gracias'},
        'pt': {'ola', 'olá', 'ajuda', 'minha', 'conta', 'pagamento', 'obrigado'},
    }
    words = set(re.findall(r'[^\W\d_]+', t))
    scores = sorted(((len(words & values), lang) for lang, values in vocab.items()), reverse=True)
    if scores[0][0] and scores[0][0] > scores[1][0]:
        return scores[0][1], None
    return 'und', 'language_unknown_or_mixed'


def analyze(request: TriageRequest) -> TriageResponse:
    knowledge = get_knowledge()
    # Reported player's quotes do not establish the reporter's intent.
    text = '\n'.join(message.text for message in request.messages if message.role == 'player')
    t = normalize(text)
    lang, language_note = language(text)
    topics = {key for key, pattern in PATTERNS.items() if pattern.search(t)}
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
        r'массов.*баг|(?:все|всех).*вылета|mass.*bug|угро[зж].*(?:жизн|реал)|убью.*(?:реал|жизн)|kill you.*real', t))
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
    incomplete = certainty == 0 or lang == 'und' or any(item['status'] != 'resolved' for item in resolutions)
    human = (critical or category in knowledge.human_always or request.kind == 'complaint'
             or category == 'report.complaint' or 'admin' in topics or incomplete)
    reasons = ['wave_2_lexical_baseline', 'confidence_is_uncalibrated', 'draft_generator_not_connected',
               'duplicate_detection_not_implemented']
    if knowledge.taxonomy.get('version', '').startswith('draft'):
        reasons.append('taxonomy_is_draft')
    if language_note:
        reasons.append(language_note)
    if critical or category in knowledge.human_always or 'admin' in topics or request.kind == 'complaint' or category == 'report.complaint':
        reasons.append('human_review_required_no_personal_action')
    if incomplete:
        reasons.append('insufficient_context_or_ambiguous_reference')
    attachments = sum(message.attachments for message in request.messages)
    if attachments:
        reasons.append('attachments_reported_not_inspected')
    nickname = re.search(r'\b[A-Za-z][A-Za-z0-9]{1,24}_[A-Za-z][A-Za-z0-9_]{1,24}\b', text)
    server = re.search(r'(?:сервер|server)\s*(?:№|#|:)?\s*(\d{1,3})\b', text, re.I)
    notices = {
        'ru': 'Автоматический черновик пока недоступен.',
        'kk': 'Автоматты жауап жобасы әзірге қолжетімсіз.',
        'uz': 'Avtomatik javob loyihasi hozircha mavjud emas.',
        'en': 'An automatic draft is not available yet.',
        'es': 'El borrador automático aún no está disponible.',
        'pt': 'O rascunho automático ainda não está disponível.',
        'und': 'Автоматический черновик пока недоступен.',
    }
    return TriageResponse(
        lang=lang, category=category, priority=priority, is_spam_or_duplicate=False,
        extracted=Extracted(nickname=nickname.group() if nickname else None, rule_refs=references,
                            server=server.group(1) if server else None, attachments_claimed=attachments,
                            has_evidence=False),
        draft_reply=DraftReply(lang=lang, text=notices[lang]), needs_human=human,
        confidence=certainty if not incomplete else min(certainty, .25), reasons=reasons,
        retrieved_rules=knowledge.search(text), reference_resolutions=resolutions,
    )


def triage(request: TriageRequest, *, draft_provider=None, use_configured_provider: bool = True) -> TriageResponse:
    from support_copilot.drafting import add_draft
    result = analyze(request)
    result.reasons.remove('draft_generator_not_connected')
    return add_draft(request, result, get_knowledge(), provider=draft_provider,
                     use_configured_provider=use_configured_provider)
