"use strict";
// Presentation, navigation and local preferences. Decisions live in the API/SQLite.
const admin = {
  view: "reports",
  page: 0,
  pageSize: 10,
  filtered: [],
  returnFocus: null,
  request: 0,
};
const viewLabels = {
  reports: "REPORTS",
  applications: "APPLICATIONS",
  dashboard: "DASHBOARD",
  players: "PLAYERS",
  logs: "SERVER LOGS",
  settings: "SETTINGS",
};
const eventNames = {
  created: "Создано обращение",
  analyzed: "Выполнен анализ",
  saved: "Сохранена правка",
  escalated: "Передано на ручную проверку",
  take: "Взято в работу",
  release: "Назначение снято",
  resolve: "Завершено локально",
  reopen: "Открыто повторно",
  game_message: "Сообщение игрока от подписанного сервера",
  mock_reply: "Ответ записан в MOCK OUTBOX",
  mock_escalate: "Эскалация записана в MOCK OUTBOX",
};
function button(text, cls, fn) {
  const el = node("button", cls, text);
  el.type = "button";
  el.addEventListener("click", fn);
  return el;
}
function renderStats() {
  const values = [
    ["inbox", tickets.length, "TOTAL REPORTS", "В локальной очереди", ""],
    [
      "alert",
      tickets.filter((t) => ["p0", "p1"].includes(t.priority)).length,
      "HIGH PRIORITY",
      "Приоритет P0 и P1",
      "urgent",
    ],
    [
      "shield",
      tickets.filter((t) => t.needs_human).length,
      "HUMAN REVIEW",
      "Требуется участие человека",
      "needs_human",
    ],
    [
      "check",
      tickets.filter((t) => t.status === "resolved").length,
      "RESOLVED",
      "Завершено локально",
      "resolved",
    ],
  ];
  $("stats").replaceChildren(
    ...values.map(([i, n, title, sub, filter]) => {
      const b = button("", "stat", () => {
        navigate("reports");
        resetFilters();
        if (filter === "urgent") $("filter-priority").value = filter;
        else $("filter-status").value = filter;
        renderAdminQueue();
      });
      const top = node("span", "stat-top", title);
      top.append(icon(i));
      b.append(
        top,
        node("strong", "", String(n).padStart(2, "0")),
        node("div", "stat-bottom", sub),
      );
      return b;
    }),
  );
}
function renderAdminQueue() {
  renderStats();
  const query = $("search").value.trim().toLocaleLowerCase(),
    status = $("filter-status").value,
    lang = $("filter-lang").value,
    priority = $("filter-priority").value;
  admin.filtered = tickets.filter(
    (t) =>
      (admin.view !== "applications" || t.kind === "report") &&
      (!query ||
        `${t.title} ${t.preview} ${t.id} ${t.mentioned_nickname || ""} ${t.player_id || ""}`
          .toLocaleLowerCase()
          .includes(query)) &&
      (!status ||
        (status === "needs_human"
          ? t.needs_human
          : status === "mine"
            ? !!t.assigned_to
            : t.status === status)) &&
      (!lang || t.lang === lang) &&
      (!priority ||
        (priority === "urgent"
          ? ["p0", "p1"].includes(t.priority)
          : t.priority === priority)),
  );
  const order = $("sort-order").value;
  admin.filtered.sort((a, b) =>
    order === "priority"
      ? (a.priority || "z").localeCompare(b.priority || "z") ||
        b.created_at.localeCompare(a.created_at)
      : order === "oldest"
        ? a.created_at.localeCompare(b.created_at)
        : b.created_at.localeCompare(a.created_at),
  );
  admin.page = Math.min(
    admin.page,
    Math.max(0, Math.ceil(admin.filtered.length / admin.pageSize) - 1),
  );
  $("heading-count").textContent = tickets.length;
  $("nav-count").textContent = tickets.length;
  $("queue-count").textContent = admin.filtered.length;
  const list = $("ticket-list");
  list.replaceChildren();
  for (const t of admin.filtered.slice(
    admin.page * admin.pageSize,
    (admin.page + 1) * admin.pageSize,
  )) {
    const row = node("tr", current?.id === t.id ? "selected" : "");
    const id = node("td", "id-cell");
    id.append(
      node("strong", "", `#${shortId(t.id)}`),
      node("small", "", dateTime(t.created_at)),
    );
    const mentioned = node("td");
    const person = node("div", "person-cell");
    person.append(
      node(
        "span",
        "avatar",
        t.mentioned_nickname
          ? t.mentioned_nickname.slice(0, 2).toUpperCase()
          : "—",
      ),
      node("strong", "", t.mentioned_nickname || "Не установлен"),
    );
    mentioned.append(
      person,
      node(
        "span",
        "subtext",
        t.mentioned_nickname ? "Упоминание из текста" : "Нужен разбор",
      ),
    );
    const reporter = node("td");
    reporter.append(
      node("span", "", t.player_id ? `ID ${t.player_id}` : "Не указан"),
      node("span", "subtext", t.player_id ? "Вымышленный игрок · MOCK" : "Нет ID аккаунта"),
    );
    const reason = node("td");
    reason.append(
      node("strong", "reason-title", t.title),
      node(
        "span",
        "reason-type",
        `${t.kind === "complaint" ? "ЖАЛОБА" : "ЗАПРОС"} · ${t.lang?.toUpperCase() || "ЯЗЫК НЕ ОПРЕДЕЛЁН"}`,
      ),
    );
    const p = node("td");
    p.append(
      badge(
        t.priority
          ? `${t.priority.toUpperCase()} · ${{ p0: "Critical", p1: "High", p2: "Medium", p3: "Low" }[t.priority]}`
          : "Не определён",
        priorityKind(t.priority),
      ),
    );
    const s = node("td");
    const label =
      t.status === "reply_simulated" ? "Reply · MOCK" : t.status === "resolved"
        ? "Closed"
        : t.status === "escalated"
          ? "Human review"
          : t.assigned_to
            ? "In progress"
            : t.status === "saved"
              ? "Reviewed"
              : t.status === "analyzed"
                ? "Analyzed"
                : "Open";
    s.append(
      node(
        "span",
        `status-label ${t.status === "resolved" ? "closed" : t.assigned_to || t.status === "escalated" ? "progress" : ""}`,
        label,
      ),
    );
    if (t.source === "demo") s.append(node("span", "subtext", "Демо"));
    const actions = node("td"),
      buttons = node("div", "row-actions");
    const view = button("VIEW DETAILS", "button small outline", () =>
      selectTicket(t.id),
    );
    view.setAttribute("aria-label", `Открыть ${t.title}`);
    const take = button(
      t.assigned_to ? "TAKEN" : "QUICK TAKE",
      `button small ${t.assigned_to ? "secondary" : "primary"}`,
      () => quickTake(t),
    );
    take.setAttribute("aria-label", `Взять в работу: ${t.title}`);
    take.disabled = busy || !!t.assigned_to;
    take.dataset.taken = String(!!t.assigned_to);
    view.disabled = busy;
    buttons.append(view, take);
    actions.append(buttons);
    row.append(id, mentioned, reporter, reason, p, s, actions);
    list.append(row);
  }
  $("empty-state").hidden = tickets.length > 0;
  if (tickets.length && !admin.filtered.length) {
    const row = node("tr", "no-match"),
      cell = node("td");
    cell.colSpan = 7;
    cell.append(
      node("p", "", "Обращения по этим фильтрам не найдены."),
      button("Сбросить фильтры", "text-button", resetFilters),
    );
    row.append(cell);
    list.append(row);
  }
  updatePagination();
}
function updatePagination() {
  const pages = Math.max(1, Math.ceil(admin.filtered.length / admin.pageSize));
  $("page-info").textContent = `${admin.page + 1} / ${pages}`;
  $("page-prev").disabled = busy || admin.page === 0;
  $("page-next").disabled = busy || admin.page >= pages - 1;
  document
    .querySelectorAll('[data-taken="true"]')
    .forEach((b) => (b.disabled = true));
}
function resetFilters() {
  for (const id of [
    "search",
    "filter-status",
    "filter-priority",
    "filter-lang",
  ])
    $(id).value = "";
  admin.page = 0;
  renderAdminQueue();
}
function updateWorkflowActions() {
  if (!current) return;
  $("take-current").textContent = current.assigned_to
    ? "Снять назначение"
    : "Взять в работу";
  $("take-current").disabled = busy;
  const senior = ["lead", "admin"].includes(config?.operator?.role);
  const blocked = !current.analysis || (current.needs_human && !senior);
  $("resolve-current").textContent =
    current.status === "resolved" ? "Открыть повторно" : "Завершить";
  $("resolve-current").disabled =
    busy || (blocked && current.status !== "resolved");
  $("resolve-hint").textContent =
    current.status === "resolved"
      ? "Завершено в локальной очереди. Ответ не доставлялся игроку."
      : !current.analysis
        ? "Сначала выполните анализ обращения."
        : current.needs_human
          ? (senior ? "Для завершения ручной проверки заполните комментарий к решению. Автор будет записан в историю." : "Нужна ручная проверка руководителем. Доступны сохранение и эскалация.")
          : "Завершение изменит только состояние локальной очереди.";
}
function setBackgroundInert(value) {
  document.querySelector(".sidebar").inert = value;
  for (const child of $("main").children)
    if (child !== $("workarea") && child !== $("drawer-shade"))
      child.inert = value;
}
function openDetails() {
  admin.returnFocus = document.activeElement;
  $("workarea").hidden = false;
  $("drawer-shade").hidden = false;
  $("workarea").setAttribute("role", "dialog");
  $("workarea").setAttribute("aria-modal", "true");
  setBackgroundInert(true);
  $("close-details").focus();
}
function closeDetails() {
  if (busy || !canLeave()) return false;
  $("workarea").hidden = true;
  $("drawer-shade").hidden = true;
  setBackgroundInert(false);
  dirty = false;
  draftValue = current?.edited_draft || "";
  noteValue = "";
  if (current) renderTicket();
  if (admin.returnFocus?.isConnected) admin.returnFocus.focus();
  else $("search").focus();
  return true;
}
async function quickTake(t) {
  if (busy || !canLeave()) return;
  setBusy(true);
  try {
    const result = await api(`/tickets/${t.id}/workflow`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: t.revision, action: "take" }),
    });
    if (current?.id === t.id) {
      current = result;
      renderTicket();
    }
    await refreshQueue();
    notify("Обращение взято в работу. Назначение сохранено на сервере.");
  } catch (e) {
    notify(errorText(e));
  } finally {
    setBusy(false);
  }
}
async function workflow(action) {
  if (!current || busy) return;
  const note = action === "resolve" ? noteValue.trim() : "";
  if (action === "resolve" && current.needs_human && !note) {
    setTab("result");
    $("decision-note").focus();
    notify("Заполните комментарий: что проверено и почему обращение можно завершить.");
    return;
  }
  if (!canLeave()) return;
  setBusy(true);
  try {
    current = await api(`/tickets/${current.id}/workflow`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: current.revision, action, note }),
    });
    draftValue = current.edited_draft || "";
    noteValue = "";
    dirty = false;
    renderTicket();
    await refreshQueue();
    notify(
      {
        take: "Назначение сохранено.",
        release: "Назначение снято.",
        resolve:
          "Обращение завершено локально. Сообщение игроку не отправлено.",
        reopen: "Обращение открыто повторно.",
      }[action],
    );
  } catch (e) {
    notify(errorText(e));
  } finally {
    setBusy(false);
  }
}
function info(title, paragraphs, actions = []) {
  $("info-title").textContent = title;
  $("info-body").replaceChildren(...paragraphs.map((p) => node("p", "", p)));
  $("info-footer").replaceChildren(
    ...actions,
    button("Понятно", "button secondary", () => $("info-dialog").close()),
  );
  $("info-dialog").showModal();
}
function navigate(view) {
  if (!Object.hasOwn(viewLabels, view) || busy) return;
  if (!$("workarea").hidden && !closeDetails()) return;
  admin.view = view;
  admin.request++;
  admin.page = 0;
  $("breadcrumb-view").textContent = viewLabels[view];
  $("page-title").replaceChildren(
    document.createTextNode(viewLabels[view]),
    node("span", "title-dot", "."),
  );
  $("page-description").textContent = {
    reports: "Каждое решение начинается с фактов.",
    applications:
      "Вопросы, платежи и техническая помощь — в общей очереди поддержки.",
    dashboard:
      "Состояние обращений. Только данные вашего рабочего пространства.",
    players: "Упоминания в обращениях. Без предположений о репутации.",
    logs: "История локальных действий, сохранённых на этом сервере.",
    settings: "Настройте рабочее пространство под свой ритм.",
  }[view];
  document.querySelectorAll("[data-view]").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
    if (b.dataset.view === view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  const table = ["reports", "applications"].includes(view);
  $("reports-view").hidden = !table;
  $("secondary-view").hidden = table;
  $("table-title").textContent =
    view === "applications"
      ? "SUPPORT REQUESTS"
      : "PLAYER COMPLAINTS & REPORTS";
  if (table) renderAdminQueue();
  else renderSecondary();
}
function card(title, description) {
  const c = node("section", "content-card");
  c.append(node("h2", "", title), node("p", "", description));
  return c;
}
function renderDashboard() {
  const grid = node("div", "overview-grid");
  const priorities = card(
    "Приоритеты обращений",
    "Результат анализа помогает выбрать порядок проверки.",
  );
  for (const [key, label] of Object.entries({
    p0: "P0 · Критичный",
    p1: "P1 · Высокий",
    p2: "P2 · Обычный",
    p3: "P3 · Низкий",
    unknown: "Без анализа",
  })) {
    const n = tickets.filter((t) =>
      key === "unknown" ? !t.priority : t.priority === key,
    ).length;
    const row = node("div", "metric-row"),
      meter = node("meter");
    meter.min = 0;
    meter.max = Math.max(1, tickets.length);
    meter.value = n;
    meter.setAttribute("aria-label", label);
    row.append(node("span", "", label), meter, node("strong", "", n));
    priorities.append(row);
  }
  const process = card(
    "Сначала — важное",
    "Ваша очередь сохраняется между перезапусками приложения.",
  );
  process.append(
    node(
      "div",
      "inline-notice",
      "Платежи, доступ к аккаунтам и апелляции требуют ручной проверки. Система не обещает результат рассмотрения.",
    ),
  );
  for (const [label, n] of [
    ["В работе у оператора", tickets.filter((t) => t.assigned_to).length],
    ["Сохранено правок", tickets.filter((t) => t.status === "saved").length],
    [
      "Передано человеку",
      tickets.filter((t) => t.status === "escalated").length,
    ],
  ]) {
    const row = node("div", "setting-row");
    row.append(node("span", "", label), node("strong", "", n));
    process.append(row);
  }
  process.append(
    button("К обращениям →", "button primary", () => navigate("reports")),
  );
  grid.append(priorities, process);
  return grid;
}
function renderPlayers() {
  const wrap = node("div");
  wrap.append(
    node(
      "p",
      "inline-notice",
      "Показаны никнеймы, извлечённые из проанализированных текстов. Роль, личность и игровой аккаунт не подтверждены; история не определяет виновность.",
    ),
  );
  const grouped = new Map();
  for (const t of tickets) {
    if (!t.mentioned_nickname) continue;
    const list = grouped.get(t.mentioned_nickname) || [];
    list.push(t);
    grouped.set(t.mentioned_nickname, list);
  }
  const grid = node("div", "player-list");
  for (const [name, items] of grouped) {
    const item = node("article", "player-card");
    item.append(
      node("span", "avatar", name.slice(0, 2).toUpperCase()),
      node("h3", "", name),
      node("p", "", `Упоминаний в обращениях: ${items.length}`),
      node("p", "", "Фракция и дата регистрации: нет данных."),
      button("Посмотреть обращения →", "button secondary", () => {
        navigate("reports");
        $("search").value = name;
        renderAdminQueue();
      }),
    );
    grid.append(item);
  }
  if (!grouped.size)
    grid.append(
      card(
        "Пока нет извлечённых никнеймов",
        "Выполните анализ обращения. Данные аккаунтов из игры не подключены.",
      ),
    );
  wrap.append(grid);
  return wrap;
}
async function renderLogs() {
  const request = ++admin.request;
  const c = card(
    "Журнал действий",
    "Время показано в часовом поясе браузера. Игровые события и серверные логи не подключены.",
  );
  $("secondary-view").replaceChildren(c);
  const latest = [...tickets]
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, 50);
  c.append(
    node("p", "muted", "Загружаем историю последних 50 обновлённых обращений…"),
  );
  try {
    const records = [];
    for (let offset = 0; offset < latest.length; offset += 5) {
      if (request !== admin.request) return;
      const batch = await Promise.all(
        latest.slice(offset, offset + 5).map((t) => api(`/tickets/${t.id}`)),
      );
      for (const t of batch)
        for (const e of t.events)
          records.push({ ...e, title: t.title, ticketId: t.id });
    }
    if (request !== admin.request) return;
    c.lastChild.remove();
    records.sort((a, b) => b.at.localeCompare(a.at));
    for (const e of records.slice(0, 150)) {
      const row = node("article", "log-item");
      row.append(
        node("time", "", dateTime(e.at)),
        node("span", "", `${eventNames[e.kind] || e.kind} · ${e.title}`),
        button(`#${shortId(e.ticketId)} →`, "text-button", () =>
          selectTicket(e.ticketId),
        ),
      );
      c.append(row);
    }
    if (!records.length)
      c.append(node("p", "", "Пока нет событий. Создайте первое обращение."));
  } catch (e) {
    if (request === admin.request)
      c.append(node("p", "form-error", errorText(e)));
  }
}
function preference(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem("area-" + key)) ?? fallback;
  } catch {
    return fallback;
  }
}
function savePreference(key, value) {
  try {
    localStorage.setItem("area-" + key, JSON.stringify(value));
  } catch {
    notify("Настройка применена до перезагрузки: память браузера недоступна.");
  }
}
function renderSettings() {
  const c = card(
    "Ваше рабочее пространство",
    "Настройки вида сохраняются в этом браузере.",
  );
  for (const [key, label, description] of [
    [
      "compact",
      "Компактная таблица",
      "Меньше вертикальных отступов, больше обращений на экране.",
    ],
    [
      "no-motion",
      "Уменьшить движение",
      "Отключить анимации переходов и открытия карточек.",
    ],
  ]) {
    const row = node("label", "setting-row"),
      text = node("span", "", label);
    text.append(node("small", "", description));
    const input = node("input");
    input.type = "checkbox";
    input.checked = document.body.classList.contains(key);
    input.addEventListener("change", () => {
      document.body.classList.toggle(key, input.checked);
      savePreference(key, input.checked);
    });
    row.append(text, input);
    c.append(row);
  }
  const mode = node("div", "setting-row");
  mode.append(
    node("span", "", "Режим анализа"),
    badge(
      config?.provider === "responses" ? "Внешняя модель" : "Локальные шаблоны",
    ),
  );
  c.append(
    mode,
    node(
      "p",
      "",
      "Сессия и роль проверяются сервером. Игровые санкции и реальная доставка ответов не подключены.",
    ),
    button("О системе и ограничениях", "button secondary", () =>
      $("about-dialog").showModal(),
    ),
    button("Завершить сеанс", "text-button", logoutSession),
  );
  return c;
}
function renderSecondary() {
  if (admin.view === "logs") {
    renderLogs();
    return;
  }
  $("secondary-view").replaceChildren(
    admin.view === "dashboard"
      ? renderDashboard()
      : admin.view === "players"
        ? renderPlayers()
        : renderSettings(),
  );
}
async function logoutSession() {
  if (busy || !canLeave()) return;
  try {
    const result = await requestApi('/auth/logout', {method:'POST'});
    dirty = false;
    location.replace(result.redirect || '/auth/login');
  } catch (error) { notify(errorText(error)); }
}

for (const b of document.querySelectorAll("[data-view]")) {
  b.setAttribute(
    "aria-label",
    `${viewLabels[b.dataset.view]} — ${b.querySelector("small")?.textContent || ""}`,
  );
  b.addEventListener("click", () => navigate(b.dataset.view));
}
$("close-details").onclick = closeDetails;
$("drawer-shade").onclick = closeDetails;
$("take-current").onclick = () =>
  workflow(current.assigned_to ? "release" : "take");
$("resolve-current").onclick = () => {
  const action = current.status === "resolved" ? "reopen" : "resolve";
  info(
    action === "resolve"
      ? "Завершить обращение?"
      : "Открыть обращение повторно?",
    [
      action === "resolve"
        ? "Будет изменён только статус локальной очереди. Сохранённый ответ остаётся в истории; игроку ничего не отправляется."
        : "Обращение вернётся в работу с сохранённой историей.",
    ],
    [
      button(
        action === "resolve" ? "Завершить" : "Открыть",
        "button primary",
        () => {
          $("info-dialog").close();
          workflow(action);
        },
      ),
    ],
  );
};
for (const id of ["ban-info", "mute-info"])
  $(id).onclick = () =>
    info(
      id === "ban-info"
        ? "BAN · требуется интеграция"
        : "MUTE · требуется интеграция",
      [
        "Сервер игры не подключён. Это действие недоступно: оно не создаёт наказание и не меняет игровой аккаунт.",
        "Сначала требуется подтверждение личности игрока, материалов и полномочий оператора. Для текущего обращения можно сохранить обоснование или отметить передачу человеку.",
      ],
    );
$("logout").onclick = logoutSession;
$("reset-filters").onclick = resetFilters;
$("sort-order").onchange = () => {
  admin.page = 0;
  renderAdminQueue();
};
$("page-prev").onclick = () => {
  admin.page--;
  renderAdminQueue();
};
$("page-next").onclick = () => {
  admin.page++;
  renderAdminQueue();
};
document.querySelector(".brand").onclick = (e) => {
  e.preventDefault();
  navigate("reports");
};
document.addEventListener("keydown", (e) => {
  if (document.querySelector("dialog[open]")) return;
  if (e.key === "Escape" && !$("workarea").hidden) {
    e.preventDefault();
    closeDetails();
  }
  if (
    (e.ctrlKey || e.metaKey) &&
    e.key.toLowerCase() === "k"
  ) {
    e.preventDefault();
    navigate("reports");
    if ($("workarea").hidden) $("search").focus();
  }
  if (e.key === "Tab" && !$("workarea").hidden) {
    const focusable = [
      ...$("workarea").querySelectorAll(
        'button:not(:disabled),textarea,input,select,a,[tabindex="0"],summary',
      ),
    ].filter((el) => el.getClientRects().length);
    const first = focusable[0],
      last = focusable.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  }
});
for (const key of ["compact", "no-motion"])
  document.body.classList.toggle(key, preference(key, false));
start();
