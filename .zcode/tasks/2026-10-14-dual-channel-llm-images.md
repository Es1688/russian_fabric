# Task: dual-channel-llm-images

- Дата: 2026-10-14
- Проект: content-fabric-max
- Ветка: feature/dual-channel-llm-images
- Статус: done

## Задача
Две независимые цепочки отказоустойчивости: текст — каждый LLM-вызов сначала
идёт в OpenRouter (qwen/qwen3.8-flash), при любой ошибке — в Yandex GPT
(резерв); картинки — генерация в fal.ai (fal-ai/flux/dev), при сбое —
деградация поста до текстового. YandexART — заглушка до будущей задачи.
План: docs/plans/2026-10-14-dual-channel-llm-images.md (согласован,
adversarial-ревью), workflow субагентов:
docs/plans/2026-10-14-dual-channel-llm-images-workflow.md.

## Требования
- §2 (LLM): factory.llm → дефолты OpenRouter + disable_reasoning; новая секция
  factory.llm_fallback (Yandex, ключ LLM_API_KEY, env LLM_FALLBACK_*);
  fail-fast на ОБА ключа при активном LLM-контуре; readiness-проба каналов
  на старте (config-kind → ValueError, infra → WARNING и продолжение);
  FallbackWriter/FallbackCompletion (per-request failover без состояния);
  события bot_stats (llm_primary_ok/llm_fallback_used/ошибки по kind) +
  немедленный config-алерт с дедупом ≤1/сутки на канал; строка каналов
  в ежедневной сводке.
- §3 (изображения): пакет images/ (протокол ImageGenerator, FalImageGenerator
  с поллингом queue.fal.run и ретраями на транзиентное, YandexArtStub,
  FallbackImageGenerator, MAX_IMAGE_BYTES 10 МБ потоково); секция
  image_generation в rubrics.yaml + значение image: generate + fail-fast
  FAL_KEY; генерация в Bank/Generative пайплайнах
  (image_ref="generated:…", сбой → text-only + alert, TTL-prune 7 дней);
  publisher: ветка generated:-ссылок с path-traversal-защитой и удалением
  файла после успешной публикации.
- §4: README (env-таблица), ARCHITECTURE.md (ADR-002, ADR-003), .env.example.

## Ограничения
- RSS-only (ADR-001); секреты только в .env; max_news_bot/data/ не трогать;
  без новых зависимостей (aiohttp/pytest); сеть в тестах отключена
  (локальные фейк-серверы).
- Деплой и правки .env на VPS — вне задачи (оператор при деплое).

## Acceptance criteria
- [x] Полный uv run pytest зелёный: 954 passed, 1 skipped (+88 теста к базе 866).
- [x] Независимый ревьюер workflow сверил дифф с планом §2–§4; все три
  находки закрыты и перепроверены лидом: high — wiring генератора в
  build_rubrics_runtime; medium — передача db=/alerter= в пайплайны;
  low — дедуп config-алерта только после успешной отправки.
- [x] .env.example, README, ARCHITECTURE.md обновлены (ADR-002/ADR-003).

## Изменяемые файлы
- config/ (loader.py, sources.yaml, rubrics.py, rubrics.yaml), writers/
  (llm.py, completion.py, fallback.py, readiness.py), images/ (новый пакет),
  bank/pipeline.py, rubrics/generative.py, publishers/slot.py,
  bot/ (rubrics_runtime.py, daily_summary.py, db_backup.py), main.py,
  тесты (test_loader, test_llm_writer, test_fallback_llm, test_main_contour,
  test_rubrics_runtime, test_rubrics_config, test_fal_images,
  test_bank_pipeline, test_generative_pipeline, test_db_backup,
  test_slot_publisher, test_daily_summary).

## Итог
Реализовано dynamic workflow субагентов (run dwfrun-acd0878d…, 8 фаз,
гейты uv run pytest, независимый ревью + подтверждающие + фиксеры) и ручной
фикс лида для low-находки (дедуп алерта). Отклонений от плана нет; YandexART —
заглушка по решению оператора от 2026-10-17. Долги: правки .env на VPS при
деплое (добавить OPENROUTER_API_KEY и FAL_KEY, удалить LLM_BASE_URL,
перенести LLM_MODEL → LLM_FALLBACK_MODEL) — оператор; полноценный клиент
YandexART — будущая задача. Docker-сборку перед деплоем прогнать отдельно.
