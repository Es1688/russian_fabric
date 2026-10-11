# Task: autonomous-channel

- Дата: 2026-10-10
- Проект: content-fabric-max
- Ветка: feature/autonomous-channel
- Статус: done

## Задача
Реализовать «Автономный канал в MAX» — суточную сетку рубрик (утро, погода,
тема дня, второй пост, хорошие новости, ночь) со слотовым планировщиком,
банком контента и наблюдаемостью — по плану
`docs/plans/2026-10-09-autonomous-channel.md` (Этапы 1–5; Этап 0 — исследование
API/источников — выполнен отдельным прогоном, отчёт `docs/plans/2026-10-09-epoch0-research.md`).

## Требования
- Миграция v4: slot_runs, digest_items, rubric_state, content_bank, лог фильтров.
- Слотовый планировщик: grace, порядок слотов, UNIQUE-захват, publishing/unknown_delivery,
  recovery без повторной отправки вслепую, выключатели.
- Погода: Open-Meteo batch на 7 точек, резерв met.no, правила/шаблон/валидатор, восход/закат.
- Банк контента: сезонность, event_date, антиповтор (Jaccard), алерт запаса.
- Новости: сбор (news_sources.yaml), цепочка фильтров, выпуск 3–4 пунктов, деградация.
- Картинки: загрузка в MAX (POST /uploads → CDN → token), режим bank.
- Коммутация: RUBRICS_ENABLED (fail-fast «ровно один контур»), dry-run, сводка 22:40, бэкап.
- Независимое ревью реализации, отчёт `docs/plans/2026-10-09-impl-report.md`.

## Ограничения
- RSS-only (ADR в ARCHITECTURE.md), секреты только в .env, коммит/пуш — с подтверждения.

## Acceptance criteria
- [x] Полный `uv run pytest` зелёный в конце каждой фазы и финально (852 passed).
- [x] Независимый ревьюер: 1 high-находка подтверждена и исправлена (smoke_test при daily_cap 6).
- [x] Все 8 фаз реализации зелёные (доска progress).
- [ ] Тестовая неделя (Этап 6) — вручную оператором, вне этой задачи.

## Изменяемые файлы
- Новые пакеты: weather/, bank/, rubrics/, bot/slot_scheduler.py, bot/daily_summary.py,
  bot/rubrics_admin.py, bot/rubrics_runtime.py, bot/db_backup.py, max_api/uploads.py,
  publishers/slot.py, config/rubrics.{py,yaml}, config/news_sources.{py,yaml},
  utils/repositories.py, writers/completion.py.
- Изменены: main.py (коммутация контуров), max_api/client.py+errors.py, config/loader.py
  (fail-fast валидация), bot/handlers.py (аргумент рубрики в /factory), utils/db.py
  (миграция v4), writers/validator.py, core/models.py, scripts/healthcheck.py.
- Тесты: +26 файлов, suite 296 → 852.
- Документы: README.md, .env.example, docs/plans/* (план, workflow-план, отчёты).

## Итог
Этапы 1–5 реализованы workflow-прогоном (10 фаз, гейты uv run pytest после каждой),
независимая проверка подтвердила покрытие плана; 1 high (конфликт smoke_test/daily_cap)
исправлен в ходе прогона. Остались известные долги (из ревью, не блокируют dry-run):
recovery может опубликовать вчерашний generated-слот (medium), дайджест превышает
бюджет §7 3 LLM-вызова (medium), возможен дубль при ручном /factory run во время
фонового тика (medium). Этап 6 (тестовая неделя) и наполнение банка — за оператором.
Коммит: feature/autonomous-channel → squash в main (см. git log), push выполнен
с явного разрешения оператора.
