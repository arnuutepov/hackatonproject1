"use strict";
// Transport boundary: same-origin operator API, bounded requests, sanitized errors.
function errorText(error) {
  if (error.name === "AbortError")
    return "Время ожидания истекло. Сервер мог завершить запрос: обновите очередь перед повтором.";
  if (error.status === 409)
    return "Обращение изменилось в другом окне или ещё не разобрано. Сохраните свой текст и обновите очередь.";
  if (error.status === 503)
    return "Сервис или локальное хранилище недоступны. Проверьте наличие rules.json и taxonomy.json, права на базу и журнал сервера.";
  if (error.status === 422)
    return "Проверьте поля: нужен непустой текст, допустимое количество вложений и актуальная версия обращения.";
  if (error.status === 403)
    return "Откройте рабочее место на localhost или 127.0.0.1. Запросы с других сайтов запрещены.";
  if (error.status === 404)
    return "Обращение или пункт не найден. Обновите очередь.";
  return "Не удалось выполнить запрос. Проверьте соединение с локальным сервером.";
}

async function api(path, options = {}) {
  return requestApi(`/operator${path}`, options);
}

async function requestApi(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      ...options,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
    });
    if (response.status === 401) location.replace('/auth/login');
    if (!response.ok) {
      const error = new Error("Request failed");
      error.status = response.status;
      throw error;
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}
