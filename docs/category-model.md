# Локальная модель категорий (opt-in эксперимент) · 31.10.2026

Исходная точка — a529510 (70% human-dev / 62% assistant-silver control).
Этот вариант добавляет инструменты обучения и включаемый бэкенд категорий.
**Качество на реальных обращениях не измерено**: в пакете не было реальных текстов,
taxonomy и обучающей разметки. Все проверки ниже — синтетические и механические.

## Что изменилось

- `support_copilot/category_model.py` — инференс без scikit-learn/numpy. Артефакт — JSON
  (словарь, IDF, веса), без pickle и чужих бинарных моделей; целостность проверяется по SHA-256.
- `support_copilot/category_training.py` — проверка данных, групповое разбиение, обучение,
  сравнение кандидатов, выбор порога на validation, отдельная оценка `category-holdout-v1`.
- `service.analyze` вызывает выбор бэкенда. По умолчанию используется `rules`.
  Архив сравнивался с исходным снимком; последующие исправления правил из
  `category-audit` сохранены, поэтому побайтовое совпадение с ним здесь не заявляется.
- `measure`/`quality report` пишут поле `category_backend`; формулы метрик не менялись.

## Переключатель (одинаков для HTTP, CLI и измерителей)

| Переменная | Значение |
|---|---|
| `COPILOT_CATEGORY_BACKEND` | `rules` (по умолчанию), `shadow`, `tfidf`; другое значение → `rules` + причина `category_backend_unknown_value_rules_used` |
| `COPILOT_CATEGORY_MODEL` | путь к `model.json`; по умолчанию `outputs/category-model/current/model.json` |
| `COPILOT_CATEGORY_ALLOW_SMOKE` | `1` разрешает загрузку synthetic smoke-артефакта; только для тестов |

`shadow`: категорию решают правила, в `reasons` пишутся `category_shadow_candidate:<код>` и
`category_shadow_would_be:<accepted|причина>`. С этого режима начинается сравнение.

`tfidf`: модель меняет категорию, только если:

1. правило не выбрало `report.payment`/`report.account` или категорию из `needs_human_always`;
2. в тексте нет апелляции по правилу `APPEAL`;
3. модель не вводит `complaint.cheat` при обычной установке софта без независимых признаков чита,
   и `complaint.admin`/`report.complaint` при «пересмотреть запись/видео» без контекста наказания;
4. оценка не ниже порога, выбранного на validation.

Иначе остаётся категория правил с причиной `category_guard:*` или
`category_model_abstained:below_validation_threshold`.

Эскалация считается по **обеим** категориям, правиловой и итоговой. Если у правил не было
категориальной уверенности (certainty 0), случай остаётся incomplete. Модель не снижает
`needs_human`, не убирает p0/p1 (они зависят от тем, а не от категории), не меняет safety-флаги
и не разрешает автоматическую отправку ответа. Модель может сменить p2↔p3, потому что p3 задаётся
категорией (`invalid/question/offtopic`).

При отсутствии или ошибке артефакта используются правила, в `reasons` пишется
`category_model_unavailable:<код>` (`artifact_missing`, `artifact_checksum_mismatch`,
`artifact_unreadable`, `artifact_format_unknown`, `smoke_model_not_allowed`, `taxonomy_mismatch`,
`kind_not_trained`, …), в лог уходит одно предупреждение на состояние файла.

`confidence` остаётся номинальным уровнем, `confidence_method` в режиме модели —
`tfidf_linear_validation_threshold_uncalibrated`. Ни margin LinearSVC, ни максимум
LogisticRegression не являются вероятностью правильности.

## Формат данных

Одна строка JSONL, поля строго:
`example_id, group_id, split (train|validation|test или отсутствует до разбиения), request,
category, label_provenance, approved_for_training`. Лишние поля (`admin_reply`, `punishment`,
`verdict` …) отклоняются на верхнем уровне обучающей записи. `request` проходит
типовую проверку `TriageRequest`: вложенные лишние поля отбрасываются, роль
`admin` запрещена и не может попасть в признаки.

`label_provenance`: `human`, `human_adjudicated`, `assistant_silver`, `synthetic`,
`feedback_unconfirmed`. Для обучения по умолчанию подходят только `human*` с
`approved_for_training=true`. `--smoke-synthetic` дополнительно допускает `synthetic` и помечает
артефакт `smoke_test_only`. Сервис такой артефакт не загружает.

Проверки (ошибка = код + ID, без текстов): дубли `example_id`; категория вне taxonomy или не
того kind; нет текста player; `group_id` в нескольких split; одинаковый нормализованный текст
с разными метками или в разных split; совпадение с текстами frozen dev/control из
`evaluation/frozen-inputs.json` (если файлы лежат локально, иначе статус `skipped_files_absent`).

Признаки строятся только из сообщений `role=player`: `safety_view`, маскирование ссылок, ников
`Name_Surname` и цифр. `reported_player`, ID, группы, split, provenance и метки в признаки не попадают.

## Обучение и выбор

- Отдельная модель на `report` и на `complaint`.
- Кандидаты: признаки `word_1_2`, `char_wb_3_5` (символьные 3–5-граммы внутри слов) и их объединение
  × `LinearSVC(C=0.3/1/3)` и `LogisticRegression(C=1/10)` × `class_weight` none/balanced, всего 30.
  Это стартовая сетка, победитель заранее не известен.
- Векторизатор и классификатор обучаются только на train. Выбор на validation: максимум
  macro-F1 (по всем классам данного kind) среди кандидатов, чья accuracy не ниже лучшей минус
  `--accuracy-tolerance` (0.02).
- Порог: максимум validation-accuracy **рантайм-гибрида** (с теми же guard'ами), затем macro-F1,
  затем меньше изменений против правил. Если модель нигде не лучше правил, порог `null` —
  модель всегда воздерживается.
- Validation использован и для выбора модели, и для порога, поэтому его цифры оптимистичны.
  Независимая оценка возможна только на отдельном human test.
- Экспорт проверяется: предсказания JSON-инференса обязаны совпасть со scikit-learn, иначе
  артефакт не пишется. Одинаковые данные и seed дают побайтно одинаковый `model.json`.
- В metadata: seed, версии Python/scikit-learn/numpy/scipy, SHA файла данных и снимка строк,
  SHA taxonomy и кода, счётчики по split/классам/provenance, исключённые строки, статус frozen-проверки,
  таблица кандидатов, поиск порога, классы taxonomy, которых нет в train, хеши групп и текстов
  для проверки утечки при оценке.

## Команды (машина команды, настоящий Git-клон)

```bash
make install                       # как раньше
make install-train                 # scikit-learn/numpy/scipy только для обучения

# 1. Новая разметка без split -> групповое разбиение (существующие split не перемешиваются)
make category-splits IN=outputs/category-data/labels-v1.jsonl OUT=outputs/category-data/labels-v1.split.jsonl
make category-validate IN=outputs/category-data/labels-v1.split.jsonl

# 2. Обучение и сравнение кандидатов (test не читается для выбора)
make category-train IN=outputs/category-data/labels-v1.split.jsonl OUT=outputs/category-model/v1
make category-check MODEL=outputs/category-model/v1/model.json

# 3. Финальная оценка один раз на новом human test
make category-eval MODEL=outputs/category-model/v1/model.json \
     IN=outputs/category-data/labels-v1.split.jsonl OUT=outputs/category-model/v1/holdout-report.json

# 4. Те же замороженные dev/control прежними измерителями: до и после
export COPILOT_DRAFT_PROVIDER=template COPILOT_VERIFIED_TEMPLATES=on
.venv/bin/python -m support_copilot.quality report --output outputs/classifier-next/before-quality.json
.venv/bin/python -m support_copilot.measure --output outputs/classifier-next/before-detail
export COPILOT_CATEGORY_MODEL=outputs/category-model/v1/model.json
COPILOT_CATEGORY_BACKEND=shadow .venv/bin/python -m support_copilot.measure --output outputs/classifier-next/shadow-detail
COPILOT_CATEGORY_BACKEND=tfidf  .venv/bin/python -m support_copilot.quality report --output outputs/classifier-next/after-quality.json
COPILOT_CATEGORY_BACKEND=tfidf  .venv/bin/python -m support_copilot.measure --output outputs/classifier-next/after-detail
COPILOT_CATEGORY_BACKEND=tfidf  make load-check
COPILOT_DRAFT_PROVIDER=template make check          # default backend: прежнее поведение
```

`make category-eval` выдаёт `rules`, `model_only` и `hybrid_runtime` с accuracy, bootstrap CI,
`macro_f1_all`/`macro_f1_present`, per-class и разбивкой по kind, а также fixed/broken/
wrong→wrong против правил и счётчик переопределений обязательных категорий (должен быть 0).
Без `--allow-non-human` оценка на не-human метках запрещена. С флагом отчёт помечает, что это
не human gold.

Dev/control уже видели, это не слепой тест. Решение о штатном включении — только по новому
human holdout вместе с проверкой эскалаций на 200 frozen-кейсах. Если правила лучше, default
остаётся `rules`.

## Известные дефекты правил

`tests/test_category_rule_defects.py` фиксирует пары «работает / ломается» как strict xfail:
админ-свидетель → admin; «несправедливо/ошибочно» перебивают нарушение; просьба наказать
другого → admin; вопрос без как/где и «?» в репорте → offtopic; закрытый список «не + глагол»
для help; вопрос о сроке мута → complaint; латинская `M` и цифра `0` вместо букв ломают словари.
В исходном архиве правила по умолчанию не менялись. В этой ветке сохранены
последующие исправления правил из `category-audit`; их влияние на приватные
данные после объединения пока не измерено.
Обучаемый бэкенд — место для этих случаев.

## Следующие эксперименты (не реализованы)

SetFit или многоязычный encoder — только если TF-IDF не хватит, с проверкой покрытия KK/UZ,
лицензии и задержки на машине команды. Внешняя LLM — позже, opt-in, только для неоднозначных
случаев и без платных вызовов по умолчанию.
