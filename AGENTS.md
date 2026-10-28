# AGENTS.md — content-fabric-max

## Что это за проект
Бот мессенджера MAX: ведёт автономный канал рубрик для аудитории старшего возраста
СПб и Ленобласти — суточная сетка (утро, погода, тема дня, второй пост, «хорошие
новости» из RSS, спокойной ночи), слотовый планировщик, банк контента, картинки
fal.ai. Единственный publishing-контур — рубрики (ADR-004); legacy-циклы
(линк-дайджест и контент-фабрика IT-новостей из Habr/vc.ru) удалены. Текст постов
пишется двухканальным LLM: OpenRouter первичный, Yandex AI Studio резервный
(per-request failover). Стек: Python 3.12, aiohttp + feedparser (без aiogram —
собственный тонкий клиент MAX и long polling), SQLite; зависимости — uv, тесты —
pytest. Деплой: Docker Compose на VPS через GitHub Actions
(`.github/workflows/docker.yml`).

## Структура
| Путь | Что это | Можно менять? |
|---|---|---|
| `max_news_bot/main.py` | Точка входа бота (старт единственного контура) | да |
| `max_news_bot/max_api/` | Тонкий aiohttp-клиент MAX Bot API (клиент + ошибки) | да |
| `max_news_bot/bot/` | Long polling, хендлеры, слотовый планировщик, рантайм рубрик, сводка, бэкап | да |
| `max_news_bot/rubrics/` | Пайплайны рубрик (generative, digest, фильтры новостей) | да |
| `max_news_bot/bank/` | Банк контента: отбор, антиповтор, валидатор | да |
| `max_news_bot/weather/` | Погода: Open-Meteo с резервом met.no, шаблон, валидатор | да |
| `max_news_bot/images/` | Генерация картинок fal.ai (flux/dev) | да |
| `max_news_bot/parsers/` | RSS-парсеры (`SourceConfig` + `Fetcher` в `base.py`) | да |
| `max_news_bot/publishers/` | Публикация слотов в MAX (`slot.py`), алерты | да |
| `max_news_bot/writers/` | LLM-клиент и failover (`completion.py`, `fallback.py`), проба каналов, валидатор | да |
| `max_news_bot/core/` | Модели данных (NewsItem, SlotRun, банк и пр.) | да |
| `max_news_bot/utils/` | БД и репозитории, текстовые хелперы, форматтер, логирование | да |
| `max_news_bot/config/rubrics.yaml` | Сетка рубрик и LLM-каналы — конфиг контура (обязателен) | да |
| `max_news_bot/config/news_sources.yaml` | «Мягкие» RSS-ленты рубрики «Хорошие новости» | да |
| `max_news_bot/data/` | Рабочая SQLite-БД (в git не коммитится) | нет (gitignore) |
| `max_news_bot/tests/` | pytest-тесты | да |
| `infra/` | Compose-файлы продакшена (VPS) | да |
| `.github/workflows/` | CI/CD деплой на VPS | да |
| `ARCHITECTURE.md` | Архитектурные решения (ADR-001–ADR-004) | docs-only |

## Ключевые правила
- MVP работает только на RSS-источниках; парсинг HTML отвергнут окончательно
  (ADR-004), откат — только через новый ADR.
- Платформенные решения порта (тонкий клиент вместо SDK, long polling вместо
  webhook) — ADR-001; двухканальный LLM — ADR-002; единый контур рубрик — ADR-004.
- Секреты (`MAX_BOT_TOKEN`, `OPENROUTER_API_KEY`, `LLM_API_KEY`, `FAL_KEY`) —
  только в `.env`; в git не попадают.
- Результат валидируется: `uv run pytest` из корня репозитория.

## Соглашения
- Язык: код и коммиты — английский; документация — русский.
- Деплой/пуш — только с подтверждения пользователя (Level 0 + project.yaml).
