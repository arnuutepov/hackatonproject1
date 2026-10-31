# API и пакетная проверка для жюри

> **Обновление 31.10:** токен необязателен. Если `COPILOT_TRIAGE_TOKEN` не задан, `/triage`
> и `/health` отвечают без авторизации на 127.0.0.1:8000. Если задан — нужен `Authorization: Bearer`.
> `make predict` принимает и контракт `{kind, messages}`, и сырой формат организаторов
> (`ticket_id`, `images_count`, сообщения `admin` и всё после них отбрасываются); `ticket_id`/`id`
> копируется в выходную строку; некорректная строка получает ответ с `needs_human=true`.

Сначала установите зависимости и поместите два разрешённых справочника:
Windows — `quickstart.ps1 -SetupOnly`; Linux/macOS — команды README.md.
`.env.example` содержит имена настроек; `.env` автоматически не загружается.

## HTTP POST /triage — без браузера

В PowerShell в папке проекта:

```powershell
$env:COPILOT_DRAFT_PROVIDER = 'template'
$env:COPILOT_TRIAGE_TOKEN = & .\.venv\Scripts\python.exe -c 'import secrets; print(secrets.token_urlsafe(32))'
Write-Host "Скопируйте этот токен во второе окно PowerShell: $env:COPILOT_TRIAGE_TOKEN"
.\.venv\Scripts\python.exe -m uvicorn support_copilot.api_only:app --host 127.0.0.1 --port 8002
```

Токен служебного API — случайная локальная строка доступа. Это **не ключ Gemini**
и не пароль оператора. Сервер API слушает только этот компьютер.

Во втором PowerShell подставьте выведенный токен:

```powershell
$token = 'СКОПИРУЙТЕ_ТОКЕН_ИЗ_ПЕРВОГО_ОКНА'
$body = @{kind='report'; messages=@(@{role='player'; text='Застрял в текстурах'; attachments=0})} | ConvertTo-Json -Depth 4
$result = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8002/triage' -Headers @{Authorization="Bearer $token"} -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$result | ConvertTo-Json -Depth 12
```

Ожидается JSON с `lang`, `category`, `priority`, `draft_reply`, `needs_human`,
`extracted` и причинами решения. HTTP 401 означает неправильный служебный токен;
422 — некорректный вход. Ctrl+C в первом окне останавливает API.

Эквивалент Linux/macOS:

```bash
export COPILOT_DRAFT_PROVIDER=template
export COPILOT_TRIAGE_TOKEN=$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')
# В этом же shell можно запустить сервер в фоне и выполнить curl.
.venv/bin/python -m uvicorn support_copilot.api_only:app --host 127.0.0.1 --port 8002 &
curl --fail-with-body http://127.0.0.1:8002/triage \
  -H "Authorization: Bearer $COPILOT_TRIAGE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind":"report","messages":[{"role":"player","text":"Застрял в текстурах","attachments":0}]}'
```

## make predict IN=tickets.jsonl OUT=pred.jsonl

Linux/macOS с GNU Make:

```bash
COPILOT_DRAFT_PROVIDER=template make predict IN=examples/submission-demo.jsonl OUT=outputs/submission.pred.jsonl
```

Windows — то же вычислительное ядро:

```powershell
$env:COPILOT_DRAFT_PROVIDER = 'template'
.\run.ps1 -Mode Predict -InputFile examples/submission-demo.jsonl -OutputFile outputs/submission.pred.jsonl
```

Вход — UTF-8 JSONL, один объект контракта на строку. Выход — по одному JSON
ответу на каждую строку, в том же порядке. Пример строки:

```json
{"kind":"report","messages":[{"role":"player","text":"Застрял в текстурах","attachments":0}]}
```

Собственные обращения сохраните, например, в `data/my-tickets.jsonl` и укажите
этот путь вместо examples. Отсутствующие `role` и `attachments` получают значения
`player` и `0`; отсутствующий `kind` — `complaint` с консервативной проверкой.
Ответы администратора и целевые поля не добавляйте во вход. Для исходной выгрузки
организаторов сначала используйте адаптер `dataset.py`, описанный в docs/wave-1.md.

## Метрики и ключ

```bash
python -m support_copilot.project_metrics --no-dev --http --output outputs/jury-check
python -m support_copilot.ai_runtime --check
```

Вторая команда вызывает модель только при явных настройках провайдера/ключа и
может расходовать квоту. Первая всегда офлайн относительно внешних моделей.
Для Google задайте `COPILOT_DRAFT_PROVIDER=gemini`, `GEMINI_API_KEY` и
`COPILOT_GEMINI_MODEL=gemini-2.5-flash-lite`. Удобный скрытый ввод для интерфейса
обеспечивает `start-model.ps1`. Примеры используют template для воспроизводимости.
