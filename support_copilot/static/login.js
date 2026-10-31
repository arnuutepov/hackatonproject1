"use strict";
document.getElementById('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.getElementById('login-submit'), error = document.getElementById('login-error');
  button.disabled = true; error.hidden = true;
  try {
    const response = await fetch('/auth/login', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username:document.getElementById('username').value, password:document.getElementById('password').value})});
    document.getElementById('password').value = '';
    if (!response.ok) {
      error.textContent = response.status === 429 ? 'Слишком много попыток. Повторите через минуту.' : response.status === 403 ? 'Требуется HTTPS. Для локального HTTP владелец должен явно включить COPILOT_INSECURE_LOCAL_HTTP=1.' : 'Вход не выполнен. Проверьте учётные данные и доступность сервиса.';
      error.hidden = false; return;
    }
    location.replace('/');
  } catch { error.textContent = 'Сервис недоступен. Повторите вход.'; error.hidden = false; }
  finally { button.disabled = false; }
});
