# Architecture — Content Fabric MAX News Bot

Небольшой production-бот мессенджера MAX: ведёт автономный канал рубрик для аудитории
старшего возраста СПб и Ленобласти. Проект — порт Telegram-бота content_fabric на российский
стек доставки: мессенджер MAX (platform-api2.max.ru) и OpenAI-совместимые LLM-провайдеры —
OpenRouter первичным каналом, Yandex AI Studio резервным (ADR-002).

Единственный publishing-контур — слотовый планировщик рубрик (`bot/slot_scheduler.py`,
конфиг `config/rubrics.yaml`); legacy-циклы (линк-дайджест и контент-фабрика IT-новостей)
удалены (ADR-004). Главный принцип порта сохранён: платформенная специфика MAX сосредоточена
в `max_api` и `publishers`, всё остальное платформенно-независимо.

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

## ADR-004: Единственный контур — рубрики; фабрика и дайджест удалены

### Контекст и причины

Исторически в процессе жили два взаимоисключающих контура: legacy (линк-дайджест
`Pipeline` + контент-фабрика `ContentFactory`, IT-источники Habr/vc.ru) и контур
рубрик, переключаемые `RUBRICS_ENABLED`/`FACTORY_ENABLED` с fail-fast на конфликт.
Канал окончательно определился как канал одной тематики (контент для читателей
старшего возраста), IT-направление из RSS больше не нужно. Дублирование механики
(два планировщика, `FallbackWriter`/`FallbackCompletion`, два издателя, LLM-конфиг
в секции `factory` файла `sources.yaml`, который читал и контур рубрик) стало
чистой сложностью.

### Решения

1. `SlotScheduler` — единственный publishing-контур: `main.py` всегда стартует
   `slot_scheduler_loop` + `daily_summary_loop` + `daily_backup_loop` + polling.
2. Переключатели `RUBRICS_ENABLED`/`FACTORY_ENABLED` и валидация конфликта удалены;
   `rubrics.yaml` обязателен и должен содержать хотя бы одну включённую рубрику.
3. LLM-каналы переехали в `rubrics.yaml` (`llm:`/`llm_fallback:`) — один контур,
   один конфиг-файл; `sources.yaml` удалён целиком. LLM-стек один:
   `LlmCompletionClient` + `FallbackCompletion` (транспортные хелперы и
   `SKIP_MARKER` переехали в `writers/completion.py` из удалённого `writers/llm.py`).
4. Удалены: `core/factory.py`, `core/pipeline.py`, `bot/scheduler.py`,
   `publishers/post.py`, `publishers/max.py`, `publishers/base.py`, `writers/llm.py`,
   `writers/base.py` (`Writer`/`WriteResult`), `FallbackWriter`, `utils/filters.py`,
   HTML-парсеры `parsers/habr.py`/`parsers/vc_ru.py` (RSS-only, см. решение MVP),
   модель `Post` и методы БД по `posts`/`sent_news`.
5. Команды: остались `/start /chatid /status /stats /factory
   run|pause|resume|status|retract slot`; удалены `/sources`, `/news`, `/posts` и
   `/factory retract <post_id>`; `/stats` читает публикации из `slot_runs`.
6. `scripts/healthcheck.py` проверяет только слоты (heartbeat `slot_heartbeat`,
   «тихий день», вечерняя сводка); digest/factory-правила и их env-гейты удалены —
   иначе после деплоя контейнер краснел бы по старым `run`-событиям.
7. Таблицы `posts`, `sent_news`, `sources_state` в рабочей БД не мигрируются и не
   удаляются: код их больше не пишет, схема создаётся как раньше, чтобы прод-БД
   поднималась без пересборки.

### Последствия

- Механика надёжности фабрики не потерялась — её закрывает слотовая машина
  состояний (recovery `generated`-строк без повторного LLM, пауза рубрики после
  N неудач, глобальная пауза MAX API, circuit-брейкеры источников).
- История ADR-001–003 сохраняет силу, кроме упоминаний wiring'а фабрики
  (`FallbackWriter` в `main.py`), которое заменил `FallbackCompletion`
  в `bot/rubrics_runtime.py`.

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
| `/status` | краткое состояние бота: uptime, heartbeat планировщика, ошибки |
| `/stats` | статистика публикаций за последние дни (по `slot_runs`) |
| `/chatid` | chat_id текущего чата/канала (для `MAX_CHANNEL_ID`) и user_id отправителя (для `ADMIN_IDS`) |
| `/factory run|pause|resume|status|retract slot` | рычаги рубрик (аргумент — рубрика или `all`), только для админа |

Ленты рубрики «Хорошие новости» правятся в `news_sources.yaml`, сетка — в `rubrics.yaml`.

### 4. Контент-фабрика — удалена (ADR-004)

Эскиз «`ContentFactoryPublisher` во внешнюю фабрику» не понадобился, реализованный
параллельный цикл `ContentFactory` (LLM-посты из IT-новостей) удалён вместе с
дайджест-циклом: канал определён как канал одной тематики, и его ведёт единственный
слотовый планировщик рубрик. История решений — в ADR-004.

### 5. HTML и preview

Отвергнуто окончательно: MVP работает только на RSS (архитектурное решение),
HTML-парсеры Habr/vc.ru удалены вместе с их источниками (ADR-004). Возврат — только
через новый ADR.

### 6. Webhook

Переход с long polling на webhook MAX — после стабилизации MVP: нужен публичный
https-домен с TLS от доверенного CA и ACK обновлений в течение 30 секунд
(см. ADR-001). До этого момента long polling не требует внешнего ingress.

---

## Архитектура MVP

```
rubrics.yaml (сетка + LLM-каналы)          news_sources.yaml («мягкие» RSS)
      |                                            |
      v                                            v
SlotScheduler (тик 45 c) -------- digest-рубрика: CompositeFetcher (RSS-only)
      | захват UNIQUE(rubric_id, slot_date)        |
      v                                            v
пайплайн рубрики (bank / generative / weather / digest + LLM + валидаторы)
      |
      v
MaxSlotPublisher -> MaxClient (max_api) -> MAX Bot API (канал / превью админам)
```

Ключевая идея прежняя: издатель — граница между ядром и внешней системой.
`max_api.MaxClient` — тонкий REST-клиент (aiohttp), единственное место, знающее
специфику MAX Bot API (заголовок Authorization без Bearer, REST-ресурсы, `mid`,
двухшаговый `POST /uploads` для картинок).

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
    │   ├── handlers.py       # команды бота (рубрики)
    │   ├── slot_scheduler.py # слотовый планировщик — единственный контур
    │   ├── rubrics_admin.py  # /factory run|pause|resume|status|retract slot
    │   ├── rubrics_runtime.py# сборка контура (пайплайны, LLM, картинки, сводка)
    │   ├── daily_summary.py  # ежедневная сводка админам
    │   └── db_backup.py      # ежедневный бэкап SQLite
    │
    ├── config/
    │   ├── loader.py         # env процесса + load_rubrics_config
    │   ├── rubrics.py        # валидация rubrics.yaml (+ llm/llm_fallback)
    │   ├── rubrics.yaml      # сетка рубрик и LLM-каналы — конфиг контура
    │   └── news_sources.yaml # «мягкие» RSS для рубрики «Хорошие новости»
    │
    ├── core/
    │   ├── models.py         # NewsItem, SlotRun, ContentBankItem, ...
    │   └── __init__.py
    │
    ├── parsers/
    │   ├── base.py           # SourceConfig + интерфейс Fetcher
    │   ├── rss.py            # RSS/Atom fetcher (+ summary, общая сессия)
    │   └── composite.py      # RSS-only составной fetcher digest-рубрики
    │
    ├── publishers/
    │   ├── slot.py           # MaxSlotPublisher (канал / превью, картинки)
    │   ├── alerter.py        # Alerter + MaxAlerter (алерты админам)
    │   └── __init__.py
    │
    ├── writers/
    │   ├── completion.py     # LlmCompletionClient + FallbackCompletion-транспорт, SKIP_MARKER
    │   ├── fallback.py       # FallbackCompletion (per-request failover)
    │   ├── readiness.py      # стартовая проба обоих LLM-каналов
    │   └── validator.py      # валидация вывода LLM (лимит 4000)
    │
    ├── rubrics/              # пайплайны рубрик (generative, digest, news_filters)
    ├── bank/                 # банк контента (отбор, антиповтор, валидатор)
    ├── weather/              # Open-Meteo + met.no, шаблон, валидатор
    ├── images/               # fal.ai (flux/dev) + фолбэк-обёртка
    │
    ├── utils/
    │   ├── db.py             # SQLite, миграции user_version, события
    │   ├── repositories.py   # репозитории slot_runs, content_bank, ...
    │   ├── text.py           # canonical_url, title_tokens, jaccard
    │   ├── sanitize.py       # санитайзер HTML-разметки сообщения
    │   ├── formatter.py      # текст поста (лимит 4000)
    │   └── logging.py        # настройка логирования
    │
    ├── scripts/
    │   ├── healthcheck.py    # Docker healthcheck (слоты; DATABASE_PATH из env)
    │   └── bank_import.py    # импорт банка контента из seed-yaml
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

`summary` — очищенный от HTML текст RSS-описания (обрезка ~1000 символов): digest-рубрика
отдаёт его LLM-классификатору как исходный материал.

Состояние публикации живёт в `SlotRun` (см. слотовую машину состояний в
`bot/slot_scheduler.py` и плане автономного канала).

Для MVP достаточно `dataclass`. `pydantic` можно добавить позже, если появится внешний API.

---

## Publisher

Публикация — отдельный интерфейс, а не часть пайплайна: `SlotPublisher`
(протокол в `bot/slot_scheduler.py`), реализация `MaxSlotPublisher` в
`publishers/slot.py` — текст + опциональная картинка, канал или превью админам
(dry-run), двухшаговая загрузка изображений через `POST /uploads`.
Переиспользование интерфейса защищает пайплайны от платформенной специфики MAX.

---

## Жизненный цикл слота (кратко)

Подробная машина состояний, recovery и выключатели описаны в плане автономного
канала (docs/plans/2026-10-09-autonomous-channel.md, §10–11) и реализованы в
`bot/slot_scheduler.py`:

```
slot_scheduler_loop (тик 45 c)
  |
  v
захват слота: INSERT pending за UNIQUE(rubric_id, slot_date)
  |
  v
пайплайн рубрики (генерация + валидаторы, до 3 попыток; резервный пост из банка)
  |
  v
MaxSlotPublisher: publishing -> published (+ skipped / failed / unknown_delivery)
```

После рестарта: `pending` внутри grace доисполняется, `generated` публикует
сохранённый текст без повторного LLM-вызова, `publishing` повторно вслепую не
отправляется — становится `unknown_delivery` с алертом админу. N неудач подряд
пауза рубрику (`/factory resume` снимает), сбой MAX API — глобальная пауза с
переотправкой, пустой источник исключается после трёх пустых ответов.

### Наблюдаемость и healthcheck

- Планировщик пишет в `bot_stats` событие `slot_heartbeat` каждые 900 с даже в
  тихий день — внешний watchdog отличает «слотов нет» от «процесс завис».
- LLM-обёртка `FallbackCompletion` пишет по событию на вызов
  (`llm_primary_ok` / `llm_fallback_used` / `llm_fallback_error_<kind>`);
  конфиг-ошибка любого канала немедленно алертит админов (дедуп — раз в сутки
  на канал), даже когда успешный резерв маскирует сбой.
- Healthcheck (Docker, `scripts/healthcheck.py`, только SQLite без сети):
  свежесть `slot_heartbeat` (окно 2×900 с); «тихий день» — после 09:00 местный
  день должен содержать хотя бы один терминальный `slot_runs`; вечерняя сводка —
  после 23:30 последнее событие `daily_summary` должно покрывать сегодняшний день.
  Правила digest/factory удалены вместе с контуром (ADR-004).
- Пустая БД без `slot_runs` — льготный период первого бута, не unhealthy.

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
Фоновые циклы (`slot_scheduler_loop`, сводка, бэкап) обрабатывают свои ошибки
самостоятельно и через обработчик polling-цикла не проходят.

### Миграции

Миграции — `PRAGMA user_version`, список шагов в одном месте (`utils/db.py`):

- v1 — существующие таблицы (`sent_news`, `bot_stats`, `sources_state`)
  идемпотентно: прод-БД со старой версией `user_version = 0` корректно
  поднимается до актуальной;
- v2 — таблица `posts` (legacy фабрики: код её больше не пишет, схема создаётся,
  чтобы прод-БД поднималась без пересборки — ADR-004);
- v3 — `posts.message_id` INTEGER → TEXT (mid — строка `mid.<id>`): таблица
  пересобирается через `posts_new`, шаг начинается с `DROP TABLE IF EXISTS
  posts_new` (реентерабелен после сбоя процесса посреди миграции) и сохраняет
  счётчик AUTOINCREMENT;
- v4 — таблицы контура рубрик (`slot_runs`, `digest_items`, `rubric_state`,
  `content_bank`, `filter_log`);
- перед применением миграций к существующей БД — копия файла БД после
  чекпоинта WAL (откат = вернуть копию).

WAL включается на инициализации, `busy_timeout` — на каждом соединении.

---

## Конфигурация

`rubrics.yaml` — конфиг единственного контура (валидация при старте, опечатка
роняет запуск): сетка рубрик, `persona` канала, `targets`, `daily_cap`, погода,
генерация картинок и LLM-каналы (перенесены сюда из удалённого `sources.yaml`):

```yaml
timezone: "Europe/Moscow"
persona: |
  Ты — редактор канала для читателей старшего возраста ...

llm:
  # первичный LLM-канал (ADR-002); ключ — только в env
  base_url: "https://openrouter.ai/api/v1"
  model: "qwen/qwen3.8-flash"
  disable_reasoning: true
  timeout: 60
  max_tokens: 800
  temperature: 0.7
llm_fallback:
  base_url: "https://ai.api.cloud.yandex.net/v1"
  model: "gpt://<folder_id>/yandexgpt-5-lite"

targets:
  main:
    channel_id_env: MAX_CHANNEL_ID
daily_cap: 6
summary_time: "22:40"

rubrics:
  - id: good_morning
    time: "07:00"          # в yaml времени — в кавычках (YAML 1.1 парсит 12:30 как число)
    kind: bank             # bank | generative | weather | digest
    ...
```

`.env` хранит секреты и runtime-переопределения (полный список с комментариями —
`max_news_bot/.env.example`):

```env
MAX_BOT_TOKEN=
MAX_CHANNEL_ID=
ADMIN_IDS=
LOG_LEVEL=INFO
DATABASE_PATH=
OPENROUTER_API_KEY=
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL=
LLM_FALLBACK_BASE_URL=
LLM_FALLBACK_MODEL=
FAL_KEY=
RUBRICS_DRY_RUN=
RUBRICS_CONFIG_PATH=
```

`LLM_BASE_URL`/`LLM_MODEL` переопределяют первичный канал (OpenRouter),
`LLM_FALLBACK_BASE_URL`/`LLM_FALLBACK_MODEL` — резервный (Yandex);
`RUBRICS_DRY_RUN=false` переключает превью админам на публикацию в канал.

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

Отдельная миграционная система не нужна: создание и изменения схемы живут в `utils/db.py` в одном месте, версия схемы — `PRAGMA user_version` (v1 — базовые таблицы, v2/v3 — legacy `posts` фабрики, v4 — таблицы контура рубрик; перед применением к существующей БД — копия файла после чекпоинта WAL, см. «Миграции»). Таблицы `sent_news`/`posts`/`sources_state` — замороженная история: код их больше не пишет, рабочий контур живёт в `slot_runs`/`content_bank`/`digest_items` (ADR-004).

---

## Логирование

Нужно два уровня наблюдаемости:

- обычные application logs в stdout для Docker;
- бизнес-события в `bot_stats`: запуск, успешная публикация, ошибка источника, ошибка MAX API.

Минимальный формат логов:

```text
2026-05-25 12:00:00 INFO max_news_bot.bot.slot_scheduler published=1 skipped=0 failed=0
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

- `test_loader.py` — env читается, rubrics.yaml подхватывается, dry-run требует `ADMIN_IDS`;
- `test_rubrics_config.py` — валидация сетки и LLM-секций (fail-fast на опечатку);
- `test_slot_scheduler.py` — захват слотов, recovery, выключатели, daily_cap;
- `test_main_contour.py` — старт единственного контура, проба LLM-каналов до задач;
- `test_fallback_llm.py` — `FallbackCompletion` против локальных aiohttp-серверов (200/401/429/500, мусорный JSON), события и алерты;
- `test_rubrics_runtime.py` — сборка контура, binding пайплайнов, проба каналов;
- `test_formatter.py`, `test_validator.py`, `test_sanitize.py` — границы длин, «злые» кейсы санитайзера;
- `test_architecture.py` — инварианты: в зависимостях нет aiogram/тяжёлых библиотек;
- `test_healthcheck.py` — правила healthcheck (только слоты) на временной SQLite-базе.

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
HTML-зависимостей (beautifulsoup4, lxml) не будет: RSS-only — архитектурное решение.

---

## Итоговое решение

Проект развивается от маленького production-бота, а не от большой платформы.

Проект — маленький production-бот одной тематики: слотовый планировщик рубрик,
RSS-only сбор для дайджеста, MAX-публикация собственным тонким клиентом,
двухканальный LLM с per-request failover, Docker и наблюдаемость.

Контур стал единственным (ADR-004): legacy-циклы удалены, их роль закрыла слотовая
машина состояний. Порт на MAX + Yandex AI Studio (ADR-001) и failover LLM (ADR-002)
прошли по прежним границам: платформенная специфика сосредоточена в `max_api` и
`publishers`. Такой подход сохраняет проект простым и не мешает встроить его в
более крупную систему позже.
