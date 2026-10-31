"use strict";

const $ = (id) => document.getElementById(id);
const STATUS = {
  new: "Новое",
  analyzed: "Разобрано",
  saved: "Сохранено",
  escalated: "Передано человеку",
  resolved: "Завершено",
  reply_simulated: "Ответ · MOCK",
};
const LANG = {
  ru: "Русский",
  kk: "Қазақша",
  uz: "O‘zbekcha",
  en: "English",
  es: "Español",
  pt: "Português",
  und: "Не определён",
};
const PROVENANCE = {
  safe_template: "Безопасный шаблон",
  verified_template: "Локальный шаблон",
  validated_model_plan: "Проверенный план модели",
};
const REASONS = {
  wave_2_lexical_baseline:
    "Классификация использует локальный лексический baseline.",
  confidence_is_uncalibrated:
    "Оценка уверенности эвристическая, не вероятность правильности.",
  duplicate_scope_within_ticket: "Повторы сообщений проверяются внутри обращения; похожие обращения требуют отдельной проверки.",
  repeated_player_message_simhash: "Найден повтор сообщения игрока; безопасность и эскалация не отключены.",
  language_transliterated_ru: "Вероятно русский текст в транслитерации.",
  duplicate_detection_not_implemented:
    "Межтикетная дедупликация не подключена.",
  taxonomy_is_draft: "Таксономия источника помечена как черновик.",
  human_review_required_no_personal_action:
    "Обстоятельства или действия по обращению требуют человека.",
  movement_issue_requires_review:
    "Техническая проблема с движением требует проверки специалистом.",
  rule_question_requires_review:
    "Не удалось подтвердить применимость проверенного правила.",
  insufficient_context_or_ambiguous_reference:
    "Недостаточно контекста либо ссылка на правило неоднозначна.",
  attachments_reported_not_inspected:
    "Есть сведения о вложениях. Сами изображения не просматривались.",
  language_unknown_or_mixed: "Язык не определён однозначно.",
  language_shared_script_ambiguous:
    "Алфавит не позволяет однозначно определить язык.",
  recognized_uk_outside_contract: "Распознан язык вне текущего контракта.",
  draft_safe_template: "Использован контролируемый безопасный шаблон.",
  draft_model_plan_validated:
    "План внешней модели прошёл программные проверки.",
  draft_answer_sources_insufficient:
    "Источников недостаточно для содержательного ответа по правилам.",
  draft_context_truncated: "Контекст ограничен бюджетом генерации.",
  mixed_language_requires_review: "Смешанная речь требует проверки.",
  suspected_kazakh_encoding: "Возможны искажённые казахские символы.",
  conflicting_observable_fields:
    "В сообщениях противоречат извлекаемые сведения.",
  prompt_injection_suspected:
    "Обнаружена возможная попытка подменить инструкции.",
  instruction_override_attempt:
    "Попытка подменить инструкции: указания из сообщения игрока не исполняются.",
};
let config = null,
  tickets = [],
  current = null,
  draftValue = "",
  noteValue = "",
  dirty = false;
let busy = false,
  activeTab = "result",
  selectionSequence = 0,
  toastTimer;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function badge(text, kind = "neutral") {
  return node("span", `badge ${kind}`, text);
}
function shortId(id) {
  return id.slice(0, 8).toUpperCase();
}
function priorityKind(priority) {
  return (
    { p0: "red", p1: "amber", p2: "blue", p3: "neutral" }[priority] || "neutral"
  );
}
function statusKind(status) {
  return (
    { saved: "green", escalated: "amber", analyzed: "blue" }[status] ||
    "neutral"
  );
}
function dateTime(value) {
  return new Intl.DateTimeFormat("ru", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function notify(text) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 5500);
}

function setBusy(value) {
  busy = value;
  document.body.classList.toggle("busy", value);
  document.querySelectorAll("button").forEach((button) => {
    button.disabled = value;
  });
  $("analyze-label").textContent = value
    ? "Обработка…"
    : current?.analysis
      ? "Повторить анализ"
      : "Проанализировать";
  $("workarea").setAttribute("aria-busy", String(value));
  $("assistant-submit").disabled = value || !config?.ai?.assistant_configured || $("assistant-submit").dataset.pending === "true";
  $("assistant-check").disabled = value || !config?.ai?.draft_configured || $("assistant-check").dataset.pending === "true";
  if (!value) {
    updateWorkflowActions();
    updatePagination();
  }
}

function canLeave() {
  return (
    !dirty ||
    window.confirm("Есть несохранённые правки. Продолжить без сохранения?")
  );
}

function renderQueue() {
  renderAdminQueue();
}

function setTab(tab) {
  activeTab = tab;
  for (const name of ["result", "history", "outbox"]) {
    $(`tab-${name}`).classList.toggle("active", name === tab);
    $(`tab-${name}`).setAttribute("aria-selected", String(name === tab));
    $(`tab-${name}`).tabIndex = name === tab ? 0 : -1;
    $(`${name}-panel`).hidden = name !== tab;
  }
}

function renderHistory() {
  const list = $("history-list");
  list.replaceChildren();
  $("history-count").textContent = current.events.length;
  const names = eventNames;
  for (const event of [...current.events].reverse()) {
    const item = node("article", "history-event");
    item.append(
      node("h3", "", names[event.kind] || event.kind),
      node("p", "", `${dateTime(event.at)} · версия ${event.revision}`),
    );
    if (event.detail.note) item.append(node("p", "", event.detail.note));
    if (event.detail.needs_human)
      item.append(node("p", "", "Проверка человеком остаётся обязательной."));
    if (event.detail.chain_block !== undefined)
      item.append(button(`Записано в блок #${event.detail.chain_block} →`, "text-button", () =>
        openChainBlock(event.detail.chain_block)));
    if (event.detail.draft !== undefined) {
      const details = node("details");
      details.append(
        node("summary", "", "Показать текст этой версии"),
        node("div", "history-draft", event.detail.draft),
      );
      item.append(details);
    }
    list.append(item);
  }
}

function renderDiff() {
  const original = current?.analysis?.draft_reply.text || "";
  const edited = draftValue;
  const box = node("div", "diff-box");
  if (original === edited) {
    box.textContent = "Текст совпадает с исходным черновиком.";
  } else {
    let prefix = 0,
      suffix = 0;
    while (
      prefix < Math.min(original.length, edited.length) &&
      original[prefix] === edited[prefix]
    )
      prefix++;
    while (
      suffix < Math.min(original.length, edited.length) - prefix &&
      original[original.length - suffix - 1] ===
        edited[edited.length - suffix - 1]
    )
      suffix++;
    box.append(document.createTextNode(original.slice(0, prefix)));
    const removed = original.slice(prefix, original.length - suffix);
    const added = edited.slice(prefix, edited.length - suffix);
    if (removed) box.append(node("del", "", removed));
    if (added) box.append(node("ins", "", added));
    if (suffix)
      box.append(
        document.createTextNode(original.slice(original.length - suffix)),
      );
  }
  $("draft-diff").replaceChildren(box);
}

function updateDirty() {
  dirty = Boolean(
    current?.analysis &&
    (draftValue !== current.edited_draft || noteValue.trim()),
  );
  $("draft-state").textContent = dirty
    ? "Есть несохранённые правки"
    : current?.status === "saved" || current?.status === "escalated"
      ? "Сохранено на сервере"
      : "Без изменений";
  $("draft-state").classList.toggle("dirty", dirty);
  renderDiff();
}

function reasonLabel(reason) {
  return (
    REASONS[reason] ||
    (reason.startsWith("verified_template:")
      ? "Применён узкий локальный шаблон."
      : reason)
  );
}

async function openRule(sourceId) {
  $("rule-title").textContent = "Загрузка пункта…";
  $("rule-section").textContent = "";
  $("rule-body").textContent = "";
  $("rule-hash").textContent = "";
  $("rule-dialog").showModal();
  try {
    const rule = await api(`/rules/${encodeURIComponent(sourceId)}`);
    $("rule-title").textContent = rule.ref
      ? `Пункт ${rule.ref}`
      : "Раздел правил";
    $("rule-section").textContent = `${rule.section} · ID ${rule.source_id}`;
    $("rule-body").textContent = rule.text;
    $("rule-hash").textContent = `SHA-256 источника: ${rule.hash}`;
  } catch (error) {
    $("rule-title").textContent = "Пункт недоступен";
    $("rule-body").textContent = errorText(error);
  }
}

function renderAnalysis() {
  const result = current.analysis;
  $("analysis-placeholder").hidden = Boolean(result);
  $("analysis-result").hidden = !result;
  $("provider-badge").textContent = result
    ? PROVENANCE[result.draft_reply.provenance] || result.draft_reply.provenance
    : "Ожидание";
  if (!result) return;
  const summary = $("result-summary");
  summary.replaceChildren();
  for (const [label, value] of [
    ["Категория", config?.categories[result.category] || result.category],
    [
      "Приоритет",
      `${result.priority.toUpperCase()} · ${config?.priorities[result.priority] || ""}`,
    ],
    ["Язык ответа", LANG[result.draft_reply.lang] || result.draft_reply.lang],
    ["Уверенность", ({low:"Низкая",medium:"Средняя",high:"Высокая"}[result.confidence_level] || "Низкая") + " · эвристика, не вероятность"],
    [
      "Маршрут",
      current.needs_human ? "Требуется человек" :
        result.reasons.includes('verified_template:rule_question_insult')
          ? "Можно отправить после проверки" : "Черновик для проверки",
    ],
  ])
    summary.append(node("dt", "", label), node("dd", "", value));

  const attention = $("attention");
  attention.replaceChildren();
  const warnings = [];
  if (current.needs_human)
    warnings.push(
      "Решение требует участия человека. Сохранение правки не отменяет этот маршрут.",
    );
  if (result.safety_flags.length)
    warnings.push(...result.safety_flags.map(reasonLabel));
  if (result.reference_resolutions.some((item) => item.status !== "resolved"))
    warnings.push(
      "В обращении есть неизвестная или неоднозначная ссылка на правило.",
    );
  if (
    current.knowledge_hashes &&
    config &&
    Object.keys(current.knowledge_hashes).some(
      (key) => current.knowledge_hashes[key] !== config.knowledge_hashes[key],
    )
  )
    warnings.push(
      "Источники изменились после анализа. Открываемый пункт относится к текущей версии: перед решением повторите анализ.",
    );
  attention.hidden = !warnings.length;
  if (warnings.length) {
    const title = node("div", "attention-title");
    title.append(icon("alert"), document.createTextNode("Требует внимания"));
    attention.append(title);
    for (const warning of [...new Set(warnings)])
      attention.append(node("p", "", warning));
  }
  $("reasons").replaceChildren(
    ...result.reasons.map((reason) => node("li", "", reasonLabel(reason))),
  );

  const rules = $("rules-list");
  rules.replaceChildren();
  const cited = new Set(result.draft_reply.cited);
  const candidates = new Map(
    result.retrieved_rules.map((rule) => [rule.source_id, rule]),
  );
  const ids = [...new Set([...cited, ...candidates.keys()])];
  $("rules-count").textContent = ids.length;
  const visibleCount = Math.max(2, cited.size);
  let moreRules = null;
  if (ids.length > visibleCount) {
    moreRules = node("details", "more-rules");
    moreRules.append(
      node("summary", "", `Показать ещё: ${ids.length - visibleCount}`),
    );
  }
  for (const [index, id] of ids.entries()) {
    const rule = candidates.get(id);
    const card = node("button", "rule-card");
    card.type = "button";
    const heading = node("div", "rule-card-heading");
    heading.append(
      node("span", "", rule?.ref ? `Пункт ${rule.ref}` : id),
      badge(
        cited.has(id) ? "В черновике" : "Кандидат",
        cited.has(id) ? "blue" : "neutral",
      ),
    );
    card.append(heading);
    if (rule)
      card.append(
        node("span", "rule-section-name", `${rule.section} · ${id}`),
        node("p", "", rule.text),
      );
    card.addEventListener("click", () => openRule(id));
    if (index >= visibleCount && moreRules) moreRules.append(card);
    else rules.append(card);
  }
  if (moreRules) rules.append(moreRules);
  if (!ids.length)
    rules.append(
      node(
        "div",
        "no-rules",
        "Подходящие основания не найдены или цитирование ограничено политикой. Черновик не подтверждает нарушение.",
      ),
    );
  $("references").replaceChildren();
  for (const reference of result.reference_resolutions) {
    const status = {
      resolved: "найден",
      ambiguous: "неоднозначен",
      missing: "не найден",
    }[reference.status];
    $("references").append(
      node("p", "", `Пункт из обращения «${reference.reference}»: ${status}.`),
    );
  }
  $("draft").value = draftValue;
  $("draft").lang =
    result.draft_reply.lang === "und" ? "ru" : result.draft_reply.lang;
  $("decision-note").value = noteValue;
  updateDirty();
  const generation = result.generation;
  let cost = "Расход API не измерен";
  if (generation.cost_status === "no_api_calls") cost = "Вызовов API модели: 0";
  else if (generation.api_cost_usd !== null)
    cost = `Оценка API: $${generation.api_cost_usd.toFixed(6)}`;
  $("run-meta").textContent =
    `${PROVENANCE[result.draft_reply.provenance]} · анализ ядра: ${current.analysis_ms} мс · ${cost}. Уверенность — некалиброванная эвристика. Доставка игроку не выполнялась.`;
}

function renderTicket() {
  $("empty-state").hidden = tickets.length > 0;
  $("ticket-workspace").hidden = !current;
  if (!current) return;
  updateWorkflowActions();
  $("ticket-short-id").textContent = `#${shortId(current.id)}`;
  $("case-title").textContent = current.title;
  $("case-meta").textContent =
    `${dateTime(current.created_at)} · версия ${current.revision} · ${current.connector ? (current.connector.mode === "game" ? "Подписанный игровой сервер" : "Тестовый коннектор · MOCK") : current.source === "demo" ? "Вымышленный пример" : "Добавлено оператором"}`;
  $("case-labels").replaceChildren(
    badge(current.payload.kind === "complaint" ? "Жалоба" : "Репорт"),
    badge(STATUS[current.status], statusKind(current.status)),
  );
  const messages = $("messages");
  messages.replaceChildren();
  let attachments = 0;
  current.payload.messages.forEach((message) => {
    const item = node(
      "article",
      `message${message.role === "reported_player" ? " other" : ""}`,
    );
    item.append(
      node(
        "div",
        "message-role",
        message.role === "player" ? "Игрок · заявитель" : "Второй участник",
      ),
    );
    const text = node("div", "message-text", message.text);
    if (current.analysis?.lang !== "und" && current.analysis?.lang)
      text.lang = current.analysis.lang;
    item.append(text);
    if (message.attachments)
      item.append(
        node(
          "div",
          "message-attachments",
          `Заявлено вложений: ${message.attachments}`,
        ),
      );
    attachments += message.attachments;
    messages.append(item);
  });
  const chat = $("chat-messages");
  chat.replaceChildren();
  for (const message of current.chat_messages || []) {
    const item = node("article", `chat-message ${message.role === "admin" ? "admin" : "player"}`);
    item.append(node("div", "message-role", `${message.role === "admin" ? "Администратор" : "Игрок"} · ${dateTime(message.created_at)}`), node("div", "message-text", message.text));
    chat.append(item);
  }
  if (!current.chat_messages?.length) chat.append(node("p", "section-description", "Сообщений пока нет. Начните обсуждение."));
  $("evidence").textContent = attachments
    ? `Вложений по сведениям обращения: ${attachments}. Изображения системе не предоставлены и не просмотрены.`
    : "Вложений не указано. Наличие или отсутствие нарушения не установлено.";
  const extracted = $("extracted");
  extracted.replaceChildren();
  const fields = current.analysis
    ? [
        [
          "Ник",
          current.analysis.extracted.nickname || "Не указан / не извлечён",
        ],
        [
          "Сервер",
          current.analysis.extracted.server || "Не указан / неоднозначен",
        ],
        [
          "Пункты",
          current.analysis.extracted.rule_refs.join(", ") || "Не указаны",
        ],
      ]
    : [["Анализ", "Ещё не выполнен"]];
  for (const [label, value] of fields) {
    const row = node("div");
    row.append(node("dt", "", label), node("dd", "", value));
    extracted.append(row);
  }
  $("analyze-label").textContent = current.analysis
    ? "Повторить анализ"
    : "Проанализировать";
  renderAnalysis();
  renderHistory();
  renderConnector();
  setTab(activeTab);
}

async function selectTicket(id, force = false) {
  if (busy || (!force && !canLeave())) return;
  if (!force && id === current?.id) {
    openDetails();
    return;
  }
  const sequence = ++selectionSequence;
  setBusy(true);
  try {
    const ticket = await api(`/tickets/${id}`);
    if (sequence !== selectionSequence) return;
    current = ticket;
    draftValue = ticket.edited_draft || "";
    noteValue = "";
    dirty = false;
    activeTab = "result";
    renderQueue();
    renderTicket();
    openDetails();
  } catch (error) {
    notify(errorText(error));
  } finally {
    if (sequence === selectionSequence) setBusy(false);
  }
}

async function refreshQueue() {
  tickets = (await api("/tickets")).tickets;
  renderQueue();
}

async function start() {
  if (busy) return;
  setBusy(true);
  try {
    const [settings, queue] = await Promise.all([
      api("/config"),
      api("/tickets"),
    ]);
    config = settings;
    $("assistant-model").textContent = config.ai?.draft_configured ? `${config.provider} · ${config.assistant_model}` : "ИИ не настроен";
    $("assistant-status").textContent = config.ai?.draft_configured ? "Настройка есть; связь с ИИ ещё не проверена." : config.ai?.state === "configuration_error" ? "Проверьте ключ и настройки модели на сервере." : "Включён локальный шаблонный режим.";
    $("assistant-submit").disabled = !config.ai?.assistant_configured;
    $("assistant-check").disabled = !config.ai?.draft_configured;
    $("operator-name").textContent = config.operator.username;
    $("operator-role").textContent = config.operator.local_development ? "Явный режим разработки" : config.operator.role;
    tickets = queue.tickets;
    $("connection-dot").className = "status-dot online";
    $("connection-label").textContent = "Сервер подключён";
    $("about-provider").textContent =
      ["responses", "capybara", "gemini"].includes(config.provider)
        ? "внешняя модель включена явно; запрос анализа может вызвать API."
        : "локальные шаблоны, внешняя модель выключена.";
    $("error-banner").hidden = true;
    renderQueue();
    if (current) {
      current = await api(`/tickets/${current.id}`);
      draftValue = current.edited_draft || "";
      noteValue = "";
      dirty = false;
      renderTicket();
    }
    if (!["reports", "applications"].includes(admin.view)) renderSecondary();
    // Leave the table visible until an operator chooses a case.
  } catch (error) {
    $("connection-dot").className = "status-dot offline";
    $("connection-label").textContent = "Нет подключения";
    $("error-text").textContent = errorText(error);
    $("error-banner").hidden = false;
    $("empty-state").hidden = false;
  } finally {
    setBusy(false);
  }
}

async function seedExamples() {
  if (busy || !canLeave()) return;
  setBusy(true);
  try {
    const result = await api("/demo", { method: "POST" });
    await refreshQueue();
    setBusy(false);
    if (tickets.length) await selectTicket(tickets[0].id, true);
    notify(
      result.created
        ? "Добавлено 6 вымышленных примеров. Анализ запускается отдельно."
        : "Примеры уже в очереди. Повторных копий не создано.",
    );
  } catch (error) {
    notify(errorText(error));
  } finally {
    setBusy(false);
  }
}

async function analyzeCurrent() {
  if (!current || busy || !canLeave()) return;
  if (
    current.analysis &&
    !window.confirm(
      "Повторить анализ? Текущий черновик будет заменён. Сохранённые версии останутся в истории.",
    )
  )
    return;
  setBusy(true);
  const id = current.id;
  try {
    current = await api(`/tickets/${id}/analyze`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: current.revision }),
    });
    draftValue = current.edited_draft;
    noteValue = "";
    dirty = false;
    activeTab = "result";
    renderTicket();
    await refreshQueue();
    notify("Разбор готов. Проверьте основания и черновик перед сохранением.");
  } catch (error) {
    notify(errorText(error));
  } finally {
    setBusy(false);
  }
}

async function saveDecision(action) {
  if (!current?.analysis || busy) return;
  if (!draftValue.trim()) {
    $("draft").focus();
    notify("Черновик не должен быть пустым.");
    return;
  }
  if (action === "escalate" && current.connector) {
    await performMockAction("escalate");
    return;
  }
  setBusy(true);
  try {
    current = await api(`/tickets/${current.id}/decision`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: current.revision,
        draft: draftValue,
        action,
        note: noteValue,
      }),
    });
    draftValue = current.edited_draft;
    noteValue = "";
    dirty = false;
    renderTicket();
    await refreshQueue();
    notify(
      action === "escalate"
        ? "Решение сохранено и отмечено для человека. Передачи в игровую систему нет."
        : "Правка сохранена на сервере. Ответ игроку не отправлялся.",
    );
  } catch (error) {
    notify(errorText(error));
  } finally {
    setBusy(false);
  }
}

async function createTicket(event) {
  event.preventDefault();
  if (busy) return;
  const text = $("new-message").value.trim(),
    reply = $("new-reply").value.trim();
  const title = $("new-title").value.trim(),
    attachments = Number($("new-attachments").value);
  if (
    !text ||
    !title ||
    !Number.isSafeInteger(attachments) ||
    attachments < 0
  ) {
    $("new-error").hidden = false;
    $("new-error").textContent =
      "Введите заголовок, сообщение и неотрицательное целое количество вложений.";
    return;
  }
  setBusy(true);
  $("new-error").hidden = true;
  try {
    const messages = [{ role: "player", text, attachments }];
    if (reply)
      messages.push({ role: "reported_player", text: reply, attachments: 0 });
    const ticket = await api("/tickets", {
      method: "POST",
      body: JSON.stringify({
        title,
        request: { kind: $("new-kind").value, messages },
      }),
    });
    $("new-dialog").close();
    $("new-form").reset();
    await refreshQueue();
    setBusy(false);
    await selectTicket(ticket.id, true);
    notify("Обращение добавлено. Запустите анализ, когда будете готовы.");
  } catch (error) {
    $("new-error").textContent = errorText(error);
    $("new-error").hidden = false;
  } finally {
    setBusy(false);
  }
}

for (const id of ["search", "filter-status", "filter-lang", "filter-priority"])
  $(id).addEventListener(id === "search" ? "input" : "change", renderQueue);
$("new-ticket").addEventListener("click", () => {
  if (canLeave()) {
    $("new-error").hidden = true;
    $("new-dialog").showModal();
  }
});
$("new-form").addEventListener("submit", createTicket);
$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!current || busy) return;
  const text = $("chat-text").value.trim();
  if (!text) return;
  setBusy(true);
  try {
    current = await api(`/tickets/${current.id}/chat`, { method: "POST", body: JSON.stringify({expected_revision: current.revision, text}) });
    $("chat-text").value = "";
    renderTicket();
    await refreshQueue();
    notify("Сообщение сохранено локально. Доставка игроку не подключена.");
  } catch (error) { notify(errorText(error)); }
  finally { setBusy(false); }
});
$("add-demo").addEventListener("click", seedExamples);
$("empty-demo").addEventListener("click", seedExamples);
$("analyze").addEventListener("click", analyzeCurrent);
$("save-decision").addEventListener("click", () => saveDecision("save"));
$("escalate").addEventListener("click", () => saveDecision("escalate"));
$("draft").addEventListener("input", () => {
  draftValue = $("draft").value;
  updateDirty();
});
$("decision-note").addEventListener("input", () => {
  noteValue = $("decision-note").value;
  updateDirty();
});
$("restore-draft").addEventListener("click", () => {
  if (
    draftValue !== current.analysis.draft_reply.text &&
    !window.confirm(
      "Вернуть исходный черновик? Несохранённый текст будет заменён.",
    )
  )
    return;
  draftValue = current.analysis.draft_reply.text;
  $("draft").value = draftValue;
  updateDirty();
});
$("copy-draft").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(draftValue);
    notify("Черновик скопирован.");
  } catch {
    $("draft").focus();
    $("draft").select();
    notify("Текст выделен. Скопируйте его сочетанием Ctrl+C / ⌘C.");
  }
});
$("tab-result").addEventListener("click", () => setTab("result"));
$("tab-history").addEventListener("click", () => setTab("history"));
$("tab-outbox").addEventListener("click", () => setTab("outbox"));
for (const name of ["result", "history", "outbox"])
  $(`tab-${name}`).addEventListener("keydown", (event) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const tabs = ["result", "history", "outbox"];
      const next =
        event.key === "Home"
          ? "result"
          : event.key === "End"
            ? "outbox"
            : tabs[(tabs.indexOf(activeTab) + (event.key === "ArrowLeft" ? 2 : 1)) % tabs.length];
      setTab(next);
      $(`tab-${next}`).focus();
    }
  });

for (const id of ["help"])
  $(id).addEventListener("click", () => $("about-dialog").showModal());
for (const button of document.querySelectorAll("[data-close]"))
  button.addEventListener("click", () => $(button.dataset.close).close());
$("refresh").addEventListener("click", () => {
  if (canLeave()) start();
});
$("retry").addEventListener("click", () => {
  if (canLeave()) start();
});
window.addEventListener("beforeunload", (event) => {
  if (dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("keydown", (event) => {
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key.toLowerCase() === "s" &&
    current?.analysis &&
    !document.querySelector("dialog[open]")
  ) {
    event.preventDefault();
    saveDecision("save");
  }
});
// Boot is owned by admin-views.js, after all views are registered.
