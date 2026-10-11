# Итоговый отчёт о реализации автономного канала (контур рубрик) — ревизия 2

- **Проект:** content-fabric-max (бот мессенджера MAX, порт Telegram-бота content_fabric).
- **План:** `docs/plans/2026-10-09-autonomous-channel.md` (разделы §2–§14).
- **Дата отчёта:** 2026-10-10. Ревизия 2 — устраняют замечания чтения ревизии 1 от 2026-10-10.
- **Источники и статус проверок.** Каркас фактов — данные, переданные оркестрирующим скриптом (статусы фаз, отчёты 16 шагов, гейты, находки ревью, пробелы, отклонения). В отличие от ревизии 1, все фактологические утверждения перепроверены чтением текущего кода репозитория (ссылки `путь:строка` — состояние на 2026-10-10) и прогонами, перечисленными в разделе 6. Что осталось от данных шагов (команды smoke/11-файлов/ephem-сверки), помечено как непереданное и невоспроизводимое по этому отчёту.

## 0. Сводка

Все контрольные гейты workflow пройдены (статусы переданы скриптом; что именно проверял каждый гейт, в задачу не передавалось):

| Гейт | Статус |
|---|---|
| Каркас: миграция и конфиг | ok |
| Каркас: планировщик | ok |
| Каркас: команды | ok |
| Погода | ok |
| Утро и ночь | ok |
| Новости | ok |
| Дневные рубрики | ok |
| Интеграция | ok |
| Финальная проверка после правок | ok |

**Единый прогон suite (выполнен при составлении этой ревизии):** `uv run pytest -q` → `852 passed, 1 skipped, 1 deselected, 2 warnings in 19.28s`, код возврата 0. На момент шага «Новости: выпуск» collect-only насчитывал 801 тест — сьюта выросла; 1 skipped — существующий LLM-тест, 1 deselected — исключённый маркером тест.

**Главное изменение против ревизии 1:** единственная подтверждённая находка ревью **П-1 (high) устранена в коде после ревью** — рубрика-заглушка `smoke_test` выключена в конфиге (`rubrics.yaml:212`), dry-run-превью исключены из подсчёта daily_cap (`bot/slot_scheduler.py:452-454`), добавлены два регресс-теста сценария «7 слотов при cap 6» (`tests/test_slot_scheduler.py:519,550`). По переданным данным состав правок финального гейта не раскрывался; судя по текущему коду, правка касалась именно П-1. Остальные 8 находок (3 medium, 5 low) перепроверены чтением кода и остаются открытыми (раздел 3); 7 пробелов реализации (раздел 4), тестовая неделя и деплой не начинались (раздел 7).

## 1. Что сделано (по разделам плана)

### 1.1 Каркас: хранилище (§9) — шаг «Миграция v4»

Файлы: `max_news_bot/utils/db.py`, `max_news_bot/core/models.py`, `max_news_bot/utils/repositories.py`, `max_news_bot/tests/test_db.py`, `max_news_bot/tests/test_repositories.py`.

- Миграция v4 в существующем механизме `Database.initialize()` (`utils/db.py:335-357`): `PRAGMA user_version` шагами до 4; перед обновлением существующего файла — `wal_checkpoint(TRUNCATE)` и копия `{имя_БД}.bak-v{старая_версия}` рядом с файлом (`utils/db.py:346-350`).
- Новые таблицы: `slot_runs` (`UNIQUE(rubric_id, slot_date)`, статусы pending→generated→publishing→published + skipped/failed/unknown_delivery), `digest_items`, `rubric_state` (пауза и override цели, переживают перезапуск), `content_bank` (тип, текст, метаданные, review_status, show_count, `last_shown_at`, сезонность months, `event_date` — модель `core/models.py:104-116`), `filter_log`.
- Хранилище §8: SQL только в `utils/db.py` + `utils/repositories.py`; существующие таблицы не изменены.
- Прогоны шага: `uv run pytest max_news_bot/tests/test_repositories.py max_news_bot/tests/test_db.py -q` → 45 passed; compileall OK.
- Состояние локальной dev-БД (проверено при составлении ревизии, read-only): `max_news_bot/data/max_news_bot.db` имеет `user_version=2`, таблиц контура рубрик ещё нет — миграция v4 применится при первом запуске `initialize()` (с бэкапом `.bak-v2`).

### 1.2 Каркас: конфиг рубрик (§8) — шаг «Конфиг рубрик»

Файлы: `max_news_bot/config/rubrics.py`, `max_news_bot/config/rubrics.yaml`, `max_news_bot/config/loader.py`, `max_news_bot/tests/conftest.py`, `max_news_bot/tests/test_pipeline.py`, `max_news_bot/tests/test_loader.py`.

- Глобальная часть (timezone, persona, image_style, targets, daily_cap, summary_time 22:40, точки погоды) и все поля рубрики §8 — схема `config/rubrics.py`, данные `rubrics.yaml`.
- Startup-валидация: конфиг валидируется даже при выключенном контуре (`config/loader.py:254-256`); опечатка роняет запуск. Взаимоисключаемость контуров (`config/loader.py:257-263`), дословно сообщение: `Conflicting publishing contours: RUBRICS_ENABLED=true starts the rubrics scheduler while the legacy factory loop is enabled (FACTORY_ENABLED=true); exactly one publishing contour may be active - disable one of them`. Прочие fail-fast: `RUBRICS_ENABLED=true` без включённых рубрик (`loader.py:267-269`), LLM-рубрики без `LLM_API_KEY` (`loader.py:270-277`), dry-run без `ADMIN_IDS` (`loader.py:278-283`).
- Сетка шести рубрик §2 закреплена в `rubrics.yaml`; grace-линейка по рубрикам (подтверждено чтением): good_morning **60**, weather **30**, topic_day **30**, second_post **30**, good_news **20**, good_night **60** — т.е. **60/30/30/30/20/60**, шесть значений; jitter погоды ≤ 2 мин (`rubrics.yaml:97`). Полная таблица «время → рубрика» — в разделе 8.1.
- Точки погоды §4: СПб + 6 городов области, СПб первым (`rubrics.yaml:46-67`).
- Прогоны шага: `test_rubrics_config.py test_loader.py test_pipeline.py` → 60 passed; + `test_factory/test_handlers/test_architecture` → 57 passed.

### 1.3 Каркас: слотовый планировщик (§10, §11) — шаг «Слотовый планировщик»

Файлы: `max_news_bot/bot/slot_scheduler.py`, `max_news_bot/utils/repositories.py`, `max_news_bot/tests/test_repositories.py`, `max_news_bot/tests/test_slot_scheduler.py`.

- Тик 45 с (`slot_scheduler.py:45`), слоты в tz конфига, детерминированный jitter `slot_jitter_minutes` (hash `rubric_id:slot_date`, `slot_scheduler.py:246-257`; погода ≤ 2 мин, не обгоняет утро), исполнение слотов тика строго по времени — stable sort (`:395-418`). Захват — INSERT pending под `UNIQUE(rubric_id, slot_date)` (`:476-480`); skip-причины missed_grace / rubric_paused / no_topic / daily_cap (`:442-457`); daily_cap считает статусы CAP_STATUSES (`:53-63`) **без dry-run-строк** (`:452-454`, `count_for_date(..., dry_run=False)`) — превью админам капу не расходуют.
- Жизненный цикл §10: до 3 попыток генерации `MAX_GENERATION_ATTEMPTS=3` (`:46`), attempts персистятся до вызова LLM (`:582-585`); config-ошибка без перегенераций (`:593-596`); резерв из банка `on_llm_failure=bank` (`:624-641`); затем failed+алерт (`:642-651`). Перед сетевой отправкой слот переводится в publishing (`:674-677`).
- Recovery: `startup_recover` — одиночный писатель **в смысле „один процесс на БД“** (docstring `:330-336`, со ссылкой на §15 плана): на старте в полёте ничего нет, каждая строка pending/generated/publishing — наследие сбоя. pending перезапускается в пределах grace иначе missed_grace (`:532-539`); generated публикует сохранённый текст без LLM (`:540-542`); publishing → unknown_delivery + алерт, вслепую не публикуется (`:544-560`).
- Выключатели §11: 3 failed-слота подряд → пауза рубрики (durable, `rubric_state`) + алерт (`:739-749`); сбой MAX API (infra) → глобальная пауза 300 с (`:70-72`, `:753-770`), один алерт на инцидент, recovery-нотис после успеха — дословно `Slot publishing recovered after a MAX API pause.` (`:701-705`); источник с 3 пустыми ответами подряд → исключение `SourceHealthBreaker` (`:197-243`).
- Протоколы `RubricPipeline`/`SlotPublisher`, `GenerationOutcome`/`SlotPublishResult`; `slot_scheduler_loop` с heartbeat `slot_heartbeat` раз в 900 с (`:819-867`).
- Прогоны шага: `test_slot_scheduler.py test_repositories.py -q` → 44 passed; 3× повтор `test_slot_scheduler.py` → 25 passed; `test_db.py -q` → 27 passed; smoke полного дня сетки §2 (команда скрипта smoke в данных шага не передана — воспроизведение по этому отчёту невозможно).

### 1.4 Каркас: команды и наблюдаемость (§11) — шаг «Команды и наблюдаемость»

Файлы: `max_news_bot/bot/handlers.py`, `max_news_bot/bot/rubrics_admin.py`, `max_news_bot/bot/daily_summary.py`, `max_news_bot/bot/slot_scheduler.py`, `max_news_bot/config/rubrics.py`, `max_news_bot/config/rubrics.yaml`, `max_news_bot/utils/repositories.py`, `max_news_bot/utils/db.py`, `max_news_bot/scripts/healthcheck.py`.

- Рычаги: `/factory pause all|<рубрика>`, `run`, `resume`, `status`, `retract` с аргументом рубрики; `/factory retract slot <run_id>` — retract дубля для unknown_delivery (§10). `run_slot_now` проходит те же гарды паузы/cap/UNIQUE, что и плановый слот (`slot_scheduler.py:366-386`, `:442-480`).
- Ежедневная сводка одним сообщением в слот 22:40 МСК (`rubrics.yaml:40`): вышло, пропущено и почему, резервы банка, расходы на LLM; алерт при запасе банка < 30 дней по типам (механизм `bank/pipeline.py:432-446`). **Куда доставляется:** личными сообщениями MAX каждому id из `ADMIN_IDS` (`publishers/alerter.py:37-42`); каждый админ обязан однократно `/start` бота, иначе доставка ему молча падает в лог (`alerter.py:21-23`).
- Heartbeat: `slot_heartbeat` (`slot_scheduler.py:49-50`, `:848-861`) + правила `RUBRICS_ENABLED` в `scripts/healthcheck.py`.
- Семантика команд: голые `/factory pause|resume` при включённом контуре рубрик дополнительно ставят/снимают паузу со всех рубрик; без контура поведение прежнее. Какие тесты фиксируют прежнее поведение: `test_factory_bare_command_still_runs_factory_cycle` (`tests/test_handlers.py:284`), `test_factory_pause_sets_flag` (`:306`), `test_factory_resume_clears_both_pauses` (`:315`), `test_factory_rubric_argument_answers_disabled_without_contour` (`:358`). Оговорка: буквальную «побайтовость» (diff-сравнение выводов) ни один тест не утверждает — это формулировка отчёта шага; перечисленные тесты покрывают прежние ветки поведения.
- Прогоны шага: `uv run pytest` по 11 тестовым файлам → 251 passed (список 11 файлов в данных шага не передан — состав прогона по этому отчёту не восстанавливается).

### 1.5 Погода (§4, Этап 2) — шаги «Open-Meteo», «Шаблон и правила», «met.no»

Файлы: `max_news_bot/weather/` (`base.py`, `open_meteo.py`, `met_no.py`, `fallback.py`, `rules.py`, `template.py`, `validator.py`, `sun.py`, `format.py`, `__init__.py`, `pipeline.py`), тесты `test_weather_*.py`, `max_news_bot/tests/weather_builders.py`.

- **Open-Meteo** (основной): один batch-GET на все 7 точек из `weather.points`, hourly temperature_2m / apparent_temperature / precipitation / weather_code / wind_speed_10m, `wind_speed_unit=ms`, `forecast_days=1`, `timezone=Europe/Moscow`. Модель `WeatherSnapshot/PointForecast/WeatherWindow` (WMO weather_code), протокол `WeatherProvider`, ошибки infra/data. Сбой города области — точка выпадает; отказ СПб — `WeatherDataError`. Таймаут 15 с, сессия на вызов, сбои типизированы (§8).
- **Шаблон и правила**: таблица правил советов; шаблон (СПб подробно + строка на город области + 1–2 совета); восход/закат чистой формулой (`weather/sun.py`); валидатор (правдоподобие по точкам, числа только из API, лимит 4000, без магнитных бурь и давления). Сверка формулы с независимой библиотекой ephem (расхождение ≤ 1,5 мин) — заявление отчёта шага; команда сверки не передана и при составлении ревизии не повторялась.
- **met.no** (резерв): `locationforecast/2.0/compact`, без ключа, User-Agent по ToS; семь GET с изоляцией и per-request таймаутами; UTC→Europe/Moscow с фильтрацией завтрашнего хвоста; `symbol_code`→WMO; apparent temperature по формуле BOM (эмпирическая формула кажущейся температуры Австралийского бюро метеорологии). `FallbackWeatherProvider` уходит к резерву при ошибке основного; провал обоих — сводная ошибка.
- **Врезка в слот**: `weather/pipeline.py` — адаптер kind=weather; LLM не участвует; после исчерпания ретраев слот skip'ается с алертом, **резервного поста для погоды нет — решение плана §4** (`weather/pipeline.py:9-13`, `fallback_post` возвращает config-ошибку `:95-103`; в yaml — `on_llm_failure: skip`, `rubrics.yaml:105`).
- Прогоны шагов: `test_weather_open_meteo.py` → 23 passed; `test_weather_sun/rules/template/validator` → 64 passed; `test_weather_met_no.py test_weather_fallback.py` → 32 passed; все weather+architecture → 125 passed, 1 skipped. Все погодные тести — localhost fake-server / duck-typed сессии, без сети.

### 1.6 Утро и ночь (§3, §6, §7, Этап 3) — шаги «Банк контента», «Генеративный пайплайн»

Файлы: `max_news_bot/bank/` (`pipeline.py`, `antirepeat.py`, `validator.py`, `postcards.py`), `max_news_bot/rubrics/generative.py`, `max_news_bot/writers/completion.py`, `max_news_bot/writers/validator.py`, `max_news_bot/utils/repositories.py`, `max_news_bot/config/rubrics.yaml`, тесты `test_bank_*.py`, `test_generative_pipeline.py`, `test_postcards.py`.

- **Банк**: отбор с сезонностью (months) и `event_date`; `mark_shown` обновляет show_count/`last_shown_at`; алерт запаса < 30 дней; LLM только оформляет запись (валидатор чисел/годов/имён). **Когда расходуется запись (граница двух механизмов, замечание читателя рев.1):**
  - `BankPipeline` (kind: bank — good_morning, second_post): `mark_shown` вызывается только после успешного LLM-оформления и всех валидаторов — отклонённое оформление запись НЕ расходует (`bank/pipeline.py:347-390`: отказы оформления возвращаются до `mark_shown` на `:390`); но слот, упавший позже на доставке, запись сжигает — это осознанный выбор антиповтора (комментарий `:386-388`);
  - `GenerativePipeline` (kind: generative): seed-материал помечается показанным ДО генерации и валидации кандидата (`rubrics/generative.py:418-427`) — отклонённые кандидаты продвигают ротацию seed'ов; это и есть суть находки П-7;
  - `MaxSlotPublisher`: открытка bare-type помечается при каждом resolve (`publishers/slot.py:129-130`) — повторные отправки после infra-сбоя повторно двигают счётчик (вторая половина П-7).
- **Генеративный пайплайн** — точная механика бюджета LLM (замечание читателя рев.1 п. 3): жёсткий лимит `LLM_CALLS_PER_SLOT = 3` на слот (`rubrics/generative.py:61`), счётчик `_SlotBudget` живёт между попытками, ключ — run id (`:190-210`; crash-leftover с attempts>1 стартует с потраченными attempts−1). Внутри одной попытки: генерация (spent+1, `:284-291`) → локальные валидаторы бесплатно (`:293-296`) → проверяющий LLM да/нет; если бюджета на чекер нет — кандидат отвергается fail-safe, непроверенный пост не публикуется (`:298-303`); попытка при исчерпанном бюджете мгновенно возвращает failed (`:256-261`). Совместимость с циклом планировщика «1 вызов + до 2 перегенераций»: полный цикл (генерация+чекер = 2 вызова) реально проходит на первой попытке; вторая попытка получает последний вызов на генерацию и её кандидат не может быть проверен → fail-safe; **третья попытка планировщика LLM-вызовов уже не делает** — бюджет режет цикл раньше. Итог: не более 3 LLM-вызовов на слот гарантировано; «до 2 перегенераций» — верхняя граница попыток планировщика, из них не более двух сопровождаются LLM-вызовами.
- Антиповтор §3: последние `history_n` постов рубрики в промпте + Jaccard ≥ similarity_threshold + клише. Безопасность §7: зоны риска в промпте (`_SAFETY_RULES`, `generative.py:85-95`), чекер да/нет, инъекции из материала пресечены тегами `<material>`/`<post>`. Fallback: резерв из банка без LLM (`:322-361`, тип reserve, сезонность, least-shown).
- Прогоны шагов: `test_bank_validator + test_bank_pipeline + test_repositories` → 62 passed; со смежными → 159 passed; `test_generative_pipeline.py -x -q` → 35 passed; с регрессией задетых модулей → 168 passed.

### 1.7 Новости (§5, Этапы 0 и 4) — шаги «Сбор», «Фильтры», «Выпуск»

Файлы: `max_news_bot/config/news_sources.py`, `max_news_bot/utils/repositories.py`, `max_news_bot/rubrics/news.py`, `max_news_bot/rubrics/news_filters.py`, `max_news_bot/rubrics/digest.py`, `max_news_bot/rubrics/digest_validator.py`, `max_news_bot/core/models.py`, тесты `test_news_sources_config.py`, `test_shadow_news.py`, `test_news_filters.py`, `test_digest_pipeline.py`, `test_digest_validator.py`.

- **Теневой сбор (Этап 0)**: загрузчик `news_sources.py`; `ShadowNewsRepository` — отдельная sqlite-БД (WAL), таблицы `seen_news`/`shadow_runs`; `ShadowNewsCollector` через существующий Fetcher-протокол, URL-дедуп по образцу `core/pipeline.py:44-48` и `utils/db.py:359-381`; `shadow_news_loop` (`rubrics/news.py:161-179`, часовой интервал, переживает сбой прохода). Изоляция доказана тестом: боевые `sent_news`/`posts` не тронуты. **Цикл не подключён**: в `main.py:109-114` стартуют только slot_scheduler/daily_summary/daily_backup — запуск теневого сбора сейчас невозможен (раздел 4, п. 1).
- **Цепочка фильтров §5**: стоп-лист (префиксные основы), тематические исключения (6 групп), LLM-классификатор, второй независимый проход с отказом при расхождении, свежесть (0–48 ч по умолчанию), повторы (is_seen + in-batch URL + Jaccard заголовков ≥ 0,7). Fail-safe §7: нечитаемый ответ = отказ; транспортный сбой = error-verdict; молчаливого пропуска нет. Лог §13: оба прохода и все отказы — в `filter_log`.
- **Выпуск**: `DigestPipeline` — фиды → фильтры → одно LLM-сокращение → сборка кодом → валидатор → `digest_items`. Лимит пунктов: 3–4 (`MAX_ITEMS=4`, `MIN_FULL_ITEMS=3`, `digest.py:63-65`); в классификатор уходит не более 12 свежайших кандидатов (`MAX_FILTER_CANDIDATES=12`, `digest.py:65-69`, срез `:402`) — каждый стоит двух вызовов классификатора, отсюда несоблюдение бюджета «3 вызова на слот» для дайджеста (находка П-3). Деградация: 1–2 новости — короче; 0 — факт дня из банка.
- Прогоны шагов: `test_news_sources_config.py test_shadow_news.py` → 34 passed; смежные → 121 passed; `test_news_filters.py` → 36 passed; соседние → 100 passed; `test_architecture` → 5 passed; `test_digest_validator.py test_digest_pipeline.py` → 42 passed; смежные → 197 passed; +`test_slot_scheduler` → 71 passed.

### 1.8 Дневные рубрики (§2) — шаги «Тема дня», «Второй пост»

Файлы (оба шага — только тесты, продакшн-механика существовала от ранних фаз): `max_news_bot/tests/test_generative_pipeline.py`, `test_slot_scheduler.py`, `test_rubrics_config.py`, `test_second_post.py`.

- **Тема дня (пн–сб)**: темы из `topics_by_weekday` (парсер `config/rubrics.py:316-337`; тема в промпт и no_topic-skip `rubrics/generative.py:263-272`); приоритет события дня (`event_date`) над фактом (`rubrics/generative.py:394-400` — порядок `bank_types: [event, fact]` в `rubrics.yaml:116-121`); воскресенье — тихий день: дневные рубрики не создают даже skipped-слот (`bot/slot_scheduler.py:409-410`, `:428-433`), ежедневная ночная выходит. Добавлено 9 тестов.
- **Второй пост (~15:30, пн–сб)**: ротация рецепт/совет/вопрос/факт без картинки (`bank/pipeline.py`, блок `rubrics.yaml:136-156`); сезонность внутри ротации; антиповтор (Jaccard ≥ 0,7); исчерпан тип дня → резерв из банка. Новый `test_second_post.py` (7 тестов).
- Прогоны шагов: `test_generative_pipeline.py test_slot_scheduler.py test_rubrics_config.py -q` → 110 passed (9 новых); смежные → 67 passed; `test_second_post.py -q` → 7 passed; с соседями → 75 passed.

### 1.9 Картинки (§12, §9, §10) — шаг «Картинки»

Файлы: `max_news_bot/max_api/client.py`, `max_news_bot/max_api/errors.py`, `max_news_bot/max_api/uploads.py`, `max_news_bot/bank/postcards.py`, `max_news_bot/bank/pipeline.py`, `max_news_bot/publishers/slot.py`, `max_news_bot/tests/test_bank_pipeline.py`.

- Загрузка изображений через `POST /uploads` (формат из отчёта исследования epoch0 §1 — исследовательский этап проекта, `docs/plans/2026-10-09-epoch0-research.md`). Точная семантика ретраев `attachment.not.ready` (замечание читателя рев.1 п. 22): **до 4 попыток отправки, между ними 3 паузы 1с/2с/4с** — интервал растёт как `1·2^(n−1)` (`publishers/slot.py:47-50`, `:196-215`, `ATTACHMENT_PAUSE_SECONDS=1.0`); каждый ретрай шлёт то же сообщение с тем же токеном, без перезагрузки файла. Живым вызовом формат `POST /uploads` не подтверждён — открытый live-вопрос (раздел 4, п. 7).
- `slot_runs.image_ref`: пайплайн банка записывает конкретный ref банк-открытки.
- Деградации: локальная проблема открытки → пост текстом с warning (`publishers/slot.py:123-139`); транспортный сбой загрузки → infra-ошибка без отправки (`:100-104`), планировщик повторит с сохранёнными text+image_ref. Каталог открыток — параметр `postcards_dir`; дефолт в composition root — `{каталог_БД}/postcards` (`bot/rubrics_runtime.py:72`, `:205`); конвенция: `metadata["image"]` — путь относительно каталога.
- Прогоны шага: `test_max_uploads.py test_postcards.py test_slot_publisher.py test_bank_pipeline.py test_max_client.py` → 120 passed; соседние затронутые → 147 passed.

### 1.10 Интеграция и переключатель (§14, Этапы 1 и 4) — шаг «Интеграция и переключатель»

Файлы: `max_news_bot/main.py`, `max_news_bot/bot/rubrics_runtime.py`, `max_news_bot/bot/db_backup.py`, `max_news_bot/weather/pipeline.py`, `max_news_bot/weather/__init__.py`, `max_news_bot/config/rubrics.py`, `max_news_bot/config/loader.py`, `max_news_bot/config/rubrics.yaml`, `max_news_bot/tests/test_rubrics_config.py`.

- Коммутация: `RUBRICS_ENABLED=true` → `slot_scheduler_loop` + `daily_summary_loop` + `daily_backup_loop` (`main.py:109-114`), legacy-циклы `scheduler_loop`/`factory_scheduler_loop` не создаются (`main.py:124,132`); `false` → как раньше. Fail-fast конфликта проверен тестом с настоящей `main()`.
- `RUBRICS_DRY_RUN` (default true; `config/rubrics.py:566`): превью админам, префикс `[DRY-RUN]` (`publishers/slot.py:164`); при dry-run обязателен `ADMIN_IDS` (`loader.py:278-283`). Рубрика-заглушка `smoke_test` — **выключена по умолчанию** (`rubrics.yaml:212`), включается на время обкатки (см. П-1 и раздел 7). Тестовый target в yaml закомментирован (`rubrics.yaml:32-33`, env `MAX_TEST_CHANNEL_ID`).
- `bot/rubrics_runtime.py` — composition root (роутер пайплайнов по kind); `bot/db_backup.py` — ежедневный бэкап: `VACUUM INTO` в слот 23:10 МСК, окно 30 мин, retention 7, файл `{каталог_БД}/backups/{имя_БД}-{ГГГГ-ММ-ДД}.db`, запись через tmp+rename, сбой — алерт админам (`db_backup.py:32-42`, `:59-61`, `:93`, `:111-122`, `:146-171`); `weather/pipeline.py` — адаптер погоды.
- Прогоны шага: целевые тесты → 128 passed; соседние suites → 377 passed.

## 2. Покрытие §14 (этапы плана)

| Этап §14 | Что требовалось | Статус | Детали |
|---|---|---|---|
| Этап 0 | Теневой сбор новостей 2–3 дня в отдельной БД; проверка YandexART и загрузки картинок | **Частично** | Механизм есть и изолирован тестами, но `shadow_news_loop` не подключён (`main.py:109-114`) — запуск невозможен. Загрузка картинок реализована, формат `POST /uploads` живым вызовом не подтверждён; YandexART не проверялся (режим bank допустим по плану, пункт формально не закрыт). |
| Этап 1 | Коммутация `RUBRICS_ENABLED`, обкатка на заглушке через dry-run/тестовый target; автосверка по сообщениям канала — только если MAX API позволяет | **Сделано, с оговорками** | Коммутация + dry-run + заглушка есть; П-1 после ревью устранена (заглушка выключена по умолчанию, dry-run не расходует daily_cap, регресс-тесты `test_slot_scheduler.py:519,550`); автосверка не проверялась и не задокументирована — метода чтения сообщений канала в клиенте нет. |
| Этап 2 | Погода: правила, шаблон, валидатор | **Сделано** | Таблица правил, шаблон, валидатор, восход/закат формулой, Open-Meteo + met.no с fallback (раздел 1.5). |
| Этап 3 | Утро/ночь: generative-пайплайн, банк | **Сделано** | `GenerativePipeline` с жёстким бюджетом ≤ 3 LLM-вызовов и чекером; банк с сезонностью/антиповтором/fallback (раздел 1.6). Импорт готовых порций не реализован (раздел 4, п. 2). |
| Этап 4 | При `RUBRICS_ENABLED=true` не стартуют legacy-циклы; digest-слот на цепочке фильтров | **Сделано** | Оба legacy-цикла не создаются (тест с настоящей `main()`); `DigestPipeline` использует `NewsFilterChain`; рубрика `good_news` в yaml совместима (разделы 1.7, 1.10). |

## 3. Находки ревью

Определения статусов (уточнено по замечанию читателя рев.1 п. 10): **verified** — ревью подтвердило находку чтением кода с доказательствами; ни одна находка не воспроизводилась прогоном. **unconfirmed** — заявлена ревью, автопроверкой не подтверждалась. Дополнительно, при составлении ревизии 2 каждая находка перепроверена чтением текущего кода — колонка «Перепроверка»: подтверждена / подтверждена частично / не перепроверялась. Ссылки — на текущее состояние репозитория (2026-10-10), поэтому у части находок номера строк отличаются от переданных ревью.

| # | Суть | Severity | Статус ревью | Перепроверка (ревизия 2) |
|---|---|---|---|---|
| П-1 | smoke_test при daily_cap=6 съедает слот good_night | high | verified | **Исправлено после ревью** — см. ниже |
| П-2 | recovery публикует вчерашний generated-слот | medium | unconfirmed | Подтверждена чтением |
| П-3 | дайджест не ограничен бюджетом §7 | medium | unconfirmed | Подтверждена частично |
| П-4 | дубль публикации при ручном /factory run | medium | unconfirmed | Подтверждена чтением |
| П-5 | повтор после глобальной паузы с задержкой ×2 | low | unconfirmed | Подтверждена чтением |
| П-6 | пустой ADMIN_IDS при dry_run=false глотает алерты | low | unconfirmed | Подтверждена чтением |
| П-7 | счётчики показов банка завышаются | low | unconfirmed | Подтверждена чтением |
| П-8 | digest_items записываются до публикации | low | unconfirmed | Подтверждена чтением |
| П-9 | dry-run: частичная доставка = успех | low | unconfirmed | Подтверждена чтением |

### П-1. smoke_test при daily_cap=6 съедает ночной слот — high → устранена после ревью

- **Суть находки:** при включённой седьмой рубрике-заглушке (10:07) и `daily_cap=6` седьмой по времени слот `good_night` (21:30) каждый рабочий день резался бы капой.
- **Состояние на момент ревью (передано ревью):** `smoke_test enabled: true`, cap-подсчёт без учёта dry-run.
- **Текущее состояние (проверено чтением, 2026-10-10):**
  - `rubrics.yaml:212` — `smoke_test` **`enabled: false`**; комментарий `rubrics.yaml:200-210` прямо объясняет причину: сетка §2 — шесть слотов при cap 6, седьмой включённый слот срезался бы капой; заглушку включать на время обкатки.
  - `bot/slot_scheduler.py:449-456` — daily_cap считается через `count_for_date(..., dry_run=False)` (`:452-454`): dry-run-превью капу не расходуют, обкатка всей сетки в dry-run не голодает ночную рубрику.
  - Регресс-тесты на сценарий «7 слотов при cap 6», отсутствовавший на момент ревью: `tests/test_slot_scheduler.py:519` (`test_seven_slots_at_cap6_dry_run_keeps_the_night_slot`) и `:550` (`test_seven_slots_at_cap6_live_cuts_the_seventh_slot`).
- **Остаточный риск:** если оператор включит `smoke_test` в боевом режиме (dry_run=false) и забудет выключить — поведение вернётся; комментарий в yaml это предупреждает.

### П-2. recovery публикует вчерашний сгенерированный пост — medium, подтверждена чтением

- **Где:** `bot/slot_scheduler.py:502-560` (на момент ревью — `:512-536`).
- **Что:** `startup_recover` → `_recover_stale(all_rows=True)`: pending-ветка проверяет grace (`:532-539`), generated-ветка (`:540-542`) уходит сразу в `_publish` без проверки даты, grace, паузы рубрики и daily_cap. Рестарт на следующий день после вечернего сбоя опубликует вчерашний пост — для погоды прямое нарушение §4 «вчерашнюю погоду не публикуем».
- **Контекст «одиночного писателя» (замечание читателя рев.1 п. 4):** инвариант `startup_recover` (`:330-336`) — «один процесс на БД»: на старте в полёте ничего нет, каждая незавершённая строка — наследие сбоя. Ручной голый `/factory run` не создаёт второй процесс, но вызывает `scheduler.tick()` конкурентно с фоновым циклом в том же процессе (`bot/rubrics_admin.py:147-151`) — без взаимного исключения; именно этот зазор эксплуатирует П-4.

### П-3. дайджест не ограничен бюджетом §7 — medium, подтверждена частично

- **Где:** `rubrics/digest.py:65-69` (`MAX_FILTER_CANDIDATES = 12`, комментарий «every candidate costs two classifier calls»), срез `:402`; таймаут LLM по умолчанию 60 с — `config/loader.py:172` (перепроверено).
- **Что:** до 2×12=24 вызовов классификаторов + 1 shorten против «не более 3 вызовов LLM на слот»; при таймаутах один `_execute` дайджеста может тянуться ~25 минут без save — дольше stale_minutes=10 (следствие для П-4) и на грани окна healthcheck 2×900 с.
- **Статус перепроверки:** лимит 12 и двойной проход подтверждены чтением `digest.py`; строки `news_filters.py:447-475` (по два `_classify` на элемент) при составлении ревизии не перечитывались — взяты из данных ревью.

### П-4. возможен дубль публикации при ручном /factory run — medium, подтверждена чтением

- **Где:** `bot/rubrics_admin.py:147-151` (прямой `await scheduler.tick()`), `bot/slot_scheduler.py:351` (tick → `_recover_stale`), `:518-540` (stale pending → повторное `_execute`), `:476-480` (UNIQUE защищает только захват).
- **Что:** если фоновый тик исполняет слот дольше stale_minutes=10 (реалистично для дайджеста, см. П-3), `_recover_stale` конкурентного ручного тика примет живой pending за зависший и исполнит слот повторно — обе корутины дойдут до `_publish`, в канал уйдут два одинаковых выпуска. Дубль-рычаг существует: `/factory retract slot <run_id>`.
- **Статус перепроверки:** цепочка подтверждена чтением; утверждение «GenerativePipeline маркирует каждый вызов, digest — нет» (`digest.py:210-266`) из данных ревью — прочитанный фрагмент `digest.py:250-270` маркеров попыток не содержит.

### П-5. повтор публикации после глобальной паузы с задержкой ×2 — low, подтверждена чтением

- **Где:** `bot/slot_scheduler.py:708-714` (infra → save generated + `_trip_global_pause`, пауза 300 с), `:562-566` (`_is_fresh`: updated_at новее now−10 мин), `:476-480` (`try_claim` → None → return).
- **Что:** строка остаётся fresh по updated_at, `_recover_stale` её не трогает до истечения stale_minutes=10, `_actionable` повторно слот не находит (UNIQUE); пост выходит, но с задержкой ~10 мин вместо заявленных в алерте 300 с («retry then» — «повторит по истечении паузы»; текст алерта — `slot_scheduler.py:765-770`).

### П-6. пустой ADMIN_IDS при dry_run=false молча глотает алерты — low, подтверждена чтением

- **Где:** `config/loader.py:278-283` (fail-fast только `if rubrics.dry_run and not admin_ids`), `publishers/alerter.py:37-42` (`_broadcast` по пустому списку — цикл не выполняется), `bot/rubrics_runtime.py:236-238` (alerter подключён).
- **Что:** при `RUBRICS_DRY_RUN=false` и пустом `ADMIN_IDS` запуск валиден; все алерты (breakers, unknown_delivery, сбои банка/бэкапа) и сводка 22:40 доставляются некому — молча. Наблюдаемость §11 теряется без признаков сбоя.

### П-7. счётчики показов банка завышаются при повторах — low, подтверждена чтением

- **Где:** `bank/pipeline.py:386-390` (расход записи — после валидации) и `:426-427` (карточка bare-type — при каждом выборе), `publishers/slot.py:129-130` (mark_shown при каждом resolve) и `:140` (upload при каждой публикации), `rubrics/generative.py:418-427` (seed — до валидации кандидата).
- **Что:** два независимых завышения: (а) publisher повторно помечает показанной ту же открытку после infra-сбоя и recovery (show_count +2 за показ); (б) generative-пайплайн расходует seed-запись до валидации — отклонённые кандидаты продвигают ротацию seed'ов. Граница с «отклонённое оформление запись не расходует» (шаг «Банк контента») приведена в разделе 1.6: то утверждение верно для `BankPipeline`, П-7 — про `GenerativePipeline` и publisher.

### П-8. digest_items записываются до фактической публикации — low, подтверждена чтением

- **Где:** `rubrics/digest.py:260` (`await self._record_items(run, entries)` внутри `_generate` до возврата), `:336-343`; фильтр is_seen — `news_filters.py:529-537` (строки из данных ревью, при ревизии не перечитывались).
- **Что:** отвергнутый позже выпуск или упавшая публикация с ретрай-генерацией на повторной попытке отфильтруют те же URL как «already sent» — повтор необоснованно деградирует (короче выпуск или факт из банка вместо новостей).

### П-9. dry-run превью: частичная доставка считается успехом — low, подтверждена чтением

- **Где:** `publishers/slot.py:161-183` (delivered>0 → success, `message_id` не заполняется; сбой одного админа не ретраится сверх общих ретраев отправки), `slot_scheduler.py:691-698` (run published с max_message_id=None).
- **Что:** остальные админы превью не увидят; retract по dry-run-слотам невозможен (формально корректно — превью не в канале), но сводка/статус не показывают частичную доставку.

## 4. Пробелы реализации и непроверенные вопросы

Название «пробелы покрытия» в данных ревью касалось не тестового покрытия: это нереализованные функции и live-вопросы (уточнение по замечанию читателя рев.1 п. 14). Тестовое покрытие реализованного — раздел 6.

1. **Этап 0 (§14): теневой сбор новостей невозможно запустить** — `shadow_news_loop`/`ShadowNewsCollector` существуют (`rubrics/news.py:161-179`), но не подключены ни в `main.py` (стартуют только slot_scheduler/daily_summary/daily_backup, `main.py:109-114`), ни отдельным CLI; grep по кодовой базе вне тестов — ноль вызовов (перепроверено при ревизии: в `main.py` импорта `shadow_news_loop` нет).
2. **Этап 0/§6: нет инструмента импорта банка контента** — план: «готовые порции входят импортом после ручной проверки» и «банк блокирует старт тестовой недели»; в `scripts/` только `healthcheck.py`; `ContentBankRepository.add` (`repositories.py:482`) используется только тестами. Формат записи задан моделью `ContentBankItem` (`core/models.py:104-116`: item_type, text, metadata, review_status pending|approved|rejected, months, event_date), но инструмента массового импорта и формата порций (файл для ручной проверки) нет.
3. **§9/§11: смена цели рубрики (`rubric_state.target`) недоступна оператору** — `RubricStateRepository.set_target` (`repositories.py:456`) не вызывается ни одной командой `/factory` ни другим кодом (перепроверено grep при ревизии: вызовов вне тестов нет); поле и защита от невалидного target в планировщике есть (`slot_scheduler.py:458-475`), рычага нет.
4. **§11: выключатель «рост доли отклонённых новостей → алерт» не реализован** — `filter_log` пишется, порога с алертом нет.
5. **§11: еженедельный отчёт с 2–3 случайными постами на просмотр не реализован.**
6. **§10/Этап 1: автосверка по последним сообщениям канала** — проверка «позволяет ли MAX API» не выполнялась и нигде не задокументирована; метода чтения сообщений канала в клиенте нет.
7. **§12/Этап 0: живое подтверждение формата `POST /uploads`** — реализовано по отчёту исследования как лучшей гипотезе; доступность YandexART не проверялась (режим bank допустим по плану, пункт Этапа 0 формально не закрыт).

## 5. Отклонения от плана

1. **jitter_minutes 5–10 у не-погодных рубрик и min/max_chars — редакционные решения:** план фиксировал только grace-линейку и jitter погоды ≤ 2 мин. Grace-линейка в `rubrics.yaml` выдержана точно: **60/30/30/30/20/60** (шесть значений — по одной на рубрику сетки: good_morning/weather/topic_day/second_post/good_news/good_night; в ревизии 1 раздел отклонений ошибочно цитировал пять значений — исправлено).
2. **Бюджет §7 «не более 3 вызовов LLM на слот» соблюдается жёстко только в generative-рубриках** (единый счётчик генерация+чекер, раздел 1.6); для digest осознанно не соблюдается — план §5 сам требует два LLM-прохода на каждую новость, верхнего предела на слот в реализации нет (П-3).
3. **Семантика команд:** голые `/factory pause|resume` при включённом контуре рубрик дополнительно ставят/снимают паузу со всех рубрик — план такого поведения не описывал; без контура прежние ветки покрыты тестами (раздел 1.4; буквальная «побайтовость» тестами не утверждается).
4. **Расходы LLM в сводке — оценка числом вызовов** (attempts LLM-рубрик + filter_log за сутки), токены в `slot_runs` не хранятся; решение без новой миграции (схема зафиксирована Этапом 1).
5. **Ревьюёром не запускался полный тестовый suite** (инструкция шага это запрещала — тесты гонит скрипт); все находки ревью — чтение кода. При составлении ревизии 2 suite прогнан составителем (раздел 6) и закрывает этот пробел: 852 passed.

## 6. Что проверено (команды)

### 6.1. Проверки, выполненные при составлении этой ревизии (2026-10-10)

| Команда | Результат |
|---|---|
| `uv run pytest -q` | `852 passed, 1 skipped, 1 deselected, 2 warnings in 19.28s`, код возврата 0 — единый прогон полного suite |
| `uv run python -m compileall -q max_news_bot && echo COMPILE_OK` | `COMPILE_OK` — синтаксис всего дерева после всех правок |
| `uv run ruff --version` | `error: Failed to spawn: ruff … No such file or directory` — линтер в проекте не установлен, линт не выполнялся |
| `uv run python -c "…"` (read-only sqlite по `max_news_bot/data/max_news_bot.db`) | `user_version=2`; таблиц content_bank/slot_runs/rubric_state/digest_items/filter_log нет — dev-БД ещё не мигрирована, банк пуст |
| чтение кода (Read/grep/sed) | все ссылки `путь:строка` в разделах 1–4, 8 — текущее состояние репозитория |

Прогон pytest подтверждает и неразрушимость: suite прошёл на рабочем дереве с ~50 незакоммиченными файлами без сети (2 warnings — известные RuntimeWarning в `test_rss.py`, не связанные с контуром рубрик).

### 6.2. Прогоны шагов workflow (команды и цифры — из отчётов шагов; при ревизии повторно не запускались)

| Шаг | Команда (uv run pytest …) | Результат |
|---|---|---|
| Миграция v4 | `test_repositories.py test_db.py -q` | 45 passed; compileall OK (шага) |
| Конфиг рубрик | `test_rubrics_config.py test_loader.py test_pipeline.py` | 60 passed |
| Конфиг рубрик | + `test_factory`/`test_handlers`/`test_architecture` | 57 passed |
| Слотовый планировщик | `test_slot_scheduler.py test_repositories.py -q` | 44 passed |
| Слотовый планировщик | 3× `test_slot_scheduler.py` | 25 passed (стабильность на реальных часах) |
| Слотовый планировщик | `test_db.py -q` | 27 passed |
| Слотовый планировщик | smoke полного дня сетки §2 | 6 слотов, порядок, jitter — ok; **команда smoke в данных шага не передана, воспроизведение по отчёту невозможно** |
| Команды и наблюдаемость | 11 тестовых файлов | 251 passed; **список 11 файлов не передан, состав прогона не восстанавливается** |
| Погода: Open-Meteo | `test_weather_open_meteo.py` | 23 passed |
| Погода: Open-Meteo | sanity-прогон `test_rubrics_config`+`test_architecture` (sanity — быстрая проверка неразрушения соседних модулей) | 42 passed |
| Погода: шаблон и правила | `test_weather_sun/rules/template/validator` | 64 passed |
| Погода: шаблон и правила | соседний `test_weather_open_meteo` | 23 passed (неразрушение) |
| Погода: шаблон и правила | сверка восхода/заката с ephem | ≤ 1,5 мин; **команда сверки не передана, при ревизии не повторялась** |
| Погода: met.no | `test_weather_met_no.py test_weather_fallback.py` | 32 passed |
| Погода: met.no | все weather+architecture | 125 passed, 1 skipped |
| Банк контента | `test_bank_validator + test_bank_pipeline + test_repositories` | 62 passed |
| Банк контента | со смежными | 159 passed |
| Генеративный пайплайн | `test_generative_pipeline.py -x -q` | 35 passed |
| Генеративный пайплайн | с регрессией задетых модулей | 168 passed |
| Картинки | `test_max_uploads/test_postcards/test_slot_publisher/test_bank_pipeline/test_max_client` | 120 passed |
| Картинки | соседние затронутые | 147 passed |
| Новости: сбор | `test_news_sources_config.py test_shadow_news.py` | 34 passed |
| Новости: сбор | смежные repositories/bank/slot_scheduler/rubrics_config | 121 passed |
| Новости: фильтры | `test_news_filters.py` | 36 passed |
| Новости: фильтры | соседние repositories/generative/shadow_news/db | 100 passed |
| Новости: фильтры | `test_architecture` | 5 passed |
| Новости: выпуск | `test_digest_validator.py test_digest_pipeline.py` | 42 passed |
| Новости: выпуск | смежные | 197 passed; +`test_slot_scheduler` → 71 passed |
| Тема дня | `test_generative_pipeline.py test_slot_scheduler.py test_rubrics_config.py -q` | 110 passed (9 новых) |
| Тема дня | смежные | 67 passed |
| Второй пост | `test_second_post.py -q` | 7 passed |
| Второй пост | с соседями | 75 passed |
| Интеграция | целевые | 128 passed |
| Интеграция | соседние suites | 377 passed |

Итого: полный suite — 852 passed (проверено в ревизии 2); целевые прогоны шагов суммарно покрывают все новые модули; погодные тесты — только localhost fake-server и duck-typed сессии, без сети.

## 7. Что осталось

### 7.1. Банк контента — наполнение (блокер тестовой недели)

Банк пуст: в локальной dev-БД даже таблицы `content_bank` нет (раздел 6.1); инструмента импорта нет (раздел 4, п. 2). Без наполнения не работают: утро good_morning (kind: bank), резервы `on_llm_failure=bank` у пяти рубрик из шести, деградации дайджеста и второго поста, seed-материал и резерв генеративных рубрик, открытки (`{каталог_БД}/postcards`, каталог не существует). Нужно: CLI/скрипт импорта порций после ручной проверки (формат записи — поля `ContentBankItem`, `core/models.py:104-116`), операторская подготовка порций и открыток. Критерий запаса — «< 30 дней по типам» (`bank/pipeline.py:432-446`), т.е. на тип, расходуемый раз в сутки, нужно ≥ 30 одобренных записей.

### 7.2. Открытые находки — рекомендуемый разбор (рекомендация составителя, не решение workflow)

- **П-2, П-4 (medium, взаимодействают через П-3):** закрыть до тестовой недели — гарды даты/grace/паузы для recovery-ветки generated и взаимное исключение ручного `tick()` (или запрет голого `/factory run` при живом тике); по П-3 — снизить верхний предел кандидатов классификатора или ввести общий бюджет слота.
- **П-6 (low, но операционный):** одну строку fail-fast на пустые `ADMIN_IDS` при `RUBRICS_DRY_RUN=false` — желательно до боевого включения: иначе алерты и сводка уходят в никуда.
- **П-5, П-7, П-8, П-9 (low):** принять к сведению и завести задачи; поведение не блокирует обкатку (задержка повтора, точность счётчиков ротации, деградация повторной попытки дайджеста, полнота dry-run-превью).
- **Воспроизведение тестами:** для каждой unconfirmed-находки рекомендуется тест на сценарий (по образцу добавленных `test_seven_slots_at_cap6_*`) — пока ни одна не воспроизведена прогоном.

### 7.3. Запуск Этапа 0 и закрытие операторских пробелов

Подключить `shadow_news_loop` (main.py или CLI) и собрать 2–3 дня теневых данных; параллельно — рычаг смены цели рубрики (пробел 3: он же нужен для перевода рубрик на тестовый канал без правки yaml), алерт на долю отклонённых новостей (4), еженедельный отчёт (5).

### 7.4. Тестовая неделя

Не начата; блокируется банком (7.1), П-2/П-4 (7.2) и незапущенным теневым сбором (7.3). Конкретное решение по dry-run, которое нужно принять перед неделей (по замечанию читателя рев.1 п. 25), — выбор одного из двух режимов обкатки:
- **(а) превью-режим:** `RUBRICS_DRY_RUN=true` (дефолт) + непустой `ADMIN_IDS` — все посты всех шести рубрик идут личными сообщениями админам с префиксом `[DRY-RUN]`, канал не затрагивается, daily_cap не расходуется (`slot_scheduler.py:449-456`); здесь же можно временно включить `smoke_test` — в dry-run он капу не ест;
- **(б) боевой режим в тестовый канал:** раскомментировать target `test` (`rubrics.yaml:32-33`, env `MAX_TEST_CHANNEL_ID`), `RUBRICS_DRY_RUN=false`, перевести рубрики на `target: test` (рычагом смены цели — после реализации пробела 3, либо правкой yaml).
Перед боем: live-проверка формата `POST /uploads` (пробел 7), при желании — автосверка по сообщениям канала (пробел 6).

### 7.5. Деплой и коммит

Деплой в рамках workflow не выполнялся и в отчётах шагов не упоминается; изменения не закоммичены — рабочее дерево содержит ~50 изменённых/новых файлов (проверено `git status` при ревизии). Рекомендация по оформлению: нарезать коммиты по областям (миграция+репозитории; конфиг рубрик; планировщик; админ/наблюдаемость; погода; банк+generative; новости; картинки; интеграция+main; тесты), прогнав перед каждым `uv run pytest -q`. Коммит, push и деплой на VPS (Docker Compose через GitHub Actions) — только с явного подтверждения пользователя (правила AGENTS.md). План отката миграции v4 — раздел 8.5.

## 8. Справочник оператора

### 8.1. Сетка §2 (из `rubrics.yaml`, состояние 2026-10-10)

| Время (МСК) | Рубрика | Дни | kind | grace | jitter | Картинка | on_llm_failure |
|---|---|---|---|---|---|---|---|
| 07:00 | good_morning (открытки/фразы) | ежедневно | bank | 60 | 5 | postcard | bank |
| 07:05 | weather (прогноз) | ежедневно | weather | 30 | ≤2 | — | **skip** (§4: резерва нет) |
| 12:00 | topic_day (тема дня) | пн–сб | generative | 30 | 10 | — | bank |
| 15:30 | second_post (ротация) | пн–сб | bank | 30 | 10 | — | bank |
| 19:00 | good_news (дайджест) | ежедневно | digest | 20 | 10 | — | bank |
| 21:30 | good_night (спокойной ночи) | ежедневно | generative | 60 | 5 | postcard | bank |
| 10:07 | smoke_test (заглушка обкатки) | (все) | bank | 60 | 0 | — | bank — **`enabled: false`** |

Воскресенье — тихий день: дневные рубрики (12:00, 15:30) слотов не создают; ежедневные (07:00, 07:05, 19:00, 21:30) выходят. Сводка — 22:40, бэкап — 23:10. daily_cap=6: при шести включённых слотах капа не срабатывает; седьмой включённый слот в боевом режиме режется (см. П-1).

### 8.2. Env-переменные включения контура

| Переменная | Default | Назначение / требуемое значение |
|---|---|---|
| `RUBRICS_ENABLED` | false (`config/rubrics.py:580`) | true — старт контура рубрик; конфликт с `FACTORY_ENABLED=true` роняет запуск |
| `RUBRICS_DRY_RUN` | true (`config/rubrics.py:566`) | true — превью админам; false — публикация в target рубрик. При true обязателен `ADMIN_IDS` |
| `ADMIN_IDS` | — | id через запятую; получатели всех алертов и сводки 22:40; каждый админ должен однократно `/start` бота |
| `MAX_CHANNEL_ID` | обязателен (`loader.py:230`) | боевой канал target `main` |
| `MAX_TEST_CHANNEL_ID` | — | канал target `test` (раскомментировать `rubrics.yaml:32-33`) для обкатки боевой доставкой |
| `MAX_BOT_TOKEN`, `LLM_API_KEY` | обязателен / обязателен при LLM-рубриках | токен бота; ключ Yandex AI Studio (`loader.py:239-240`, `:270-277`) |

Минимальный чек-лист перед первой обкаткой: `RUBRICS_ENABLED=true`, `ADMIN_IDS=<ids>`, `MAX_BOT_TOKEN`, `LLM_API_KEY`, `MAX_CHANNEL_ID`; режим — 8.4(а) или (б).

### 8.3. Пути (файлы контура)

| Что | Путь | Источник |
|---|---|---|
| Рабочая БД | `max_news_bot/data/max_news_bot.db` (env `DATABASE_PATH`) | `loader.py:285-289` |
| Ежедневный бэкап (23:10, retention 7) | `{каталог_БД}/backups/{имя_БД}-{ГГГГ-ММ-ДД}.db` | `db_backup.py:59-61`, `:93` |
| Бэкап перед миграцией | `{каталог_БД}/{имя_БД}.bak-v{старая_версия}` | `utils/db.py:349-350` |
| Каталог открыток | `{каталог_БД}/postcards` (параметр `postcards_dir`publisher'а) | `rubrics_runtime.py:72`, `:205` |
| Теневая БД новостей | отдельный sqlite-файл (`shadow_db_path()`) | `rubrics/news.py` |

### 8.4. Доставка алертов и сводки

все алерты (breakers, unknown_delivery, сбои банка/бэкапа, глобальная пауза и recovery) и ежедневная сводка 22:40 отправляются личными сообщениями MAX каждому id из `ADMIN_IDS` (`publishers/alerter.py:37-42`); сбой доставки одному админу логируется и не блокирует остальных. Ограничение платформы: бот может писать пользователю только после его `/start` (`alerter.py:21-23`). Пустой `ADMIN_IDS` при `RUBRICS_DRY_RUN=false` валиден и глушит всё наблюдение — П-6.

### 8.5. Откат миграции v4

Механизм: перед повышением `user_version` существующий файл копируется в `{имя_БД}.bak-v{N}` после `wal_checkpoint(TRUNCATE)` (`utils/db.py:341-350`) — копия самодостаточна (номер версии хранится в файле). Процедура открата на проде: остановить контейнер → заменить рабочий файл БД копией `.bak-v3` (или `.bak-v2` для дев-БД) → откатить код на версию до контура рубрик → стартовать. Плановая процедура отката в данных workflow не описана — приведённая схема следует из механизма `initialize()`; перед первым боевым включением рекомендуется зафиксировать её в runbook оператора.

### 8.6. Глоссарий жаргона отчёта

- **epoch0** — исследовательский этап проекта, отчёт `docs/plans/2026-10-09-epoch0-research.md` (источник формата `POST /uploads`).
- **retry then** — «повторит по истечении паузы» (формулировка алерта глобальной паузы, `slot_scheduler.py:765-770`).
- **формула BOM** — эмпирическая формула кажущейся температуры Бюро метеорологии Австралии (Bureau of Meteorology), применена в met.no-провайдере.
- **sanity-прогон** — быстрая проверка неразрушения смежных модулей.
- **collect-only** — сбор тестов без запуска (`pytest --collect-only`): 801 на шаге «Новости: выпуск» против 853 фактически собранных сейчас.
- **dry-run** — режим `RUBRICS_DRY_RUN`: посты доставляются админам как превью с префиксом `[DRY-RUN]`, канал не затрагивается, daily_cap не расходуется.
- **stale_minutes** — порог «свежести» строки slot_runs (10 мин): что старше — считается зависшим и подлежит recovery.
