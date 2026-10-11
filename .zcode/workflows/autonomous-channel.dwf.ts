/* zcode-workflow
description: "Реализация автономного канала MAX по плану
  docs/plans/2026-10-09-autonomous-channel.md: Этап 0 (исследование) и Этапы 1–5
  (код, тесты, независимая проверка)."
whenToUse: Когда нужно выполнить исследование Этапа 0 или реализацию Этапов 1–5
  плана автономного канала MAX (docs/plans/2026-10-09-autonomous-channel.md).
  Сначала mode=research, после решений оператора — mode=build.
args:
  images_mode:
    type: string
    description: "Режим картинок для build: bank (открытки из банка) или yandexart
      (+ генерация)"
    default: bank
  mode:
    type: string
    description: research — Этап 0 (исследование и решения оператора); build — Этапы
      1–5 реализация
    default: research
  rss_sources:
    type: string
    description: Путь к YAML с подтверждённым оператором списком RSS-источников (для
      build, фазы новостей)
*/
// Workflow: реализация автономного канала MAX по плану
// docs/plans/2026-10-09-autonomous-channel.md
// mode=research — Этап 0: исследование и решения оператора
// mode=build — Этапы 1–5: код, гейты, независимая проверка, отчёт

const mode = args.mode === "build" ? "build" : "research";
const imagesMode = args.images_mode === "yandexart" ? "yandexart" : "bank";
const rssSources = typeof args.rss_sources === "string" ? args.rss_sources.trim() : "";

const PLAN_PATH = "docs/plans/2026-10-09-autonomous-channel.md";
const RESEARCH_PATH = "docs/plans/2026-10-09-epoch0-research.md";
const IMPL_REPORT_PATH = "docs/plans/2026-10-09-impl-report.md";

interface StageResult {
  /** Изменённые/созданные файлы, пути относительно корня репозитория. */
  files_changed: string[];
  /** Добавленные тестовые файлы — по ним гоняется быстрый гейт. */
  tests_added: string[];
  /** Закрытые пункты плана вида "§9 slot_runs" — их сверяет независимый ревьюер. */
  plan_items_covered: string[];
  /** Что сделано и что осталось за рамками, 1–2 строки. */
  notes: string;
}

interface ResearchFact {
  /** Факт одной строкой. */
  fact: string;
  /** URL источника факта. */
  sourceUrl: string;
}

interface ResearchNote {
  /** Тема исследования. */
  topic: string;
  /** Найденные факты, каждый с URL. */
  findings: ResearchFact[];
  /** Рекомендация одной строкой. */
  recommendation: string;
  /** Риски и ограничения. */
  risks: string[];
}

interface RssCandidate {
  url: string;
  title: string;
  /** Раздел: культура/наука/природа/спорт-победы/добрые истории. */
  section: string;
  /** Итог проверки: доступность, формат, условия ссылок. */
  check: string;
}

interface RssShortlist {
  candidates: RssCandidate[];
  /** Отклонённые кандидаты и причины. */
  rejected: { url: string; reason: string }[];
}

interface ReviewFinding {
  /** Файл и строка, к которым относится находка. */
  where: string;
  /** Что не так, одним предложением. */
  what: string;
  /** Доказательство: прочитанные строки или команда с выводом. */
  evidence: string;
  /** high — только потеря данных, краш или неверный результат. */
  severity: "low" | "medium" | "high";
}

interface ReviewResult {
  findings: ReviewFinding[];
  /** Пункты плана §14 Этапов 1–5, не закрытые реализацией. */
  coverage_gaps: string[];
  /** Отклонения от плана с обоснованием. */
  deviations: string[];
}

interface Confirmation {
  /** verified — проблема воспроизводится по коду; unconfirmed — нет. */
  status: "verified" | "unconfirmed";
  /** Где посмотрел и что увидел. */
  comment: string;
}

interface WrittenReport {
  /** Путь записанного markdown-файла. */
  path: string;
  /** Полный текст отчёта. */
  report: string;
}

interface Finding {
  where: string;
  what: string;
  evidence: string;
  /** verified — подтверждено независимо; unconfirmed — не перепроверялось. */
  status: "verified" | "unconfirmed";
  severity: "low" | "medium" | "high";
}

interface WorkflowReport {
  conclusion: string;
  findings: Finding[];
  verified: string[];
  notCovered: string[];
}

const DEV_PERSONA = {
  system:
    "Ты — опытный Python-разработчик (Python 3.12, asyncio, aiohttp, SQLite), работаешь в репозитории бота MAX. " +
    "Сначала прочитай план и существующий код; минимальные изменения в стиле проекта; " +
    "комментарии и идентификаторы в коде — английские. " +
    "Строго запрещено: git commit/push, читать или выводить содержимое .env, изменять max_news_bot/data/ и infra/. " +
    "Тесты — pytest, без сетевых вызовов и реальных ключей (моки/fakes по образцу существующих тестов). " +
    "Полный тестовый suite после тебя запускает скрипт — сам прогоняй только целевые тесты своих модулей. " +
    "Верни точный результат по заданному типу. Если гейт невозможно пройти или инструкции противоречат друг другу — " +
    "эскалируй вопросом и скажи прямо, а не обходи.",
};

const RESEARCH_PERSONA = {
  system:
    "Ты — аккуратный исследователь. Каждый факт сопровождай ссылкой на источник (URL). " +
    "Если сеть или документация недоступны — прямо скажи в risks, что удалось проверить, а что нет; не выдумывай. " +
    "Отвечай по-русски.",
};

const REVIEW_PERSONA = {
  system:
    "Ты — независимый ревьюер, видишь код впервые. Твоя задача — найти, что сломается и чего не хватает, а не одобрить. " +
    "Ничего не редактируй и тесты не запускай — их запускает скрипт. " +
    "Каждая находка — с доказательством (файл:строка). severity high — только потеря данных, краш или неверный результат. " +
    "Отвечай по-русски.",
};

const CONFIRM_PERSONA = {
  system:
    "Ты — подтверждающий ревьюер. Прочитай код и реши, воспроизводится ли находка. " +
    "Ничего не редактируй и тесты не запускай. Отвечай по-русски.",
};

function task(sections: string, files: string, head: string): string {
  return (
    `План — ${PLAN_PATH}, сначала прочитай разделы: ${sections}. Ориентиры в коде: ${files}.\n` +
    `Задача: ${head}\n` +
    "Напиши код и тесты, прогони целевые тесты своих модулей и верни StageResult " +
    "(files_changed, tests_added, plan_items_covered, notes)."
  );
}

function skipped(): StageResult {
  return {
    files_changed: [],
    tests_added: [],
    plan_items_covered: [],
    notes: "шаг пропущен: предыдущая фаза заблокирована",
  };
}

function mark(stage: string, status: string): void {
  report({ stage, status }, "progress");
}

async function implementStage(stage: string, instructions: string): Promise<StageResult> {
  log(`Шаг: ${stage}`);
  let result = await agent(`Разработчик: ${stage}`, DEV_PERSONA).ask<StageResult>(instructions);
  if (result.tests_added.length === 0) return result;
  const fixer = agent(`Ремонт: ${stage}`, DEV_PERSONA);
  for (let round = 0; round < 3; round++) {
    const targeted = await world.run("uv", ["run", "pytest", "-q", ...result.tests_added], { timeoutMs: 600_000 });
    if (targeted.exitCode === 0) return result;
    if (round === 2) break;
    result = await fixer.ask<StageResult>(
      `Целевые тесты падают (исправление ${round + 1} из 2):\n${(targeted.stdout + targeted.stderr).slice(0, 8000)}\nИсправь и верни обновлённый StageResult.`,
    );
  }
  return result;
}

const gateLog: string[] = [];

async function phaseGate(name: string): Promise<boolean> {
  const fixer = agent(`Ремонт suite: ${name}`, DEV_PERSONA);
  let ok = false;
  for (let round = 0; round < 3; round++) {
    const full = await world.run("uv", ["run", "pytest", "-q"], { timeoutMs: 600_000 });
    ok = full.exitCode === 0;
    if (ok || round === 2) break;
    await fixer.ask<StageResult>(
      `Полный тестовый suite падает (исправление ${round + 1} из 2):\n${(full.stdout + full.stderr).slice(0, 8000)}\nИсправь и верни StageResult.`,
    );
  }
  gateLog.push(`${name}: ${ok ? "ok" : "fail"}`);
  return ok;
}

// ---------- Запуск A: исследование (Этап 0) ----------

if (mode === "research") {
  phase("Исследование API и источников");
  log("Три исследователя параллельно: MAX uploads, YandexART, RSS-источники");
  const [uploads, art, rss] = await Promise.all([
    agent("Исследователь MAX uploads", RESEARCH_PERSONA).ask<ResearchNote>(
      "Исследуй загрузку файлов в MAX Bot API (метод POST /uploads). Выясни по официальной документации: " +
        "формат запроса (multipart? поля?), аутентификацию, ограничения (типы, размеры), как токен загрузки " +
        "прикрепляется к сообщению. Прочитай max_news_bot/max_api/client.py и опиши, что именно добавить " +
        "(имя метода, сигнатура, формат ответа, ошибки). Используй доступные тебе средства (поиск, документация, curl); " +
        "непроверяемое — в risks, не выдумывай. Верни ResearchNote, каждый факт — с sourceUrl.",
    ),
    agent("Исследователь YandexART", RESEARCH_PERSONA).ask<ResearchNote>(
      "Исследуй YandexART (генерация изображений): доступен ли через Yandex AI Studio (тот же OpenAI-совместимый " +
        "endpoint, что используется в max_news_bot/writers/, или отдельный асинхронный API Yandex Cloud), формат " +
        "запроса и ответа, стоимость, лимиты. Прочитай max_news_bot/writers/, чтобы понять текущий endpoint. " +
        "Каждый факт — с URL; недоступное — в risks.",
    ),
    agent("Исследователь RSS-источников", RESEARCH_PERSONA).ask<RssShortlist>(
      `Подбери шортлист из 5–8 федеральных «мягких» RSS-источников для рубрики «Хорошие новости»: только разделы ` +
        `культура, наука, природа, спорт-победы, добрые истории; общие ленты и политика не допускаются (план ${PLAN_PATH}, §5). ` +
        `Каждого кандидата проверь: лента доступна, в элементах есть заголовок, ссылка и дата, описания достаточно ` +
        `для формата «заголовок + одна фраза + ссылка». Отклонённых — с причиной. Верни RssShortlist.`,
    ),
  ]);

  phase("Сводка решений для оператора");
  const decisionWriter = agent("Составитель решений", RESEARCH_PERSONA);
  const decision = await decisionWriter.ask<WrittenReport>(
    `Три исследования завершены (JSON ниже). Составь по-русски markdown-отчёт для оператора с разделами: ` +
      `MAX uploads, YandexART, RSS-шортлист (таблица кандидатов: источник, раздел, итог проверки), риски и открытые ` +
      `вопросы, блок «что подтвердить оператору» (режим картинок bank|yandexart, финальный RSS-лист). Запиши файл ` +
      `по пути ${RESEARCH_PATH} и верни { path, report } — report это полный текст файла.\n` +
      `JSON: ${JSON.stringify({ uploads, art, rss })}`,
  );

  const facts: Finding[] = [];
  for (const note of [uploads, art]) {
    for (const f of note.findings) {
      facts.push({ where: note.topic, what: f.fact, evidence: f.sourceUrl, status: "unconfirmed", severity: "low" });
    }
  }
  for (const c of rss.candidates) {
    facts.push({ where: "RSS-шортлист", what: `${c.title} (${c.section}): ${c.check}`, evidence: c.url, status: "unconfirmed", severity: "low" });
  }
  for (const f of facts) report(f);

  try {
    await artifact.file("research-report", decision.path, {
      title: "Решения Этапа 0",
      description: "MAX uploads, YandexART, шортлист RSS-источников и вопросы к оператору.",
    });
  } catch {
    await decisionWriter.ask(`Файл ${decision.path} не удалось опубликовать как артефакт. Проверь, что файл записан и доступен, перезапиши его и подтверди.`);
    try {
      await artifact.file("research-report", decision.path, { title: "Решения Этапа 0" });
    } catch {
      log(`Артефакт отчёта не опубликован; файл должен быть в ${RESEARCH_PATH}`);
    }
  }

  return {
    conclusion:
      `Этап 0 завершён: отчёт сохранён в ${RESEARCH_PATH}. Подтвердите режим картинок (bank|yandexart) и ` +
      `финальный RSS-лист, затем запускайте mode=build с аргументами images_mode и rss_sources.`,
    findings: facts,
    verified: [],
    notCovered: [
      "факты исследований не перепроверялись независимо — решения принимает оператор по отчёту со ссылками",
      "проверка источников зависит от доступности сети у исследовательских субагентов; недоступное отмечено в рисках отчёта",
    ],
  };
}

// ---------- Запуск B: реализация (Этапы 1–5) ----------

artifact.board("progress", {
  title: "Прогресс реализации",
  key: "stage",
  status: "status",
  columns: ["делается", "готово", "блок"],
});

let blocked = "";
const stages: { name: string; result: StageResult }[] = [];

const MIGRATION_ASK = task(
  "§6 (content_bank), §9 (миграции и таблицы)",
  "max_news_bot/utils/db.py (самописные миграции, PRAGMA user_version, текущая версия 3), max_news_bot/core/",
  "Миграция v4 и слой репозиториев. Таблицы: slot_runs — UNIQUE(rubric_id, slot_date), статусы " +
    "pending → generated → publishing → published и skipped/failed/unknown_delivery, поля по §9 (причина пропуска, " +
    "ошибка, попытки, цель, признак dry-run, текст, image_ref, source_refs, max_message_id, метки времени; " +
    "slot_date считается в таймзоне конфига, хранение времени UTC); digest_items; rubric_state (паузы и смена цели, " +
    "переживающие перезапуск); content_bank — по §6 (тип, текст, метаданные, статус проверки, число показов, дата " +
    "последнего показа; сезонность и event_date); таблица лога фильтров. Существующие таблицы не менять. SQL — только " +
    "в слое репозиториев. Тесты: применение миграции с v3, round-trip репозиториев.",
);
const CONFIG_ASK = task(
  "§8 (архитектура и конфиг), §14 (Этап 1)",
  "max_news_bot/config/loader.py, max_news_bot/config/sources.yaml",
  "Конфиг рубрик и его загрузка. Глобальная часть: timezone, persona, image_style, targets, daily_cap, точки погоды. " +
    "На рубрику: id, enabled, time, days, grace_minutes, jitter_minutes, kind, target, source, topics_by_weekday, " +
    "prompt_overlay, min_chars, max_chars, image, history_n, similarity_threshold, validators, on_llm_failure. " +
    "YAML-файл рядом с sources.yaml. Startup-валидация: опечатка роняет запуск; активен ровно один контур публикации — " +
    "RUBRICS_ENABLED (по умолчанию false) или legacy-циклы; конфликтная конфигурация роняет запуск с внятной причиной. " +
    "Тесты: валидный конфиг грузится; опечатка роняет; конфликт режимов роняет; дефолт false.",
);
const SCHEDULER_ASK = task(
  "§2 (сетка рубрик), §10 (жизненный цикл слота), §11 (выключатели)",
  "max_news_bot/bot/scheduler.py (заменяется слотовым), max_news_bot/core/factory.py (breaker, автопауза, дедупликация алертов — паттерны переиспользуются)",
  "Слотовый планировщик. Тик 30–60 секунд; слот в пределах grace_minutes — запуск, истёк — skipped(missed_grace); " +
    "пауза, нет темы или исчерпан daily_cap — skipped с причиной; слоты одного тика исполняются строго в порядке " +
    "времени; захват слота — INSERT pending за UNIQUE. Жизненный цикл со статусами включая publishing и " +
    "unknown_delivery: сбой генерации — 1 вызов LLM плюс до 2 перегенераций, затем резервный пост из банка, затем " +
    "failed и алерт; перед сетевым вызовом публикации слот переводится в publishing; после рестарта зависшие pending " +
    "подхватываются, generated публикует сохранённый текст без повторного LLM, а publishing повторно вслепую не " +
    "публикуется — unknown_delivery и алерт. Выключатели: N отказов подряд — пауза рубрики с алертом; падение MAX API " +
    "или лимиты — глобальная пауза с повтором; источник с тремя пустыми ответами подряд — исключение и алерт. " +
    "Тесты: порядок слотов, grace, UNIQUE-захват, recovery-семантика, выключатели (время мокается).",
);
const ADMIN_ASK = task(
  "§11 (наблюдение и рычаги)",
  "max_news_bot/bot/handlers.py (команды /factory pause/run/resume/status/retract уже есть), scripts/healthcheck.py",
  "Команды и наблюдаемость. Расширь /factory (pause all|рубрика, run, resume, status, retract) аргументом рубрики с " +
    "сохранением обратной совместимости. Ежедневная сводка одним сообщением в фиксированный слот 22:40 МСК: вышло, " +
    "пропущено и почему, резервы банка, расходы на LLM. Расширь heartbeat в scripts/healthcheck.py. Тесты на парсинг " +
    "команд, совместимость и сводку.",
);
const WEATHER_OMETEO_ASK = task(
  "§4 (погода)",
  "max_news_bot/max_api/client.py (как устроен HTTP-слой), max_news_bot/parsers/",
  "Интерфейс погодного провайдера и основная реализация Open-Meteo: один batch-запрос на 7 точек (Петербург и шесть " +
    "городов области по §4, координаты в конфиге), таймаут и изоляция вызова, парсинг ответа в единую модель данных " +
    "погоды. Тесты с моками HTTP.",
);
const WEATHER_TEMPLATE_ASK = task(
  "§4 (погода)",
  "max_news_bot/writers/validator.py (паттерн валидации)",
  "Правила, шаблон и валидатор погоды. Таблица правил советов: зонт и ветер, гололёд (переход через ноль с осадками), " +
    "жара, перепады, сезонные. Шаблон поста: Петербург подробно (утро, день, вечер, ощущается как, осадки, ветер), " +
    "строка на каждый город области, 1–2 совета, восход и закат чистой формулой без API. Валидатор: правдоподобие по " +
    "каждой точке, числа в тексте совпадают с данными API, укладка в лимит 4000 символов. Магнитные бури и давление не " +
    "включаются. Тесты на правила, шаблон, валидатор и расчёт восхода/заката.",
);
const WEATHER_METNO_ASK = task(
  "§4 (погода)",
  "модуль Open-Meteo из предыдущего шага (общий интерфейс провайдера)",
  "Резервный провайдер met.no (Yr) второй очередью: без API-ключа, batch не поддерживается — семь отдельных запросов " +
    "со своими таймаутами и изоляцией; переключение на резерв при сбое основного. Тесты с моками HTTP.",
);
const BANK_ASK = task(
  "§3 (антиповтор), §6 (банк контента)",
  "max_news_bot/utils/ (Jaccard-дедуп заголовков), max_news_bot/core/",
  "Пайплайн банка контента. Отбор с учётом сезонности (поле месяца/сезона у дачных советов и рецептов) и event_date " +
    "у фактов «в этот день»; обновление числа показов и даты последнего показа; алерт при запасе меньше 30 дней по " +
    "типам (приоритет — резервные посты). LLM только оформляет запись банка: валидатор проверяет, что числа, годы и " +
    "имена не изменились и ничего нового не появилось. Антиповтор: последние 5–7 постов рубрики в промпте плюс " +
    "Jaccard-дедуп с порогом из конфига рубрики (существующая реализация переиспользуется). Тесты.",
);
const GENERATIVE_ASK = task(
  "§3 (голос и контент), §7 (безопасность по конструкции)",
  "max_news_bot/writers/ (LLM-писатель и валидатор), max_news_bot/core/factory.py (recovery-паттерн)",
  "Генеративный пайплайн утро/ночь. Persona по §3 (уважительно, тепло, на «вы», простые слова, без сленга, " +
    "англицизмов и иронии), prompt_overlay рубрики, антиповтор из конфига. Цепочка отступления: генерация → " +
    "валидаторы → проверяющий LLM (короткий запрос да/нет: медицинские советы, цифры выплат, политика, выдумка) → до " +
    "2 перегенераций → резервный пост из банка → пропуск слота с алертом; суммарно не более 3 вызовов LLM на слот. " +
    "Тесты с мок-LLM, включая подсчёт вызовов.",
);
const IMAGES_ASK = task(
  "§12 (картинки)",
  `max_news_bot/max_api/client.py, max_news_bot/publishers/, отчёт исследования ${RESEARCH_PATH}`,
  imagesMode === "yandexart"
    ? "Загрузка изображений в MAX: метод POST /uploads по формату из отчёта исследования (если формат не " +
      "подтверждён — реализуй по лучшей гипотезе и пометь в notes), image_ref в slot_runs, публикация с картинкой. " +
      "Плюс генерация через YandexART по разделу отчёта (асинхронный API: постановка в очередь и опрос) — если отчёт " +
      "подтверждает доступность; иначе заглушка и заметка в notes. Тесты с мок-сервером."
    : "Загрузка изображений в MAX: метод POST /uploads по формату из отчёта исследования (если формат не " +
      "подтверждён — реализуй по лучшей гипотезе и пометь в notes), image_ref в slot_runs, публикация с открыткой из " +
      "банка. YandexART не подключается. Тесты с мок-сервером.",
);
const newsSrc =
  rssSources !== ""
    ? `подтверждённый оператором список ${rssSources}`
    : `шортлист кандидатов из отчёта ${RESEARCH_PATH} (окончательно не утверждён — пометь это в notes)`;
const NEWS_FETCH_ASK = task(
  "§5 (источники и дедупликация), §14 Этап 0 (теневой сбор)",
  "max_news_bot/parsers/, max_news_bot/core/pipeline.py (mark_sent)",
  `Сбор для рубрики «Хорошие новости»: источники — ${newsSrc}. Выборка и дедупликация по образцу sent_news; теневой режим — запись в отдельную sqlite-БД или shadow-таблицы, боевые дедуп-таблицы (sent_news, posts) не трогаются. Тесты.`,
);
const NEWS_FILTERS_ASK = task(
  "§5 (цепочка фильтров), §7 (безопасность)",
  "max_news_bot/writers/ (LLM-клиент)",
  "Цепочка фильтров новостей, каждый шаг может только отклонить: стоп-лист ключевых слов (смерть, ДТП, пожар, суд, " +
    "задержан, война, болезнь и т.п.); тематические исключения (сборы денег, медицинские прорывы, выплаты и льготы, " +
    "политика, реклама и пресс-релизы, акции); LLM-классификатор тональности (позитивная, нейтральная, негативная, " +
    "сомнительная); второй независимый проход с другой формулировкой — при расхождении отказ; свежесть 24–48 часов; " +
    "повторы. Все ответы классификаторов и причины отклонений — в таблицу лога фильтров. Тесты с мок-LLM, включая " +
    "случай расхождения проходов.",
);
const DIGEST_ASK = task(
  "§5 (выпуск)",
  "max_news_bot/writers/validator.py, max_news_bot/core/pipeline.py (digest-пайплайн)",
  "Выпуск хороших новостей: 3–4 пункта, у каждого заголовок, одна фраза и ссылка; чужие тексты целиком не " +
    "воспроизводятся; LLM только сокращает. Валидатор сверяет числа, имена и ссылки с исходной записью; запрет ссылок " +
    "из валидатора постов фабрики на дайджест не переносится — ссылка в пункте обязательна. Деградация: если прошло " +
    "меньше трёх новостей — выпуск короче или заменяется фактом дня из банка. Тесты.",
);
const TOPIC_DAY_ASK = task(
  "§2 (темы дня), §6 (банк, event_date)",
  "конфиг рубрик (topics_by_weekday), пайплайны банка",
  "Рубрика «Тема дня» (пн–сб): темы по дням недели из конфига (topics_by_weekday), календарь, «в этот день» по " +
    "event_date банка; воскресенье — тихий день, дневных рубрик нет. Тесты.",
);
const SECOND_POST_ASK = task(
  "§2 (второй пост)",
  "пайплайны банка и генерации",
  "Рубрика «Второй пост» (пн–сб): ротация рецепт/совет/вопрос/факт с антиповтором и сезонностью из банка. Тесты.",
);
const CUTOVER_ASK = task(
  "§14 (Этапы 1 и 4, флаг RUBRICS_ENABLED), §9 (ежедневный бэкап)",
  "max_news_bot/main.py (сейчас оба legacy-цикла стартуют безусловно)",
  "Коммутация режимов в main.py: при RUBRICS_ENABLED=true стартуют планировщик рубрик и задачи (сводка, ежедневный " +
    "бэкап через .backup или VACUUM INTO), legacy-циклы scheduler_loop и factory_scheduler_loop не стартуют; при " +
    "false — как сейчас. Fail-fast на конфликт режимов уже в валидации конфига — проверь, что он реально срабатывает " +
    "на старте. Добавь рубрику-заглушку для обкатки через dry-run (превью админам) и/или тестовый target, без " +
    "публикации в боевой канал. Тесты на стартовую коммутацию.",
);

phase("Каркас: миграция и конфиг рубрик");
let migration = skipped();
let rubricConfig = skipped();
if (blocked === "") {
  mark("Каркас: миграция и конфиг", "делается");
  const migrationTask = implementStage("миграция v4", MIGRATION_ASK);
  const configTask = implementStage("конфиг рубрик", CONFIG_ASK);
  [migration, rubricConfig] = await Promise.all([migrationTask, configTask]);
  stages.push({ name: "Миграция v4", result: migration }, { name: "Конфиг рубрик", result: rubricConfig });
  if (await phaseGate("каркас: миграция и конфиг")) {
    mark("Каркас: миграция и конфиг", "готово");
  } else {
    blocked = "Каркас: миграция и конфиг рубрик";
    mark("Каркас: миграция и конфиг", "блок");
  }
}

phase("Каркас: слотовый планировщик");
let schedulerRes = skipped();
if (blocked === "") {
  mark("Каркас: слотовый планировщик", "делается");
  schedulerRes = await implementStage("слотовый планировщик", SCHEDULER_ASK);
  stages.push({ name: "Слотовый планировщик", result: schedulerRes });
  if (await phaseGate("каркас: планировщик")) {
    mark("Каркас: слотовый планировщик", "готово");
  } else {
    blocked = "Каркас: слотовый планировщик";
    mark("Каркас: слотовый планировщик", "блок");
  }
}

phase("Каркас: команды и наблюдаемость");
let admin = skipped();
if (blocked === "") {
  mark("Каркас: команды и наблюдаемость", "делается");
  admin = await implementStage("команды и наблюдаемость", ADMIN_ASK);
  stages.push({ name: "Команды и наблюдаемость", result: admin });
  if (await phaseGate("каркас: команды")) {
    mark("Каркас: команды и наблюдаемость", "готово");
  } else {
    blocked = "Каркас: команды и наблюдаемость";
    mark("Каркас: команды и наблюдаемость", "блок");
  }
}

phase("Погода");
let ometeo = skipped();
let weatherTemplate = skipped();
let metno = skipped();
if (blocked === "") {
  mark("Погода", "делается");
  const ometeoTask = implementStage("погода: Open-Meteo", WEATHER_OMETEO_ASK);
  const templateTask = implementStage("погода: шаблон и правила", WEATHER_TEMPLATE_ASK);
  [ometeo, weatherTemplate] = await Promise.all([ometeoTask, templateTask]);
  metno = await implementStage("погода: met.no", WEATHER_METNO_ASK);
  stages.push(
    { name: "Погода: Open-Meteo", result: ometeo },
    { name: "Погода: шаблон и правила", result: weatherTemplate },
    { name: "Погода: met.no", result: metno },
  );
  if (await phaseGate("погода")) {
    mark("Погода", "готово");
  } else {
    blocked = "Погода";
    mark("Погода", "блок");
  }
}

phase("Утро и ночь");
let bankRes = skipped();
let generative = skipped();
let images = skipped();
if (blocked === "") {
  mark("Утро и ночь", "делается");
  const bankTask = implementStage("банк контента", BANK_ASK);
  const genTask = implementStage("генеративный пайплайн", GENERATIVE_ASK);
  [bankRes, generative] = await Promise.all([bankTask, genTask]);
  images = await implementStage("картинки", IMAGES_ASK);
  stages.push(
    { name: "Банк контента", result: bankRes },
    { name: "Генеративный пайплайн", result: generative },
    { name: "Картинки", result: images },
  );
  if (await phaseGate("утро и ночь")) {
    mark("Утро и ночь", "готово");
  } else {
    blocked = "Утро и ночь";
    mark("Утро и ночь", "блок");
  }
}

phase("Хорошие новости");
let newsFetch = skipped();
let newsFilters = skipped();
let digest = skipped();
if (blocked === "") {
  mark("Хорошие новости", "делается");
  newsFetch = await implementStage("новости: сбор", NEWS_FETCH_ASK);
  newsFilters = await implementStage("новости: фильтры", NEWS_FILTERS_ASK);
  digest = await implementStage("новости: выпуск", DIGEST_ASK);
  stages.push(
    { name: "Новости: сбор", result: newsFetch },
    { name: "Новости: фильтры", result: newsFilters },
    { name: "Новости: выпуск", result: digest },
  );
  if (await phaseGate("новости")) {
    mark("Хорошие новости", "готово");
  } else {
    blocked = "Хорошие новости";
    mark("Хорошие новости", "блок");
  }
}

phase("Дневные рубрики");
let topicDay = skipped();
let secondPost = skipped();
if (blocked === "") {
  mark("Дневные рубрики", "делается");
  const topicTask = implementStage("тема дня", TOPIC_DAY_ASK);
  const secondTask = implementStage("второй пост", SECOND_POST_ASK);
  [topicDay, secondPost] = await Promise.all([topicTask, secondTask]);
  stages.push({ name: "Тема дня", result: topicDay }, { name: "Второй пост", result: secondPost });
  if (await phaseGate("дневные рубрики")) {
    mark("Дневные рубрики", "готово");
  } else {
    blocked = "Дневные рубрики";
    mark("Дневные рубрики", "блок");
  }
}

phase("Интеграция и переключатель режимов");
let cutover = skipped();
if (blocked === "") {
  mark("Интеграция и переключатель режимов", "делается");
  cutover = await implementStage("интеграция и переключатель", CUTOVER_ASK);
  stages.push({ name: "Интеграция и переключатель", result: cutover });
  if (await phaseGate("интеграция")) {
    mark("Интеграция и переключатель режимов", "готово");
  } else {
    blocked = "Интеграция и переключатель режимов";
    mark("Интеграция и переключатель режимов", "блок");
  }
}

phase("Независимая проверка реализации");
const stageSummaries = stages.map((s) => ({
  name: s.name,
  files: s.result.files_changed,
  covered: s.result.plan_items_covered,
  notes: s.result.notes,
}));
const review = await agent("Независимый ревьюер", REVIEW_PERSONA).ask<ReviewResult>(
  `Реализация автономного канала ${blocked === "" ? "завершена, все фазы зелёные" : "частичная, заблокирована на фазе: " + blocked}. ` +
    `Отчёты шагов: ${JSON.stringify(stageSummaries)}. Твоя задача — найти, что сломается и чего не хватает. Прочитай план ` +
    `${PLAN_PATH} и весь diff (git status и git diff своими инструментами; новые файлы читай целиком). Сверь: (1) полнота — ` +
    `все ли пункты §14 Этапов 1–5 закрыты (plan_items_covered против плана, пропущенное — в coverage_gaps); (2) отклонения ` +
    `от плана; (3) реальные баги в новом коде: дубли публикаций, гонки, утечки ресурсов, работа со временем и таймзонами, ` +
    "идемпотентность. Верни ReviewResult; в evidence — файл:строка.",
);
const highIdx = review.findings.map((f, i) => ({ f, i })).filter((x) => x.f.severity === "high");
const confirmations = new Map<number, Confirmation>();
if (highIdx.length > 0) {
  log(`Ревьюер нашёл ${review.findings.length} находок, из них high: ${highIdx.length} — подтверждаю независимо`);
  const confirmedList = await Promise.all(
    highIdx.map((x) =>
      agent(`Подтверждающий ревьюер ${x.i + 1}`, CONFIRM_PERSONA).ask<Confirmation>(
        `Независимый ревьюер нашёл проблему. Прочитай код и реши, воспроизводится ли она: ${JSON.stringify(x.f)}. ` +
          "Верни Confirmation: где посмотрел и что увидел.",
      ),
    ),
  );
  highIdx.forEach((x, j) => confirmations.set(x.i, confirmedList[j]));
}
const merged: Finding[] = review.findings.map((f, i) => {
  const c = confirmations.get(i);
  return { where: f.where, what: f.what, evidence: f.evidence, severity: f.severity, status: c ? c.status : "unconfirmed" };
});
for (const f of merged) report(f);

let highFixed = 0;
const verifiedHigh = highIdx.filter((x) => confirmations.get(x.i)?.status === "verified");
if (verifiedHigh.length > 0) {
  const reviewFixer = agent("Ремонт по ревью", DEV_PERSONA);
  for (const x of verifiedHigh) {
    const c = confirmations.get(x.i);
    await reviewFixer.ask<StageResult>(
      `Подтверждённая ревьюером проблема: ${JSON.stringify(x.f)}\nКомментарий подтверждающего: ${c ? c.comment : ""}\nИсправь и верни StageResult.`,
    );
    highFixed += 1;
  }
}
const finalGate = verifiedHigh.length > 0 ? await phaseGate("финальная проверка после правок") : true;
mark("Независимая проверка", finalGate ? "готово" : "блок");

phase("Итоговый отчёт");
const reportWriter = agent("Составитель отчёта");
let implReport = await reportWriter.ask<WrittenReport>(
  `Собери по-русски итоговый markdown-отчёт о реализации автономного канала. Данные: статусы фаз и отчёты шагов ` +
    `${JSON.stringify(stageSummaries)}; гейты: ${gateLog.join("; ")}; находки ревью с статусами: ${JSON.stringify(merged)}; ` +
    `пробелы покрытия: ${JSON.stringify(review.coverage_gaps)}; отклонения от плана: ${JSON.stringify(review.deviations)}. ` +
    `Структура: что сделано по этапам плана; покрытие §14; находки с статусами и доказательствами; что проверено (команды); ` +
    `что осталось (банк контента, тестовая неделя, деплой). Запиши файл по пути ${IMPL_REPORT_PATH} и верни { path, report }.`,
);
const readerNotes = await agent("Читатель отчёта").ask<string[]>(
  `Прочитай файл ${IMPL_REPORT_PATH} глазами оператора, который не видел этой сессии. Что непонятно, какие утверждения ` +
    "не подкреплены самим текстом, что оператор спросит дальше? Суди по тексту, по репозиторию не ходи. Верни список замечаний.",
);
if (readerNotes.length > 0) {
  implReport = await reportWriter.ask<WrittenReport>(
    `Читатель отчёта отметил: ${JSON.stringify(readerNotes)}\nУстрани замечания в файле ${IMPL_REPORT_PATH} и верни обновлённый { path, report }.`,
  );
}
try {
  await artifact.file("impl-report", implReport.path, {
    title: "Отчёт о реализации автономного канала",
    description: "Этапы, покрытие плана, находки ревью и статус проверок.",
    primary: true,
  });
} catch {
  await reportWriter.ask(`Файл ${implReport.path} не удалось опубликовать как артефакт. Проверь, что файл записан и доступен, перезапиши его и подтверди.`);
  try {
    await artifact.file("impl-report", implReport.path, {
      title: "Отчёт о реализации автономного канала",
      description: "Этапы, покрытие плана, находки ревью и статус проверок.",
      primary: true,
    });
  } catch {
    log(`Артефакт отчёта не опубликован; файл должен быть в ${IMPL_REPORT_PATH}`);
  }
}

return {
  conclusion:
    blocked === ""
      ? `Реализация Этапов 1–5 завершена. Финальный полный suite: ${finalGate ? "зелёный" : "красный"}. ` +
        `Подтверждённых high-находок исправлено: ${highFixed}; unconfirmed: ${merged.filter((f) => f.status === "unconfirmed").length}. ` +
        `Отчёт: ${IMPL_REPORT_PATH}. Коммит и деплой — за оператором после ревью diff.`
      : `Реализация остановлена — фаза «${blocked}» не прошла гейт после двух исправлений. Гейты: ${gateLog.join("; ")}. ` +
        `Отчёт: ${IMPL_REPORT_PATH}.`,
  findings: merged,
  verified: [`uv run pytest -q — полный suite в конце каждой фазы и финально: ${gateLog.join("; ")}`],
  notCovered: [
    "тестовая неделя (§13 плана) — вручную оператором",
    "банк контента — наполняется отдельно, импорт после ручной проверки",
    "коммит и деплой — оператор после ревью diff",
    "живые внешние API (MAX /uploads, Open-Meteo, met.no, LLM) покрыты только моками",
  ],
};
