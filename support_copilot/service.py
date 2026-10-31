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
# Patterns run on safety_view(): casefolded, ё→е, apostrophe variants → "'".
# Word boundaries matter: bare substrings such as «оск» or «матер» also hit «поскольку»,
# «Москва», «материалы»; bare «\bчит» hits «читать»; bare «kill» hits «skill».
TOPICS = {
    'payment': r'донат|плат[её]ж|оплат|списал|пополнени|payment|paid|donat|to[ʻ’\x27]?lov|төлем',
    'account': r'взлом|аккаунт|акк\b|доступ|парол|не могу войти|пропал[^\n]{0,80}(?:машин|имущество)|hacked|account|akkaunt|password|login',
    'admin': (r'админ|обжал|апелляц|разбан|не соглас(?:ен|на)|незаконн|\bбан(?:а|у|ом|е|ы|ить|ил[аи]?|ят|ен|или)?\b|забан|'
              r'\bмут(?:а|ом|е|ы|ить|ил[аи]?|ят)?\b|замут|наказан|\bварн\w*|\bwarn\b|деморган|\bджайл\w*|\bjail\b|'
              r'unban|appeal|admin|ban berd|razban|модератор|хелпер'),
    'cheat': (r'\bчит(?!а[тлюейв])|\bаим|aimbot|\bcheat|\bхакер|\bhacker|\bвх\b|\bwh\b|wallhack|спидхак|speed ?hack|'
              r'\bсофт\w*|\bфлай\w*|fly ?hack|godmode|годмод|бессмерт|(?<!вы)летает|сквозь стен|через стен|'
              r'\bклео\b|\bcleo\b|\bchit\w*|автоклик|\bмакрос\w*|\bsoft\b'),
    'scam': (r'обман|мошенн|скам|украл|scam|aldadi|алдап|развел|развод|\bкинул\w*|\bкидал\w*|\bкидок|'
             r'не (?:отдал|вернул)\w*|aldab|aldov|firibgar|алдады|алаяқ|\bstole\b'),
    'rmt_ads': (r'реклам|реал(?:ьные|ьных)? день|прода[её]т[^\n]{0,80}акк|rmt|reklama|'
                r'вирт\w*[^\n]{0,30}(?:за реал|за руб|рубл)|прода(?:[её]т|ю|м)[^\n]{0,30}вирт|t\.me/|discord\.gg|рекламир|пиарит'),
    'insult': (r'\bоск(?!ол|ал|ар|уд)\w*|оскорб|обзыв|обозв|\bупом\w*|униж|унизил|'
               r'\bмат(?:ом|ы|ами|ах)?\b|матер(?:ит|ил|ится|ился|ятся|ные|ное|ный|ная|ными|щин)|'
               r'токсич|расизм|\bрасист|мамк|мамаш|твою мать|\bлох(?:и|ом|а)?\b|\bчмо\b|дебил|\bдаун\b|идиот|'
               r'\bурод\w*|\bсук[аи]\b|шлюх|пид[оа]р|еблан|долб[ао][её]?б|\bгандон|мразь|\bнах(?:уй)?\b|\bпизд|'
               r'insult|\bosk\w*|oskorb|\bupom\w*|called me|\bracist|\btoxic|'
               r"so'?k(?:di|ish|ib|adi|kan)|suk(?:di|ish)|haqorat|xaqorat|onamni|onangni|ahmoq|"
               r'балағат|боқта|сөк(?:ті|іп)|қорла|анамды|ақымақ'),
    'dm': (r'\bдм(?:ер\w*|щик\w*|ит|ят|ил\w*|ишь)?\b|\bрдм\b|\bdm\b|\brdm\b|\bдб\b|drive ?by|'
           r'\bубил(?!ся|ась|ось)\w*|\bубив\w*|\bубий\w*|\bубь(?:ет|ют|ешь)\b|'
           r'стрел(?:я|ил)|застрел|пристрел|\bсбил(?!ся|ась|ось|и с толку)\w*|давит|задавил|таран|'
           r'\bизбил|\bударил|\bбь(?:ет|ют)\b|\bkill|ubil|ubivaet|'
           r"\botdi\b|o'?ldir|uldir|\botib\b|\burdi\b|өлтір|\bұр(?:ды|ып)\b|\bатып\b"),
    'help': (r'застр|\bбаг(?!о?юз|аюз)|текстур|вылет|лагает|\bлаг(?:и|ов|ает|ают)?\b|фриз|зависа|завис(?:ла|ло|ает)|'
             r'черн\w* экран|не (?:запуска|загружа|грузит|работает|открыва|отобража|сохраня)\w*|'
             r'\b(?:персонаж|герой|машина|транспорт)\w*\s+не\s+(?:двигается|движется|едет)\b|ошибк|краш|'
             r'провалил|под карт|под земл|телепорт|stuck|\bbug|\blag\b|crash|\berror\b|'
             r'zastr|\bbag\b|vylet|qotib|ishlamay|\bxato|тұрып қал|жұмыс істемей|\bқате'),
    'question': (r'\bкак\b|\bгде\b|\bможно\b|\bсколько\b|\bзачем\b|\bкогда\b|\bhow\b|\bwhere\b|qanday|qayer|қалай|қайда|каким|'
                 r'\b(?:подскажите|скажите|уточните)\s+(?:мне\s+)?(?:стоимость|цену|цен[ауе]|сколько|где|как)\b'),
}
# Russian case forms: «на сервере 4», «с сервера 12».
SERVER = re.compile(r'(?:сервер(?:а|е|у|ом)?|server)\s*(?:№|#|:)?\s*(\d{1,3})\b', re.I)
MOVEMENT_ISSUE = re.compile(r'\b(?:персонаж|герой|машина|транспорт)\w*\s+не\s+(?:двигается|движется|едет)\b')
PATTERNS = {key: re.compile(value, re.I) for key, value in TOPICS.items()}

# Category selection only. Topics above keep feeding priority, escalation and detected_topics unchanged
# in spirit; the helpers below decide which single category a multi-topic ticket gets.
OFFENCES = ('cheat', 'scam', 'rmt_ads', 'insult', 'dm')
# The reporter contests a punishment applied to them: an appeal is admin even if it names an offence.
APPEAL = re.compile(
    r'обжал|апелляц|разбан|не соглас(?:ен|на)|незаконн|несправедлив|ошибочно|'
    r'пересмотр\w*[^\n.!?]{0,40}(?:наказ|бан|мут|варн|блокиров|деморган|джайл)|'
    r'сним(?:ите|ите с меня|и)\s+(?:бан|мут|наказ|варн|блок)|снять\s+(?:бан|мут|наказ|варн|блок)|'
    r'(?:меня|мне|мой акк\w*)[^\n.!?]{0,25}(?:забан|замут|наказ|выдал|кикнул|заблок|посадил)|'
    r'(?:забанил|замутил|наказал|выдал|кикнул|заблокировал)\w*\s+(?:меня|мне)\b|'
    r'(?:забан|замут|выдал|дали|дал)\w*[^\n.!?]{0,25}(?:без причины|ни за что|просто так)|'
    r"unban|appeal|razban|ban berd|nohaq|bandan chiqar|jazoni|noto'?g'?ri\s+(?:ban|blok)|бандан шығар|жаза[^\n]{0,25}(?:келісп|кайта|қайта)")
# An administrator is the subject or object of the complaint (not merely addressed).
ADMIN_ACTOR = re.compile(
    r'\b(?:админ(?:а|ом|ы|ов|ка)?|администратор(?:а|ом|ы|ов)?|модератор(?:а|ом|ы|ов)?|модер(?:а|ом|ы|ов)?|'
    r'хелпер(?:а|ом|ы|ов)?|следящ\w*|admin(?:s)?|moderator)\b')
ADMIN_MISCONDUCT = re.compile(r'без причины|ни за что|несправедлив|незаконн|злоупотреб|превыш|оскорб|игнор|не отвеча|абуз|'
                              r'besabab|nohaq|себепсіз|unfair|abuse')
PUNISHMENT = re.compile(r'\bбан(?:а|у|ом|е|ы|ил[аи]?|ят|ен|или)?\b|забан|\bмут(?:а|ом|е|ы|ил[аи]?|ят)?\b|замут|наказ|'
                        r'\bварн\w*|\bwarn\b|деморган|\bджайл\w*|\bjail\b|\bкикнул\w*|заблок|блокировк|\bban(?:ned)?\b|blokla|бұғат')
# Addressing the staff or asking them to punish someone else is not a complaint about the staff.
STAFF_ADDRESS = re.compile(
    r'(?:уважаем\w*\s+)?\b(?:админ\w*|администрац\w*|модер\w*|хелпер\w*)\s*[,!:]|'
    r'(?:прошу|просьба|обращаюсь к|пишу|уважаем\w*)\s+(?:\w+\s+){0,2}?(?:админ\w*|администрац\w*|модер\w*)|'
    r'(?:\b(?:админ\w*|администрац\w*|модер\w*|хелпер\w*)\s+)?(?:пожалуйста\s+)?'
    r'(?:забан(?:ьте|ить|ь)\b|накаж(?:ите|и)\b|наказать|выда(?:йте|ть|й)\b|дайте|дать\b|примите меры|'
    r'разберитесь|разобраться|посмотрите|проверьте|ban (?:him|her|them|this)|please ban|'
    r'ban bering|jazolang|бан беріңіз|жазалаңыз)[^\n.!?]{0,40}')
# Explicit complaint wording without a recognized violation.
COMPLAINT_INTENT = re.compile(
    r'жалоб|пожалова|нарушает|нарушил|нарушени|nonrp|нон ?рп|\bнрп\b|флуд|багоюз|баг ?аюз|'
    r'мешает|помеха|zhalob|jalob|shikoyat|qoidani buz|шағым|ереже\w* бұз|report (?:a |this )?player')
NICKNAME = re.compile(r'\b[A-Za-z][A-Za-z0-9]{1,24}_[A-Za-z][A-Za-z0-9_]{1,24}\b')
# Explicit requests aimed at a third party are not an appeal against the
# reporter's own sanction. Used only for category selection, never safety.
THIRD_PARTY_SANCTION_REQUEST = re.compile(
    r'\b(?:прошу|пожалуйста)\s+(?:выдайте\s+)?(?:бан|мут|варн)\s+(?:ему|ей|им|игроку)\b|'
    r'\b(?:прошу|пожалуйста)\s+(?:забанить|забаньте|наказать|накажите|замутить|замутьте)\s+(?:его|ее|её|их|игрока)\b|'
    r'\b(?:он|она|они|игрок)\s+долж(?:ен|на|ны)\s+быть\s+наказан\w*\b')
SANCTION_DURATION_QUESTION = re.compile(r'^\s*сколько\s+длится\s+(?:мут|бан|варн)\s+за\s+[^.!?]+\??\s*$')
NEGATION = re.compile(r"(?:^|[^\w])(?:не|ни|без|not|never|didn't|did not)\s+$")
QUESTION_START = re.compile(r'^\W*(?:как|где|можно|сколько|зачем|почему|когда|что|how|where|why|qanday|qayer|қалай|қайда)\b')
SOFTWARE_REQUEST = re.compile(
    r'\b(?:установ\w*|скач\w*|обнов\w*|куп\w*)\s+(?:[\w-]+\s+){0,3}'
    r'(?P<software>софт\w*|software|soft)\b')


def _topic_matches(topic: str, text: str):
    """Exclude a neutral software noun, retaining independently matched cheat evidence."""
    software_spans = ([match.span('software') for match in SOFTWARE_REQUEST.finditer(text)]
                      if topic == 'cheat' else [])
    for match in PATTERNS[topic].finditer(text):
        if (topic == 'cheat' and re.fullmatch(r'софт\w*|soft', match.group())
                and any(start <= match.start() < end for start, end in software_spans)):
            continue
        yield match


def _affirmed(topic: str, text: str) -> bool:
    """True when at least one match is not directly negated («не убивал», «я не читер»)."""
    for match in _topic_matches(topic, text):
        if not NEGATION.search(text[max(0, match.start() - 16):match.start()]):
            return True
    return False


def _admin_signal(text: str, staff_view: str) -> bool:
    """Admin category evidence on the view with staff addressing and punishment requests removed."""
    return bool(APPEAL.search(text) or PUNISHMENT.search(staff_view)
                or (ADMIN_ACTOR.search(staff_view) and ADMIN_MISCONDUCT.search(staff_view)))


def choose_category(kind: str, text: str, topics: set[str]) -> tuple[str, float]:
    """Pick one category; `text` is the safety_view of the player's own messages."""
    staff_view = THIRD_PARTY_SANCTION_REQUEST.sub(' ', STAFF_ADDRESS.sub(' ', text))
    offences = [topic for topic in OFFENCES if topic in topics and _affirmed(topic, text)]
    words = len(re.findall(r'[^\W\d_]+', text))
    # DB alone is a common in-game report shorthand; ordinary database discussions stay untouched.
    bare_db = re.fullmatch(r'\s*db\s*(?:https?://\S+\s*)?', text) is not None
    if bare_db:
        return ('report.complaint' if kind == 'report' else 'complaint.dm'), .5
    if kind == 'report':
        from support_copilot.rule_question import matches as matches_rule_question
        # Personal payment/account risks keep their mandatory categories first, unchanged.
        for topic in ('payment', 'account'):
            if topic in topics:
                return 'report.' + topic, .65
        if matches_rule_question(text):
            return 'report.question', .65
        # Narrow generic question, not a contested sanction. Risk topics still
        # participate in escalation independently of the category.
        if SANCTION_DURATION_QUESTION.fullmatch(text):
            return 'report.question', .65
        if _admin_signal(text, staff_view) or offences:
            return 'report.complaint', .65
        for topic in ('help', 'question'):
            if topic in topics:
                return 'report.' + topic, .65
        if COMPLAINT_INTENT.search(text):
            return 'report.complaint', .5
        return 'report.offtopic', 0.0
    # Generic expressions of unfairness alone do not identify an appeal when
    # the message also reports another player's conduct.
    appeal = APPEAL.search(THIRD_PARTY_SANCTION_REQUEST.sub(' ', text))
    generic_unfairness = appeal and re.fullmatch(r'несправедлив\w*|ошибочно', appeal.group())
    if appeal and not (generic_unfairness and offences):
        return 'complaint.admin', .65
    if offences:
        # «Админ оскорбил меня» is about the admin; «Админ, забаньте читера» is about the cheater.
        staff = ADMIN_ACTOR.search(staff_view)
        first_offence = next((m for topic in offences for m in _topic_matches(topic, text)), None)
        if staff and first_offence and staff.start() < first_offence.start():
            return 'complaint.admin', .65
        return 'complaint.' + offences[0], .65
    if PATTERNS['admin'].search(staff_view):
        return 'complaint.admin', .65
    # A nickname plus at least one descriptive word is a complaint about someone, not an empty form.
    if COMPLAINT_INTENT.search(text) or (NICKNAME.search(text) and re.search(r'[^\W\d_]', NICKNAME.sub(' ', text))):
        return 'complaint.other', .5
    question_like = 'question' in topics and (text.rstrip().endswith('?') or QUESTION_START.search(text))
    if words < 4 or question_like:
        return 'complaint.invalid', 0.0
    return 'complaint.other', 0.0


from support_copilot.language_id import language
from support_copilot import category_model


def _rule_inputs(request: TriageRequest):
    """Player text, independent safety assessment, matching view and detected topics."""
    # Reported player's quotes do not establish the reporter's intent.
    text = '\n'.join(message.text for message in request.messages if message.role == 'player')
    assessment = assess(request)
    t = safety_view(text)
    topics = {key for key in PATTERNS if next(_topic_matches(key, t), None) is not None}
    topics.update(assessment.topics)
    return text, assessment, t, topics


# Re-watching a recording is not an appeal; this mirrors the APPEAL rule's punishment-context requirement.
REWATCH = re.compile(r'пересмотр\w*[^\n.!?]{0,40}(?:запис|видео|ролик|клип|повтор|момент)')


def model_vetoes(t: str, topics: set[str]) -> dict[str, str]:
    """Categories a learned model may not introduce where the rule core has a reviewed protection."""
    vetoes = {}
    if SOFTWARE_REQUEST.search(t) and 'cheat' not in topics:
        vetoes['complaint.cheat'] = 'category_guard:ordinary_software_not_cheat'
    if REWATCH.search(t) and not APPEAL.search(t) and 'admin' not in topics:
        vetoes['complaint.admin'] = vetoes['report.complaint'] = 'category_guard:rewatch_not_appeal'
    return vetoes


def rule_category(request: TriageRequest) -> tuple[str, float, bool, dict[str, str]]:
    """Rule-core category exactly as analyze() computes it, the appeal flag and model vetoes."""
    _, _, t, topics = _rule_inputs(request)
    category, certainty = choose_category(request.kind, t, topics)
    return category, certainty, bool(APPEAL.search(t)), model_vetoes(t, topics)


def analyze(request: TriageRequest) -> TriageResponse:
    knowledge = get_knowledge()
    text, assessment, t, topics = _rule_inputs(request)
    lang, language_note = language(t)
    if assessment.flags & {'mixed_language_requires_review', 'suspected_kazakh_encoding'}:
        lang, language_note = 'und', None
    rules_category, rules_certainty = choose_category(request.kind, t, topics)
    # Opt-in learned backend may replace the category only; the default 'rules' returns it unchanged.
    decision = category_model.decide(request, rules_category, rules_certainty, appeal=bool(APPEAL.search(t)),
                                     vetoes=model_vetoes(t, topics),
                                     categories=knowledge.categories, human_always=knowledge.human_always,
                                     taxonomy_sha256=knowledge.hashes.get('taxonomy'))
    category, certainty = decision.category, decision.certainty
    for code in {category, rules_category}:
        if code not in knowledge.categories or knowledge.categories[code]['kind'] != request.kind:
            raise ValueError('Category incompatible with taxonomy')
    # Escalation never relies on the learned category alone: either category can require a human.
    reviewed = {category, rules_category}
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
    transliterated = language_note == 'language_transliterated_ru'
    incomplete = (bool(assessment.flags) or certainty == 0 or rules_certainty == 0 or lang == 'und' or transliterated
                  or any(item['status'] != 'resolved' for item in resolutions))
    mandatory_review = bool(reviewed & knowledge.human_always) or 'report.complaint' in reviewed
    # Recognizing a movement failure is not a verified fix for it.
    movement_review = bool(MOVEMENT_ISSUE.search(t))
    human = (bool(assessment.flags) or critical or mandatory_review or movement_review or request.kind == 'complaint'
             or 'admin' in topics or incomplete)
    reasons = ['wave_2_lexical_baseline', 'confidence_is_uncalibrated', 'draft_generator_not_connected',
               'duplicate_scope_within_ticket']
    if knowledge.taxonomy.get('version', '').startswith('draft'):
        reasons.append('taxonomy_is_draft')
    if language_note:
        reasons.append(language_note)
    if movement_review:
        reasons.append('movement_issue_requires_review')
    if transliterated:
        reasons.append('transliterated_input_requires_review')
    if critical or mandatory_review or 'admin' in topics or request.kind == 'complaint':
        reasons.append('human_review_required_no_personal_action')
    if incomplete:
        reasons.append('insufficient_context_or_ambiguous_reference')
    reasons.extend(decision.reasons)
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
        **({'confidence_method': decision.confidence_method} if decision.confidence_method else {}),
    )


def triage(request: TriageRequest, *, draft_provider=None, use_configured_provider: bool = True,
           use_verified_templates: bool | None = None) -> TriageResponse:
    from support_copilot.drafting import add_draft
    from support_copilot.rule_question import answer as answer_rule_question, matches as matches_rule_question
    result = analyze(request)
    result.reasons.remove('draft_generator_not_connected')
    # Explicitly injected providers exercise their path; normal API/CLI requests
    # can skip generation entirely for narrow, verified support workflows.
    templates = os.environ.get('COPILOT_VERIFIED_TEMPLATES','on') == 'on' if use_verified_templates is None else use_verified_templates
    player_text = '\n'.join(message.text for message in request.messages if message.role == 'player')
    if request.kind == 'report' and matches_rule_question(player_text):
        approved = answer_rule_question(request, result, get_knowledge()) if templates and draft_provider is None else None
        if approved is not None:
            result.draft_reply = approved
            result.draft_available = True
            result.reasons.append('verified_template:rule_question_insult')
            return result
        # Never let a model downgrade an unverified version of this workflow.
        result.needs_human = True
        result.reasons.append('rule_question_requires_review')
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
