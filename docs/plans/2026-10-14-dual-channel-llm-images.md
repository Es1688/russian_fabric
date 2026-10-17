# Двухканальные LLM и изображения: OpenRouter+fal (основные), Yandex GPT+YandexART (резерв)

Дата: 2026-10-14. Статус: согласован к внедрению (adversarial-ревью, раунды 1–3 +
самопроверка лида; обновлён 2026-10-17: YandexART переведён в заглушку).

Подтверждённые решения:

- Приоритет связок: OpenRouter (LLM) + fal.ai (изображения) — основные; Yandex GPT —
  резервный LLM. Модели: `qwen/qwen3.8-flash` (OpenRouter, как в исходном
  content_fabric), `fal-ai/flux/dev` (fal, как в visual-studio),
  `gpt://<folder>/yandexgpt-5-lite` (резервный LLM, Yandex).
- YandexART пока ЗАГЛУШКА (решение оператора от 2026-10-17): fal — единственный
  активный провайдер изображений, при его сбое пост деградирует до текстового.
  Полноценный клиент YandexART (`art://<folder>/aliceai-image-art-3.0`, research
  `2026-10-09-epoch0-research.md` §2) — будущая задача: интерфейс `ImageGenerator`,
  конфиг-секция и события наблюдаемости резервируются уже сейчас, чтобы подключение
  не требовало переделок.
- Схема отказоустойчивости: независимый per-request failover без состояния. Липкое
  переключение «связками» (общий флаг активной пары LLM+картинка, пробы возврата)
  отклонено: домены отказа независимы (лежащий OpenRouter не должен уводить текст на
  другую модель при живом fal), уровни критичности разные (ошибка LLM = поста нет,
  ошибка картинки = деградация до текста), а цена — состояние в БД, кулдауны/пробы
  возврата и операционные сюрпризы.
- Триггер перехода на резерв — любая ошибка основного канала (infra / config / content).
- Картинки: по флагу в рубрике `image: generate`; банк открыток (`image: <bank type>`)
  работает как раньше. Factory-контур остаётся текстовым (вне рамок).
- Секреты: `OPENROUTER_API_KEY` (основной LLM), `FAL_KEY` (fal). `LLM_API_KEY`
  сохраняет значение — ключ Yandex, теперь обслуживает резервный LLM-канал и YandexART.
  Миграция деплоя точечная (см. §4): env-перекрытия `LLM_BASE_URL`/`LLM_MODEL` в `.env`
  VPS указывают на Yandex и молча сделали бы Yandex первичным — их убрать/перенести в
  `LLM_FALLBACK_*` (folder_id из `LLM_MODEL` нужен и для `art://`), добавить
  `OPENROUTER_API_KEY` и `FAL_KEY`.
- Обязательность каналов (подтверждено оператором на ревью): при активном LLM-контуре
  обязательны оба LLM-ключа, при рубриках с `image: generate` — `FAL_KEY`; тихий
  запуск без заявленного канала исключён fail-fast'ом. Требование «оба провайдера
  изображений» вернётся вместе с реальным клиентом YandexART (сейчас заглушка).

Оценка усложнения: относительно плана «один резервный LLM-канал + один генератор» —
примерно +20% (клиент YandexART в этом этапе — заглушка, протокол генератора и
обёртка failover ~100 строк, +6–8 тестов). Новых зависимостей нет — всё на aiohttp
(test_architecture.py ограничение тяжёлых зависимостей не затрагивается).

## 1. Суть

Сегодня проект привязан к одному провайдеру текста (Yandex AI Studio) и не генерирует
изображения вовсе (режим банка открыток; YandexART исследован в
`docs/plans/2026-10-09-epoch0-research.md` §2, но не подключён). Задача — две
независимые цепочки отказоустойчивости:

- текст: каждый LLM-вызов сначала идёт в OpenRouter, при любой ошибке — в Yandex GPT;
- картинка: генерация в fal.ai; при сбое — деградация до поста без картинки (как у
  банка открыток сегодня). Второй провайдер изображений (YandexART) — заглушка,
  подключается будущей задачей без переделок интерфейса.

Без липкого состояния: каждый вызов начинает с основного канала; если тот лежит,
теряется одна попытка с таймаутом (при частоте слотов незаметно). Кулдаун основного
канала в памяти процесса — возможное будущее улучшение, в план не входит.

Вне рамок: изображения в factory-контуре (`Post` без поля картинки, `MaxPostPublisher`
без вложений — отдельная задача, если понадобится), sticky-переключение связками,
учёт стоимости картинок в ежедневной сводке, ПОЛНОЦЕННЫЙ клиент YandexART (в этом
этапе — заглушка; события `image_art_*` наблюдаемости появятся вместе с ним).

## 2. LLM: OpenRouter первичный, Yandex GPT резервный

Принцип конфигурации: `factory.llm` остаётся «первичным каналом» (дефолты меняются на
OpenRouter), новый `factory.llm_fallback` — «резервным» (дефолты Yandex).

### 2.1. Конфиг — `config/loader.py` + `config/sources.yaml`

- `LlmConfig` получает `disable_reasoning: bool = False`.
- `factory.llm` (первичный): дефолты меняются на
  `base_url: https://openrouter.ai/api/v1`, `model: qwen/qwen3.8-flash`,
  `disable_reasoning: true`; секрет — env `OPENROUTER_API_KEY`; env-переопределения
  `LLM_BASE_URL` / `LLM_MODEL` сохраняются.
- Новый `factory.llm_fallback`: дефолты Yandex (`https://ai.api.cloud.yandex.net/v1`,
  `gpt://FOLDER_ID/yandexgpt-5-lite`, `disable_reasoning: false`); секрет —
  существующий env `LLM_API_KEY`; env-переопределения `LLM_FALLBACK_BASE_URL` /
  `LLM_FALLBACK_MODEL`.
- `FactoryConfig.llm_fallback: LlmConfig | None`; при активном LLM-контуре резерв
  обязателен (см. fail-fast ниже). Timeout / max_tokens / temperature общие с
  первичным каналом.
- Fail-fast (обновить `load_config`): при enabled-фабрике / LLM-рубриках обязательны
  ОБА ключа — первичный `OPENROUTER_API_KEY` и резервный `LLM_API_KEY` (сейчас
  проверяется только `LLM_API_KEY`). Отсутствие любого → ValueError с понятным
  сообщением: тихий запуск с одним каналом ломает заявленную отказоустойчивость.
  Одиночный LLM-канал остаётся возможен только вне LLM-контуров (digest/weather).
- Readiness-проба LLM-каналов на старте (битый резерв не должен ждать аварии primary,
  чтобы обнаружиться): при активном LLM-контуре оба канала получают минимальный
  completion-зонд (`max_tokens=1`, короткий промпт; стоимость — доли цента). Исход:
  200 → канал готов; config-kind (401/402/403/404) → fail-fast с понятной ошибкой
  (ключ/модель резервного канала битые — это статическая ошибка деплоя); infra-kind
  (таймаут/сеть/5xx) → WARNING-лог и продолжение запуска — временная недоступность
  внешнего сервиса не должна блокировать старт бота (иначе доступность резерва
  становится зависимостью запуска, что противоречит цели отказоустойчивости).
  Проба проверяет транспорт/авторизацию (HTTP 200 + парсимый ответ), не контент.
  Изображения при старте НЕ пробуем: генерация тарифицируется за запрос
  (fal ~$0.025, YandexART ~2,23 ₽) — их работоспособность подтверждается первой
  реальной генерацией, а config-сбой канала изображений ловится немедленным алертом
  (см. §2.6).

### 2.2. Транспорт — `writers/llm.py`, `writers/completion.py`

- Опциональный `disable_reasoning` в `LlmWriter.__init__` и
  `LlmCompletionClient.__init__`: при True в payload добавляется
  `"reasoning": {"enabled": False}` — порт из исходного content_fabric
  (`news_bot/writers/llm.py:162-165`): thinking-модели OpenRouter иначе сжигают весь
  `max_tokens` на рассуждения и возвращают `content=null`.
- `_normalize_content` (строка + список text-частей) уже поддерживает форму ответа
  OpenRouter/Qwen — не меняется.

### 2.3. Обёртки — новый `writers/fallback.py`

- `FallbackWriter(primary, fallback)` (протокол `Writer`): `written` / `skipped` от
  primary возвращаются сразу; `status="error"` любого error_kind → warning-лог
  (kind, error) → вызов fallback; если ошиблись оба — `WriteResult` от fallback с
  комбинированным `error` («primary=…; fallback=…») и kind от fallback (битый ключ
  Yandex корректно даст config → autopause фабрики). В `post.llm_model` фиксируется
  реально отработавшая модель.
- `FallbackCompletion(primary, fallback)` (протокол `LlmCompletion`): при
  `LlmCompletionError` любого kind → лог → fallback; ошибка fallback пробрасывается
  как есть.
- Промпт один на оба канала (он провайдер-агностичный).

### 2.4. Wiring

- `main.py` (factory-контур): при наличии `llm_fallback` — второй `LlmWriter`
  (параметры из fallback-секции, timeout/max_tokens/temperature из первичной) +
  `FallbackWriter(...)`, общий `http_session`. Перед стартом фоновых задач —
  readiness-проба обоих LLM-каналов (см. §2.1).
- `bot/rubrics_runtime.py::_build_completion` (rubrics-контур): тот же паттерн для
  `LlmCompletionClient` → одним местом покрывает generative / bank / digest; проба —
  там же, при сборке рантайма (сеть вызывается один раз на старт, до первого слота).
  Инвариант докстринга `build_rubrics_runtime` «Construction performs no network
  calls» сознательно пересматривается: readiness-проба — единственный сетевой вызов
  сборки, обновить докстринг.

### 2.5. Тесты

- `tests/test_llm_writer.py`: + кейс «payload содержит `reasoning: {"enabled": False}`
  при флаге» (существующий тест «без флага поля нет» остаётся).
- Новый `tests/test_fallback_llm.py` на двух `FakeLlmServer`: primary ок → fallback
  не зовётся; 500 / 401 / refusal у primary → пост от qwen (`llm_model` = резервная);
  оба упали → комбинированная ошибка, kind от fallback; skipped → без fallback;
  те же сценарии для `FallbackCompletion`.
- Тесты loader: две секции, env-переопределения, резерв ⟺ `LLM_API_KEY`, обновлённый
  fail-fast.
- Тесты наблюдаемости: события каналов пишутся в `bot_stats` и агрегируются по дате,
  переживают пересоздание соединения (durable); сводка читает из БД; config-ошибка
  ЛЮБОГО канала (primary/резерв, LLM/изображения) → admin-алерт, infra-ошибка → без
  алерта; readiness-проба: config-kind у любого LLM-канала → ValueError на старте,
  infra-kind → WARNING и запуск продолжается; fail-fast: нет `OPENROUTER_API_KEY`
  или `LLM_API_KEY` при активном LLM-контуре → ValueError.

### 2.6. Наблюдаемость отказоустойчивости (LLM и картинки)

- Тихий постоянный failover не должен скрывать деградацию основного канала, поэтому:

- Суточные счётчики каналов — durable, на уже существующей append-only таблице
  `bot_stats` (schema в `utils/db.py`; daily_summary уже использует её для дедупа):
  одно событие на вызов (`event = "llm_primary_ok" / "llm_fallback_used" /
  "image_fal_ok" / "image_art_used" / "image_none" / "llm_<канал>_error_<kind>"`,
  details — контекст), агрегация сводкой через COUNT по дате. Новая таблица и
  миграция не нужны; рестарты не теряют данные. Warning-лог при каждом срабатывании
  fallback.
- Существующие пути алертов не дублируются: в rubrics-контуре config-ошибка ИТОГОВОГО
  исхода уже алертится шедулером (`bot/slot_scheduler.py:619`), в factory-контуре —
  autopause (`core/factory.py:258`). Новый хук в обёртках закрывает оставшийся случай:
  config-ошибка канала, замаскированная успешным fallback (включая каналы
  изображений, чей сбой деградирует в text-only и до шедулера как config не доходит).
- Ежедневная сводка админам дополняется строкой «LLM: primary N / fallback M;
  картинки: fal A / art B / без картинки C», построенной из `bot_stats` (в дополнение
  к существующей оценке LLM-вызовов в `_llm_text`), — деградация видна без
  просмотра логов.
- Немедленный admin-алерт (штатный `MaxAlerter`, не чаще одного раза в сутки на
  канал, дедуп в памяти процесса) отправляется при **config-ошибке ЛЮБОГО канала** —
  основного ИЛИ резервного, LLM и изображений — по семантике `error_kind=config`
  действующей таксономии (401/402/403/404 — ключ, баланс, неверная модель/endpoint),
  а не по урезанному списку HTTP-кодов: битый резерв так же опасен, как битый primary,
  и не должен ждать аварии основного канала, чтобы обнаружиться. Повтор алерта после
  рестарта при всё ещё битой конфигурации допустим — проблема не исчезла,
  durable-дедуп не нужен. Infra- и content-ошибки остаются в логах и сводке.
  Пороговые окна «N за 15 минут» в MVP не вводим — слоты редкие, сводки достаточно.

## 3. Изображения: fal единственный активный провайдер, YandexART — заглушка (контур рубрик)

MAX-обвязка готова (`MaxMediaUploader.upload_image` → token → attachment,
`publishers/slot.py`); добавляется пакет `max_news_bot/images/`.

### 3.1. Протокол и клиенты — новые `images/base.py`, `images/fal.py`, `images/yandex_art.py`, `images/fallback.py`

- `ImageGenerator` протокол: `async generate(prompt: str) -> bytes`;
  `ImageGenerationError(kind, message)` с общей таксономией: config (401/402/403 —
  ключ/баланс), infra (сеть, 429, 5xx, таймаут, битый ответ).
- `FalImageGenerator` — порт `FalClient` из `~/work/it/visual-studio`
  (`scripts/fal_adapter.py`) на aiohttp: `POST https://queue.fal.run/{endpoint}` с
  `Authorization: Key {FAL_KEY}` → поллинг `status_url` (интервал ~1.5 с, общий
  таймаут из конфига, ~120 с) до COMPLETED (FAILED → ошибка) → `GET response_url` →
  `images[0].url` → скачивание bytes. Ретраи только на транзиентное (сеть, 429, 5xx) с
  экспоненциальным backoff. Payload `{"prompt", "image_size": {"width","height"}}`.
- `YandexArtStub` — заглушка второго провайдера: класс с тем же интерфейсом
  `ImageGenerator`, `generate()` сразу бросает `ImageGenerationError("config",
  "yandex_art: not implemented (stub)")`; в рантайм НЕ подключается (см. решения
  оператора). Докстринг фиксирует контракт будущего клиента по research §2:
  синхронный `POST {yandex_base_url}/images/generations`,
  `{"model": "art://<folder>/aliceai-image-art-3.0", "prompt", "size": "1024x1024"}`
  → `data[0].b64_json`; авторизация Bearer, при 401 — повтор с
  `Api-Key`/`OpenAI-Project: <folder_id>` (открытый вопрос research §2.3); folder_id
  парсится из модели резервного LLM-канала (`gpt://<folder>/…`); лимит промпта
  500 символов. Заглушка не даёт будущему подключению требовать переделок.
- `FallbackImageGenerator(primary, fallback: ImageGenerator | None)`: ошибка primary
  любого kind → лог → fallback (если подключён); резерва нет или обе упали →
  `ImageGenerationError` наружу. Сейчас fallback=None (YandexART — заглушка, в рантайм
  не ставится); когда клиент появится, wiring не меняется — передаётся второй
  генератор.
- Лимиты памяти: `MAX_IMAGE_BYTES = 10 МБ` на декодированные bytes для обоих
  провайдеров — потоковое чтение с обрывом по лимиту (fal: скачивание `images[0].url`;
  YandexART: декод `b64_json` с проверкой длины), malformed base64 / oversized →
  `ImageGenerationError` (infra) → деградация до поста без картинки. Тесты на
  oversized payload и malformed base64 обязательны.

### 3.2. Конфиг — `config/rubrics.py` + `rubrics.yaml`

- Топ-секция `image_generation:` {`fal:` {model: `fal-ai/flux/dev`},
  `yandex_art:` {size: "1024x1024"}, timeout_seconds: 120}. Провайдер активен при
  наличии своего ключа (`FAL_KEY` / `LLM_API_KEY` + folder_id); порядок приоритета
  фиксирован fal → yandex_art. Ключ `image_generation` добавить в whitelist топ-ключей
  `_TOP_LEVEL_KEYS` валидатора `config/rubrics.py` (неизвестный топ-ключ роняет
  запуск) + тесты в `test_rubrics_config.py`. Значение `generate` в `image:` —
  зарезервированное слово (не имя типа банка): коллизий нет, имена типов банка живут
  в БД, а не в конфиге.
- Fail-fast: есть рубрики с `image: generate` → `FAL_KEY` обязателен (отсутствие →
  ValueError на старте). Секция `yandex_art` в `image_generation` резервируется:
  парсится и валидируется, но провайдер не активируется (заглушка) и ключа не
  требует — требование «оба провайдера» вернётся вместе с реальным клиентом. Для
  рубрик без генерации (банк открыток / без картинки) секция `image_generation`
  не нужна.
- Значение per-rubric `image: generate` → генерация; `image: <bank type>` → банк
  открыток как сейчас; без `image` → без картинки.
- Промпт детерминированный, без LLM-раунда: глобальный `image_style` (уже лежит в
  rubrics.yaml, сейчас никем не читается) + тема (первые ~200 символов текста поста),
  суммарно ≤ 500 символов.

### 3.3. Пайплайны

- `BankPipeline._postcard_ref` и `GenerativePipeline.generate` / `fallback_post`:
  при `rubric.image == "generate"` → генерация через `FallbackImageGenerator` → bytes
  в `data/generated/<slot_run_id>.png` → `image_ref = "generated:<имя файла>"`
  (провайдер-агностично; файл персистентен — ретрай публикации не перегенерирует
  картинку). Любой сбой генерации → warning-лог + пост без картинки + запись в
  `alert` исхода слота (картинка не роняет слот).
- Жизненный цикл generated-файлов (анти-переполнение диска): файл удаляется после
  успешной публикации слота (publisher подтверждает успех — файл больше не нужен,
  ретраи до успеха продолжают его использовать); страховочный TTL-prune — в
  ежедневном обслуживании (рядом с daily backup) сносить файлы старше 7 дней.
  Тесты: файл удалён после успешной publish, survives при неуспехе, prune по TTL.

### 3.4. Publisher — `publishers/slot.py`

- Ветка `generated:`-ссылок в `_postcard_attachments`: чтение из generated-директории
  с path-traversal-защитой (по образцу `bank/postcards.py`) → upload → attachment.
  Локальная проблема файла → text-only деградация; транспортная ошибка → infra
  (семантика — как с открытками банка).

### 3.5. Тесты

- `tests/test_fal_images.py` + `FakeFalServer` (submit → IN_PROGRESS/COMPLETED →
  response → bytes): успех, статус FAILED, 401 → config, 500 → retry → успех, таймаут
  поллинга.
- Тест заглушки: `YandexArtStub.generate` бросает `ImageGenerationError` вида config
  с понятным сообщением; конфиг-секция `yandex_art` парсится и валидируется, но не
  требует ключа и не активирует провайдера.
- `FallbackImageGenerator` и пайплайн-тесты с фиктивным генератором (по образцу
  `MockLlm`): `image: generate` → `generated:`-ссылка; сбой fal при fallback=None →
  text-only + alert; с подключённым фиктивным резервом — резерв спасает пост
  (контракт на будущее, без YandexART-клиента).
- Publisher: разрешение `generated:`-ссылок, traversal-guard, деградация.
- Конфиг-тесты секции `image_generation` и значения `image: generate`.

## 4. Документация и валидация

- `.env.example`: `OPENROUTER_API_KEY` (первичный LLM),
  `LLM_FALLBACK_BASE_URL` / `LLM_FALLBACK_MODEL` (резерв), комментарий к
  `LLM_API_KEY` (теперь ключ резервного канала Yandex: GPT + ART), `FAL_KEY`;
  README — env-таблица.
- `sources.yaml`: обновлённая секция `factory.llm` (OpenRouter-дефолты) +
  `factory.llm_fallback`; `rubrics.yaml`: `image_generation` + комментарий к
  `image: generate`.
- `ARCHITECTURE.md`: ADR-002 (двухканальный LLM, приоритет OpenRouter) и ADR-003
  (изображения: fal — активный провайдер, YandexART отложен заглушкой; при сбое fal —
  деградация до текстового поста).
- Валидация: `uv run pytest` из корня; все тесты на локальных фейк-серверах, сеть не
  нужна.
- Деплой: в `.env` на VPS — (1) добавить `OPENROUTER_API_KEY` и `FAL_KEY`;
  (2) УДАЛИТЬ/очистить `LLM_BASE_URL` и ПЕРЕНЕСТИ `LLM_MODEL` в `LLM_FALLBACK_MODEL`
  — сейчас они перекрывают yaml и молча оставили бы Yandex первичным каналом (оба
  канала Yandex, OpenRouter не используется); (3) `LLM_API_KEY` не трогать (ключ
  Yandex, резервный LLM-канал). При активном LLM-контуре оба LLM-ключа обязательны
  (fail-fast); при рубриках с `image: generate` обязателен `FAL_KEY` (YandexART —
  заглушка, ключа не требует). Коммит — после тестов и подтверждения пользователя.
