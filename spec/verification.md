# Verification — план тестирования

Сопроводительный документ к `free-llm-proxy.md`. Описывает, как мы
проверяем, что прокси ведёт себя по спецификации.

## 1. Принципы

- **Прагматика без порога coverage.** Не гонимся за процентом, целимся
  в критичные ветки: `router`, `cooldown`, error-mapping, refresher
  «не теряет старый snapshot».
- **По умолчанию быстро.** Все тесты, кроме `live`, прогоняются за
  секунды без сети. Сетевые / реальные зависимости — под маркером
  `@pytest.mark.live` и пропускаются по умолчанию.
- **Один источник истины — спека.** Если поведение в спеке (`§5` правила
  fallback, `§4` контракт API) не покрыто хотя бы одним тестом, это
  баг тестов, а не «фича».

## 2. Слои

### 2.1. Unit (без I/O)
Чистые функции и pydantic-схемы:

- `select_candidates(snapshot, request, cooldowns) -> list[Model]` —
  табличные тесты по матрице из `spec/§5.1`: каждое сочетание
  поля запроса × capability-флага модели + порядок по `rank` ASC +
  пропуск моделей с активным cooldown.
- `parse_retry_after(headers)` — секунды, HTTP-date, отсутствие
  заголовка, мусор.
- `map_upstream_error(exc) -> Outcome` — `openai.RateLimitError` →
  `rate_limited`, `APITimeoutError` → `error(generic)`, `APIStatusError`
  с 4xx≠429 → `client_error` (без cooldown, прокидываем клиенту).
- `Cooldowns` — добавление, проверка `is_cooled_down(now)`, очистка
  записей по моделям, удалённым из нового snapshot.
- pydantic-схема `Model` — парсинг JSON-фикстуры из `tests/fixtures/`,
  отсутствующие optional-поля, неизвестные поля игнорируются
  (`extra="ignore"`).

### 2.2. Component (мок HTTP через `respx`)
Узкие интеграционные тесты с одним замоканным внешним вызовом:

- **Refresher:** 200+валидный JSON → snapshot подменился, метрика
  `freellm_active_models` обновлена; 5xx/невалидный JSON/таймаут →
  старый snapshot уцелел, в логах WARN; первый fetch на старте провалился
  → snapshot пуст, `/ready` отдаёт 503.
- **Upstream wrapper:** проверка передачи `Authorization: Bearer
  $OPENROUTER_API_KEY` и `base_url`, маппинг 200 / 429 (с/без
  `Retry-After`) / 503 / timeout / прочих 4xx в наши outcome-категории.
- **Cooldown integration:** 429 с `Retry-After: 7` → запись в
  `Cooldowns` ровно на 7 c (заморозка времени `time-machine`); по
  истечении модель снова попадает в выборку.

### 2.3. E2E in-process (FastAPI `ASGITransport` + `respx`)
Поднимаем приложение в памяти через `httpx.ASGITransport(app=app)`,
все исходящие HTTP моким `respx`. Покрываем сценарии из `spec/§4` и
`§5`:

- `POST /v1/chat/completions`:
  - happy path: первая модель отвечает 200 — клиент получает её ответ
    + заголовок `x-free-llm-proxy-model: <id>`;
  - rate-limit: модель #1 → 429, модель #2 → 200; в `request_done` логе
    `attempts` длиной 2 с правильными `outcome`;
  - все модели недоступны → 503 `all_models_unavailable`;
  - 4xx, кроме 429 (например, 400 invalid_request) → пробрасывается
    клиенту as-is, fallback **не** триггерится;
  - capability-фильтр: запрос с `tools=[...]` → модели без
    `supportsTools` пропущены даже при низком `rank`;
  - после фильтра пусто → 400 `no_capable_model`;
  - `stream=true` happy path: ответ `text/event-stream`, заголовок
    `x-free-llm-proxy-model` присутствует, в теле минимум 4 `data:`
    события (3 чанка + `[DONE]`);
  - `stream=true` с 429 на первой модели → fallback на вторую,
    клиент получает обычный SSE второй модели;
  - `stream=true` с 400 → passthrough клиенту, fallback не триггерится.
- `GET /v1/models` — проекция snapshot в OpenAI-формат, сортировка по
  `rank`; пустой snapshot → 503.
- `GET /ready` — 200 если есть хотя бы одна модель не в cooldown'е,
  иначе 503.
- `POST /admin/refresh` — триггерит fetch и обнуляет cooldown'ы.
- Auth: 401 без `Authorization`, 403 с неверным токеном (на всех
  эндпоинтах, кроме `/health` и `/metrics`).

### 2.4. Live smoke (`@pytest.mark.live`, ручной запуск)
Прогон против **реально работающего прокси** с реальным
`OPENROUTER_API_KEY`. Нужен запущенный сервис на `localhost:8080`
и `PROXY_API_KEY` в env.

Сценарий — **три последовательных запроса**:

```jsonc
POST /v1/chat/completions
{
  "messages": [
    {"role": "user", "content": "Сколько букв р в слове трансфорррмер?"}
  ]
}
```

Проверяем:

1. Все три ответа — статус 200.
2. `choices[0].message.content` непустой в каждом.
3. У каждого ответа есть заголовок `x-free-llm-proxy-model`.
4. (Опционально) три записи `request_done` со `status=200` в логах
   процесса — если тест умеет к ним подключиться.

**Корректность ответа модели не проверяем** — free-модели галлюцинируют,
это не предмет наших тестов. Цель smoke'а — убедиться, что OpenAI-формат
соблюдается и реальный OpenRouter не дрейфанул по контракту. Три
последовательных вызова дают шанс зацепить cooldown по 429 и переход на
следующую модель.

### 2.5. Schema-тест (`@pytest.mark.live`)
Один реальный GET к `https://shir-man.com/api/free-llm/top-models` +
парсинг ответа нашей pydantic-схемой `Model`:

- Список не пуст.
- Все элементы валидно парсятся (обязательные поля `id`, `rank` есть).
- Незнакомые поля не ломают парсинг (схема с `extra="ignore"`).

Тест помечен `live`, потому что зависит от внешней доступности
shir-man.com и поэтому может флапать. Цель — поймать drift схемы до
того, как он сломает прод.

## 3. Lint и форматирование

- `ruff check .` — линт.
- `ruff format --check .` — проверка форматирования.
- `ruff format .` — применить форматирование.
- Конфиг — в `pyproject.toml` (либо `ruff.toml`): `line-length=100`,
  `target-version="py312"`, набор правил `E,F,I,N,B,UP,SIM,RUF`.
- `mypy` пока **не** добавляем (CI нет, см. §6) — добавим, когда
  появится CI или станет больно без статической проверки.

## 4. Инструменты (в `requirements.in`)

- `pytest`
- `pytest-asyncio`
- `respx` — мок httpx; работает и для `openai` SDK (тот ходит через
  httpx), и для нашего fetcher'а refresher'а.
- `time-machine` — заморозка `datetime.now()` для cooldown-тестов.
- `ruff` — линт + форматирование.

## 5. Структура `tests/`

```
tests/
├── conftest.py                # фикстуры: app, asgi_client, frozen_time, snapshot
├── fixtures/
│   └── top-models.json        # один сохранённый ответ shir-man.com,
│                              # обновляем вручную при изменениях схемы
├── test_registry.py           # Cooldowns, snapshot swap
├── test_router.py             # select_candidates (табличные)
├── test_refresher.py          # удерживаем старый snapshot при сбое
├── test_upstream.py           # маппинг ошибок SDK, parse_retry_after
├── test_api_chat.py           # /v1/chat/completions сценарии
├── test_api_models.py         # /v1/models
├── test_api_admin.py          # /admin/refresh
├── test_api_ops.py            # /health, /ready, /metrics, auth
└── test_live.py               # всё под @pytest.mark.live (smoke + schema)
```

`tests/fixtures/top-models.json` — реальный ответ shir-man.com,
сохранённый один раз. Это базовый snapshot для unit/component/E2E.
Перегенерация: `curl https://shir-man.com/api/free-llm/top-models > tests/fixtures/top-models.json`
(делается руками при изменении схемы; ловится schema-тестом из §2.5).

## 6. Команды

- `pytest` — быстрые тесты (unit + component + E2E in-process). Live
  пропускаются автоматически.
- `pytest -m live` — только live (нужны `OPENROUTER_API_KEY`,
  `PROXY_API_KEY` и запущенный сервис).
- `pytest -m "not live"` — то же, что `pytest`, но явно.
- `pytest tests/test_router.py::test_capability_filter_tools` —
  одиночный тест.
- `ruff check . && ruff format --check .` — линт + проверка формата.

Регистрация маркера: в `pyproject.toml`
```toml
[tool.pytest.ini_options]
markers = ["live: requires real network / API keys; opt-in via -m live"]
```

CI пока нет — все команды запускаются локально. Когда появится
GitHub Actions, прогон будет `pytest -m "not live"` + ruff;
`live` останутся ручными.

## 7. Что **не** тестируем

- Реальную часовую периодичность обновления (`MODELS_REFRESH_SEC`
  патчим до секунд в тестах refresher'а).
- Качество / правильность ответов LLM (галлюцинации free-моделей).
- Streaming (его нет в MVP, см. `spec/§9`).
- Concurrency cooldown'ов под нагрузкой и нагрузочный профиль —
  отложено; будет смысл, когда появятся первые пользователи.
