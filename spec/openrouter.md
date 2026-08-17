# OpenRouter passthrough — спецификация

Сопроводительный документ к **`free-llm-proxy.md`**. Описывает третий
вход в прокси — **прозрачное проксирование OpenRouter** под префиксом
`/api/openrouter`. В отличие от fallback-конвейера (`/v1/...`) и
Anthropic-шима (`/api/anthropic`), здесь **модель указывает клиент**, а
прокси ничего не выбирает, не переводит и не ретраит — только
подставляет `OPENROUTER_API_KEY` и передаёт байты. Всё, что здесь не
переопределено, наследуется из основной спеки.

## 1. Цель

Дать клиентам полный доступ к OpenRouter API через ту же единую точку и
тот же `PROXY_API_KEY`, что и остальные эндпоинты, — без раздачи самого
`OPENROUTER_API_KEY`. Типовой сценарий: клиенту нужна **конкретная**
модель (в т.ч. платная), а не «лучшая бесплатная по rank».

Решения зафиксированы 2026-08-17 (по согласованию):

- **Скоуп** — прозрачный прокси **всех** путей `/api/v1/*` OpenRouter,
  тело не парсим (raw passthrough).
- **Модели** — без ограничений: любая модель, включая платные
  (кредиты списываются с `OPENROUTER_API_KEY`; риск осознаётся
  владельцем ключа).
- **Изоляция** — ошибки upstream (`429`/`5xx`) уходят клиенту as-is и
  **не** трогают общую cooldown-таблицу / registry.
- **Префикс** — `/api/openrouter`, симметрично `/api/anthropic`.
- **Провизионные пути режем** (решение 2026-08-17, вторым заходом):
  ключевые/аккаунтные эндпоинты OpenRouter через прокси недоступны —
  см. блоклист в §2.1.

## 2. Точки входа (namespace `/api/openrouter`)

Клиент указывает базовый URL прокси; хвост пути повторяет OpenRouter:

```
base_url = http://localhost:8080/api/openrouter/api/v1
клиент → POST /api/openrouter/api/v1/chat/completions
прокси → POST https://openrouter.ai/api/v1/chat/completions
```

- Маппинг: `/api/openrouter/<tail>` → `<OPENROUTER_PROXY_BASE>/<tail>`,
  где `OPENROUTER_PROXY_BASE = https://openrouter.ai` (env, §6).
  То есть проксируется **весь хвост как есть**, включая `/api/v1`:
  ничего не переписываем, клиентские SDK с
  `base_url=.../api/openrouter/api/v1` работают без сюрпризов.
- Для удобства OpenAI-SDK-клиентов (ждут `.../v1`) дополнительно
  принимаем `/api/openrouter/v1/<tail>` как алиас
  `/api/openrouter/api/v1/<tail>` — одна строка в роутере.
- Методы — любые (`GET`/`POST`/`DELETE`/`PATCH`/...); query-параметры
  передаются без изменений.
- Реализация — catch-all route (`/{path:path}`) в новом роутере
  `api/openrouter.py`, никакой валидации тела: JSON, SSE, бинарные
  ответы — всё байт-в-байт.

### 2.1. Блоклист: провизионные/аккаунтные пути

Пути управления ключами и аккаунтом OpenRouter через прокси **не
проксируются** — `PROXY_API_KEY` даёт доступ к инференсу, а не к
управлению чужим `OPENROUTER_API_KEY`. Блокируются хвосты (после
нормализации алиаса, без учёта регистра, по префиксу сегментов):

- `api/v1/key`, `api/v1/keys` — информация о ключе и provisioning-ключи;
- `api/v1/credits` — баланс/кредиты аккаунта;
- `api/v1/auth` — OAuth-обмен ключей.

Ответ — `403` в нашем OpenAI-style формате:
`{"error":{"code":"forbidden_path","message":"This OpenRouter path is not exposed via the proxy."}}`.
До upstream такой запрос не доходит. Список захардкожен (не env):
расширение — правкой кода.

## 3. Аутентификация

Как в остальных методах: `Authorization: Bearer <PROXY_API_KEY>`,
зависимость `require_proxy_key`, ошибки в нашем OpenAI-style формате
(`401 missing_authorization` / `403 invalid_token`).

Клиентский `Authorization` **не** пробрасывается в OpenRouter — на его
место подставляется `Bearer <OPENROUTER_API_KEY>`.

## 4. Правила проксирования

### 4.1. Заголовки

Запрос к OpenRouter:

- `Authorization: Bearer <OPENROUTER_API_KEY>` — всегда подставляем.
- `HTTP-Referer` / `X-Title` — из настроек (`openrouter_referer`,
  `openrouter_title`), как в `Upstream`.
- Из клиентских заголовков пробрасываем `Content-Type`, `Accept`,
  `Accept-Encoding` и `X-*`-заголовки (кроме `X-Api-Key`); остальные —
  дроп (в т.ч. hop-by-hop: `Host`, `Connection`, `Content-Length` —
  httpx выставит свои).

Ответ клиенту:

- Статус — как у OpenRouter.
- Заголовки — пробрасываем всё, кроме hop-by-hop (`Connection`,
  `Transfer-Encoding`, `Keep-Alive`, ...). `Content-Type`,
  `Content-Encoding`, `X-RateLimit-*`, `Retry-After` доходят до клиента
  как есть.

### 4.2. Тело и streaming

- Тело запроса читаем и отправляем как байты, не разбирая.
- Тело ответа отдаём потоково байт-в-байт (`aiter_raw()` +
  `StreamingResponse`) — одинаково работает для JSON и для SSE
  (`stream=true` у клиента прозрачно превращается в SSE от OpenRouter).
  Благодаря raw-передаче `Content-Encoding` (gzip и т.п.) сохраняется
  без перекодирования.
- Никаких ретраев, fallback'ов и модификаций тела. Поле `model` —
  целиком ответственность клиента.

### 4.3. Отношения с registry

Эндпоинт **не читает и не пишет** snapshot/cooldowns:

- работает, даже когда snapshot пуст (`/ready` = 503 не мешает);
- `429`/`503` от OpenRouter не ставят cooldown моделям
  fallback-конвейера.

## 5. Ошибки

| Ситуация                                   | Ответ клиенту                                              |
|--------------------------------------------|------------------------------------------------------------|
| Любой HTTP-ответ OpenRouter (2xx–5xx), кроме 401/403 | passthrough: статус + тело + заголовки as-is        |
| `401`/`403` от OpenRouter                  | `502 upstream_auth_error` с хвостом ключа — как в §5.3 основной спеки (это плохой `OPENROUTER_API_KEY`, а не ошибка клиента) |
| Сетевая ошибка / таймаут до первого байта ответа | `502 {"error":{"code":"upstream_unreachable","message":...}}` |
| Обрыв в середине потокового ответа         | соединение с клиентом закрывается (статус уже отправлен, ничего не дописываем) |

> Предложение на ревизию: перевод `401/403 → 502` выбран ради
> единообразия с остальными эндпоинтами и чтобы клиент не путал ошибку
> своего `PROXY_API_KEY` с ошибкой ключа прокси. Если нужен строго
> «прозрачный» прокси — заменить на passthrough.

## 6. Конфигурация

Новых обязательных env нет. Добавляются опциональные:

| Переменная                       | Default                  | Назначение                                    |
|----------------------------------|--------------------------|-----------------------------------------------|
| `OPENROUTER_PROXY_ENABLED`       | `true`                   | смонтировать ли `/api/openrouter`-роутер      |
| `OPENROUTER_PROXY_BASE`          | `https://openrouter.ai`  | корень, куда уходит хвост пути                |
| `OPENROUTER_PROXY_TIMEOUT_SEC`   | `120`                    | таймаут одного запроса (свой, не `UPSTREAM_TIMEOUT_SEC`: платные/тяжёлые модели могут отвечать дольше 30 с) |

Все — в `config.py` (`Settings`) и `.env.example`. Существующий
`Upstream` (openai SDK) не используется — у passthrough свой
`httpx.AsyncClient` (создаётся в `create_app`, закрывается в lifespan).

## 7. Метрики и логи

- Новый counter `freellm_openrouter_proxy_requests_total{method, status}`.
  В общие `freellm_requests_total` / `freellm_upstream_attempts_total`
  **не** пишем — там семантика fallback-конвейера.
- Лог `request_done` с `api: "openrouter"`: `request_id`, `method`,
  `path` (хвост без query), `status`, `duration_ms`, `requested_model`
  (если тело — JSON с полем `model`; парсим только для лога,
  best-effort, ошибки парсинга молча игнорируем). Тела запроса/ответа
  **не логируем** (как везде).

## 8. Структура кода (планируемые изменения)

```
src/free_llm_proxy/
└── api/
    └── openrouter.py    # catch-all роутер: заголовки (§4.1),
                         #   потоковая передача тела (§4.2), ошибки (§5)
```

Правки существующих файлов:

- `main.py` — `include_router(openrouter.router, prefix="/api/openrouter")`
  под флагом `OPENROUTER_PROXY_ENABLED`; создание/закрытие
  `httpx.AsyncClient` для passthrough.
- `config.py` / `.env.example` — три новые опции (§6).
- `CLAUDE.md` — упомянуть третий вход (док-задача при реализации).

## 9. Не входит (нецели)

- Выбор модели, fallback, cooldown — этого здесь нет намеренно.
- Ограничение на `:free`-модели или лимиты трат — нет (решение §1);
  если понадобится, добавим флагом отдельной задачей.
- Кэширование, переписывание тел, WebSocket-проксирование.
- Отдельный ключ авторизации для этого namespace — используется общий
  `PROXY_API_KEY`.

## 10. Verification — что добавить в тесты

Принципы из `spec/verification.md` (быстро, без сети, `respx` поверх
`ASGITransport`; `live` — opt-in).

`tests/test_api_openrouter.py`:

- happy path POST `chat/completions`: тело ушло в OpenRouter байт-в-байт,
  `Authorization` заменён на `OPENROUTER_API_KEY`, `HTTP-Referer`/`X-Title`
  подставлены, ответ (статус+тело+`Content-Type`) вернулся as-is.
- GET `models` c query-параметрами → параметры дошли до upstream.
- алиас `/api/openrouter/v1/...` → тот же upstream-путь `/api/v1/...`.
- streaming: SSE-байты от замоканного OpenRouter доходят до клиента без
  изменений, `Content-Type: text/event-stream` сохранён.
- `429` от upstream → `429` клиенту + **cooldown-таблица не изменилась**.
- `401` от upstream → `502 upstream_auth_error` (хвост ключа в тексте).
- сетевая ошибка → `502 upstream_unreachable`.
- auth: без ключа → `401`, с неверным → `403` (OpenAI-style формат).
- блоклист (§2.1): `api/v1/credits`, `api/v1/keys`, `api/v1/key`,
  `v1/keys` (через алиас) → `403 forbidden_path`, upstream **не**
  вызывался; соседние пути (`api/v1/keysmith` — гипотетический) не
  задеты префиксным матчем по сегментам.
- `OPENROUTER_PROXY_ENABLED=false` → `404` на любой путь namespace.

Live (`@pytest.mark.live`, в `tests/test_live.py`): один реальный POST
`/api/openrouter/api/v1/chat/completions` с явной `:free`-моделью —
`200`, непустой `choices[0].message.content`. Платные модели в live не
дёргаем.

## 11. Открытые вопросы

- `401/403 → 502` vs полный passthrough (§5) — принято `502`,
  пересмотреть на ревью.
- Нужен ли отдельный (второй) входной ключ для этого namespace, чтобы
  раздавать доступ «только к passthrough» или «только к fallback»
  отдельно? Пока нет — общий `PROXY_API_KEY`.

> Вопрос о провизионных путях (`/api/v1/keys`, `/api/v1/credits`)
> закрыт 2026-08-17: **режем**, см. §2.1.
