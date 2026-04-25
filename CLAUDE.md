# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: spec stage, no implementation yet

Источник истины — **`spec/free-llm-proxy.md`**. Перед любым изменением кода сверяйся с
этим документом: там зафиксированы архитектура, контракт API, поведение
fallback, переменные окружения, структура репозитория и пошаговый план
(`§10 План реализации`). Спецификация написана на русском; общение с
пользователем тоже на русском.

На данный момент в репо есть только спека и скелет питон-окружения. Если
тебя просят «продолжить» или «реализовать дальше» — ориентируйся на
следующий незакрытый шаг плана из §10.

## Что строим (TL;DR — детали в spec)

OpenAI-совместимый HTTP-прокси (`POST /v1/chat/completions`, non-stream),
который:

1. Раз в час подтягивает рейтинг бесплатных моделей с
   `https://shir-man.com/api/free-llm/top-models` (`RefreshWorker`,
   `asyncio` task). Если источник упал — продолжаем работать со старым
   snapshot'ом в памяти; на диск ничего не пишем.
2. На каждый запрос фильтрует snapshot по capability клиента (см. таблицу
   в §5.1: `tools` → `supportsTools`, `response_format.json_schema` →
   `supportsStructuredOutputs`, `seed` → `supportsSeed`, и т.д.).
3. Идёт по `rank` ASC, пропуская модели в cooldown; реальный вызов — в
   OpenRouter (`https://openrouter.ai/api/v1`) через `openai` Python SDK
   с подменённым `base_url`.
4. **429 / 503** от провайдера → cooldown с уважением к `Retry-After`/
   `X-RateLimit-Reset` (дефолт 5 мин) → следующая модель. **5xx /
   timeout** → cooldown 60 c → следующая. **Прочие 4xx fallback не
   триггерят** — пробрасываем клиенту as-is. Лимит — 5 попыток, 30 c на
   upstream.

Ключевые компоненты (когда появятся в `src/free_llm_proxy/`):
- `registry.py` — in-memory snapshot моделей + таблица cooldown'ов.
- `refresher.py` — фоновый воркер, дёргающий `MODELS_LIST_URL`.
- `router.py` — чистая функция выбора кандидатов (хорошо ложится на
  табличные тесты).
- `upstream.py` — обёртка `openai.AsyncOpenAI`, маппинг ошибок SDK
  (`RateLimitError`, `APITimeoutError`, `APIStatusError`) в наши outcome-
  категории и парсинг заголовков rate-limit.
- `api/` — FastAPI-роутеры, разделённые по эндпоинтам.

## Намеренные нецели (не реализовывать без явного запроса)

Перечислены в `spec/§9`. Самое важное, обо что легко споткнуться:

- **Streaming (`stream=true`) не поддерживаем** — на такой запрос отдаём
  `400 streaming_not_supported`. Не добавляй стриминг «заодно».
- Только OpenRouter; multi-provider routing — нецель.
- LiteLLM proxy был рассмотрен и **сознательно отклонён** в пользу
  собственного FastAPI-сервиса — не предлагай вернуться к нему как к
  «упрощению».
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
  применяешь `uv pip sync requirements.txt`. Сейчас `requirements.in`
  пустой — наполнение начнётся на шаге 1 плана (FastAPI, uvicorn,
  pydantic-settings, openai, httpx, prometheus-client, structlog/json-logging
  на выбор).
- `.python-version` и `venv/` локальные и **в `.gitignore`** — не
  коммить их обратно.

## Команды (появятся по мере реализации)

Точные команды (запуск, тесты, линт, docker-compose) не зафиксированы
жёстко — выбираются на шаге 1 плана. Ориентиры из спеки:

- HTTP-сервер: `uvicorn free_llm_proxy.main:app --host 0.0.0.0 --port 8080`
  (env: `HOST`/`PORT`).
- Контейнер: `docker compose up` с `.env` (см. `.env.example` после
  шага 1). Обязательные env: `OPENROUTER_API_KEY`, `PROXY_API_KEY`.
- Тесты: `pytest tests/` — структура каталога описана в `spec/§8`.
  Mock OpenRouter рекомендуется через `respx` поверх `httpx.ASGITransport`.

Когда будешь добавлять эти команды реально — обнови этот раздел
конкретными командами проекта (Makefile / pyproject scripts), а не
«ориентирами».

## Git

- Remote: `git@github.com:promsoft/free-llm-proxy.git`, основная ветка
  `main`, push разрешён только с явного запроса пользователя.
- Сообщения коммитов — на русском, формат `<scope>: <что сделано>` без
  ссылок на тикеты (примеры в `git log`).
