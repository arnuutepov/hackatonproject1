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
    output.append(node("p", "section-description", `${result.model || "Capybara"} · ${usage ? usage.input_tokens + usage.output_tokens + " токенов" : "расход не передан"} · стоимость уточняйте в кабинете`));
  } catch (error) {
    if (current?.id === ticketId) output.replaceChildren(node("p", "", "ИИ недоступен. Проверьте серверный ключ, баланс и доступность модели. Используйте проверенный черновик."));
  } finally { delete submit.dataset.pending; submit.disabled = config?.provider !== "capybara"; }
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
