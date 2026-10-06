# Content Fabric MAX — News Bot (MVP)

Порт Telegram-бота [content_fabric](https://github.com/) на мессенджер **MAX**:
собирает новости из RSS-источников (Habr, vc.ru), фильтрует по ключевым словам,
дедуплицирует в SQLite и публикует в канал MAX. Контент-фабрика дополнительно
пишет оригинальные посты через Yandex AI Studio (OpenAI-совместимый API,
модель `yandexgpt-5-lite`).

Платформы доставки — российские (MAX, Yandex Cloud): прокси не нужны, бот и LLM
доступны с VPS в РФ напрямую. Ключевые решения порта — в
[ARCHITECTURE.md](ARCHITECTURE.md), раздел ADR-001.

## Состав репозитория

- `max_news_bot/` — код бота (точка входа `max_news_bot/main.py`,
  запуск `python -m max_news_bot.main`)
  - `max_api/` — тонкий aiohttp-клиент MAX Bot API
  - `bot/` — long polling (`polling.py`), команды, планировщик
  - `parsers/`, `core/`, `publishers/`, `writers/`, `utils/`, `config/`
- `infra/` — Docker Compose для продакшена (VPS)
- `.github/workflows/docker.yml` — CI/CD деплой на VPS из ветки `main`
- [ARCHITECTURE.md](ARCHITECTURE.md) — архитектурные решения (включая ADR-001)

## Стек

Python 3.12, aiohttp + feedparser (без aiogram — собственный тонкий клиент MAX),
SQLite, uv, pytest. Деплой: Docker (образ `python:3.12-slim`, непривилегированный
пользователь) → DockerHub → VPS через GitHub Actions.

## Быстрый старт

Требуются [uv](https://docs.astral.sh/uv/) и Python 3.12+.

```bash
uv sync                                          # зависимости
cp max_news_bot/.env.example max_news_bot/.env   # заполнить MAX_BOT_TOKEN, MAX_CHANNEL_ID, ...
uv run python -m max_news_bot.main               # запуск
uv run pytest                                    # тесты
```

## Подготовка (prerequisites)

### MAX

1. **Создать бота**: напишите [@masterbot](https://dev.max.ru) в MAX, создайте
   бота и получите токен → `MAX_BOT_TOKEN` в `.env`.
2. **Канал**: создайте канал и добавьте бота **администратором с правом
   публикации** сообщений.
3. **Узнать chat_id**: в проекте есть команда `/chatid` — вызовите её
   (например, в канале, куда добавлен бот) и подставьте целое число в
   `MAX_CHANNEL_ID`.
4. **Админы**: перечислите ID в `ADMIN_IDS` (через запятую). Каждый админ
   обязан **один раз написать боту `/start`** — в MAX бот не может инициировать
   личную переписку, без этого не доставятся превью фабрики и алерты.

### Yandex AI Studio (для контент-фабрики)

1. В [Yandex Cloud](https://console.yandex.cloud) подключите платный
   **billing-аккаунт** (бесплатного tier нет) и создайте API-ключ AI Studio →
   `LLM_API_KEY`.
2. Скопируйте `folder_id` каталога и соберите имя модели → `LLM_MODEL`:
   `gpt://<folder_id>/yandexgpt-5-lite`.
3. `LLM_BASE_URL` уже вписан в `.env.example`:
   `https://ai.api.cloud.yandex.net/v1` (OpenAI-совместимый endpoint).

## Переменные окружения

Полный список с комментариями — в [max_news_bot/.env.example](max_news_bot/.env.example).

| Переменная | Назначение |
|---|---|
| `MAX_BOT_TOKEN` | токен бота MAX от @masterbot |
| `MAX_CHANNEL_ID` | chat_id канала MAX (целое число, см. `/chatid`) |
| `ADMIN_IDS` | ID админов через запятую; каждый должен написать боту `/start` |
| `LOG_LEVEL` | уровень логирования (`INFO`) |
| `POST_INTERVAL_HOURS` | интервал дайджест-цикла в часах |
| `REQUEST_TIMEOUT` | таймаут HTTP-запросов (RSS/MAX/LLM), сек |
| `DATABASE_PATH` | путь к SQLite (в Docker: `/app/data/max_news_bot.db`) |
| `LLM_API_KEY` | ключ Yandex AI Studio |
| `LLM_BASE_URL` | `https://ai.api.cloud.yandex.net/v1` |
| `LLM_MODEL` | `gpt://<folder_id>/yandexgpt-5-lite` |
| `FACTORY_ENABLED` | включает/выключает фабрику |
| `FACTORY_DRY_RUN` | `true` — превью админам, `false` — публикация в канал |
| `FACTORY_INTERVAL_HOURS` | интервал цикла фабрики в часах |
| `FACTORY_ACTIVE_HOURS` | окно активности фабрики для healthcheck (`08:00-23:00`) |

Секреты (`MAX_BOT_TOKEN`, `LLM_API_KEY`) живут только в `.env` и в git не попадают.

## Контент-фабрика

Второй, независимый от дайджеста цикл: отбор новостей по своим ключевым словам →
LLM (Yandex AI Studio) переписывает в оригинальный пост → публикация в тот же
канал MAX. У фабрики свой интервал, своя дедупликация и свои лимиты. Подробности —
в [ARCHITECTURE.md](ARCHITECTURE.md), раздел «Контент-фабрика».

### Безопасная раскатка

Автопубликация включается только после раскатки; переход на следующий этап —
только при выполнении критериев предыдущего:

1. **Dry-run** (`FACTORY_DRY_RUN=true`): посты генерируются и уходят админам в
   личку, а не в канал. Критерии выхода: не менее 20 постов; владелец оценил бы
   ≥90% как «опубликовал бы»; 0 ложных срабатываний валидатора; 0 полностью
   неудачных циклов (`written == 0` и `failed > 0`).
2. **Тестовый канал**: `MAX_CHANNEL_ID` указывает на тестовый канал; 2–3 дня с
   теми же критериями; `/factory retract` проверен вручную.
3. **Прод**: `posts_per_cycle: 1`, неделя без инцидентов — затем постепенное
   увеличение.

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

`/status`, `/sources`, `/stats`, `/news` (ручной сбор), `/chatid`,
`/factory run|pause|resume|status|retract`, `/posts`. Команды управления —
только для `ADMIN_IDS`.
