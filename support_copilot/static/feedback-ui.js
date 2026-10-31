"use strict";
$("feedback-open").addEventListener("click",async () => {
  if (!["lead","admin"].includes(config?.operator?.role)) {
    info("Разметка правок",["Правки автоматически попадают в очередь после сохранения. Второй разметчик с ролью lead/admin должен проверить их; автодообучения нет."]);
    return;
  }
  try {
    const queue=await api("/feedback");
    info("Разметка правок",["Проверьте правку независимо от автора. Заполните все поля; подтверждение не меняет решение по обращению и не запускает обучение."]);
    const host=$("info-body");
    const pending=queue.items.filter(item=>!item.reviewer).slice(0,10);
    if (!pending.length) host.append(node("p","","Нет правок для проверки."));
    for (const item of pending) {
      const card=node("section","private-chat");
      card.append(node("h3","",`Правка #${item.id} · версия ${item.revision}`),
        node("p","",`Черновик: ${item.original_draft}`),node("p","",`Правка: ${item.edited_draft}`));
      const fields={};
      const choices={category:config.categories,priority:config.priorities,
        lang:{ru:"Русский",kk:"Қазақша",uz:"O‘zbekcha",en:"English",es:"Español",pt:"Português",und:"Не определён"},
        needs_human:{true:"Нужен человек",false:"Человек не обязателен"}};
      const labels={category:"Категория",priority:"Приоритет",lang:"Язык",needs_human:"Участие человека"};
      for (const [key,options] of Object.entries(choices)) {
        const label=node("label","",labels[key]);const select=document.createElement("select");
        select.append(new Option("Выберите…",""));
        for (const [value,title] of Object.entries(options)) select.append(new Option(title,value));
        label.append(select);card.append(label);fields[key]=select;
      }
      const result=node("p","section-description","");
      const submit=button("Подтвердить разметку","button primary",async()=>{
        if (Object.values(fields).some(select=>!select.value)) {result.textContent="Заполните все поля.";return;}
        submit.disabled=true;
        try {
          const payload=Object.fromEntries(Object.entries(fields).map(([key,select])=>[key,select.value]));
          payload.needs_human=payload.needs_human==="true";
          await api(`/feedback/${item.id}/approve`,{method:"POST",body:JSON.stringify(payload)});
          result.textContent="Подтверждено вторым оператором. В обучение автоматически не передаётся.";
        } catch {result.textContent="Не удалось подтвердить: нужен другой оператор и категория того же типа обращения.";submit.disabled=false;}
      });
      card.append(submit,result);host.append(card);
    }
  } catch {info("Разметка правок",["Очередь недоступна. Нужна роль lead/admin и работающий сервер."]);}
});

$("duplicate-check").addEventListener("click",async()=>{
  if (!current) return;
  const id=current.id;
  $("duplicate-results").textContent="Проверяю последние 500 обращений…";
  try {
    const result=await api(`/tickets/${id}/duplicates`);
    if (current?.id!==id) return;
    $("duplicate-results").textContent=result.candidates.length
      ? "Возможные дубли: "+result.candidates.map(c=>c.ticket_id.slice(0,8)).join(", ")+". Сравните вручную; объединение не выполнено."
      : "Похожих длинных сообщений не найдено. Короткие шаблонные тексты не считаются доказательством дубля.";
  } catch {$("duplicate-results").textContent="Проверка недоступна.";}
});
