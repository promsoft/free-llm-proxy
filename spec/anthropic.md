# Anthropic Messages API — спецификация

Сопроводительный документ к **`free-llm-proxy.md`**. Описывает второй
вход в прокси — **Anthropic Messages API** — поверх той же машинерии
(registry, `select_candidates`, `Upstream`, cooldown, метрики, логи).
Всё, что здесь не переопределено, наследуется из основной спеки.

## 1. Цель и контекст

**Главная цель — чтобы к прокси можно было подключить Claude Code**
(ровно так же, как сейчас подключается Qwen Code к OpenAI-эндпоинту).
Попутно это даёт совместимость с официальным `anthropic` SDK и прочими
клиентами **Anthropic Messages API**. Все они получают единую точку
входа в наш прокси к бесплатным моделям OpenRouter.

Требования Claude Code и определяют объём MVP: он **всегда стримит**,
**активно зовёт tools**, дёргает **`count_tokens`** и шлёт ключ через
**`x-api-key`/`Authorization`** — поэтому всё это в MVP (см. §1.1, §2.1).

Ключевой факт, определяющий всю архитектуру:

> **OpenRouter не имеет нативного Anthropic-эндпоинта** — он
> OpenAI-совместим (`/api/v1/chat/completions`). Значит, наш
> Anthropic-эндпоинт — это **трансляционный шим**:
> Anthropic Messages → OpenAI Chat Completions → OpenRouter →
> OpenAI-ответ → Anthropic-ответ.

Из этого следует главный принцип: **upstream-конвейер не дублируем**.
Выбор модели (`select_candidates`), цикл fallback, cooldown'ы по `429`/
`5xx` (§5 основной спеки), парсинг `Retry-After`, метрики и
`request_done`-лог — переиспользуются ровно как есть. Новый код — это
**две границы перевода** (request in / response out) и **роутер**.

### 1.1. Объём MVP (по согласованию)

| Возможность                         | В MVP | Примечание                                            |
|-------------------------------------|:-----:|-------------------------------------------------------|
| Текстовые сообщения + `system`      |  ✅   | `system` (string/array) → OpenAI system message       |
| `tools` + `tool_use`/`tool_result`  |  ✅   | нужно для агентов (Claude Code активно зовёт tools)   |
| Streaming (`stream=true`)           |  ✅   | полная событийная модель Anthropic SSE (§6)           |
| `POST .../v1/messages/count_tokens` |  ✅   | приблизительная оценка (§8)                           |
| Images (`type:"image"` блоки)       |  ❌   | нецель MVP (§11)                                       |
| `thinking`/reasoning блоки          |  ❌   | дропаем на входе, не генерим на выходе (§11)           |
| `cache_control` (prompt caching)    |  ❌   | игнорируем (§11)                                       |

## 2. Точки входа (namespace `/api/anthropic`)

Anthropic-API монтируется под **отдельным префиксом `/api/anthropic`**,
изолированно от OpenAI-`/v1`. Клиент указывает базовый URL прокси, SDK
сам дописывает `/v1/messages`:

```
ANTHROPIC_BASE_URL = http://localhost:8080/api/anthropic
              SDK → http://localhost:8080/api/anthropic/v1/messages
```

| Метод | Путь                                      | Auth        | Назначение                          |
|-------|-------------------------------------------|-------------|-------------------------------------|
| POST  | `/api/anthropic/v1/messages`              | x-api-key/Bearer | Anthropic Messages (stream + non-stream) |
| POST  | `/api/anthropic/v1/messages/count_tokens` | x-api-key/Bearer | Оценка `input_tokens`               |

- Существующий OpenAI-`/v1/...` (`/chat/completions`, `/models`) и его
  алиас `/api/v1/...` **не трогаем**. Anthropic полностью под своим
  префиксом → коллизии путей (в т.ч. с `/v1/models`) нет.
- Anthropic-формат для `GET /v1/models` в MVP **не делаем** (открытый
  вопрос §12). Если Claude Code дёрнет `/api/anthropic/v1/models` —
  получит `404` в Anthropic-формате; на работу `/v1/messages` это не
  влияет.
- В `main.py` — один дополнительный `include_router(anthropic.router)`
  с `prefix="/api/anthropic"`.

### 2.1. Подключение Claude Code

Claude Code конфигурируется через env (или `settings.json`). Минимум:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080/api/anthropic
export ANTHROPIC_AUTH_TOKEN=local-dev-key   # это PROXY_API_KEY (→ Authorization: Bearer)
# либо: export ANTHROPIC_API_KEY=local-dev-key   (→ заголовок x-api-key)
claude
```

Что отсюда следует для прокси (и почему MVP выглядит именно так):

- Claude Code **дописывает `/v1/messages`** к `ANTHROPIC_BASE_URL`, поэтому
  базовый URL указывает на наш namespace **без** `/v1` на конце.
- Claude Code шлёт ключ либо в `x-api-key` (`ANTHROPIC_API_KEY`), либо в
  `Authorization: Bearer` (`ANTHROPIC_AUTH_TOKEN`) — **принимаем оба** (§3).
- Claude Code **всегда работает в streaming-режиме** → streaming
  обязателен (§6).
- Claude Code — агент, **активно вызывает инструменты** → `tools` /
  `tool_use` / `tool_result` обязательны (§4.2–4.3).
- Claude Code зовёт **`count_tokens`** перед запросами для оценки
  контекста → эндпоинт есть, пусть и приблизительный (§8).
- `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL`, которые шлёт Claude
  Code, **игнорируются** — модель выбираем по `rank`. Их можно ставить в
  любое значение (или не ставить).
- Большой `system`-массив с `cache_control`, который шлёт Claude Code,
  обрабатываем: текст склеиваем, `cache_control` дропаем (§4.1, §12).
- Лишние заголовки Claude Code (`anthropic-beta`, `x-app`, `user-agent`
  и пр.) — игнорируем; `anthropic-version` логируем (§3).
- Пример для `qween/`-style README положим аналог в `claude-code/`
  (док-задача при реализации, вне этой спеки).

## 3. Аутентификация

Сверяем `PROXY_API_KEY`, принимая ключ из **любого** из двух заголовков:

1. `x-api-key: <PROXY_API_KEY>` — так шлёт официальный Anthropic SDK и
   Claude Code в режиме `ANTHROPIC_API_KEY`.
2. `Authorization: Bearer <PROXY_API_KEY>` — так шлёт Claude Code в
   режиме `ANTHROPIC_AUTH_TOKEN` и единообразно с остальными нашими
   эндпоинтами.

Если присутствуют оба — допустимо любое совпадение с `PROXY_API_KEY`.
Заголовок `anthropic-version` (например, `2023-06-01`) **читаем и
логируем**, но строгую валидацию значения не требуем (free-tier шим, не
притворяемся реальным API). Отсутствие `anthropic-version` — не ошибка.

Ошибки авторизации — в **Anthropic-формате** (см. §7), не в нашем
OpenAI-style `{"error":{"code":...}}`:

```json
// 401, нет валидного ключа
{"type": "error", "error": {"type": "authentication_error", "message": "..."}}
```

Реализация: отдельная FastAPI-зависимость `require_anthropic_key`
(новая, рядом с `require_proxy_key` в `auth.py`), потому что и источник
ключа (два заголовка), и формат ошибки отличаются от bearer-зависимости
OpenAI-эндпоинтов.

## 4. Перевод запроса: Anthropic → OpenAI

Тело — стандартный Anthropic Messages request. Переводим в
OpenAI-Chat-Completions-dict, который дальше едет в существующий
конвейер без изменений.

### 4.1. Поля верхнего уровня

| Anthropic                    | OpenAI                              | Примечание                                            |
|------------------------------|-------------------------------------|-------------------------------------------------------|
| `model`                      | — (игнор)                           | как везде: выбираем по `rank`; requested-id логируем  |
| `max_tokens` (**обязателен**)| `max_tokens`                        | у Anthropic обязателен; отсутствие → `400 invalid_request_error` |
| `system` (string)            | `messages[0] = {role:"system", content}` | prepend                                          |
| `system` (array text-блоков) | то же, текст блоков склеен          | не-text блоки в system → дроп + WARN                  |
| `messages`                   | `messages` (см. §4.2)               | перевод content-блоков                                |
| `tools`                      | `tools` (см. §4.3)                  | `input_schema` → `function.parameters`                |
| `tool_choice`                | `tool_choice` (см. §4.3)            | `auto/any/tool/none` → OpenAI-эквивалент              |
| `stop_sequences`             | `stop`                              | список as-is                                          |
| `temperature`                | `temperature`                       | as-is                                                 |
| `top_p`                      | `top_p`                             | as-is                                                 |
| `top_k`                      | — (дроп)                            | в OpenAI Chat Completions нет; дроп + DEBUG-лог       |
| `metadata`                   | — (дроп)                            | `metadata.user_id` не пробрасываем                    |
| `stream`                     | `stream`                            | управляет веткой обработки (§6)                       |

### 4.2. Перевод `messages` и content-блоков

Anthropic `content` бывает строкой или массивом блоков. Правила:

- `content: "строка"` → OpenAI `content: "строка"` (роль сохраняется).
- `content: [блоки]`:
  - `{"type":"text","text":T}` → text. Если в сообщении один text-блок,
    схлопываем в строку; если несколько/смешанные — OpenAI-массив
    `[{"type":"text","text":T}, ...]`.
  - `{"type":"tool_use","id":ID,"name":N,"input":OBJ}` (в `assistant`) →
    переносится в `assistant.tool_calls`:
    ```json
    {"id": ID, "type": "function",
     "function": {"name": N, "arguments": "<json.dumps(OBJ)>"}}
    ```
    Текст и `tool_use` в одном assistant-сообщении → `content` (текст) +
    `tool_calls` (вызовы) в одном OpenAI-сообщении.
  - `{"type":"tool_result","tool_use_id":TID,"content":C,"is_error":E}`
    (в `user`) → **отдельное** OpenAI-сообщение
    `{"role":"tool","tool_call_id":TID,"content":<C как текст>}`.
    `is_error:true` — текст результата отдаём как есть (OpenAI не имеет
    отдельного флага ошибки у tool-сообщения); при необходимости
    префиксуем маркером — **решение: без префикса**, отдаём контент as-is.
    `content` tool_result бывает строкой или массивом text-блоков —
    склеиваем в строку (не-text блоки → дроп + WARN).
  - `{"type":"image",...}` → **не поддерживается в MVP**: `400
    invalid_request_error` с понятным сообщением (см. §11).
  - `{"type":"thinking"|"redacted_thinking",...}` → дроп + DEBUG-лог.

> Один Anthropic-`user`-message с несколькими `tool_result`-блоками
> разворачивается в **несколько** OpenAI `role:"tool"`-сообщений (по
> одному на блок) — порядок сохраняем.

### 4.3. `tools` и `tool_choice`

`tools`:
```json
// Anthropic
{"name": N, "description": D, "input_schema": SCHEMA}
// →  OpenAI
{"type": "function", "function": {"name": N, "description": D, "parameters": SCHEMA}}
```

`tool_choice`:

| Anthropic                                   | OpenAI                                              |
|---------------------------------------------|-----------------------------------------------------|
| `{"type":"auto"}` / отсутствует             | `"auto"` (или поле не ставим)                       |
| `{"type":"any"}`                            | `"required"`                                        |
| `{"type":"tool","name":N}`                  | `{"type":"function","function":{"name":N}}`         |
| `{"type":"none"}`                           | `"none"`                                            |
| `disable_parallel_tool_use: true`           | `parallel_tool_calls: false`                        |

### 4.4. Capability-фильтр

После перевода в OpenAI-shape применяем **тот же** `select_candidates`
(основная спека §5.1): наличие `tools` → требуем `supports_tools`,
`tool_choice` (≠`auto`) → `supports_tool_choice`, `stop` → `supports_stop`
и т.д. Никакой отдельной матрицы для Anthropic не вводим — переведённый
dict уже выглядит как OpenAI-запрос, фильтр работает без изменений.

Если после фильтра пусто → `400 invalid_request_error` (Anthropic-формат,
сообщение `no_capable_model`).

## 5. Перевод ответа: OpenAI → Anthropic (non-stream)

OpenAI-ответ (`resp.model_dump()` из `Upstream.chat`) → Anthropic
Messages-объект:

```json
{
  "id": "msg_<...>",
  "type": "message",
  "role": "assistant",
  "model": "<chosen_model_id>",
  "content": [ ...блоки... ],
  "stop_reason": "<map>",
  "stop_sequence": null,
  "usage": {"input_tokens": <prompt_tokens>, "output_tokens": <completion_tokens>}
}
```

- `id`: берём `chatcmpl`-id из ответа или генерим `msg_<uuid hex>`.
- `model`: реальный выбранный `model.id` (тот же, что в заголовке
  `x-free-llm-proxy-model`).
- `content`: из `choices[0].message`:
  - `message.content` (строка) → `[{"type":"text","text": C}]` (пустую/
    `null` строку → пропускаем блок).
  - `message.tool_calls[]` → по блоку `tool_use` на каждый:
    ```json
    {"type":"tool_use","id": call.id, "name": fn.name,
     "input": json.loads(fn.arguments)}
    ```
    битый JSON в `arguments` → `input: {}` + WARN (не валим ответ).
  - Порядок: сначала text-блок (если есть), затем tool_use-блоки.
- `stop_reason` — маппинг `finish_reason` (§5.1).
- `usage`: `prompt_tokens → input_tokens`, `completion_tokens →
  output_tokens` (отсутствуют → `0`).
- Заголовок ответа `x-free-llm-proxy-model: <chosen_model_id>` — как и
  на OpenAI-эндпоинте.

### 5.1. Маппинг `finish_reason` → `stop_reason`

| OpenAI `finish_reason` | Anthropic `stop_reason` |
|------------------------|-------------------------|
| `stop`                 | `end_turn`              |
| `length`               | `max_tokens`            |
| `tool_calls`           | `tool_use`              |
| `content_filter`       | `end_turn` (нет точного аналога; логируем) |
| `null`/неизвестно      | `end_turn`              |

`stop_sequence`: если знаем, какая из `stop_sequences` сработала —
ставим её; OpenRouter обычно не сообщает → `null`.

## 6. Streaming (Anthropic SSE)

`stream=true` → к OpenRouter тоже идём со `stream=true` (`Upstream.
chat_stream`), плюс просим usage: добавляем в OpenAI-запрос
`stream_options: {"include_usage": true}` (нужно для `output_tokens` в
`message_delta`).

Ответ клиенту — `text/event-stream`, формат **именованных** SSE-событий
Anthropic: каждое событие — `event: <type>\n` + `data: <json>\n\n`.

### 6.1. Порядок событий (happy path, только текст)

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","type":"message",
       "role":"assistant","model":"<id>","content":[],"stop_reason":null,
       "stop_sequence":null,"usage":{"input_tokens":<est>,"output_tokens":0}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}
        ... (по чанку) ...
event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},
       "usage":{"output_tokens":<M>}}

event: message_stop
data: {"type":"message_stop"}
```

- `input_tokens` в `message_start` известен не сразу (OpenRouter отдаёт
  usage только в финальном чанке). Ставим **best-effort оценку** (та же
  функция, что в `count_tokens`, §8); по приходу реального usage не
  пересылаем (Anthropic input_tokens в `message_start` фиксирован).
  Это задокументированное упрощение.
- `ping`-события (`event: ping\ndata: {"type":"ping"}`) — опционально,
  можно слать периодически, чтобы держать соединение; в MVP допустимо
  не слать.

### 6.2. Tool use в потоке

OpenAI отдаёт `tool_calls` чанками с `index`, `id`, `name` и
инкрементальными `arguments`. Переводим в Anthropic tool_use-блок:

```
event: content_block_start
data: {"type":"content_block_start","index":1,
       "content_block":{"type":"tool_use","id":"toolu_...","name":"get_weather","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,
       "delta":{"type":"input_json_delta","partial_json":"{\"location\":"}}
        ... (фрагменты arguments) ...
event: content_block_stop
data: {"type":"content_block_stop","index":1}
```

### 6.3. Стейт-машина транслятора потока

Это **основной объём работы**. Транслятор потребляет OpenAI-чанки и
выдаёт Anthropic-события, удерживая состояние:

- `current_index` и тип текущего открытого блока (`text` | `tool_use` |
  нет).
- При первой `delta.content` (текст): если открыт tool_use-блок —
  закрыть его (`content_block_stop`), открыть text-блок
  (`content_block_start`), затем `content_block_delta`/`text_delta`.
- При `delta.tool_calls[i]`:
  - новый `i` (приходит `id`+`name`) → закрыть текущий блок, открыть
    `tool_use`-блок (`content_block_start` с `id`,`name`,`input:{}`);
  - фрагменты `function.arguments` → `input_json_delta`/`partial_json`.
  - несколько tool_call по разным `index` → несколько Anthropic-блоков.
- При `finish_reason` (или конце потока) → закрыть текущий блок,
  `message_delta` (с `stop_reason` по §5.1 и `output_tokens` из usage,
  если пришёл), `message_stop`.

Транслятор оформляем **чистым** генератором/классом
(`AnthropicStreamTranslator`), отделённым от FastAPI, чтобы покрыть
табличными тестами (вход: список OpenAI-чанков → выход: точная
последовательность Anthropic-событий).

### 6.4. Fallback в потоке

Наследуем правила §5.6 основной спеки:

- Пока **`message_start` не отправлен** клиенту, ведём себя как
  non-stream: ошибка на `chat_stream()` (create-time) → классификация →
  cooldown → следующая модель; `4xx`(≠429) → Anthropic-error (passthrough
  статус), fallback не триггерится; все упали → `503`/`overloaded_error`.
- **После** `message_start` fallback невозможен. Mid-stream ошибка:
  - кладёт модель в cooldown (по §5.3/§5.4);
  - в поток уходит **один** кадр Anthropic-ошибки и поток закрывается:
    ```
    event: error
    data: {"type":"error","error":{"type":"<map>","message":"..."}}
    ```
    (`message_stop` после `error` не обязателен; Anthropic завершает
    поток событием `error`).
- Метрика статуса для стрима с mid-stream ошибкой — `200/mid_error`
  (как на OpenAI-эндпоинте).

## 7. Маппинг ошибок (Anthropic error format)

Все ошибки эндпоинта — строго в Anthropic-обёртке:
```json
{"type": "error", "error": {"type": "<тип>", "message": "<текст>"}}
```

Типы и статусы Anthropic:

| Ситуация (наш `Outcome`/случай)                  | HTTP | Anthropic `error.type`     |
|--------------------------------------------------|:----:|----------------------------|
| невалидный JSON / нет `max_tokens` / image-блок  | 400  | `invalid_request_error`    |
| после capability-фильтра пусто (`no_capable_model`) | 400 | `invalid_request_error`   |
| нет/неверный ключ прокси                          | 401  | `authentication_error`     |
| `client_error` (прочий 4xx от OpenRouter)         | как у upstream | `invalid_request_error` (или по статусу) — текст из тела upstream |
| `upstream_auth_error` (плохой `OPENROUTER_API_KEY`) | 502 | `api_error` (текст с хвостом ключа, как на OpenAI-эндпоинте) |
| все модели недоступны (`all_models_unavailable`)  | 503  | `overloaded_error`         |

- Логика **что считать недоступностью**, cooldown по `429`/`503`,
  «не fallback на `401/403`» — целиком из §5.3/§5.4 основной спеки;
  меняется только формат тела ответа (Anthropic вместо OpenAI-style).
- `upstream_auth_error`: сохраняем диагностику с хвостом ключа
  (`...XXXX (len=N)`), но заворачиваем в Anthropic-`api_error`.

## 8. `count_tokens`

`POST /api/anthropic/v1/messages/count_tokens` принимает то же тело, что
`/v1/messages` (без `max_tokens`-обязательности), и возвращает:
```json
{"input_tokens": <N>}
```

- **Реального токенизатора целевой модели у нас нет** (модель выбирается
  по rank в момент `/v1/messages`, токенайзеры у free-моделей разные).
  Поэтому отдаём **приблизительную оценку**: сериализуем `system` +
  `messages` (+ `tools`-схемы) в текст и оцениваем
  `ceil(len(text) / ANTHROPIC_TOKENS_PER_CHAR_DIVISOR)` (дефолт делителя
  — `4`).
- Это **намеренно грубая** оценка; задокументировано как ограничение.
  Если позже понадобится точность — подключим `tiktoken`-эвристику
  (открытый вопрос §12).
- Эндпоинт **не ходит в upstream** и не выбирает модель.

## 9. Конфигурация

Новых **обязательных** env нет — переиспользуем `OPENROUTER_API_KEY`,
`PROXY_API_KEY` и весь блок §6 основной спеки. Добавляем опциональные:

| Переменная                         | Default | Назначение                                   |
|------------------------------------|---------|----------------------------------------------|
| `ANTHROPIC_API_ENABLED`            | `true`  | смонтировать ли `/api/anthropic`-роутер      |
| `ANTHROPIC_TOKENS_PER_CHAR_DIVISOR`| `4`     | делитель для эвристики `count_tokens`         |

Обе — в `config.py` (`Settings`) и `.env.example`.

## 10. Структура кода (планируемые изменения)

Новые файлы:

```
src/free_llm_proxy/
├── anthropic_translate.py   # чистые функции перевода (без I/O, без FastAPI):
│                            #   anthropic_to_openai_request(body) -> dict
│                            #   openai_to_anthropic_response(resp, model_id) -> dict
│                            #   map_stop_reason(finish_reason) -> str
│                            #   estimate_input_tokens(body, divisor) -> int
│                            #   AnthropicStreamTranslator  (стейт-машина §6.3)
├── anthropic_schemas.py     # (опц.) pydantic-модели запроса Messages
└── api/
    └── anthropic.py         # роутер /api/anthropic/v1/messages (+count_tokens),
                             #   non-stream + stream-обработчики
```

Правки существующих файлов:

- `main.py` — `include_router(anthropic.router, prefix="/api/anthropic")`
  (под флагом `ANTHROPIC_API_ENABLED`).
- `auth.py` — зависимость `require_anthropic_key` (x-api-key **или**
  Bearer; Anthropic-формат ошибки 401).
- `config.py` / `.env.example` — две новые опции (§9).

### 10.1. Переиспользование цикла fallback

Цикл попыток с cooldown/attempt-логом сейчас живёт внутри `api/chat.py`
(`_handle_nonstream` / `_handle_stream`) и завязан на OpenAI-
`JSONResponse`/SSE. Anthropic-обработчику нужна **та же** логика выбора
и fallback, но другая сериализация ответа.

**Решение (рекомендуется):** вынести инвариантное ядро цикла из
`chat.py` в небольшой переиспользуемый помощник (модуль `fallback.py`
или функции в `upstream.py`), который принимает callbacks/возвращает
результат попытки, не зная про формат ответа. Тогда и OpenAI-, и
Anthropic-обработчики строятся поверх него. Это **затрагивает
существующий `chat.py`** (небольшой рефакторинг без смены поведения,
прикрытый текущими тестами `test_api_chat.py`).

**Альтернатива:** продублировать цикл в `anthropic.py`. Меньше риска для
`chat.py`, но дублирование логики cooldown/attempt. Выбор подтвердить
при реализации; по умолчанию идём путём извлечения общего ядра.

## 11. Метрики и логи

- Метрики **переиспользуем** без новых лейблов:
  `freellm_requests_total{status}`, `freellm_request_duration_seconds`,
  `freellm_upstream_attempts_total{model_id, outcome}`. Кардинальность
  не раздуваем.
- В `request_done`-лог добавляем поле `api: "anthropic"`, чтобы отличать
  от OpenAI-эндпоинта; остальные поля (`request_id`, `attempts`,
  `chosen_model`, `duration_ms`, `had_tools`, …) — как в §7 основной
  спеки. Тело запроса/ответа по-прежнему **не логируем**.

## 12. Не входит в Anthropic-MVP (нецели)

- **Images** (`type:"image"`-блоки) — `400` с понятным сообщением.
- **`thinking`/extended reasoning** блоки (roundtrip) — дропаем на входе,
  не генерим на выходе.
- **Prompt caching** (`cache_control`) — игнорируем.
- **`top_k`** — дропаем (нет в OpenAI Chat Completions).
- **Anthropic-формат `GET /v1/models`** под `/api/anthropic` — не делаем
  (открытый вопрос).
- **Batch API**, **Files API**, **`metadata.user_id`**, **`service_tier`**
  — нет.

## 13. Verification — что добавить в тесты

Дополняет `spec/verification.md`. Принципы те же (быстро, без сети,
`respx` поверх `ASGITransport`, `live` — opt-in).

### 13.1. Unit (чистый перевод, без I/O)
`tests/test_anthropic_translate.py`:
- `anthropic_to_openai_request`: `system` (string/array), текстовые
  сообщения, `tool_use`→`tool_calls`, `tool_result`→`role:"tool"`
  (в т.ч. несколько в одном user-сообщении), `tools`
  (`input_schema`→`parameters`), `tool_choice` (auto/any/tool/none +
  `disable_parallel_tool_use`), `stop_sequences`→`stop`, дроп `top_k`/
  `metadata`, отсутствие `max_tokens`→ошибка, image-блок→ошибка.
- `openai_to_anthropic_response`: text-only, tool_use (валидный/битый
  JSON в arguments), `usage`-маппинг, `map_stop_reason` (таблица §5.1).
- `estimate_input_tokens`: пустой/непустой ввод, влияние делителя.

### 13.2. Unit стрим-транслятора
`tests/test_anthropic_stream.py` — вход: список OpenAI-чанков, выход:
точная последовательность Anthropic-событий:
- только текст → `message_start … content_block_start(text) …
  text_delta* … content_block_stop … message_delta(end_turn) …
  message_stop`;
- один tool_use → `tool_use`-блок с `input_json_delta`,
  `stop_reason:tool_use`;
- текст + tool_use (смена блока, корректные `index`);
- несколько tool_call (разные `index` → разные блоки);
- mid-stream исключение → событие `event: error`.

### 13.3. E2E in-process (`ASGITransport` + `respx`)
`tests/test_api_anthropic.py`:
- non-stream happy path: ответ в Anthropic-форме, `content[0].type=="text"`,
  заголовок `x-free-llm-proxy-model`, `stop_reason`, `usage`.
- tools round-trip: запрос с `tools` → ответ с `tool_use`-блоком.
- rate-limit fallback: модель #1 → `429`, модель #2 → `200`; в
  `request_done` две попытки.
- все модели недоступны → `503` `overloaded_error` (Anthropic-формат).
- `4xx` (≠429) от upstream → passthrough в Anthropic-error, fallback не
  триггерится.
- capability-фильтр: `tools` при отсутствии `supports_tools`-моделей →
  `400 invalid_request_error` (`no_capable_model`).
- `stream=true` happy path: `text/event-stream`, порядок событий
  `message_start … message_stop`, ≥ N кадров; заголовок присутствует.
- `stream=true` с `429` на первой модели → fallback до `message_start`,
  клиент видит нормальный поток второй модели.
- `count_tokens` → `{"input_tokens": N>0}`.

### 13.4. Auth
`tests/test_anthropic_auth.py`:
- `x-api-key` валидный → `200`; `Authorization: Bearer` валидный → `200`;
  нет заголовка / неверный ключ → `401` `authentication_error`
  (Anthropic-формат).

### 13.5. Live (`@pytest.mark.live`)
Добавить в `tests/test_live.py` сценарий: реальный `POST
/api/anthropic/v1/messages` против запущенного прокси — проверяем `200`,
непустой `content[0].text`, заголовок `x-free-llm-proxy-model`.
Корректность ответа модели не проверяем (как и в основной спеке).

### 13.6. Ручной acceptance-критерий (главная цель)
**Реальный Claude Code подключается и работает** против запущенного
прокси:
```bash
export ANTHROPIC_BASE_URL=http://localhost:8080/api/anthropic
export ANTHROPIC_AUTH_TOKEN=$PROXY_API_KEY
claude   # отвечает на запрос, успешно выполняет хотя бы один tool-call
```
Проверяем: интерактивный ответ приходит (streaming), tool-вызовы
(например, чтение файла) проходят round-trip. Это финальный признак, что
шим достаточно полон. Не автоматизируем — ручная проверка при сдаче.

## 14. Открытые вопросы

- **Точность `count_tokens`**: грубая эвристика `len/4` vs зависимость
  `tiktoken`. Пока — эвристика.
- **Anthropic-формат `/v1/models`** под `/api/anthropic`: нужен ли
  Claude Code на практике? Пока `404`.
- **Извлечение общего ядра fallback** (§10.1): рефакторинг `chat.py` или
  дублирование — подтвердить при реализации.
- **`is_error` у `tool_result`**: отдавать контент as-is (текущее
  решение) или префиксовать маркером ошибки для модели.
- **`top_k`**: молчаливый дроп (текущее решение) или `400`.
