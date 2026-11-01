# Content Fabric MAX — News Bot (MVP)

Бот мессенджера **MAX**: ведёт автономный канал для аудитории старшего возраста
СПб и Ленобласти — суточная сетка рубрик, слотовый планировщик, банк контента,
погода, «хорошие новости» из RSS с LLM-фильтрами, наблюдаемость. Текст постов
пишется через двухканальный LLM — OpenRouter (первичный канал) с per-request
failover на Yandex GPT (резерв). Реализовано по плану
[docs/plans/2026-10-09-autonomous-channel.md](docs/plans/2026-10-09-autonomous-channel.md).

Контур рубрик — **единственный** publishing-контур процесса (ADR-004): раньше
рядом с ним существовали legacy-циклы (линк-дайджест и контент-фабрика IT-новостей
из Habr/vc.ru) с переключателем `RUBRICS_ENABLED`/`FACTORY_ENABLED`; они удалены.

Платформы доставки — российские (MAX, Yandex Cloud): прокси не нужны, бот и LLM
доступны с VPS в РФ напрямую. Ключевые решения порта, двухканального LLM и
единого контура — в [ARCHITECTURE.md](ARCHITECTURE.md), разделы ADR-001–ADR-004.

## Состав репозитория

- `max_news_bot/` — код бота (точка входа `max_news_bot/main.py`,
  запуск `python -m max_news_bot.main`)
  - `max_api/` — тонкий aiohttp-клиент MAX Bot API (+ загрузка изображений:
    `POST /uploads` → CDN → токен в attachments)
  - `bot/` — long polling (`polling.py`), команды (`handlers.py`,
    `rubrics_admin.py`), слотовый планировщик (`slot_scheduler.py`), ежедневная
    сводка (`daily_summary.py`)
  - `rubrics/` — пайплайны рубрик: генеративный (утро/ночь/тема дня/второй пост),
    новости, дайджест, цепочка фильтров и валидаторы
  - `bank/` — банк контента: отбор с сезонностью, антиповтор, открытки
  - `weather/` — Open-Meteo (batch на 7 точек), резерв met.no, таблица правил,
    шаблон, восход/закат формулой, валидатор
  - `parsers/`, `core/`, `publishers/`, `writers/`, `utils/`, `config/`
  - `config/rubrics.yaml` — сетка рубрик и LLM-каналы (единственный конфиг
    контура); `config/news_sources.yaml` — подтверждённый список «мягких» RSS
    для рубрики «Хорошие новости»
- `infra/` — Docker Compose для продакшена (VPS)
- `.github/workflows/docker.yml` — CI/CD деплой на VPS из ветки `main`
- [ARCHITECTURE.md](ARCHITECTURE.md) — архитектурные решения (включая ADR-001)
- `docs/plans/` — план автономного канала, отчёты исследования (Этап 0) и
  реализации

## Стек

Python 3.12, aiohttp + feedparser (без aiogram — собственный тонкий клиент MAX),
SQLite (WAL, самописные миграции через `PRAGMA user_version`; схема рубрик —
миграция v4: `slot_runs`, `digest_items`, `rubric_state`, `content_bank`, лог
фильтров), uv, pytest. Деплой: Docker (образ `python:3.12-slim`, непривилегированный
пользователь) → DockerHub → VPS через GitHub Actions.

## Быстрый старт

Требуются [uv](https://docs.astral.sh/uv/) и Python 3.12+.

```bash
uv sync                                          # зависимости
cp max_news_bot/.env.example max_news_bot/.env   # заполнить MAX_BOT_TOKEN, MAX_CHANNEL_ID, ...
uv run python -m max_news_bot.main               # запуск
uv run pytest                                    # тесты
```

По умолчанию контур работает в dry-run: посты уходят превью админам. Подробнее —
ниже, в разделе «Раскатка».

## Подготовка (prerequisites)

### MAX

1. **Создать бота**: напишите [@masterbot](https://dev.max.ru) в MAX, создайте
   бота и получите токен → `MAX_BOT_TOKEN` в `.env`.
2. **Канал**: создайте канал и добавьте бота **администратором с правом
   публикации** сообщений.
3. **Узнать chat_id**: в проекте есть команда `/chatid` — вызовите её
   (например, в канале, куда добавлен бот) и подставьте целое число в
   `MAX_CHANNEL_ID`.
4. **Админы**: перечислите **user_id** админов в `ADMIN_IDS` (через запятую) —
   не chat_id лички: личные сообщения бот адресует по `user_id`. Узнать свой
   user_id можно командой `/chatid` в личном диалоге с ботом. Каждый админ
   обязан **один раз написать боту `/start`** — в MAX бот не может инициировать
   личную переписку, без этого не доставятся превью фабрики/рубрик и алерты.

### LLM-каналы (рубрики)

Текст постов пишется через двухканальный LLM (ADR-002): каждый вызов сначала
идёт в первичный канал, при любой ошибке — в резервный (per-request failover
без состояния, промпт общий).

1. **Первичный канал — OpenRouter**: создайте API-ключ на
   [openrouter.ai](https://openrouter.ai) → `OPENROUTER_API_KEY`. Дефолтная
   модель `qwen/qwen3.8-flash` и endpoint уже вписаны в `rubrics.yaml`;
   переопределения — `LLM_BASE_URL` / `LLM_MODEL` (не указывайте там Yandex —
   это молча сделало бы резервный канал первичным).
2. **Резервный канал — Yandex AI Studio**: в [Yandex Cloud](https://console.yandex.cloud)
   подключите платный **billing-аккаунт** (бесплатного tier нет), создайте
   API-ключ AI Studio → `LLM_API_KEY`, скопируйте `folder_id` каталога и
   соберите имя модели → `LLM_FALLBACK_MODEL`: `gpt://<folder_id>/yandexgpt-5-lite`.
   Тот же ключ в будущем обслужит генерацию изображений YandexART.
3. При включённых рубриках LLM-видов (generative, digest) обязательны **оба**
   ключа: отсутствие любого роняет запуск с fail-fast ошибкой — тихая работа
   с одним каналом ломает заявленную отказоустойчивость.

### Изображения (fal.ai)

Рубрика со значением `image: generate` в `rubrics.yaml` генерирует картинку
через fal.ai (модель `fal-ai/flux/dev`); при сбое генерации пост деградирует
до текстового, слот не падает. Создайте API-ключ на
[fal.ai](https://fal.ai) → `FAL_KEY`: он обязателен, если хотя бы одна
включённая рубрика использует `image: generate` (иначе запуск падает с
fail-fast ошибкой). Второй провайдер YandexART — зарезервированная заглушка:
ключа пока не требует (ADR-003).

## Переменные окружения

Полный список с комментариями — в [max_news_bot/.env.example](max_news_bot/.env.example).

| Переменная | Назначение |
|---|---|
| `MAX_BOT_TOKEN` | токен бота MAX от @masterbot |
| `MAX_CHANNEL_ID` | chat_id канала MAX (целое число, см. `/chatid`) |
| `ADMIN_IDS` | **user_id** админов через запятую (не chat_id лички; узнать — `/chatid` в личке с ботом); каждый должен написать боту `/start` |
| `LOG_LEVEL` | уровень логирования (`INFO`) |
| `DATABASE_PATH` | путь к SQLite (в Docker: `/app/data/max_news_bot.db`) |
| `OPENROUTER_API_KEY` | ключ OpenRouter — **первичный** LLM-канал |
| `LLM_API_KEY` | ключ Yandex AI Studio — **резервный** LLM-канал (Yandex GPT); тот же ключ обслужит YandexART |
| `LLM_BASE_URL` | переопределение базы URL первичного канала (дефолт `https://openrouter.ai/api/v1`) |
| `LLM_MODEL` | переопределение модели первичного канала (дефолт `qwen/qwen3.8-flash`) |
| `LLM_FALLBACK_BASE_URL` | база URL резервного канала (дефолт `https://ai.api.cloud.yandex.net/v1`) |
| `LLM_FALLBACK_MODEL` | модель резервного канала: `gpt://<folder_id>/yandexgpt-5-lite` |
| `FAL_KEY` | ключ fal.ai — генерация картинок для рубрик с `image: generate`; обязателен при таких рубриках |
| `EGRESS_PROXY_URL` | socks5h-прокси (строго `socks5h://host:port`) для иностранных API — OpenRouter и fal.ai; пусто (дефолт) = всё напрямую. На VPS — локальный вход stelthVPN `socks5h://127.0.0.1:1080` (порт = `STELTHVPN_RU_BOT_SOCKS_PORT`), требует `network_mode: host` в compose (ADR-005) |
| `RUBRICS_DRY_RUN` | dry-run контура (дефолт `true`): посты уходят админам из `ADMIN_IDS`, а не в канал; при `true` `ADMIN_IDS` обязателен |
| `RUBRICS_CONFIG_PATH` | путь к yaml рубрик (дефолт `max_news_bot/config/rubrics.yaml`; файл обязателен) |

Секреты (`MAX_BOT_TOKEN`, `OPENROUTER_API_KEY`, `LLM_API_KEY`, `FAL_KEY`)
живут только в `.env` и в git не попадают.

## Автономный канал рубрик

Суточная сетка (МСК, максимум 6 постов, `daily_cap` — страховка от ручных
запусков):

| Время | Рубрика | Источник |
|---|---|---|
| 07:00 | Доброе утро | банк открыток и фраз |
| 07:05 | Погода СПб и области | Open-Meteo (batch на 7 точек), резерв met.no |
| 12:00 | Тема дня (пн–сб) | банк тем и календарь |
| 15:30 | Второй пост (пн–сб) | ротация: рецепт, совет, вопрос, факт |
| 19:00 | Хорошие новости | «мягкие» федеральные RSS + цепочка фильтров |
| 21:30 | Спокойной ночи | банк и LLM |

Воскресенье — тихий день (только утро, погода, ночной пост). Ежедневная сводка
админам — 22:40.

Ключевые свойства:

- **Единый контур** (ADR-004): рубрики — единственный publishing-контур,
  переключателей нет; `rubrics.yaml` обязателен, должен содержать хотя бы одну
  включённую рубрику и полностью валидируется при старте — опечатка роняет
  запуск, а не пост в 07:00.
- **Жизненный цикл слота**: захват `INSERT pending` за `UNIQUE(rubric_id,
  slot_date)`, статусы `generated → publishing → published` (+ `skipped`,
  `failed`, `unknown_delivery`); после рестарта `pending` подхватывается,
  `generated` публикует сохранённый текст без повторного LLM, `publishing`
  повторно вслепую не публикуется — алерт администратору.
- **Выключатели**: N отказов подряд — пауза рубрики, падение MAX API — глобальная
  пауза, пустой источник — исключение; алерты дедуплицируются.
- **Банк контента** (`content_bank`): сезонность, `event_date` для «в этот день»,
  антиповтор (Jaccard), алерт при запасе меньше 30 дней; LLM только оформляет
  записи, валидатор сверяет числа/имена. Наполняется импортом после ручной
  проверки.
- **Погода**: один batch-запрос Open-Meteo на 7 точек (СПб + 6 городов области),
  резерв met.no отдельными запросами, советы из таблицы правил, восход/закат
  чистой формулой; упал Петербург — слот пропускается, вчерашняя погода не
  публикуется.
- **Хорошие новости**: только тематические ленты из
  [config/news_sources.yaml](max_news_bot/config/news_sources.yaml); стоп-лист,
  тематические исключения, двойной LLM-классификатор, свежесть 24–48 ч; выпуск
  3–4 пунктов (заголовок, фраза, ссылка), LLM только сокращает.

### Раскатка контура рубрик

1. **Dry-run** (`RUBRICS_DRY_RUN=true`, дефолт): посты генерируются по сетке и
   уходят админам в личку; боевой канал не затрагивается. Для обкатки каркаса
   есть выключенная рубрика-заглушка `smoke_test` в `rubrics.yaml`.
2. **Тестовый канал**: `MAX_CHANNEL_ID` (или target в `rubrics.yaml`) указывает
   на закрытый канал; тестовая неделя по §13 плана (слоты, дубли, recovery,
   выключатели — ломать намеренно).
3. **Прод**: `RUBRICS_DRY_RUN=false`, банк контента наполнен (блокирует старт
   тестовой недели, не разработку).

### Наполнение банка контента

Банк — таблица `content_bank`; импорт — `scripts/bank_import.py` из
seed-файла YAML (формат и пример — [docs/bank-seed.example.yaml](docs/bank-seed.example.yaml)).
Записи импортируются как `approved` (тексты проверяются до импорта;
`--pending` — отложить на ревью), повторный импорт того же файла идемпотентен;
после импорта печатается запас по каждому типу (порог алерта — 30).

```bash
# локальная проверка seed-файла без записи в БД
uv run python -m max_news_bot.scripts.bank_import --file seed.yaml --dry-run

# на VPS: положить seed-файл в data-каталог (смонтирован в /app/data)
docker exec maxnewsbot python /app/max_news_bot/scripts/bank_import.py \
    --file /app/data/bank_seed.yaml
```

Открытки (тип `postcard`) требуют файл изображения в каталоге `postcards`
data-каталога (VPS: `/opt/maxnewsbot/data/postcards/`); импорт сверяет
существование файла.

## Docker

Build context — **корень репозитория** (pyproject.toml и uv.lock лежат в корне
и копируются в образ до кода для кэширования слоя зависимостей), Dockerfile —
внутри пакета:

```bash
docker build -f max_news_bot/Dockerfile .    # из корня репозитория
```

- Дев (сборка локально): `docker compose -f max_news_bot/docker-compose.yml up -d --build`
  — env `max_news_bot/.env`, база `max_news_bot/data/`.
- Прод локально (образ как на VPS):
  `docker compose -p maxnewsbot -f infra/compose.prod.yml up -d --build`.

## CI/CD и деплой

Push в `main` → GitHub Actions собирает образ, пушит в DockerHub и деплоит по
SSH на VPS (`docker compose -p maxnewsbot up -d`). Схема и имена шагов — как в
исходном проекте content_fabric.

### Variables (Settings → Secrets and variables → Actions → Variables)

| Variable | Значение |
|---|---|
| `DOCKER_IMAGE_REPOSITORY` | `content-fabric-max-newsbot` (полный образ: `<DOCKER_USERNAME>/content-fabric-max-newsbot`, например `eshim88/content-fabric-max-newsbot`) |
| `VPS_APP_DIR` | необязательна; дефолт уже зашит в workflow — `/opt/maxnewsbot` |

### Secrets

| Secret | Назначение |
|---|---|
| `DOCKER_USERNAME` | пользователь DockerHub (например, `eshim88`) |
| `DOCKER_PASSWORD` | access token DockerHub |
| `VPS_HOST` | хост VPS |
| `VPS_USER` | SSH-пользователь |
| `VPS_SSH_KEY` | приватный SSH-ключ |
| `VPS_PORT` | порт SSH (опционально, дефолт 22) |

### Подготовка VPS (однократно)

- каталог `/opt/maxnewsbot/data/` создаётся workflow, права на него чинятся
  под uid 10001 автоматически;
- файл `/opt/maxnewsbot/.env` должен существовать (скопируйте заполненный
  `.env` на VPS) — workflow падает с понятной ошибкой, если его нет.

## Полезные команды бота

`/start`, `/chatid`, `/status`, `/stats` и `/factory
run|pause|resume|status|retract slot` — с аргументом рубрики или `all`
(например, `/factory pause weather`); `/factory run <рубрика>` запускает
сегодняшний слот вручную под теми же лимитами. Команды управления — только для
`ADMIN_IDS`.
