# Task: rubrics-only-contour

- Дата: 2026-10-28
- Проект: content-fabric-max
- Ветка: feature/rubrics-only-contour
- Статус: done

## Задача

Сделать контур рубрик единственным publishing-контуром бота MAX: удалить
legacy-контур (линк-дайджест Pipeline + контент-фабрика ContentFactory,
IT-источники Habr/vc.ru) и переключатели RUBRICS_ENABLED/FACTORY_ENABLED.
Тематика канала — одна: контент для читателей старшего возраста. Сетка
рубрик, банк контента, схема БД и деплой не меняются.

## Требования

- `main.py` запускает только контур рубрик; RUBRICS_ENABLED/FACTORY_* нигде
  не читаются (включая scripts/healthcheck.py)
- `sources.yaml` удалён; LLM-каналы настроены в `rubrics.yaml`
  (`llm:`/`llm_fallback:`), ключи только в env (OPENROUTER_API_KEY,
  LLM_API_KEY)
- `SourceConfig` перенесён в `parsers/base.py` (нужен CompositeFetcher и
  `as_source_config` digest-рубрики)
- `scripts/healthcheck.py`: digest/factory правила удалены, rubrics-правило
  безусловно; оба compose-файла живы
- Legacy-модули и мёртвые методы БД удалены; импорт-граф чист
- Команды: остаются /start /chatid /status /stats /factory
  run|pause|resume|status|retract slot; удаляются /sources, /news, /posts,
  /factory retract <post_id>

## Ограничения

- Секреты — только в .env, в git не попадают
- RSS-only; HTML-парсеры удаляются в сторону уже принятого решения (не откат)
- Таблицы posts/sent_news в рабочей БД не мигрируются и не удаляются
- Деплой и push — только с явного разрешения пользователя (Level C)
- Untracked `reports/` не подмешивается к задаче (остаётся на месте)

## Acceptance criteria

- [ ] main.py запускает только контур рубрик; RUBRICS_ENABLED/FACTORY_* нигде не читаются
- [ ] sources.yaml удалён; LLM-каналы в rubrics.yaml, ключи только в env
- [ ] scripts/healthcheck.py проверяет только слоты; compose-файлы живы
- [ ] legacy-модули и мёртвые методы БД удалены; test_architecture зелёный
- [ ] `uv run pytest` — весь набор зелёный
- [ ] smoke: RUBRICS_DRY_RUN=true — слот выполняется, превью админам;
      python max_news_bot/scripts/healthcheck.py на тестовой БД возвращает 0
- [ ] README / ARCHITECTURE.md / AGENTS.md обновлены

## Изменяемые файлы

- Конфиг: rubrics.yaml, config/rubrics.py, config/loader.py, sources.yaml (удалить)
- Рантайм: main.py, bot/rubrics_runtime.py, bot/handlers.py, writers/readiness.py
- Парсеры: parsers/base.py, parsers/composite.py, parsers/habr.py (удалить),
  parsers/vc_ru.py (удалить)
- Writers: writers/base.py, writers/fallback.py, writers/llm.py (удалить)
- Core/publishers: core/factory.py (удалить), core/pipeline.py (удалить),
  bot/scheduler.py (удалить), publishers/post.py (удалить),
  publishers/max.py (удалить)
- Utils: utils/filters.py (удалить), utils/text.py, utils/db.py
- Скрипты: scripts/healthcheck.py
- Тесты: tests/* (список в плане), conftest.py, mocks.py
- Доки: README.md, ARCHITECTURE.md, AGENTS.md, .zcode/project.yaml (новый)

## Итог

Сделано (2026-10-29, ветка feature/rubrics-only-contour):

- Конфиг: LLM-каналы перенесены в `rubrics.yaml` (`llm:`/`llm_fallback:`,
  env-оверрайды сохранены); `sources.yaml` удалён; `config/loader.py` переписан
  до тонкой env-обёртки; `config/rubrics.py`: безусловные проверки (файл обязателен,
  есть включённая рубрика, оба LLM-ключа при LLM-рубриках), `enabled`-флаг и
  `empty_rubrics_config` удалены.
- `SourceConfig` перенесён в `parsers/base.py` (потребители: `CompositeFetcher`,
  `as_source_config` в `rubrics/news.py`, `parsers/rss.py`).
- `main.py` — единственный старт контура рубрик; `rubrics_runtime`/`readiness`
  читают каналы из `config.rubrics.llm` (LlmChannelsConfig в `config/rubrics.py`).
- Удалены: `core/factory.py`, `core/pipeline.py`, `bot/scheduler.py`,
  `publishers/{post,max,base}.py`, `writers/llm.py`, `writers/base.py`,
  `FallbackWriter`, `utils/filters.py`, `parsers/{habr,vc_ru}.py` + html-ветка
  composite, `Post`/`PublishedResult` из моделей, мёртвые методы `db.py`
  (posts/sent_news/state-флаги). Транспортные хелперы и `SKIP_MARKER` переехали
  в `writers/completion.py`.
- Команды: `/start /chatid /status /stats /factory run|pause|resume|status|retract
  slot`; удалены `/sources /news /posts`, retract по post_id; `/stats` читает
  публикации из `slot_runs`; `/status` — heartbeat планировщика.
- `scripts/healthcheck.py`: только слоты (безусловные правила), digest/factory
  правила и их env-гейты удалены; оба compose-файла живы.
- Тесты: 7 файлов удалено, ~14 переработано; итог — 794 passed (двойной прогон).
- Доки: README, ARCHITECTURE.md (+ новый ADR-004, переписаны разделы фабрики),
  AGENTS.md, `.env.example`, новый `.zcode/project.yaml`.
- Smoke healthcheck: первый бут → 0, тихий день → 0, протухший heartbeat → 1.

Отклонения от плана:
- `AppConfig.llm_channels` как отдельное поле не вводилось — каналы лежат в
  `config.rubrics.llm` (один файл = один контур), семантика та же.
- Найдено при реализации: `parsers/rss.py` и `utils/db.py`-хелперы
  (`_parse_dt`/`_to_db_dt`/`_MAX_ERROR_LEN`, нужны репозиториям) тоже требовали
  переноса — учтено.
- Дополнительно к плану: починена негерметичность `test_architecture`
  (реальный `.env` с `RUBRICS_DRY_RUN` протекал в сессию pytest через
  незамоканный `load_dotenv`).

Известные долги:
- Полный dry-run smoke («слот выполняется, превью уходит админам») требует живого
  MAX_BOT_TOKEN — операторская проверка на VPS после деплоя (первый прогон в
  `RUBRICS_DRY_RUN=true`, банк контента и postcards на месте).
- Untracked `reports/` на main не тронут (не задача этой ветки).

Коммит: refactor: make rubrics the single publishing contour, drop legacy factory
