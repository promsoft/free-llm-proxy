# Free-LLM Proxy — спецификация

## 1. Цель

Поднять локальный сервис, который принимает запросы в формате OpenAI Chat Completions
и автоматически маршрутизирует их на лучшую доступную бесплатную модель из публично
поддерживаемого рейтинга `https://shir-man.com/api/free-llm/top-models`.

Вызывающий код должен ходить ровно в одну точку (`POST /v1/chat/completions`)
и не задумываться, какая именно модель сейчас отвечает.

## 2. Источники данных и провайдер

### 2.1. Список моделей
- URL: `https://shir-man.com/api/free-llm/top-models`
- Период обновления: **1 раз в час** (фоновая задача).
- Полезные поля каждого элемента:
  - `rank` — приоритет (1 = лучший);
  - `id` — идентификатор в формате OpenRouter (`provider/model:free`);
  - `contextLength`, `maxCompletionTokens`;
  - capability-флаги: `supportsTools`, `supportsToolChoice`, `supportsStructuredOutputs`,
    `supportsResponseFormat`, `supportsReasoning`, `supportsIncludeReasoning`,
    `supportsSeed`, `supportsStop`;
  - `latencyMs`, `healthStatus`, `score`, `reason`.
- На время недоступности `shir-man.com` сервис **продолжает работать с
  последним удачно прочитанным снапшотом из памяти** (на диск ничего не пишем).
  Если на старте первый запрос за списком провалился — сервис не готов
  (`/ready` возвращает 503), но процесс не падает и продолжает ретраить.

### 2.2. Upstream-провайдер
- Все вызовы LLM идут в **OpenRouter** (`https://openrouter.ai/api/v1`).
- Аутентификация — через `OPENROUTER_API_KEY` (env-переменная).
- ID модели в API совпадает с `id` из списка (например, `inclusionai/ling-2.6-flash:free`).
- Используем официальный `openai` Python SDK с `base_url=https://openrouter.ai/api/v1`.

## 3. Архитектура

### 3.1. Стек
- **Python 3.12** (уже зафиксирован в `.python-version`).
- **FastAPI** + **uvicorn** — HTTP-сервер.
- **httpx** (для фонового fetch'а списка моделей) и/или `openai` SDK для самого вызова.
- **pydantic v2** — валидация запросов.
- **prometheus-client** — метрики.
- **structlog** или штатный `logging` в JSON — структурированные логи.
- **Docker** + **docker-compose**.

### 3.2. Компоненты процесса

```
                   ┌──────────────────────────────┐
HTTP запрос  ──▶   │  FastAPI router              │
(OpenAI-совм.)     │   ├─ auth (PROXY_API_KEY)    │
                   │   ├─ /v1/chat/completions    │ ──▶ Router ──▶ OpenAI SDK ──▶ OpenRouter
                   │   ├─ /v1/models              │
                   │   ├─ /health, /ready         │
                   │   ├─ /metrics                │
                   │   └─ /admin/refresh          │
                   └──────────────────────────────┘
                                 │
                                 ▼
                   ┌──────────────────────────────┐
                   │  ModelRegistry (in-memory)   │
                   │  ─ snapshot моделей          │
                   │  ─ cooldown-таблица          │
                   │  ─ generation counter        │
                   └──────────────────────────────┘
                                 ▲
                                 │ раз в час / по запросу
                   ┌──────────────────────────────┐
                   │  RefreshWorker (asyncio task)│
                   │  fetch shir-man.com          │
                   └──────────────────────────────┘
```

### 3.3. ModelRegistry (in-memory state)
- `snapshot: list[Model]` — последний успешно полученный список, отсортированный по `rank`.
- `snapshot_fetched_at: datetime`.
- `cooldowns: dict[model_id, datetime]` — до какого момента модель «спит».
- Потокобезопасность: единый `asyncio.Lock` на запись, чтение — атомарно
  (read-mostly, swap всего snapshot на новый объект).

### 3.4. Процессы
1. `RefreshWorker` — `asyncio` task, при старте делает первый fetch;
   далее цикл `await asyncio.sleep(3600)` + retry. На любую ошибку — лог,
   старый snapshot остаётся.
2. HTTP handler — синхронно (в смысле request lifecycle) пытается модели
   по порядку, пока не получит успешный ответ или не исчерпает список.

## 4. Контракт API

Все эндпоинты, кроме `/health` и `/metrics`, требуют заголовок
`Authorization: Bearer <PROXY_API_KEY>`.

### 4.1. `POST /v1/chat/completions`
- Тело — стандартный OpenAI Chat Completions request.
- Поле `model` в запросе **игнорируется** (или принимаем магическое значение
  `auto` для совместимости — иначе игнор + предупреждение в логах).
- `stream=true` **не поддерживается** в MVP — на такой запрос отвечаем `400`
  с `error.code = "streaming_not_supported"`.
- Логика обработки — см. §5.
- Ответ — как у OpenRouter (формат OpenAI), с дополнительным полем
  `x-free-llm-proxy-model: <chosen_model_id>` в response headers.

### 4.2. `GET /v1/models`
- Возвращает текущий snapshot в формате OpenAI:
  ```json
  { "object": "list", "data": [{"id": "...", "object": "model", "created": <ts>, "owned_by": "openrouter"}, ...] }
  ```
- Сортировка — по `rank` возрастанием.
- Если snapshot пуст — `503`.

### 4.3. `GET /health`
- Без авторизации. Всегда `200 {"status":"ok"}`, пока процесс жив.

### 4.4. `GET /ready`
- Без авторизации. `200`, если в snapshot есть **хотя бы одна** модель не
  в cooldown'е. Иначе `503`.

### 4.5. `GET /metrics`
- Без авторизации (или ограничить по сети — конфигурируемо).
- Prometheus-формат. Метрики:
  - `freellm_requests_total{status}` — counter,
  - `freellm_request_duration_seconds` — histogram,
  - `freellm_upstream_attempts_total{model_id, outcome}` — counter
    (`outcome` ∈ `success | rate_limited | error | filtered_out`),
  - `freellm_active_models` — gauge размер snapshot,
  - `freellm_cooldown_models` — gauge число моделей в cooldown,
  - `freellm_snapshot_age_seconds` — gauge.

### 4.6. `POST /admin/refresh`
- Авторизация — `PROXY_API_KEY`.
- Принудительный fetch + сброс всех cooldown'ов.
- Ответ: `{ "models": <count>, "fetched_at": <iso> }`.

## 5. Логика выбора и fallback

### 5.1. Фильтрация под capability запроса
Из snapshot отбираем только те модели, которые поддерживают то, что есть в
запросе:

| В запросе клиента                       | Требование к модели                                  |
|-----------------------------------------|------------------------------------------------------|
| `tools` (непустое)                      | `supportsTools == true`                              |
| `tool_choice` (не `"auto"` / отсутствует) | `supportsToolChoice == true`                       |
| `response_format.type == "json_schema"` | `supportsStructuredOutputs == true`                  |
| `response_format.type == "json_object"` | `supportsResponseFormat == true`                     |
| `seed`                                  | `supportsSeed == true`                               |
| `stop`                                  | `supportsStop == true`                               |
| `reasoning` / `reasoning_effort`        | `supportsReasoning == true`                          |

Если после фильтрации список пуст — `400`
`{"error":{"code":"no_capable_model","message":"No model in current snapshot supports requested capabilities"}}`.

### 5.2. Порядок попыток
Берём отфильтрованный список, отсортированный по `rank` ASC, и **исключаем
модели, у которых `cooldowns[id] > now`**. Кооldown — это ровно тот случай,
когда мы недавно получили `429` (или `503` от провайдера) и решили дать ей
отдохнуть.

### 5.3. Что считается «недоступностью»
| Категория                                         | Действие                                                   |
|---------------------------------------------------|------------------------------------------------------------|
| HTTP `429` или `503` от OpenRouter                | поставить cooldown (см. §5.4) и идти к следующей модели    |
| Сетевые ошибки / таймаут (>30 c)                  | короткий cooldown 60 c, идти к следующей                   |
| HTTP `5xx`, кроме `503`                           | cooldown 60 c, идти к следующей                            |
| HTTP `4xx`, кроме `429`                           | **не fallback** — это ошибка запроса, отдать клиенту as-is |
| Любая другая ошибка SDK                           | cooldown 60 c, идти к следующей                            |

Таймаут одного upstream-запроса: **30 секунд** (`UPSTREAM_TIMEOUT_SEC`, env).
Максимум попыток в одном запросе: **5** (`MAX_FALLBACK_ATTEMPTS`, env) —
чтобы не уйти в минутные хвосты на «холодных» моделях.

### 5.4. Cooldown по 429
- Если в ответе есть `Retry-After` (секунды или HTTP-date) — используем его.
- Иначе если есть `X-RateLimit-Reset` (epoch) — используем.
- Иначе — дефолт `RATE_LIMIT_COOLDOWN_SEC` (env, по умолчанию `300` = 5 мин).
- Cooldown'ы хранятся только в памяти; при рестарте обнуляются.
- При обновлении snapshot модели, которых больше нет в списке, удаляются
  из таблицы cooldown'ов.

### 5.5. Если все модели не отвечают
- HTTP `503` `{"error":{"code":"all_models_unavailable", ...}}` со списком
  попыток в логе.

## 6. Конфигурация

Все настройки — через env (читаются на старте, валидация через pydantic-settings):

| Переменная                       | Default                                  | Назначение                       |
|----------------------------------|------------------------------------------|----------------------------------|
| `OPENROUTER_API_KEY`             | — (обязательна)                          | ключ к OpenRouter                |
| `PROXY_API_KEY`                  | — (обязательна)                          | bearer-токен для входящих        |
| `MODELS_LIST_URL`                | `https://shir-man.com/api/free-llm/top-models` | источник                  |
| `MODELS_REFRESH_SEC`             | `3600`                                   | период обновления списка         |
| `UPSTREAM_BASE_URL`              | `https://openrouter.ai/api/v1`           | OpenAI-совм. эндпоинт            |
| `UPSTREAM_TIMEOUT_SEC`           | `30`                                     | таймаут одного апстрим-запроса   |
| `MAX_FALLBACK_ATTEMPTS`          | `5`                                      | максимум попыток на запрос       |
| `RATE_LIMIT_COOLDOWN_SEC`        | `300`                                    | дефолтный cooldown по 429        |
| `GENERIC_ERROR_COOLDOWN_SEC`     | `60`                                     | cooldown по 5xx/timeout          |
| `LOG_LEVEL`                      | `INFO`                                   |                                  |
| `HOST` / `PORT`                  | `0.0.0.0` / `8080`                       |                                  |

`.env` подхватывается docker-compose, в репо коммитим только `.env.example`.

## 7. Логи

JSON-логи; для каждого `chat.completions` запроса — одна запись `request_done`:

```json
{
  "ts": "...",
  "level": "INFO",
  "event": "request_done",
  "request_id": "uuid",
  "duration_ms": 1234,
  "status": 200,
  "chosen_model": "inclusionai/ling-2.6-flash:free",
  "attempts": [
    {"model": "...", "outcome": "rate_limited", "duration_ms": 80, "cooldown_until": "..."},
    {"model": "inclusionai/...", "outcome": "success", "duration_ms": 1100}
  ],
  "had_tools": true,
  "had_response_format": false
}
```
Тело prompt'а и ответ **не логируем** (PII / большой объём).

## 8. Структура репозитория

```
.
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml            # либо оставить requirements.in/.txt + uv
├── requirements.in
├── requirements.txt
├── spec/
│   └── free-llm-proxy.md     # этот файл
├── src/
│   └── free_llm_proxy/
│       ├── __init__.py
│       ├── main.py           # FastAPI app factory, lifespan
│       ├── config.py         # pydantic-settings
│       ├── models.py         # pydantic-схемы (Model, Snapshot, ChatRequest)
│       ├── registry.py       # ModelRegistry: snapshot + cooldowns
│       ├── refresher.py      # фоновая задача обновления
│       ├── router.py         # выбор модели + fallback
│       ├── upstream.py       # тонкая обёртка над openai SDK
│       ├── api/
│       │   ├── chat.py       # /v1/chat/completions
│       │   ├── models.py     # /v1/models
│       │   ├── admin.py      # /admin/refresh
│       │   └── ops.py        # /health, /ready, /metrics
│       ├── auth.py           # bearer-зависимость FastAPI
│       ├── logging.py        # JSON-логгер
│       └── metrics.py        # prometheus collectors
└── tests/
    ├── test_registry.py      # cooldown-логика, snapshot swap
    ├── test_router.py        # capability-фильтр, порядок попыток
    ├── test_refresher.py     # удерживаем старый snapshot при ошибке
    └── test_api.py           # e2e через httpx ASGI client с замоканным OpenRouter
```

## 9. Не входит в MVP (явные нецели)

- Streaming (`stream=true`).
- `/v1/completions` (legacy), `/v1/embeddings`.
- Multi-provider routing (только OpenRouter).
- Биллинг, виртуальные ключи, rate-limit на стороне прокси для пользователя.
- Кэш ответов.

## 10. План реализации (последовательность задач)

1. **Скелет проекта**: `pyproject.toml` (или фиксация `requirements.in`),
   FastAPI app factory, `/health`, Dockerfile, docker-compose, `.env.example`.
2. **Конфиг и логи**: `config.py` (pydantic-settings), JSON-логгер,
   bearer-аутентификация (`auth.py`).
3. **ModelRegistry + Refresher**: pydantic-схема `Model`, in-memory
   снапшот, фоновая задача с retry, `/admin/refresh`, `/ready`,
   тесты на «не уронить старый snapshot при ошибке fetch».
4. **Router + capability-фильтр**: чистая функция
   `select_candidates(snapshot, request, cooldowns) -> list[Model]`,
   таблично покрытая тестами.
5. **Upstream-обёртка**: `openai.AsyncOpenAI` с `base_url=OpenRouter`,
   маппинг ошибок (`RateLimitError`, `APITimeoutError`, `APIStatusError`)
   в наши outcome-категории, парсинг `Retry-After`/`X-RateLimit-Reset`.
6. **`/v1/chat/completions`**: цикл попыток, structured-лог `request_done`,
   заголовок `x-free-llm-proxy-model`.
7. **`/v1/models`**: проекция snapshot в OpenAI-формат.
8. **Метрики Prometheus** + `/metrics`.
9. **E2E-тесты** через `httpx.ASGITransport` с замоканным OpenRouter
   (`respx` или ручной мок).
10. **Docs**: README с примерами `curl` и `openai-python`.

## 11. Открытые вопросы (можно решить по ходу)

- Нужно ли поддерживать запрос с явным `model: "<id>"` как «pin» к
  конкретной модели (без fallback)? — пока нет, добавим, если попросят.
- Куда отправлять `HTTP-Referer` / `X-Title` в OpenRouter (рекомендуется
  для бесплатного тира)? — захардкодим название сервиса; вынесем в env,
  если понадобится.
- Нужно ли мерять реальный latency моделей (не доверяя `latencyMs` из
  списка)? — пока используем `rank` как есть.
