# AGENTS.md — content-fabric-max

## Что это за проект
Бот мессенджера MAX (MVP) — порт Telegram-бота content_fabric: собирает новости из
RSS-источников (Habr, vc.ru), фильтрует их по ключевым словам, дедуплицирует в SQLite
и публикует в канал MAX; контент-фабрика пишет посты через Yandex AI Studio
(OpenAI-совместимый API, yandexgpt-5-lite). Стек: Python 3.12, aiohttp + feedparser
(без aiogram — собственный тонкий клиент MAX и long polling), SQLite; зависимости —
uv, тесты — pytest. Деплой: Docker Compose на VPS через GitHub Actions
(`.github/workflows/docker.yml`).

## Структура
| Путь | Что это | Можно менять? |
|---|---|---|
| `max_news_bot/main.py` | Точка входа бота | да |
| `max_news_bot/max_api/` | Тонкий aiohttp-клиент MAX Bot API (клиент + ошибки) | да |
| `max_news_bot/bot/` | Long polling (`polling.py`), хендлеры команд и планировщик | да |
| `max_news_bot/parsers/` | RSS-парсеры источников (стратегия `base.py`) | да |
| `max_news_bot/publishers/` | Публикация в MAX (интерфейс `base.py`) | да |
| `max_news_bot/writers/` | LLM-писатель постов (Yandex AI Studio) и валидатор | да |
| `max_news_bot/core/` | Модели и пайплайн обработки (дайджест + фабрика) | да |
| `max_news_bot/utils/` | БД, фильтры, форматтер, логирование | да |
| `max_news_bot/config/sources.yaml` | Список RSS-источников и настройки | да |
| `max_news_bot/data/` | Рабочая SQLite-БД (в git не коммитится) | нет (gitignore) |
| `max_news_bot/tests/` | pytest-тесты | да |
| `infra/` | Compose-файлы продакшена (VPS) | да |
| `.github/workflows/` | CI/CD деплой на VPS | да |
| `ARCHITECTURE.md` | Архитектурные решения (включая ADR-001) | docs-only |

## Ключевые правила
- MVP работает только на RSS-источниках; отказ от парсинга HTML — архитектурное решение
  (см. `ARCHITECTURE.md`), откат — только через ADR.
- Платформенные решения порта (тонкий клиент вместо SDK, long polling вместо webhook,
  OpenAI-совместимый endpoint Yandex) — ADR-001 в `ARCHITECTURE.md`.
- Секреты (`MAX_BOT_TOKEN`, `LLM_API_KEY` и пр.) — только в `.env`; в git не попадают.
- Результат валидируется: `uv run pytest` из корня репозитория.

## Соглашения
- Язык: код и коммиты — английский; документация — русский.
- Деплой/пуш — только с подтверждения пользователя (Level 0 + project.yaml).
