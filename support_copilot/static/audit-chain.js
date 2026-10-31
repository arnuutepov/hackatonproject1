"use strict";
// Audit chain view: read-only journal. Verification and anchoring run on the server.
const chainState = {report: null, focus: null};
const chainEvents = {
  genesis: "Создание цепочки",
  "triage.completed": "Разбор завершён",
  "draft.sent": "Ответ записан · MOCK",
  "escalation.human": "Передано человеку",
  "operator.login": "Вход оператора",
  "operator.logout": "Выход оператора",
  "operator.created": "Создан оператор",
  "operator.role_change": "Изменена роль",
  "operator.password_reset": "Сброшен пароль",
  "game.event.accepted": "Подписанное событие игры",
  "key.rotated": "Смена ключа подписи",
  "chain.restored": "Восстановление из бэкапа",
};
const chainChecks = {
  index: "разрыв нумерации блоков",
  genesis: "первый блок не соответствует настройкам",
  genesis_key: "в первом блоке другой открытый ключ",
  configured_key: "цепочка подписана не ключом этого сервера",
  payload_digest: "изменены данные блока",
  block_hash: "изменён заголовок блока",
  prev_hash: "нарушена связь с предыдущим блоком",
  signature: "подпись блока не прошла проверку",
  signature_key_unavailable: "нет ключа для проверки подписи",
  timestamp: "нарушен порядок времени",
  malformed_block: "повреждена структура блока",
  tail_truncated: "удалён хвост цепочки",
  mirror_mismatch: "блок расходится с зеркалом на диске",
  mirror_corrupt: "повреждено зеркало на диске",
  anchor_mismatch: "блок расходится с подписанным якорем",
  anchor_signature: "подпись якоря не прошла проверку",
};
const chainApi = (path, options) => requestApi(`/audit${path}`, options);

function chainStatus() {
  const report = chainState.report;
  const box = node("div", "chain-status");
  if (!report) {
    box.append(badge("Не проверено"), node("span", "", "Нажмите «Проверить целостность»: сервер пройдёт всю цепочку от первого блока."));
  } else if (report.valid) {
    box.append(badge("Цепочка цела", "green"), node("span", "", `Проверено блоков: ${report.blocks_checked}. Зеркало: ${report.mirror.status}. Якоря: ${report.anchors.status}.`));
  } else {
    const found = report.first_failure;
    box.classList.add("broken");
    box.append(badge("Цепочка сломана", "red"), node("span", "", `Блок #${found.index}: ${chainChecks[found.check] || found.check}.`));
  }
  return box;
}

function chainTable(blocks) {
  const wrap = node("div", "table-scroll"), table = node("table", "chain-table");
  const head = node("thead"), row = node("tr");
  for (const title of ["БЛОК", "ВРЕМЯ", "СОБЫТИЕ", "АКТОР", "ХЕШ", "ДАННЫЕ"]) row.append(node("th", "", title));
  head.append(row);
  const body = node("tbody");
  for (const block of blocks) {
    const item = node("tr");
    item.id = `chain-block-${block.index}`;
    const broken = chainState.report?.first_failure?.index === block.index;
    if (broken) item.classList.add("chain-broken");
    if (chainState.focus === block.index) item.classList.add("chain-focus");
    const details = node("details");
    details.append(node("summary", "", "Показать"), node("pre", "chain-payload", block.payload ? JSON.stringify(block.payload, null, 2) : block.payload_json));
    const cell = node("td");
    cell.append(details);
    item.append(node("td", "mono", `#${block.index}`), node("td", "", dateTime(block.created_at)),
      node("td", "", chainEvents[block.event_type] || block.event_type), node("td", "mono", shortActor(block.actor)),
      node("td", "mono", block.block_hash.slice(0, 16)), cell);
    body.append(item);
  }
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function shortActor(actor) {
  return actor.length > 16 ? actor.slice(0, 8) : actor;
}

async function renderChain() {
  const request = ++admin.request, view = $("secondary-view");
  view.replaceChildren(card("Аудит-цепочка", "Загрузка журнала…"));
  try {
    const before = chainState.focus === null ? "" : `&before=${chainState.focus + 26}`;
    const data = await chainApi(`/blocks?limit=50${before}`);
    if (request !== admin.request) return;
    const summary = card("Аудит-цепочка", "Подписанная хеш-цепочка на одном сервере. Правка любого блока обнаруживается; удаление хвоста — при сравнении с зеркалом или якорем. Текста обращений здесь нет, только дайджесты.");
    summary.append(node("p", "chain-height", data.height < 0 ? "Цепочка пока пуста" : `Голова · блок #${data.height}`),
      node("p", "mono chain-hash", data.head_hash || "—"), chainStatus());
    const actions = node("div", "chain-actions");
    actions.append(button("Проверить целостность", "button primary", verifyChain));
    if (config?.operator?.role === "admin") actions.append(button("Зафиксировать якорь", "button secondary", anchorChain));
    if (chainState.focus !== null) actions.append(button("К голове цепочки", "text-button", () => { chainState.focus = null; renderChain(); }));
    summary.append(actions);
    const journal = card("Блоки", data.blocks.length ? "Новые сверху. Показано до 50 блоков." : "Первый блок появится вместе с первым действием.");
    if (data.blocks.length) journal.append(chainTable(data.blocks));
    view.replaceChildren(summary, journal);
    if (chainState.focus !== null) document.getElementById(`chain-block-${chainState.focus}`)?.scrollIntoView({block: "center"});
  } catch (error) {
    if (request === admin.request) view.replaceChildren(node("p", "form-error", errorText(error)));
  }
}

async function verifyChain() {
  if (busy) return;
  setBusy(true);
  try {
    chainState.report = await chainApi("/verify");
    if (chainState.report.first_failure) chainState.focus = chainState.report.first_failure.index;
    notify(chainState.report.valid ? "Цепочка цела." : "Цепочка сломана: откройте выделенный блок.");
  } catch (error) { notify(errorText(error)); }
  finally { setBusy(false); }
  await renderChain();
}

async function anchorChain() {
  if (busy) return;
  setBusy(true);
  try {
    const anchor = await chainApi("/checkpoint", {method: "POST"});
    notify(`Якорь записан: блок #${anchor.height}.`);
  } catch (error) { notify(error.status === 409 ? "Сломанную цепочку нельзя зафиксировать якорем." : errorText(error)); }
  finally { setBusy(false); }
}

function openChainBlock(index) {
  if (!canLeave()) return;
  chainState.focus = index;
  closeDetails();
  navigate("chain");
}
