# Task: yandexart-fallback-image-provider

- Дата: 2026-11-01
- Проект: content-fabric-max
- Ветка: main (прямая работа по plan mode; feature-ветка не создавалась — история репозитория ведётся прямыми коммитами в main)
- Статус: done

## Задача
Встроить YandexART (Yandex AI Studio Images API) как резервный провайдер
контура картинок с фиксированным приоритетом fal → yandex_art (ADR-003),
реализовав контракт, зафиксированный заглушкой YandexArtStub.

## Требования
- Клиент YandexArtGenerator по контракту стаба (research 2026-10-09 §2):
  синхронный POST {base}/images/generations, art://<folder_id>/<model>,
  ответ data[0].b64_json, авторизация Bearer → при 401 один повтор
  Api-Key + OpenAI-Project, без транзиентных ретраев (не идемпотентно).
- Провайдер активен при LLM_API_KEY + парсимом folder_id из
  LLM_FALLBACK_MODEL; новых env-переменных нет.
- Наблюдаемость: события image_fal_ok / image_art_used по провайдеру,
  config-алерты с дневным дедупом, combined-ошибка при отказе обоих.
- Fail-fast: рубрика image: generate требует хотя бы один провайдер.

## Ограничения
- Секреты только в .env (ключи не в логах/ошибках — тест на отсутствие утечки).
- YandexART ходит по прямой сессии, минуя socks-прокси (ADR-005).
- Ключ yandex_art.model не добавляется в поставляемый yaml (rollback-совместимость
  со старым парсером).
- Протокол ImageGenerator и вайринг не меняются.

## Acceptance criteria
- [x] YandexArtGenerator реализует контракт стаба, стаб удалён
- [x] Пара fal → yandex_art вайрится во всех конфигурациях (оба/fal-only/art-only),
      наблюдатель на wrapper'е в каждой из них
- [x] WARNING yandex_art_inactive при ключе без парсимом folder_id
- [x] Сводка: «картинки: fal A / art B / без картинки C»
- [x] uv run pytest зелёный (830 passed)
- [x] ADR-003/005, README, .env.example, rubrics.yaml обновлены

## Изменяемые файлы
- max_news_bot/images/yandex_art.py, images/fallback.py, images/__init__.py
- max_news_bot/config/rubrics.py, config/rubrics.yaml
- max_news_bot/bot/rubrics_runtime.py, bot/daily_summary.py
- max_news_bot/rubrics/generative.py, bank/pipeline.py
- max_news_bot/tests/test_yandex_art.py (новый), test_fal_images.py,
  test_rubrics_runtime.py, test_rubrics_config.py, test_generative_pipeline.py,
  test_bank_pipeline.py, test_daily_summary.py
- ARCHITECTURE.md, README.md, max_news_bot/.env.example

## Итог
Сделано по утверждённому плану (сессия plan mode) со всеми правками
adversarial-review (8 находок: наблюдатель во всех конфигурациях,
combined-ошибка, наследование base_url, WARNING при неактивном art,
явная политика повторов, модель вне yaml, проверка b64 до декодирования,
валидация size WxH). План ревью пройден, код — нет (ревью кода не запрашивалось).

Отклонений от плана нет. Известные долги:
- Живой smoke не выполнялся (нет ключей в окружении разработки): первая
  реальная генерация на VPS (~2,23 ₽) закроет вопрос Bearer vs Api-Key;
  при 404 модели aliceai-image-art-3.0 — переопределить yandex_art.model
  в yaml значением yandex-art/latest.
- Пуш в main запускает деплой через .github/workflows/docker.yml —
  контейнер на VPS перезапустится с текущим .env сервера.

Коммит: feat: yandexart fallback image provider (main).
