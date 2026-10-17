# Architecture — Content Fabric MAX News Bot

Небольшой production-бот для сбора новостей из RSS-источников и публикации в канал мессенджера MAX.
Проект — порт Telegram-бота content_fabric на российский стек доставки: мессенджер MAX (platform-api2.max.ru)
и OpenAI-совместимые LLM-провайдеры для постов — OpenRouter первичным каналом,
Yandex AI Studio резервным (ADR-002). Проект должен оставаться простым: сначала автономный MAX-бот,
затем аккуратное встраивание в систему фабрики контента.

Главный принцип: отделить сбор и подготовку новости от способа публикации. Сегодня выходом является MAX,
раньше был Telegram — ядро (RSS-сбор, фильтры, дедупликация, фабрика) при порте не поменялось.

Контент-фабрика реализована внутри этого же бота — как второй параллельный цикл; см. раздел «Контент-фабрика».

---

## ADR-001: Миграция на MAX + Yandex AI Studio

Статус: принято (порт Telegram-бота content_fabric).

### Контекст и причины

Исходный бот публиковал дайджест в Telegram и генерировал посты через зарубежный
OpenAI-совместимый провайдер. Обе платформы из РФ требуют обходных решений (прокси),
что усложняет деплой и делает его хрупким. Цель порта — полностью российский стек
доставки, работающий с VPS в РФ напрямую, без прокси-переменных и socks-зависимостей.

### Решения

1. **Тонкий собственный aiohttp-клиент MAX (`max_api/`) вместо молодых SDK**
   (max-botapi-python, aiomax). Нужная поверхность API мала (`GET /updates`,
   `POST /messages`, `GET /me`, удаление сообщения), SDK молодые и тянут лишние
   зависимости; образец уже есть в проекте — `LlmWriter` ходит в OpenAI-совместимый
   API на голом aiohttp. Свой клиент даёт полный контроль над ретраями и таймаутами.
2. **Long polling `GET /updates` для MVP.** Webhook — следующий шаг: он требует
   публичный https-домен с TLS-сертификатом от доверенного CA и подтверждение (ACK)
   обновлений в течение 30 секунд. Long polling не требует внешнего ingress,
   работает за NAT и полностью покрывает нагрузку одного бота.
3. **OpenAI-совместимый endpoint Yandex AI Studio** (`https://ai.api.cloud.yandex.net/v1`,
   модель `gpt://<folder_id>/yandexgpt-5-lite`) вместо native Completion API Yandex:
   код `LlmWriter` не меняется (та же схема `base_url` + `model` + OpenAI-протокол),
   провайдер переключается переменными окружения.
4. **Без прокси.** platform-api2.max.ru и ai.api.cloud.yandex.net доступны с
   российских IP напрямую; из зависимостей удалены aiogram и aiohttp-socks,
   прокси-переменные окружения убраны.

### Различия MAX vs Telegram, учтённые в коде

| Аспект | Telegram | MAX |
|---|---|---|
| Авторизация | `Authorization: Bearer <token>` | `Authorization: <token>` (без префикса Bearer) |
| Лимит сообщения | 4096 символов | 4000 символов |
| Rate limit | ~30 msg/s | 2 msg/sec на диалог (30 rps глобально), HTTP 429 |
| Идентификатор сообщения | `message_id` | `mid` |
| Методы API | camelCase-методы (`/sendMessage`) | REST-ресурсы (`POST /messages?chat_id=…`) |
| Адресация получателя | `chat_id` для любого диалога | `user_id` — личный диалог, `chat_id` — группа/канал; при обоих API молча предпочитает `user_id` |
| Личные сообщения | бот может писать первым | бот пишет в личку только после `/start` пользователя |

Идентификатор сообщения `mid` — **строка** вида `mid.<id>`, а не число (в отличие
от числового `message_id` Telegram): она проходит по всей цепочке как `str`
(`MaxClient.send_message` → `PostPublishResult.message_id` → `Post.message_id`),
а колонка `posts.message_id` в SQLite хранится как `TEXT` (миграция v3 перевела
её из INTEGER). Раньше код ждал от API числовой id: сообщение уже доставалось в
канал, но разбор ответа падал, публикация считалась неуспешной и `mark_sent` не
выполнялся — из-за этого дайджесты перепостились каждый цикл; строковый `mid`
чинит этот баг.

### Последствия

- `ADMIN_IDS`: каждый админ обязан один раз написать боту `/start` — иначе не
  доставятся превью фабрики (dry-run) и алерты: бот в MAX не может инициировать диалог.
- `ADMIN_IDS` — это **user_id** админов: личные сообщения (`MaxClient.send_message`)
  адресуются query-параметром `user_id`, а `chat_id` в MAX ищет группу/канал, поэтому
  отправка user_id как chat_id ломает доставку. Клиент требует ровно один из двух
  адресов (иначе ValueError), хотя сам API молча предпочёл бы `user_id`.
- Жёсткий лимит текста — 4000 символов (валидатор, форматтер).
- `PostPublisher` обрабатывает HTTP 429 (`MaxRateLimitError`) и держит паузу между
  постами под лимит 2 msg/sec на диалог.
- `/factory retract` удаляет сообщение из канала по `mid`.
- Тест архитектуры банит aiogram/aiohttp-socks в зависимостях.

---

## ADR-002: Двухканальный LLM — OpenRouter первичный, Yandex GPT резервный

Статус: принято (план docs/plans/2026-10-14-dual-channel-llm-images.md).

### Контекст и причины

Единственный LLM-провайдер (Yandex AI Studio) — единая точка отказа: его авария
останавливает и фабрику, и LLM-рубрики автономного канала. Нужен резервный
канал, но без липкого состояния. Липкое переключение «связками» (общий флаг
активной пары LLM+изображения, кулдауны и пробы возврата) отклонено: домены
отказа независимы (лежащий OpenRouter не должен уводить текст на другую модель
при живом fal), уровни критичности разные (ошибка LLM = поста нет, ошибка
картинки = деградация до текста), а цена — состояние в БД и операционные
сюрпризы.

### Решения

1. **Приоритет каналов.** Первичный — OpenRouter (`https://openrouter.ai/api/v1`,
   модель `qwen/qwen3.8-flash`, ключ `OPENROUTER_API_KEY`); резервный —
   Yandex AI Studio (`https://ai.api.cloud.yandex.net/v1`,
   `gpt://<folder_id>/yandexgpt-5-lite`, существующий ключ `LLM_API_KEY`).
   Оба — OpenAI-совместимые endpoints: `LlmWriter`/`LlmCompletionClient`
   не меняют протокол, меняются только конфиг-дефолты. В `sources.yaml`
   секция `factory.llm` остаётся «первичным каналом», новая
   `factory.llm_fallback` — «резервным»; timeout/max_tokens/temperature
   общие с первичным каналом. Резервный канал существует тогда и только
   тогда, когда задан `LLM_API_KEY`.
2. **Per-request failover без состояния** (`writers/fallback.py`): каждый
   вызов начинает с первичного канала; `WriteResult` со `status="error"`
   (любой error_kind — infra/config/content) или `LlmCompletionError` любого
   вида переключает на резерв. Промпт один на оба канала — он
   провайдер-агностичный. При ошибке обоих каналов возвращается результат
   резерва с комбинированным текстом ошибки, но error_kind остаётся от
   резерва: битый ключ Yandex корректно даёт config → автопауза фабрики.
   В `post.llm_model` фиксируется реально отработавшая модель. Цена сбоя
   первичного — одна неудачная попытка с таймаутом на вызов (при частоте
   слотов незаметно).
3. **Thinking-модели.** `LlmConfig.disable_reasoning` (дефолт первичного —
   true): qwen иначе сжигает весь `max_tokens` на рассуждения и возвращает
   `content=null`; при флаге в payload добавляется
   `reasoning: {"enabled": false}`.
4. **Fail-fast обязательности.** При активном LLM-контуре (фабрика или
   LLM-рубрики) обязательны ОБА ключа — отсутствие любого (`OPENROUTER_API_KEY`
   или `LLM_API_KEY`) роняет запуск в `load_config`: тихий запуск с одним
   каналом ломает заявленную отказоустойчивость. Одиночный канал возможен
   только вне LLM-контуров.
5. **Readiness-проба на старте** (`writers/readiness.py`): оба подключённых
   канала получают минимальный completion-зонд (`max_tokens=1`, короткий
   промпт; стоимость — доли цента) до старта фоновых задач — в `main.py`
   (фабрика) и при сборке рантайма рубрик (`bot/rubrics_runtime.py`), где он
   — единственный сетевой вызов сборки (инвариант «construction performs no
   network calls» сознательно пересмотрен). Исходы: 200 → канал готов;
   config-вид (401/402/403/404 — ключ/баланс/модель) → ValueError, запуск
   падает (статическая ошибка деплоя, а не повод ждать аварии primary);
   infra-вид (сеть/5xx/таймаут) → WARNING, запуск продолжается — временная
   недоступность внешнего сервиса не должна блокировать старт бота.
   Изображения на старте не пробуются: генерация тарифицируется за запрос.
6. **Wiring обоих контуров.** Фабрика — `FallbackWriter` в `main.py`;
   рубрики — `FallbackCompletion` в `bot/rubrics_runtime.py::_build_completion`
   (одним местом покрывает generative/bank/digest-рубрики); общая
   `aiohttp.ClientSession`.
7. **Наблюдаемость.** Каждый вызов пишет durable-событие в `bot_stats`
   (`llm_primary_ok` / `llm_fallback_used` / `llm_fallback_error_<kind>`),
   ежедневная сводка админам дополняется строкой «LLM: primary N / fallback M»
   — тихий failover не скрывает деградацию первичного канала. Немедленный
   admin-алерт — при config-ошибке ЛЮБОГО канала, даже замаскированной
   успешным fallback; дедуп в памяти процесса, не чаще раза в сутки на канал
   (повтор после рестарта при всё ещё битой конфигурации допустим).
   Infra- и content-ошибки остаются в логах и сводке.

### Последствия

- `OPENROUTER_API_KEY` обязателен при активном LLM-контуре; `LLM_API_KEY`
  теперь обслуживает резервный канал (и будущий YandexART, ADR-003).
- `LLM_BASE_URL`/`LLM_MODEL` — переопределения **первичного** канала:
  указать там Yandex — молча сделать резервный канал первичным; резерв
  переопределяется `LLM_FALLBACK_BASE_URL`/`LLM_FALLBACK_MODEL`. При деплое
  старые env-перекрытия Yandex из `.env` VPS нужно убрать/перенести.
- Кулдаун первичного канала в памяти процесса — возможное будущее улучшение,
  в решение не входит.

---

## ADR-003: Изображения — fal.ai активный провайдер, YandexART отложен заглушкой

Статус: принято (план docs/plans/2026-10-14-dual-channel-llm-images.md;
решение оператора от 2026-10-17 — YandexART переведён в заглушку).

### Контекст и причины

Контур рубрик не генерирует изображения: банк открыток раздаёт готовые файлы,
а исследованный YandexART (docs/plans/2026-10-09-epoch0-research.md §2) не
подключён. Второй активный провайдер изображений на старте не нужен и удваивает
стоимость интеграции, поэтому решено: подключить fal.ai (порт `FalClient` из
visual-studio на aiohttp), а YandexART зарезервировать заглушкой — интерфейс,
конфиг-секция и события наблюдаемости резервируются уже сейчас, чтобы
полноценный клиент не потребовал переделок.

### Решения

1. **Протокол** (`images/base.py`): `ImageGenerator.generate(prompt) -> bytes`;
   `ImageGenerationError(kind)` с общей таксономией — config (401/402/403 —
   ключ/баланс), infra (сеть, 429, 5xx, таймаут, битый ответ). `MAX_IMAGE_BYTES`
   (10 МБ) на декодированные байты любого провайдера: скачивание потоковое,
   по превышению лимита — обрыв и infra-ошибка вместо исчерпания памяти.
2. **FalImageGenerator** (`images/fal.py`) — queue-API fal:
   `POST https://queue.fal.run/{model}` (`Authorization: Key <FAL_KEY>`) →
   поллинг `status_url` (интервал ~1.5 с, общий таймаут из конфига — по
   умолчанию 120 с) до COMPLETED → `GET response_url` → `images[0].url` →
   потоковое скачивание. Ретраи только транзиентных сбоев (сеть, 429, 5xx)
   с экспоненциальным backoff; модель по умолчанию — `fal-ai/flux/dev`.
3. **YandexArtStub** (`images/yandex_art.py`) — заглушка с тем же интерфейсом:
   `generate()` бросает config-ошибку «not implemented (stub)», в рантайм не
   подключается и ключа не требует. Докстринг фиксирует контракт будущего
   клиента (синхронный `POST {base}/images/generations`,
   `art://<folder_id>/aliceai-image-art-3.0`, ответ `data[0].b64_json`,
   folder_id из модели резервного LLM-канала, лимит промпта 500 символов),
   чтобы подключение не требовало переделок протокола и wiring.
4. **Деградация вместо падения.** Активный провайдер один — fal. Обёртка
   `FallbackImageGenerator` (`images/fallback.py`) уже принимает второй
   генератор, но сегодня fallback = None; при сбое fal любого вида пост
   деградирует до текстового — картинка не роняет слот: warning,
   `bot_stats`-событие `image_none` и строка в alert исхода слота;
   config-вид дополнительно зажигает немедленный admin-алерт (как в ADR-002).
   Требование «оба провайдера изображений» вернётся вместе с реальным
   клиентом YandexART — wiring при этом не меняется (передаётся второй
   генератор).
5. **Конфиг** (`rubrics.yaml` секция `image_generation`, валидатор
   `config/rubrics.py`): `{fal: {model}, yandex_art: {size}, timeout_seconds}`;
   ключ добавлен в whitelist топ-ключей валидатора (опечатка роняет запуск).
   Секция `yandex_art` парсится и валидируется, но провайдер не активируется.
   Fail-fast: включённая рубрика с `image: generate` без `FAL_KEY` роняет
   запуск (`RubricsConfigError`) — тихая тотальная деградация всех таких
   постов недопустима. Значение `image: generate` у рубрики — зарезервированное
   слово (генерация картинки), `image: <тип банка>` — открытка из банка как
   раньше, без `image` — пост без картинки; имена типов банка живут в БД,
   коллизий нет.
6. **Пайплайн и жизненный цикл файлов** (`rubrics/generative.py`, хелпер
   `GeneratedImages` — общий с bank-веткой `bank/pipeline.py`): промпт
   детерминированный, без LLM-раунда — глобальный `image_style` + первые
   ~200 символов текста поста, суммарно ≤ 500 символов; байты пишутся в
   `data/generated/<slot_run_id>.png`, исход несёт `image_ref =
   "generated:<файл>"` — файл персистентен, ретрай публикации не
   перегенерирует картинку. Publisher (`publishers/slot.py`) читает
   `generated:`-ссылки из каталога с path-traversal-защитой и удаляет файл
   после подтверждённой публикации; страховочный TTL-prune (7 дней) —
   в ежедневном обслуживании рядом с бэкапом БД (`bot/db_backup.py`).

### Последствия

- `FAL_KEY` обязателен только при рубриках с `image: generate`; изображения
  на старте не пробуются (тарификация за запрос) — config-сбой канала виден
  по немедленному алерту первой реальной генерации.
- Ежедневная сводка показывает «картинки: fal A / без картинки C»; событие
  `image_art_used` и fal/art-разбивка вернутся вместе с клиентом YandexART.
- Factory-контур остаётся текстовым: `Post` без поля картинки,
  `MaxPostPublisher` без вложений — вне рамок решения.

---

## Цели

- Собрать MVP, который можно запустить в Docker и оставить работать без ручного присмотра.
- Использовать RSS как основной и единственный источник на первом этапе.
- Хранить состояние дедупликации в SQLite, чтобы не публиковать одну новость дважды.
- Дать минимальное управление через команды бота в MAX.
- Заложить простой контракт `NewsItem` и `Publisher`, чтобы позже заменить или дополнить MAX-публикацию интеграцией с фабрикой контента.

## Не цели на MVP

- HTML-парсинг сайтов.
- Извлечение `og:image`, сложные превью и обогащение карточек.
- Webhook-приём обновлений MAX (см. ADR-001: long polling достаточен).
- PostgreSQL, очереди, Celery, Kafka, web admin panel.
- Микросервисная архитектура.
- Сложная система ролей.

Эти вещи можно добавить позже, когда RSS-only бот стабильно работает.

---

## Путь развития

### 1. MVP

Минимальный бот:

- читает RSS-источники из `config/sources.yaml`;
- нормализует записи в `NewsItem`;
- фильтрует новости по ключевым словам;
- проверяет дедупликацию в SQLite;
- публикует новые новости в канал MAX;
- принимает команды по long polling;
- запускается через Docker Compose;
- пишет логи в stdout и события в SQLite.

### 2. Надежность

После работающего MVP добавляются production-мелочи без усложнения архитектуры:

- retries/backoff для сетевых запросов;
- `asyncio.Lock` на цикл публикации, чтобы scheduler и ручная команда не запускали сбор одновременно;
- admin whitelist через `ADMIN_IDS`;
- healthcheck для Docker;
- ротация или аккуратный вывод логов;
- базовые тесты для конфигурации, фильтрации, дедупликации и форматирования.

### 3. Управление

Команды бота:

| Команда | Назначение |
|---|---|
| `/status` | краткое состояние бота: uptime, последний запуск, ошибки |
| `/sources` | список RSS-источников и их статус |
| `/stats` | статистика публикаций за последние дни |
| `/news` | ручной запуск сбора и публикации, только для админа |
| `/chatid` | chat_id текущего чата/канала (для `MAX_CHANNEL_ID`) и user_id отправителя (для `ADMIN_IDS`) |

Изменение источников через команды можно добавить позже. Для начала источники правятся в `sources.yaml`, так проще и безопаснее.

### 4. Контент-фабрика (реализовано; иначе, чем в эскизе)

Изначально здесь был эскиз: `ContentFactoryPublisher` передаёт `NewsItem` во внутренний
API, webhook или очередь внешней фабрики, ядро сбора новостей не меняется.
Реализовано иначе — прямым параллельным циклом `ContentFactory` в том же процессе:

- `MaxPublisher` продолжает отправлять новости в канал (дайджест-цикл);
- фабрика — второй цикл «поиск → LLM-написание поста → автопубликация» со своим
  интервалом, своей дедупликацией и своими лимитами, публикует в тот же канал;
- переиспользуются общие строительные блоки: `Fetcher` (RSS-сбор), `NewsItem`
  и одна `aiohttp.ClientSession` на процесс.

Почему не ветка `Publisher`: фабрика — не «другой транспорт для новости», а другой
продукт (свой отбор, машина состояний posts, LLM-писатель с валидацией и лимитами);
её требования не влезают в `Publisher.publish(items)` без раздувания контракта
дайджеста. Подробности — в разделе «Контент-фабрика».

### 5. HTML и preview

HTML-парсинг и preview-обогащение добавляются только после стабильного RSS-пайплайна:

- `HtmlFetcher` для сайтов без RSS;
- извлечение `og:title`, `og:description`, `og:image`;
- дополнительные лимиты и таймауты, чтобы preview не тормозил публикацию.

### 6. Webhook

Переход с long polling на webhook MAX — после стабилизации MVP: нужен публичный
https-домен с TLS от доверенного CA и ACK обновлений в течение 30 секунд
(см. ADR-001). До этого момента long polling не требует внешнего ingress.

---

## Архитектура MVP

```
RSS-источники
      |
      v
RssFetcher
      |
      v
NewsItem[]
      |
      v
KeywordFilter
      |
      v
Deduplicator / SQLite
      |
      v
PostFormatter
      |
      v
Publisher
      |
      +--> MaxPublisher  ->  MaxClient (max_api)  ->  MAX Bot API
```

Ключевая идея: `Publisher` является границей между ядром бота и внешней системой публикации.
`max_api.MaxClient` — тонкий REST-клиент (aiohttp), единственное место, знающее
специфику MAX Bot API (заголовок Authorization без Bearer, REST-ресурсы, `mid`).

Это поток дайджест-цикла. Контент-фабрика — второй, параллельный цикл (см. «Контент-фабрика»).

---

## Структура проекта

```
репозиторий/
├── pyproject.toml            # зависимости (в корне, uv sync из корня)
├── uv.lock
├── infra/                    # compose-файлы продакшена (VPS)
├── .github/workflows/        # CI/CD
└── max_news_bot/
    ├── max_api/
    │   ├── client.py         # тонкий aiohttp-клиент MAX Bot API
    │   ├── errors.py         # MaxApiError, MaxRateLimitError, классификация
    │   └── __init__.py
    │
    ├── bot/
    │   ├── polling.py        # long polling GET /updates, диспетчер обновлений
    │   ├── handlers.py       # команды бота (дайджест + фабрика)
    │   └── scheduler.py      # два шедулер-цикла: дайджест и фабрика
    │
    ├── config/
    │   ├── loader.py         # YAML + .env (settings, filters, factory)
    │   └── sources.yaml      # RSS-источники и настройки
    │
    ├── core/
    │   ├── models.py         # NewsItem, Post, PublishedResult
    │   ├── pipeline.py       # дайджест: fetch -> filter -> dedupe -> publish
    │   ├── factory.py        # цикл контент-фабрики (без max_api)
    │   └── __init__.py
    │
    ├── parsers/
    │   ├── base.py           # интерфейс fetcher
    │   ├── rss.py            # RSS/Atom fetcher (+ summary, общая сессия)
    │   ├── habr.py           # источник Habr
    │   ├── vc_ru.py          # источник vc.ru
    │   ├── composite.py      # составной fetcher по списку источников
    │   └── __init__.py
    │
    ├── publishers/
    │   ├── base.py           # интерфейс Publisher (дайджест)
    │   ├── max.py            # публикация в канал MAX (дайджест)
    │   ├── post.py           # PostPublisherProtocol + PostPublisher (фабрика)
    │   ├── alerter.py        # Alerter + MaxAlerter (алерты админам)
    │   └── __init__.py
    │
    ├── writers/
    │   ├── base.py           # Writer-протокол + WriteResult
    │   ├── llm.py            # LlmWriter (OpenAI-совместимый API: OpenRouter / Yandex)
    │   ├── validator.py      # валидация вывода LLM (лимит 4000)
    │   └── __init__.py
    │
    ├── utils/
    │   ├── db.py             # SQLite, миграции user_version, дедуп, статистика
    │   ├── text.py           # canonical_url, title_tokens, jaccard
    │   ├── sanitize.py       # санитайзер HTML-разметки сообщения
    │   ├── filters.py        # keyword-фильтрация
    │   ├── formatter.py      # текст поста (лимит 4000)
    │   └── logging.py        # настройка логирования
    │
    ├── scripts/
    │   └── healthcheck.py    # Docker healthcheck (дайджест + фабрика; DATABASE_PATH из env)
    │
    ├── data/
    │   └── max_news_bot.db   # Docker volume
    │
    ├── tests/                # pytest
    │
    ├── main.py               # python -m max_news_bot.main
    ├── Dockerfile            # build context — корень репозитория
    ├── docker-compose.yml    # дев-запуск
    └── .env.example
```

Структура чуть шире минимальной, но каждый слой имеет понятную причину. Главное — не добавлять абстракции глубже этих границ, пока они реально не понадобятся.

---

## Модель данных

Единая модель новости нужна для того, чтобы парсеры, фильтры, дедупликация, MAX-публикация и фабрика говорили на одном языке.

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    category: str | None = None
    summary: str | None = None
```

`summary` — очищенный от HTML текст RSS-описания (обрезка ~1000 символов): фабрика
отдаёт его LLM как исходный материал.

Для фабрики добавлена модель `Post` — состояние поста в таблице `posts`
(см. «Машина состояний posts» в разделе «Контент-фабрика»).

Для MVP достаточно `dataclass`. `pydantic` можно добавить позже, если появится внешний API или строгая валидация payload для фабрики.

---

## Publisher

Публикация должна быть отдельным интерфейсом, а не частью парсера или scheduler.

```python
from typing import Protocol


class Publisher(Protocol):
    async def publish(self, items: list[NewsItem]) -> PublishedResult:
        ...
```

На первом этапе реализуется только `MaxPublisher` (через `max_api.MaxClient`).
Эскизная идея `ContentFactoryPublisher` не понадобилась: фабрика контента реализована
параллельным циклом (см. «Контент-фабрика»), а `Publisher` остаётся контрактом дайджест-пайплайна.

Это не усложняет MVP, но защищает проект от переписывания, когда бот станет частью фабрики контента.

---

## Поток одного цикла дайджеста

```
scheduler.py или /news
  |
  v
Pipeline.run()
  |
  +--> загрузить enabled RSS-источники
  +--> RssFetcher.fetch(source)
  +--> привести записи к NewsItem
  +--> KeywordFilter.passes(item)
  +--> db.is_sent(item.url)
  +--> Publisher.publish(items)
  +--> db.mark_sent(item.url)
  +--> db.log_event(...)
```

Важное правило: помечать новость как отправленную только после успешной публикации. Иначе можно потерять новость при ошибке MAX API.

Для защиты от двойного запуска используется один lock:

```python
publish_lock = asyncio.Lock()

async with publish_lock:
    await pipeline.run()
```

У фабрики — свой отдельный `asyncio.Lock` с тем же назначением: циклы не блокируют
друг друга. Общие при этом ресурсы (MAX-лимиты, одна SQLite) — см. «Раздельные
локи, общие ресурсы» в разделе «Контент-фабрика».

---

## Контент-фабрика

Фабрика — второй независимый цикл бота: «поиск информации → написание поста (LLM) →
автопубликация в тот же MAX-канал». Дайджест-пайплайн при этом не меняется:
у фабрики свой интервал, свой `asyncio.Lock`, свой отбор (ключевые слова и возраст
новостей), своя дедупликация и свои лимиты. Оба цикла живут в одном процессе,
публикуют в один канал одним ботом и пишут в одну SQLite-базу.

Автопубликация включается только после раскатки через dry-run с числовыми
критериями выхода (сценарий — в README).

### Стадии цикла

```
factory_scheduler_loop (свой интервал, свой asyncio.Lock)
  |
  v
проверка паузы и тихих часов --- вне окна / пауза ---> heartbeat-событие idle
  |                                                     (factory_run, JSON)
  v
recovery: draft старше stale_draft_minutes публикуется повторно
  |        (текст уже в БД, LLM не вызывается; входит в оба лимита)
  v
RSS-источники (те же, что у дайджеста) -> RssFetcher (общая ClientSession)
  |
  v
фильтр ключей фабрики (factory.include/exclude_keywords)
  |
  v
age-фильтр: max_news_age_hours по published_at (новость без даты — мимо отбора)
  |
  v
канонизация URL: срезать utm_*/fbclid и фрагмент, убрать trailing slash,
  |             хост в нижнем регистре
  v
дедупликация:
  +-- posts: статус draft | published | previewed | skipped,
  |           либо failed с attempts >= max_attempts
  +-- Jaccard заголовков >= ~0.7: посты за 7 дней + внутри пачки цикла
  |
  v
лимиты: posts_per_cycle и max_posts_per_day (по last_attempt_at)
  |
  v
LlmWriter: новость в разделителях <article>...</article>,
  |         анти-инъекция, запрет выводить ссылки, право вернуть SKIP
  v
валидатор: без URL; видимая длина ~200..max_post_chars (жёсткий предел 4000 —
  |         лимит MAX); без отказ-фраз ("As an AI...")
  v
санитайзер: whitelist <b> <i> <a href>, остальное через html.escape;
  |          href только http/https; несбалансированные теги -> fallback
  v
PostPublisher: HTTP 429 MAX (спать и повторить, до 2–3 раз),
  |            пауза ~2-3 с между постами (лимит 2 msg/sec на диалог);
  |            ссылку на источник добавляет код;
  |            dry-run -> админам, mid не сохраняется
  v
mark_post_published(mid) / mark_post_failed(error) / skipped (терминальный)
```

### Границы архитектуры

`core/factory.py` не импортирует `max_api` — как и `core/pipeline.py` (проверяется
тестом архитектуры). Вся MAX-специфика спрятана за протоколами:

```python
# writers/base.py
@dataclass(frozen=True)
class WriteResult:
    status: str              # "written" | "skipped" | "error"
    post: Post | None
    error_kind: str | None   # "content" | "infra" | "config"
    error: str | None        # обрезан ~300, без заголовков и ключей
    tokens: int = 0
    latency_ms: int = 0


class Writer(Protocol):
    async def write_post(self, item: NewsItem) -> WriteResult: ...


# publishers/post.py
class PostPublisherProtocol(Protocol):
    async def publish_post(self, post: Post, *, to_admins: bool = False) -> PostPublishResult: ...


# publishers/alerter.py
class Alerter(Protocol):
    async def send_alert(self, text: str) -> None: ...
    async def send_recovery(self, text: str) -> None: ...
```

- `WriteResult` явно различает успех, SKIP (материал нерелевантен или скуден) и
  ошибку с её классом, и несёт usage (токены, латентность) для счётчиков.
- `LlmWriter` ходит в OpenAI-совместимый API любого провайдера (`base_url` +
  `model`) на уже имеющемся aiohttp — новых pip-зависимостей нет; дефолт —
  OpenRouter (`qwen/qwen3.8-flash`), Yandex AI Studio — резервный канал (ADR-002).
- Один общий `aiohttp.ClientSession` на процесс передаётся в `RssFetcher`,
  `LlmWriter` и `MaxClient` и закрывается при shutdown.

### Машина состояний posts

Таблица `posts` (миграция v2): `news_url` UNIQUE (канонический URL), `status`,
`text` (nullable), `attempts`, `error`, `prompt_version`, `llm_model`, `tokens`,
идентификатор отправленного сообщения (в MAX — `mid`), `rejected_text`,
`created_at`, `published_at`, `last_attempt_at`.

| Статус | Что значит | Переходы |
|---|---|---|
| `draft` | текст сгенерирован, отправка ещё не подтверждена | → `published` / `previewed` / `failed` |
| `published` | отправлен в канал, сохранён `mid` | терминальный |
| `previewed` | dry-run: отправлен админам, `mid` NULL | терминальный; для healthcheck/алертов — успех |
| `failed` | зафиксирована ошибка обработки | рерайт в следующих циклах, пока `attempts < max_attempts` |
| `skipped` | LLM вернул маркер SKIP (материал нерелевантен/скуден) | терминальный, не ретраится |

Правила:

- Строка posts создаётся при любой **обработанной** ошибке (LLM, валидация,
  отправка) — чтобы вёлся счётчик attempts; колонка `text` nullable, у строк
  ошибок текста может не быть. При `CancelledError` строка не создаётся: отмена
  таска — не попытка, «зависший» draft ловит recovery.
- `attempts` инкрементируют только контентные ошибки; после `max_attempts` (3)
  пост остаётся `failed` и в отбор больше не попадает.
- `last_attempt_at` обновляется при каждой попытке, включая UPSERT-ретраи: по
  нему считается `max_posts_per_day` (`count_attempts_today`), потому что ретраи
  тоже расходуют LLM-вызовы.
- `rejected_text` — отклонённый валидатором вывод (обрезка ~2000): без него
  критерий раскатки «0 ложных срабатываний валидатора» непроверяем — не видно,
  что именно отклонено.
- `prompt_version` = sha1(prompt)[:8] считается автоматически — ручное
  обновление версии промпта забудется.

### Три класса ошибок

| Класс | Примеры | attempts | Реакция |
|---|---|---|---|
| Контентные | провал валидатора, форматирование | +1, до `max_attempts` = 3 | рерайт в следующем цикле |
| Инфраструктурные | таймаут, сеть, HTTP 429/5xx, сбои сети MAX | не растут | circuit breaker: после 2–3 подряд цикл прерывается (экономия токенов), новости ретраятся в следующем цикле без штрафа |
| Конфиг/доступ | HTTP 401/402/403/404 — протух ключ, кончился баланс Yandex, неверное имя модели | не растут | немедленная авто-пауза (флаг `factory_autopause` в `sources_state`) + немедленный алерт админу; снимается `/factory resume` |

Конфиг/доступ выделен в отдельный класс, потому что ретраи здесь бессмысленны:
без этого за три цикла сбоя провайдера все новости окна ушли бы навсегда в
`failed`. Тексты ошибок в posts и событиях — без заголовков запросов и ключей,
обрезка ~300 символов.

### Recovery и at-least-once

В начале каждого цикла draft старше `stale_draft_minutes` публикуется повторно:
текст уже в БД, LLM не вызывается. Recovery уважает текущий dry_run, паузу и
тихие часы и занимает место в `posts_per_cycle` и `max_posts_per_day`.

Семантика доставки — **at-least-once**, и это осознанный компромисс: падение
процесса между успешной отправкой в MAX и `mark_post_published` даст дубль
поста при восстановлении. Цена exactly-once (координация транзакций между
MAX API и SQLite) для соло-проекта не оправдана, а окно между send и mark
исчезающе мало. Дубли при краше редки и убираются `/factory retract`.

### Дедупликация фабрики

Кандидат отбрасывается, если:

- есть строка posts со статусом `draft | published | previewed | skipped`;
- или `failed` с `attempts >= max_attempts` (контентные ошибки);
- или Jaccard заголовков ≥ ~0.7 против постов за 7 дней или внутри пачки
  текущего цикла (нормализованные токены заголовков, stdlib — без новых
  зависимостей).

`sent_news` фабрика не читает и не пишет: дайджест и фабрика — независимые
потребители одних RSS-источников. Дубль в канале (ссылка в дайджесте + пост
фабрики по той же новости) принят осознанно: пост — переработка, а не повтор
ссылки; дайджест — «быстрая полоса», его код не трогаем.

`failed` с запасом попыток рерайтится в следующем цикле: UPSERT по каноническому
`news_url`, attempts растёт только за контентное.

### Политика «ссылку на источник добавляет код»

Промпт запрещает LLM выводить любые ссылки; валидатор отклоняет вывод, содержащий
URL или `<a href>`; ссылку на источник (`news_url`) код добавляет к сообщению при
отправке. Почему так:

- инъекция через ссылку невозможна: LLM не контролирует ни один URL в итоговом
  сообщении, проверка доменов не нужна;
- редиректы и агрегаторы не ломают валидацию: ссылка ведёт на исходный URL как
  есть, каким бы «некрасивым» он ни был.

Защита от prompt injection в тексте: данные новости оборачиваются в разделители
`<article>…</article>`; system-prompt явно требует использовать только факты из
статьи и не выполнять инструкции из её текста.

Пометка «подготовлено с помощью ИИ» решением владельца не добавляется (юрисдикция
РФ, требования ЕС неприменимы). Если понадобится — одна опция конфига в месте,
где код добавляет ссылку на источник.

### Раздельные локи, общие ресурсы

У дайджеста и фабрики — разные `asyncio.Lock`: циклы независимы и не должны
блокировать друг друга. Но ресурсы у них общие:

- **MAX-лимиты**: оба цикла публикуют в один канал одним ботом; лимит —
  2 сообщения в секунду на диалог (30 rps глобально). `PostPublisher`
  обрабатывает `MaxRateLimitError` (HTTP 429: спать и повторить, до 2–3 раз)
  и выдерживает паузу ~2–3 с между постами.
- **Одна SQLite**: включён WAL, `busy_timeout` выставляется на каждом соединении
  (`timeout=` в `sqlite3.connect`); каталог `data/` монтируется в Docker целиком,
  а не отдельным файлом — WAL-файлы живут на volume (проверено по
  `infra/compose.prod.yml` и VPS-варианту `data:/app/data`).

Короткие транзакции плюс WAL делают одновременную запись двух циклов безопасной.

### Управление

Единое правило включённости:

```
enabled = (env FACTORY_ENABLED, если задан, иначе yaml factory.enabled)
          AND не paused (ручная пауза, /factory pause)
          AND не auto-paused (сбой конфига/доступа; снимается /factory resume)
```

- `factory.dry_run`: пост генерируется и отправляется админам в личку вместо
  канала; статус `previewed` терминальный — после выключения dry-run пост
  автоматически в канал не уйдёт. Для healthcheck и алертов `previewed`
  считается успехом. Важно: админ должен был один раз написать боту `/start`
  (см. ADR-001), иначе личное сообщение не доставляется.
- `/factory pause | resume`: флаг в БД (`sources_state`, name='factory') —
  остановка цикла без редеплоя; `resume` снимает и ручную, и авто-паузу.
- `/factory run` — ручной запуск цикла; `/factory status` — состояние и причина
  остановки (paused / autopause с кодом ошибки).
- `/factory retract <id>` — удаляет сообщение из канала по `mid`
  (через MAX API), только админ. Страховочный механизм вместо постоянной
  модерации.
- `/posts` — последние посты со статусами, попытками и обрезанными ошибками.
- Все команды фабрики админские (`ADMIN_IDS`).
- `enabled=true` без `OPENROUTER_API_KEY` / `LLM_API_KEY` — ошибка старта
  (ValueError): при активном LLM-контуре обязательны оба ключа (ADR-002);
  секреты живут только в `.env`.

### Тихие часы и heartbeat

`factory.active_hours` («08:00–23:00») в таймзоне из `factory.timezone` (zoneinfo,
stdlib). Вне окна и на паузе цикл пишет heartbeat-событие `factory_run` с JSON
`{"state": "idle", "reason": "quiet_hours" | "paused"}` — healthcheck не краснеет
ночью и на паузе.

### Наблюдаемость и healthcheck

- `factory_run` (рабочие циклы) пишет в `details` JSON-счётчики стадий
  `collected/filtered/deduped/written/skipped/published|previewed/failed`,
  латентность LLM и токены. В отличие от текстовых деталей дайджеста, JSON
  машиночитаем: по нему работают healthcheck, счётчик неудачных циклов и дедуп
  алертов.
- «Неудачный цикл» = `written == 0 and failed > 0`. Цикл, где всё получило SKIP, —
  не авария. После `alert_after_failed_cycles` (3) подряд — **одно** сообщение
  админам на инцидент (факт отправки фиксируется событием `factory_alert`, без
  спама каждый цикл) и одно сообщение при восстановлении.
- Конфиг/доступ — немедленный алерт и авто-пауза, вне счётчика N циклов.
- Healthcheck фабрики: «нет публикаций (или previewed в dry-run) дольше X часов
  при наличии кандидатов» → unhealthy; heartbeat — признак живого цикла. Правило
  «нет публикаций» проверяется **только внутри активного окна**, а X вычисляется
  как длина окна + один интервал цикла (не задаётся вручную) — иначе ночное окно
  (9 ч в примере) покраснело бы к 07:00 при последнем посте в 22:30, хотя
  heartbeat подтверждает живой цикл.
- Healthcheck не ходит в MAX API: скрипт `scripts/healthcheck.py` читает только
  SQLite (`DATABASE_PATH` из окружения) — нет зависимости от мессенджера.

### Глобальный обработчик ошибок бота

Хендлеры команд не оборачиваются в try/except по одному: цикл long polling
(`bot/polling.py`) оборачивает диспетчеризацию каждого обновления в единый
try/except:

- лог `unhandled_error` с полным traceback — единственное место, где traceback
  попадает в лог;
- событие `error` в `bot_stats` (детали обрезаны до 300 символов — секреты
  не утекают), отображается в `/status`;
- короткий ответ пользователю («Internal error occurred…»).

Сам обработчик не поднимает исключений: запись в БД и ответ пользователю
обёрнуты в try/except (если БД или MAX API недоступны — только лог).
Scheduler-циклы (`bot/scheduler.py`) обрабатывают свои ошибки самостоятельно
и через обработчик polling-цикла не проходят.

### Миграции

Миграции — `PRAGMA user_version`, список шагов в одном месте (`utils/db.py`):

- v1 — существующие таблицы (`sent_news`, `bot_stats`, `sources_state`)
  идемпотентно: прод-БД со старой версией `user_version = 0` корректно
  поднимается до актуальной;
- v2 — таблица `posts` с полным набором колонок (см. «Машина состояний posts»);
- v3 — `posts.message_id` INTEGER → TEXT (mid — строка `mid.<id>`): таблица
  пересобирается через `posts_new`, шаг начинается с `DROP TABLE IF EXISTS
  posts_new` (реентерабелен после сбоя процесса посреди миграции) и сохраняет
  счётчик AUTOINCREMENT;
- перед применением миграций к существующей БД — копия файла БД после
  чекпоинта WAL (откат = вернуть копию).

WAL включается на инициализации, `busy_timeout` — на каждом соединении.

---

## Конфигурация

`sources.yaml` хранит только несекретные настройки:

```yaml
settings:
  post_interval_hours: 6
  max_news_per_source: 5
  request_timeout: 30

filters:
  include_keywords: []
  exclude_keywords: []

sources:
  - name: "Python Insider"
    type: "rss"
    url: "https://pythoninsider.blogspot.com/feeds/posts/default"
    enabled: true
    category: "python"

factory:
  enabled: false
  dry_run: true
  interval_hours: 4
  active_hours: "08:00-23:00"
  timezone: "Europe/Moscow"
  llm:
    # первичный LLM-канал (ADR-002)
    base_url: "https://openrouter.ai/api/v1"
    model: "qwen/qwen3.8-flash"
  llm_fallback:
    # резервный LLM-канал; timeout/max_tokens/temperature общие с primary
    base_url: "https://ai.api.cloud.yandex.net/v1"
    model: "gpt://<folder_id>/yandexgpt-5-lite"
```

`.env` хранит секреты и runtime-переопределения (полный список с комментариями —
`max_news_bot/.env.example`):

```env
MAX_BOT_TOKEN=
MAX_CHANNEL_ID=
ADMIN_IDS=
LOG_LEVEL=INFO
POST_INTERVAL_HOURS=6
REQUEST_TIMEOUT=30
DATABASE_PATH=
OPENROUTER_API_KEY=
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL=
LLM_FALLBACK_BASE_URL=
LLM_FALLBACK_MODEL=
FAL_KEY=
FACTORY_ENABLED=
FACTORY_DRY_RUN=
FACTORY_INTERVAL_HOURS=
FACTORY_ACTIVE_HOURS=
```

`LLM_BASE_URL`/`LLM_MODEL` переопределяют первичный канал (OpenRouter),
`LLM_FALLBACK_BASE_URL`/`LLM_FALLBACK_MODEL` — резервный (Yandex).

Переменные окружения имеют приоритет над YAML, чтобы настройки можно было менять в Docker без пересборки образа. Прокси-переменных нет: обе платформы (MAX, Yandex) доступны из РФ напрямую (ADR-001).

---

## SQLite

SQLite достаточно для небольшого production-бота. База лежит в `data/max_news_bot.db`, папка `data/` монтируется как Docker volume.

```sql
CREATE TABLE sent_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE bot_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sources_state (
    name TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Отдельная миграционная система не нужна: создание и изменения схемы живут в `utils/db.py` в одном месте, версия схемы — `PRAGMA user_version` (v1 — таблицы дайджеста идемпотентно, v2 — таблица `posts` фабрики, v3 — `posts.message_id` в TEXT под строковые mid, с копией БД перед применением; см. «Миграции» в разделе «Контент-фабрика»). Флаги управления фабрикой (`factory`, `factory_autopause`) переиспользуют `sources_state`.

---

## Логирование

Нужно два уровня наблюдаемости:

- обычные application logs в stdout для Docker;
- бизнес-события в `bot_stats`: запуск, успешная публикация, ошибка источника, ошибка MAX API.

Минимальный формат логов:

```text
2026-05-25 12:00:00 INFO max_news_bot.pipeline collected=12 published=5 skipped=7
2026-05-25 12:00:02 ERROR max_news_bot.parsers.rss source=Example error="timeout"
```

На старте достаточно `logging.basicConfig`. Ротацию файлов можно добавить позже, если бот запускается не только в Docker.

---

## Docker

Минимальный Docker setup:

- `python:3.12-slim`;
- непривилегированный пользователь (uid 10001);
- `restart: unless-stopped`;
- `env_file: .env`;
- volume `./data:/app/data`;
- healthcheck `python /app/max_news_bot/scripts/healthcheck.py` (только SQLite, без сети).

Dockerfile лежит в `max_news_bot/`, но build context — **корень репозитория**:
`docker build -f max_news_bot/Dockerfile .` — pyproject.toml и uv.lock копируются
из корня до кода, чтобы слой зависимостей (`uv sync --frozen --no-dev`) кэшировался.

```yaml
services:
  maxnewsbot:
    build:
      context: .
      dockerfile: max_news_bot/Dockerfile
    restart: unless-stopped
    env_file: max_news_bot/.env
    environment:
      DATABASE_PATH: /app/data/max_news_bot.db
    volumes:
      - max_news_bot/data:/app/data
```

Продакшен-варианты: `infra/compose.prod.yml` (локальный запуск с теми же именами,
что на VPS) и `infra/compose.prod.vps.yml` (шаблон с подстановками `__BOT_IMAGE__` /
`__APP_DIR__`, который рендерит CI через sed; проект `-p maxnewsbot`, сеть
`maxnewsbot-net`, каталог на VPS — `/opt/maxnewsbot`).

---

## Тесты

Минимальный набор тестов:

- `test_loader.py` — YAML читается, env-переменные переопределяют настройки;
- `test_filters.py` — include/exclude ключевые слова работают ожидаемо;
- `test_pipeline.py` — новая новость публикуется, дубликат пропускается;
- `test_formatter.py` — сообщение не превышает лимит MAX (4000);
- `test_db.py`, `test_text.py` — миграции, UPSERT/`last_attempt_at`, флаги, канонизация URL, Jaccard;
- `test_factory.py` — recovery, attempts только за контентное, circuit breaker, автопауза на 401/404, лимиты, дедуп, тихие часы;
- `test_llm_writer.py` — `LlmWriter` против локального aiohttp-тест-сервера (`/chat/completions`: 200/401/404/429/500, таймаут, мусорный JSON);
- `test_validator.py`, `test_sanitize.py` — границы длин, отказ-фразы, «злые» кейсы санитайзера;
- `test_architecture.py` — инварианты: core не импортирует `max_api`, в зависимостях нет aiogram/aiohttp-socks;
- `test_healthcheck.py` — правила healthcheck на временной SQLite-базе.

Интеграционные тесты с реальным MAX API на MVP не нужны. MAX лучше мокать через
`Publisher`/`MaxClient`.

Сетевой тест RSS и тест с реальным LLM помечены `@pytest.mark.network` и `@pytest.mark.llm` и исключены из дефолтного прогона (`addopts = -m "not network and not llm"`); `uv run pytest -m llm` — ручная регрессия промпта с реальным ключом.

---

## Зависимости MVP

```text
aiohttp
feedparser
pyyaml
python-dotenv
pytest
pytest-asyncio
```

Сознательно нет: `aiogram` (заменён тонким клиентом `max_api` + long polling),
`aiohttp-socks` и прокси-переменные (платформы доступны из РФ напрямую), SDK MAX
(`max-botapi-python`, `aiomax`) — см. ADR-001.

Зависимости задаются в `pyproject.toml` (корень репозитория), локально — `uv sync`.

HTML-зависимости добавляются позже:

```text
beautifulsoup4
lxml
```

---

## Итоговое решение

Проект развивается от маленького production-бота, а не от большой платформы.

В MVP реализуются только RSS, дедупликация, MAX-публикация (собственный тонкий
клиент + long polling), Docker, логи, lock и базовые команды. Для будущей фабрики
контента заранее вводятся только две легкие границы: `NewsItem` и `Publisher`.

Контент-фабрика добавлена вторым параллельным циклом, не сломав этих границ:
`Publisher` остался контрактом дайджеста, а фабрика получила свои протоколы
(`Writer`, `PostPublisherProtocol`, `Alerter`) — см. «Контент-фабрика».

Порт на MAX + Yandex AI Studio (ADR-001) прошёл по этим же границам: ядро не
изменилось, платформенная специфика сосредоточена в `max_api` и `publishers`,
LLM-провайдер переключился переменными окружения. Такой подход сохраняет проект
простым сейчас и не мешает встроить его в более крупную систему позже.
