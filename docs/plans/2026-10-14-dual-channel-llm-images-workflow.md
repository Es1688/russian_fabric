# Workflow субагентов для двухканальных LLM и изображений

Дата: 2026-10-17. Статус: черновик на согласование.

Реализует план `docs/plans/2026-10-14-dual-channel-llm-images.md` (LLM:
OpenRouter → Yandex GPT; изображения: fal, YandexART — заглушка; наблюдаемость на
`bot_stats`). Механизм: dynamic workflow (CreateWorkflow), субагенты работают по
кодовой базе репозитория, детерминированные гейты — командами. Деплой и правки
`.env` VPS (§4 плана) — вне workflow, вручную оператором.

## 1. Разделение ответственности

| Кто | Что |
|---|---|
| workflow | код и тесты обеих веток, наблюдаемость, документация, независимая проверка, отчёт |
| оператор | подтверждение запуска workflow, ключи в `.env` VPS после внедрения, коммит/пуш/деплой (с подтверждения) |
| ведущий агент (лид) | запуск workflow, разбор эскалаций, финальный прогон pytest, diff-ревью, коммит через git-finish-task |

Правила безопасности внутри workflow:

- субагенты не выполняют git-коммитов и пушей (фиксируется в промптах); изменения
  остаются в рабочей копии до ревью лидом;
- `world.run` ограничен `uv run pytest` — сетевых установок, Docker и обращения к
  реальным API нет; все внешние сервисы — локальные фейк-серверы (aiohttp.web), сеть
  не нужна (правило `addopts "-m 'not network and not llm'"` уже соблюдает это);
- секреты из `.env` субагенты не читают и не выводят; ключи в тестах не используются;
- боевая БД и `max_news_bot/data/` не трогаются (тесты используют tmp-пути/фикстуры,
  как в существующих `conftest.py`/`mocks.py`).

## 2. Структура: один запуск, два параллельных трека

Фазы исследования нет — план согласован, research (fal-протокол, YandexART, payload
OpenRouter) уже зафиксирован в `docs/plans/2026-10-14-dual-channel-llm-images.md` и
`2026-10-09-epoch0-research.md`. Один запуск `mode=build`: DAG из двух независимых
треков (LLM и изображения), общая фаза наблюдаемости, документация, независимая
проверка, отчёт.

Параллельность безопасна: внутри фазы субагенты не пересекаются по файлам (границы
перечислены в таблице ниже); пересекающиеся файлы (`sources.yaml`, `.env.example`,
`rubrics.yaml`, `pyproject` не трогаем) всегда оказываются в последовательных фазах.

## 3. Фазы и субагенты

Общий паттерн фазы: параллельные реализаторы (fresh subagent на модуль) → быстрый гейт
(targeted pytest) на каждого → сильный гейт фазы (полный pytest) → ремонт-цикл
(лимит 2 итерации, дальше эскалация лиду и стоп зависимых фаз).

### Трек LLM

| Фаза | Субагенты (параллельность) | Содержание (§ плана) | Файлы (границы) | Зависимости |
|---|---|---|---|---|
| L1 «Конфиг и транспорт» | impl-llm-config ∥ impl-llm-transport | `disable_reasoning` в `LlmConfig`; дефолты `factory.llm` → OpenRouter (`qwen/qwen3.8-flash`), секция `factory.llm_fallback` (Yandex), env `OPENROUTER_API_KEY`/`LLM_FALLBACK_*`, fail-fast на оба ключа (§2.1); флаг `disable_reasoning` в `LlmWriter`/`LlmCompletionClient` (§2.2) | impl-llm-config: `config/loader.py`, `config/sources.yaml`, `.env.example`, `tests/test_loader.py`; impl-llm-transport: `writers/llm.py`, `writers/completion.py`, `tests/test_llm_writer.py` | — |
| L2 «Обёртки и проба» | impl-fallback-writers ∥ impl-readiness | `writers/fallback.py`: `FallbackWriter`/`FallbackCompletion`, события `bot_stats` (`llm_primary_ok`/`llm_fallback_used`/ошибки по kind), хук admin-алерта на config-ошибку любого канала с in-memory дедупом ≤1/сутки (§2.3, §2.6); readiness-проба обоих LLM-каналов (`max_tokens=1`; config-kind → ValueError, infra-kind → WARNING) + wiring в `main.py` и `rubrics_runtime.py`, обновлённый докстринг (§2.1, §2.4) | impl-fallback-writers: `writers/fallback.py`, `tests/test_fallback_llm.py`; impl-readiness: `main.py`, `bot/rubrics_runtime.py`, `tests/test_main_contour.py`, `tests/test_rubrics_runtime.py` | L1 |

### Трек изображений (независим от LLM, стартует параллельно с L1)

| Фаза | Субагенты | Содержание (§ плана) | Файлы (границы) | Зависимости |
|---|---|---|---|---|
| I1 «Клиенты и конфиг» | impl-fal-client ∥ impl-image-config | пакет `images/`: протокол `ImageGenerator`, `FalImageGenerator` (queue.fal.run, поллинг, `Authorization: Key`, ретраи на транзиентное, `MAX_IMAGE_BYTES` 10 МБ, потоково), `YandexArtStub`, `FallbackImageGenerator(primary, fallback=None)` (§3.1); секция `image_generation` в rubrics.yaml + `image: generate` + `_TOP_LEVEL_KEYS` + fail-fast `FAL_KEY` (§3.2) | impl-fal-client: `images/*`, `tests/test_fal_images.py`; impl-image-config: `config/rubrics.py`, `config/rubrics.yaml`, `tests/test_rubrics_config.py` | — |
| I2 «Пайплайны и lifecycle» | impl-image-pipelines | генерация в `BankPipeline._postcard_ref`/`GenerativePipeline` (промпт `image_style`+тема ≤500, bytes → `data/generated/<run_id>.png`, `image_ref="generated:<файл>"`, события `bot_stats`, сбой → text-only + alert); lifecycle: удаление после успешной публикации, TTL-prune 7 дней в ежедневном обслуживании (§3.3) | `bank/pipeline.py`, `rubrics/generative.py`, `bot/db_backup.py` (или соседний сервис обслуживания), `tests/test_bank_pipeline.py`, `tests/test_generative_pipeline.py`, `tests/test_db_backup.py` | I1 |
| I3 «Publisher» | impl-slot-publisher | ветка `generated:`-ссылок в `_postcard_attachments`, path-traversal-защита, деградация text-only при локальных проблемах / infra при транспортных (§3.4); удаление файла после успешной publish | `publishers/slot.py`, `tests/test_slot_publisher.py` | I2 |

### Общие фазы

| Фаза | Субагенты | Содержание | Файлы | Зависимости |
|---|---|---|---|---|
| X1 «Наблюдаемость» | impl-observability | строка каналов в ежедневной сводке из `bot_stats` («LLM: primary N / fallback M; картинки: fal A / без картинки B»), рядом с существующей `_llm_text` (§2.6) | `bot/daily_summary.py`, `tests/test_daily_summary.py` | L2 + I2 |
| D1 «Документация» | impl-docs | README (env-таблица: `OPENROUTER_API_KEY`, `LLM_FALLBACK_*`, комментарий к `LLM_API_KEY`, `FAL_KEY`), `ARCHITECTURE.md` (ADR-002, ADR-003), финальный `.env.example`, комментарии `sources.yaml`/`rubrics.yaml` (§4) | `README.md`, `ARCHITECTURE.md`, `.env.example`, `config/sources.yaml`, `config/rubrics.yaml` | X1 |
| V1 «Независимая проверка» | reviewer → confirmer ∥ fixer | свежий ревьюер сверяет полный diff с планом (полнота §2–§4, отклонения, новые баги); high-находки подтверждает независимый confirmer по коду; подтверждённые — fixer | — | D1 |
| Отчёт | — (скрипт) | markdown-отчёт: что сделано по фазам, coverage плана, verified/notCovered | артефакт + `return WorkflowReport` | V1 |

## 4. Гейты (world.run)

| Уровень | Команда | Когда |
|---|---|---|
| быстрый | `uv run pytest max_news_bot/tests/<тестируемый модуль>` | после каждого реализатора |
| сильный | `uv run pytest` (полный suite) | конец каждой фазы |
| финальный | `uv run pytest` + тест startup-валидации: валидный конфиг с обеими секциями грузится; нет `OPENROUTER_API_KEY`/`LLM_API_KEY` при активном LLM-контуре → ValueError; нет `FAL_KEY` при `image: generate` → ValueError | после D1, до V1 |

В репозитории нет линтера; полный pytest — сильнейшая проверка (Docker-сборку лид
прогоняет после workflow отдельно, если нужно).

## 5. Типизированные результаты

- `StageResult`: `files_changed[]`, `tests_added[]`, `plan_items_covered[]` (ссылки
  на пункты § плана вида «§2.1 fail-fast»), `notes` — у каждого реализатора;
- `ReviewFinding`: `where`, `what`, `evidence`, `severity` (low/medium/high),
  `status` (verified/unconfirmed) — у ревьюера/confirmer'а;
- финальный `return` — `WorkflowReport { conclusion, findings[], verified[],
  notCovered[] }`.

`plan_items_covered` нужен ревьюеру V1: он сверяет ссылки с §2–§4 плана и ловит
пропущенное.

## 6. Артефакты

- board «Прогресс реализации»: карточка на фазу, статусы (делается/готово/ремонт/блок);
- markdown-отчёт (primary) — итоговый, длинная форма того, что в `return`.

## 7. Аргументы workflow

Обязательных аргументов нет: `mode=build` единственный, план и research уже в
репозитории. Зарезервирован необязательный `mode` (`build`) на случай будущих фаз.

## 8. Эскалации и отказы

- невозможный гейт / противоречивые инструкции → субагент эскалирует лиду (не «прёт»
  до лимита и не имитирует успех — фиксируется в промптах);
- падение провайдера ретраится рантаймом workflow само; эскалации это не дублируют;
- упавший запуск чинится правкой скрипта + AmendWorkflow (кэш по именам агентов),
  не перезапуском с нуля.

## 9. Критерии готовности

- все фазы зелёные, полный `uv run pytest` проходит (включая новые тесты fallback,
  readiness, fal-клиента, заглушки, пайплайнов, publisher, наблюдаемости, конфигов);
- ревьюер V1 подтвердил покрытие всех пунктов §2–§4 плана (или отклонения
  перечислены и объяснены);
- ни одной незакрытой high-находки; unconfirmed-находки перечислены в отчёте;
- события `bot_stats` и строка сводки покрыты тестами; `.env.example`, README и
  ARCHITECTURE.md обновлены.

После этого: лид прогоняет полный pytest ещё раз, ревьюит diff, коммит по
git-finish-task с подтверждения оператора. Правки `.env` на VPS (перенос
`LLM_MODEL` → `LLM_FALLBACK_MODEL`, удаление `LLM_BASE_URL`, добавление
`OPENROUTER_API_KEY` и `FAL_KEY` — §4 плана) оператор делает вручную при деплое.
