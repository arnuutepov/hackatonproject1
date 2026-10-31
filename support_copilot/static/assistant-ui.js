"use strict";
// Read-only advice, separate from player chat and outbox. No credential in browser.
let assistantHistory = [];
const assistantForm = document.getElementById("assistant-form");
assistantForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!current) return;
  const ticketId = current.id;
  const question = document.getElementById("assistant-question").value.trim();
  if (!question) return;
  const output = document.getElementById("assistant-messages");
  const submit = document.getElementById("assistant-submit");
  output.replaceChildren(node("p", "section-description", "Готовлю совет по обращению…"));
  submit.disabled = true;
  submit.dataset.pending = "true";
  try {
    const result = await api(`/tickets/${ticketId}/assistant`, {method: "POST", body: JSON.stringify({expected_revision: current.revision, question, history: assistantHistory})});
    if (!current || current.id !== ticketId) return;
    assistantHistory.push({role: "user", content: question}, {role: "assistant", content: result.answer.slice(0, 2000)});
    assistantHistory = assistantHistory.slice(-4);
    output.replaceChildren(node("p", "section-description", question), node("p", "", result.answer));
    for (const citation of result.citations) output.append(node("p", "section-description", `${citation.source_id}: ${citation.quote}`));
    const usage = result.usage;
    const cost = result.cost_usd == null ? "стоимость неизвестна" : `оценка $${result.cost_usd.toFixed(6)}`;
    output.append(node("p", "section-description", `${result.model || result.provider} · ${usage ? usage.input_tokens + usage.output_tokens + " токенов" : "расход не передан"} · ${cost}`));
  } catch (error) {
    if (current?.id === ticketId) output.replaceChildren(node("p", "", "ИИ недоступен. Проверьте серверный ключ, баланс и доступность модели. Используйте проверенный черновик."));
  } finally { delete submit.dataset.pending; submit.disabled = busy || !config?.ai?.assistant_configured; }
});

document.getElementById("assistant-check").addEventListener("click", async () => {
  const button = document.getElementById("assistant-check");
  const status = document.getElementById("assistant-status");
  button.disabled = true;
  button.dataset.pending = "true";
  status.textContent = "Проверяю ИИ на тестовом обращении…";
  try {
    const result = await api("/ai/check", {method: "POST"});
    const errors = {
      draft_provider_authentication: "Ключ отклонён или у проекта нет доступа.",
      draft_provider_rate_limited: "Исчерпана квота или превышен лимит запросов.",
      draft_provider_model_unavailable: "Модель недоступна для этого проекта.",
      draft_provider_timeout: "Провайдер не ответил вовремя.",
      draft_budget_exceeded: "Генерация остановлена: превышен бюджет запроса.",
      draft_budget_unverified: "Для модели не задан проверенный тариф.",
      draft_not_configured: "На сервере отсутствует ключ или модель.",
    };
    status.textContent = result.ok ? `Связь проверена: модель вернула корректный план (${Math.round(result.elapsed_ms)} мс).` : (errors[result.error] || "Ответ ИИ не прошёл проверку; используйте безопасный черновик.");
  } catch (error) {
    status.textContent = "Не удалось выполнить проверку. Проверьте соединение с сервером.";
  } finally {
    delete button.dataset.pending;
    button.disabled = busy || !config?.ai?.draft_configured;
  }
});
// Clear prior advice as soon as another ticket is shown.
let assistantTicket = null;
new MutationObserver(() => {
  const id = typeof current === "undefined" ? null : current?.id;
  if (id !== assistantTicket) {
    assistantTicket = id;
    assistantHistory = [];
    document.getElementById("assistant-messages").replaceChildren();
  }
}).observe(document.getElementById("ticket-title") || document.body, {childList: true, subtree: true});
