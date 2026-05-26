# free-llm-proxy

Это код к митапу https://kolodezev.ru/llm-dev.html

OpenAI-совместимый HTTP-прокси, который раз в час подтягивает рейтинг
бесплатных моделей с
[shir-man.com/api/free-llm/top-models](https://shir-man.com/api/free-llm/top-models)
и автоматически маршрутизирует запросы в первую доступную модель через
[OpenRouter](https://openrouter.ai/api/v1) с capability-фильтром и
fallback по rank.

Поддерживает два протокола поверх одного и того же пула моделей:
**OpenAI Chat Completions** (`/v1/...`) и **Anthropic Messages API**
(`/api/anthropic/...`) — последний позволяет подключать Claude Code и
`anthropic` SDK.

Полная спецификация: [`spec/free-llm-proxy.md`](spec/free-llm-proxy.md),
Anthropic-эндпоинт: [`spec/anthropic.md`](spec/anthropic.md).
Стратегия тестирования: [`spec/verification.md`](spec/verification.md).

## Quick start

### Docker

```bash
cp .env.example .env
# заполнить OPENROUTER_API_KEY и PROXY_API_KEY
docker compose up --build
```

### Локально

```bash
uv pip sync requirements.txt
pip install -e .
export OPENROUTER_API_KEY=sk-or-v1-...
export PROXY_API_KEY=local-dev-key
uvicorn free_llm_proxy.main:app --host 0.0.0.0 --port 8080
```

## Использование

### qween code

см [`qween/README.md`](qween/README.md) [`qween/settings.json`](qween/settings.json).

### curl

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Сколько букв р в слове трансфорррмер?"}]}'
```

В ответе появится заголовок `x-free-llm-proxy-model` с id выбранной
модели.

### openai-python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="local-dev-key",  # это PROXY_API_KEY, не ключ OpenRouter
)
resp = client.chat.completions.create(
    model="auto",  # игнорируется прокси
    messages=[{"role": "user", "content": "Сколько букв р в слове трансфорррмер?"}],
)
print(resp.choices[0].message.content)
```

### Claude Code

Прокси выставляет Anthropic Messages API под префиксом `/api/anthropic`.
Указываем его как базовый URL — Claude Code сам допишет `/v1/messages`:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080/api/anthropic
export ANTHROPIC_AUTH_TOKEN=local-dev-key   # это PROXY_API_KEY
# (вместо ANTHROPIC_AUTH_TOKEN можно ANTHROPIC_API_KEY — уйдёт в x-api-key)
claude
```

`ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` игнорируются — модель
выбирается по rank. Подробности перевода и ограничения — в
[`spec/anthropic.md`](spec/anthropic.md).

## Эндпоинты

| Метод | Путь                     | Auth   | Назначение                                    |
|-------|--------------------------|--------|-----------------------------------------------|
| POST  | `/v1/chat/completions`   | Bearer | OpenAI-совместимый chat (stream + non-stream) |
| GET   | `/v1/models`             | Bearer | Текущий snapshot моделей в OpenAI-формате     |
| POST  | `/api/anthropic/v1/messages` | x-api-key / Bearer | Anthropic Messages API (stream + non-stream) |
| POST  | `/api/anthropic/v1/messages/count_tokens` | x-api-key / Bearer | Приблизительная оценка `input_tokens` |

`/v1/...` и `/api/v1/...` работают одинаково — алиас для клиентов,
которые ожидают OpenRouter-style путь (`base_url=.../api/v1`).

| POST  | `/admin/refresh`         | Bearer | Принудительно перечитать список + сбросить cooldowns |
| GET   | `/health`                | —      | `{"status": "ok"}` пока процесс жив           |
| GET   | `/ready`                 | —      | 200 если есть свободная модель, 503 иначе     |
| GET   | `/metrics`               | —      | Prometheus-метрики                            |

## Поведение

- **Streaming поддерживается** — `stream=true` отвечает SSE в формате
  OpenAI Chat Completions chunks с `data: [DONE]` в конце. Fallback
  работает, пока первый чанк не отправлен клиенту; mid-stream ошибка
  отдаётся как `data: {"error": {...}}` + `data: [DONE]`.
- **Модель в запросе игнорируется** — прокси сам выбирает по rank и capability.
- **429 / 503 от OpenRouter** → cooldown с уважением к `Retry-After` (дефолт 5 мин) → следующая модель.
- **5xx / timeout** → cooldown 60 c → следующая.
- **4xx (кроме 429)** → проброс клиенту, fallback не триггерится.
- Лимит — 5 попыток на запрос, 30 с на upstream.
- **Anthropic-эндпоинт** (`/api/anthropic`) использует тот же выбор
  модели, fallback и cooldown; запрос/ответ транслируются Anthropic↔OpenAI,
  стриминг отдаётся событиями Anthropic SSE (`message_start` …
  `message_stop`), ошибки — в Anthropic-формате `{"type":"error",...}`.

## Разработка

```bash
uv pip sync requirements.txt
pip install -e .

# тесты (быстрые)
pytest

# только live (нужны OPENROUTER_API_KEY, PROXY_API_KEY и запущенный сервис на 8080)
pytest -m live

# линт + формат
ruff check .
ruff format --check .
```

## Конфигурация

Все настройки — через env. См. `.env.example` или `spec/free-llm-proxy.md` §6.
