# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Источник истины — **`spec/free-llm-proxy.md`**. Перед любым изменением кода сверяйся с
этим документом: там зафиксированы архитектура, контракт API, поведение
fallback, переменные окружения и структура репозитория. Anthropic
Messages API (`/api/anthropic`) специфицирован отдельно в
**`spec/anthropic.md`**. Стратегия тестирования — в
**`spec/verification.md`**. Спецификация написана на русском; общение с
пользователем тоже на русском.

## Что строим (TL;DR — детали в spec)

OpenAI-совместимый HTTP-прокси, который:

1. Раз в час подтягивает рейтинг бесплатных моделей с
   `https://shir-man.com/api/free-llm/top-models` (`RefreshWorker`,
   `asyncio` task). Если источник упал — продолжаем работать со старым
   snapshot'ом в памяти; на диск ничего не пишем.
2. На каждый запрос фильтрует snapshot по capability клиента (см. таблицу
   в `spec/§5.1`: `tools` → `supportsTools`, `response_format.json_schema` →
   `supportsStructuredOutputs`, `seed` → `supportsSeed`, и т.д.).
3. Идёт по `rank` ASC, пропуская модели в cooldown; реальный вызов — в
   OpenRouter (`https://openrouter.ai/api/v1`) через `openai` Python SDK
   с подменённым `base_url`.
4. **429 / 503** от провайдера → cooldown с уважением к `Retry-After`/
   `X-RateLimit-Reset` (дефолт 5 мин) → следующая модель. **5xx /
   timeout** → cooldown 60 c → следующая. **401 / 403** от OpenRouter
   считаем сигналом плохого `OPENROUTER_API_KEY`: отвечаем клиенту
   `502 upstream_auth_error` с хвостом ключа в сообщении (видно «не
   подставился ли placeholder»), fallback **не** триггерится. **Прочие
   4xx** — passthrough клиенту. Лимит — 5 попыток, 30 c на upstream.
5. **Streaming поддержан** (`stream=true` → SSE, `text/event-stream`).
   Правила mid-stream fallback — `spec/§5.6`: пока первый чанк не
   отправлен, ведём себя как non-stream; после первого чанка fallback
   невозможен, ошибка отдаётся как `data: {"error": ...}` + `data: [DONE]`.
6. **`/api/v1/...` — алиас к `/v1/...`** (для клиентов, которые ждут
   OpenRouter-style путь). Зарегистрирован двойной `include_router` в
   `main.py`.
7. **Anthropic Messages API под `/api/anthropic`** (для Claude Code /
   `anthropic` SDK) — трансляционный шим Anthropic↔OpenAI поверх того же
   конвейера. Детали и нецели — `spec/anthropic.md`. OpenRouter нативного
   Anthropic-API не имеет, поэтому всё транслируется в OpenAI и обратно.

Ключевые компоненты в `src/free_llm_proxy/`:
- `registry.py` — in-memory snapshot моделей + таблица cooldown'ов.
- `refresher.py` — фоновый воркер, дёргающий `MODELS_LIST_URL`.
- `router.py` — чистая функция `select_candidates()` (табличные тесты).
- `upstream.py` — обёртка `openai.AsyncOpenAI` с `max_retries=0`,
  `classify_exception()` мапит ошибки SDK (`RateLimitError`,
  `APITimeoutError`, `APIStatusError`) в `Outcome`-категории и парсит
  заголовки rate-limit.
- `fallback.py` — общее ядро цикла попыток (`run_fallback`,
  `apply_cooldown`, `record_attempt`), переиспользуется OpenAI- и
  Anthropic-эндпоинтами.
- `anthropic_translate.py` — чистый перевод Anthropic↔OpenAI
  (`anthropic_to_openai_request`, `openai_to_anthropic_response`,
  `AnthropicStreamTranslator`, `estimate_input_tokens`, `AnthropicError`).
- `api/` — FastAPI-роутеры по эндпоинтам (`chat.py`, `models_endpoint.py`,
  `admin.py`, `ops.py`, `anthropic.py`).
- `auth.py` — `require_proxy_key` (Bearer) и `require_anthropic_key`
  (x-api-key / Bearer, ошибки в Anthropic-формате).
- `deps.py` — FastAPI-зависимости `get_registry` / `get_refresher`.

## Намеренные нецели (не реализовывать без явного запроса)

Перечислены в `spec/§9`. Самое важное, обо что легко споткнуться:

- Только OpenRouter; multi-provider routing — нецель.
- LiteLLM proxy был рассмотрен и **сознательно отклонён** в пользу
  собственного FastAPI-сервиса — не предлагай вернуться к нему как к
  «упрощению».
- `/v1/completions` (legacy) и `/v1/embeddings` — нет.
- Cooldown'ы только в памяти, переживают только в рамках процесса.
- Тела запросов и ответов **не логируются** (PII / объём); в логе только
  выбранная модель, цепочка попыток и тайминги.

## Окружение и зависимости

- **Python 3.12.8** через `pyenv`. Окружение называется `projects_litellm`
  и симлинкнуто как `./venv` → `/home/dk/.pyenv/versions/3.12.8/envs/projects_litellm`.
  Активация: `source venv/bin/activate`.
- Менеджер пакетов — **`uv`** (уже стоит в окружении).
- Граф зависимостей: правишь `requirements.in` → пересобираешь
  `requirements.txt` командой `uv pip compile requirements.in -o requirements.txt`,
  применяешь `uv pip sync requirements.txt`. Параллельно метаданные
  пакета и dev-extras зафиксированы в `pyproject.toml` (`pip install -e .`).
- `.python-version` и `venv/` локальные и **в `.gitignore`** — не
  коммить их обратно. `.env` тоже игнорится (в репо только `.env.example`).

## Команды

- **Запуск локально:** `uvicorn free_llm_proxy.main:app --host 0.0.0.0 --port 8080`
  (env: `HOST` / `PORT`). Обязательные env: `OPENROUTER_API_KEY`, `PROXY_API_KEY`.
- **Контейнер:** `docker compose up --build` с `.env` (см. `.env.example`).
- **Тесты (быстрые):** `pytest` — `addopts = "-m 'not live'"` в pyproject
  отсекает live-прогоны. Mock OpenRouter — через `respx` поверх
  `httpx.ASGITransport`.
- **Live smoke:** `pytest -m live` — три последовательных вызова против
  реально работающего прокси на `localhost:8080` + schema-тест на
  `shir-man.com`. Нужны `OPENROUTER_API_KEY`, `PROXY_API_KEY` и
  запущенный сервис.
- **Линт + формат:** `ruff check .` и `ruff format --check .`.

## Git

- Remote: `git@github.com:promsoft/free-llm-proxy.git`, основная ветка
  `main`, push разрешён только с явного запроса пользователя.
- Сообщения коммитов — на русском, формат `<scope>: <что сделано>` без
  ссылок на тикеты (примеры в `git log`).
