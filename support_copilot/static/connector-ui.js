"use strict";

let pendingIngestion = null, pendingMockAction = null, connectorTicketId = null;
const auditStates = new Map();
const channelNames = {game_chat: "Чат игры", telegram: "Telegram"};
const connectorApi = (path, options) => requestApi(`/api/v1${path}`, options);

function renderConnector() {
  const connector = current?.connector;
  $("player-context").hidden = !connector;
  $("mock-send-controls").hidden = !connector;
  const list = $("mock-outbox-list");
  list.replaceChildren();
  if (connector) {
    if (connectorTicketId !== current.id) {
      connectorTicketId = current.id;
      $("outbound-channel").value = connector.channel;
      $("player-context").open = false;
    }
    const context = connector.context, profile = context.profile;
    $("player-context-title").textContent = `MOCK · игрок ${context.player_id} · ${profile ? profile.nickname : "профиль не найден"}`;
    const body = $("player-context-body");
    body.replaceChildren(node("p", "section-description", context.notice));
    body.append(node("p", "section-description", `Снимок: ${dateTime(context.snapshot_at)} · ${context.provider}`));
    if (profile) {
      const fields = node("dl", "extracted");
      for (const [label, value] of [["Сервер", profile.server], ["Баланс", `${profile.balance.toLocaleString("ru")} ${profile.currency}`]]) {
        const row = node("div");
        row.append(node("dt", "", label), node("dd", "", value));
        fields.append(row);
      }
      body.append(fields, node("h3", "", "Последние тестовые транзакции"));
      const labels = {pending: "В обработке", completed: "Завершена"};
      for (const tx of profile.transactions) {
        const item = node("div", "mock-transaction");
        item.append(node("span", "", `${tx.amount > 0 ? "+" : ""}${tx.amount} ${profile.currency}`),
          node("span", "", labels[tx.status] || tx.status),
          node("small", "", `${tx.id} · ${dateTime(tx.at)}`));
        body.append(item);
      }
      if (!profile.transactions.length) body.append(node("p", "section-description", "Тестовых транзакций нет."));
    } else body.append(node("p", "section-description", "Для этого ID нет тестового профиля. Баланс и история неизвестны."));
  }
  for (const event of [...(current?.outbox || [])].reverse()) {
    const item = node("article", "history-event mock-event");
    item.append(badge("MOCK · доставки нет", "amber"),
      node("h3", "", event.action === "reply" ? "Ответ игроку · имитация" : "Эскалация · имитация"),
      node("p", "", `ID ${event.player_id} · ${channelNames[event.channel]} · ${dateTime(event.at)}`),
      node("p", "mono", `action_id: ${event.action_id}`));
    const details = node("details");
    details.append(node("summary", "", "Текст и комментарий"), node("p", "history-draft", event.draft));
    if (event.note) details.append(node("p", "", event.note));
    item.append(details);
    list.append(item);
  }
  if (!current?.outbox?.length) list.append(node("p", "section-description", "Исходящих действий пока нет. Для имитации загрузите кейс через коннектор."));
  const audit = auditStates.get(current?.id);
  $("audit-status").textContent = audit
    ? audit.status === "written" ? `SQLite и файл ${audit.file}: сохранено.` : "Действие сохранено в SQLite. Файл-аудит ожидает повторной записи."
    : "SQLite хранит исходящие действия. Обновите журнал, чтобы проверить или восстановить файл-аудит.";
}

async function openSimulation() {
  if (busy || !canLeave()) return;
  setBusy(true);
  try {
    const settings = await connectorApi("/connector");
    const select = $("simulate-scenario");
    const previous = select.value || "account_kk_injection";
    select.replaceChildren(...settings.scenarios.map((item) => {
      const option = node("option", "", item.title);
      option.value = item.id;
      return option;
    }));
    const custom = node("option", "", "Своё обращение + ID игрока");
    custom.value = "custom";
    select.append(custom);
    select.value = previous;
    toggleSimulationFields();
    $("simulate-error").hidden = true;
    $("simulate-dialog").showModal();
  } catch (error) { notify(errorText(error)); }
  finally { setBusy(false); }
}

function toggleSimulationFields() {
  const custom = $("simulate-scenario").value === "custom";
  $("simulate-custom").hidden = !custom;
  for (const id of ["simulate-player", "simulate-case-title", "simulate-message"]) $(id).required = custom;
}

async function simulateTicket(event) {
  event.preventDefault();
  if (busy) return;
  const payload = {channel: $("simulate-channel").value};
  if ($("simulate-scenario").value !== "custom") payload.scenario_id = $("simulate-scenario").value;
  else {
    payload.title = $("simulate-case-title").value.trim();
    payload.player_id = Number($("simulate-player").value);
    payload.request = {kind: $("simulate-kind").value, messages: [{role: "player", text: $("simulate-message").value.trim(), attachments: 0}]};
    if (!Number.isSafeInteger(payload.player_id) || payload.player_id < 1 || !payload.title || !payload.request.messages[0].text) {
      $("simulate-error").textContent = "Введите ID игрока (положительное целое), заголовок и сообщение.";
      $("simulate-error").hidden = false;
      return;
    }
  }
  const key = JSON.stringify(payload);
  if (pendingIngestion?.key !== key) pendingIngestion = {key, event_id: crypto.randomUUID()};
  setBusy(true);
  try {
    const result = await connectorApi("/simulate-ticket", {method: "POST", body: JSON.stringify({...payload, event_id: pendingIngestion.event_id})});
    $("simulate-dialog").close();
    await refreshQueue();
    setBusy(false);
    await selectTicket(result.ticket.id, true);
    if (current?.id === result.ticket.id) pendingIngestion = null;
    notify("Тестовый кейс разобран. Профиль и транзакции вымышленные, платных вызовов нет.");
  } catch (error) {
    $("simulate-error").textContent = errorText(error);
    $("simulate-error").hidden = false;
    if (!$("simulate-dialog").open) notify("Обращение могло сохраниться. Обновите очередь; повтор с теми же полями использует прежний ID события.");
  } finally { setBusy(false); }
}

async function performMockAction(action) {
  if (!current?.connector || !current.analysis || busy) return;
  if (!draftValue.trim()) { $("draft").focus(); notify("Введите текст ответа."); return; }
  const payload = {expected_revision: current.revision, action, draft: draftValue, note: noteValue, channel: $("outbound-channel").value};
  const key = JSON.stringify({ticket_id: current.id, ...payload});
  if (pendingMockAction?.key !== key) pendingMockAction = {key, action_id: crypto.randomUUID()};
  setBusy(true);
  try {
    const result = await connectorApi(`/tickets/${current.id}/actions`, {method: "POST", body: JSON.stringify({...payload, action_id: pendingMockAction.action_id})});
    current = result.ticket;
    auditStates.set(current.id, result.audit);
    pendingMockAction = null;
    draftValue = current.edited_draft;
    noteValue = "";
    dirty = false;
    activeTab = "outbox";
    renderTicket();
    await refreshQueue();
    notify(result.audit.status === "written" ? "MOCK OUTBOX: действие и аудит сохранены. Реальной отправки нет." : "Действие сохранено в SQLite; запись файла-аудита нужно повторить во вкладке доставки.");
  } catch (error) { notify(errorText(error)); }
  finally { setBusy(false); }
}

$("simulate-ticket").addEventListener("click", openSimulation);
$("simulate-scenario").addEventListener("change", toggleSimulationFields);
$("simulate-form").addEventListener("submit", simulateTicket);
$("mock-send").addEventListener("click", () => performMockAction("reply"));
$("refresh-outbox").addEventListener("click", async () => {
  if (busy || !current || !canLeave()) return;
  setBusy(true);
  try {
    const result = await connectorApi("/outbox");
    current = await api(`/tickets/${current.id}`);
    draftValue = current.edited_draft || "";
    noteValue = "";
    dirty = false;
    auditStates.set(current.id, result.audit);
    renderTicket();
  } catch (error) { notify(errorText(error)); }
  finally { setBusy(false); }
});
