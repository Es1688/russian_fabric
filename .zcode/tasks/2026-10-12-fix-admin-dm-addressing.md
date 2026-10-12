# Task: fix-admin-dm-addressing

- Дата: 2026-10-12
- Проект: content-fabric-max
- Ветка: feature/fix-admin-dm-addressing
- Статус: done

## Задача
Починить доставку личных сообщений админам в MAX: POST /messages адресует
получателя параметром `user_id` (личный диалог) или `chat_id` (группа/канал),
а весь код шлёт админам их user_id как chat_id — API ищет групповой чат и
доставка ломается. ADMIN_IDS по смыслу — user_id (так уже работает is_admin).

## Требования
- MaxClient.send_message: kwarg `user_id: int | None = None`; адресация
  взаимоисключающая — ровно один из chat_id/user_id, иначе ValueError;
  при user_id в query уходит `user_id` вместо `chat_id`.
- Алерты (publishers/alerter.py) и оба dry-run превью (publishers/post.py,
  publishers/slot.py) адресуют админов через user_id; хелеры
  _send_with_retries прокидывают адрес; публикации в канал не трогать.
- /chatid печатает chat_id чата и user_id отправителя с пояснением, какое
  поле куда подставлять (MAX_CHANNEL_ID / ADMIN_IDS).
- В трёх ветках «Access denied.» — logger.info с user_id (текст ответа не менять).

## Ограничения
- Минимальный диф в стиле существующего кода; комментарии — только для
  неочевидных ограничений; код/коммиты — английский, Conventional Commits.
- RSS-only (ADR-001), секреты только в .env, max_news_bot/data/ не трогать,
  push/merge — только с подтверждения пользователя.

## Acceptance criteria
- [x] send_message с user_id шлёт params={"user_id": ...} и не шлёт chat_id.
- [x] Оба адреса сразу / ни одного — ValueError.
- [x] Алертер и оба publisher'а в dry-run адресуют админов через user_id
      (мок-клиент фиксирует параметры; slot — через реальный query).
- [x] /chatid показывает оба id.
- [x] `uv run pytest` из корня — весь набор зелёный (866 passed; 861 на main + 5 новых).
- [x] README.md (ADMIN_IDS = user_id, узнать через /chatid), ARCHITECTURE.md
      и .env.example обновлены.

## Изменяемые файлы
- max_news_bot/max_api/client.py
- max_news_bot/publishers/alerter.py
- max_news_bot/publishers/post.py
- max_news_bot/publishers/slot.py
- max_news_bot/bot/handlers.py
- max_news_bot/tests/test_max_client.py, test_post_publisher.py,
  test_slot_publisher.py, test_handlers.py (+ тест на алертер)
- README.md, ARCHITECTURE.md

## Итог
Адресация личных сообщений админам переведена на user_id: MaxClient.send_message
принял kwarg user_id с проверкой «ровно один из chat_id/user_id» (ValueError до
запроса); алерты (alerter.py) и оба dry-run превью (post.py, slot.py) шлют админам
по user_id, публикации в канал не тронуты; /chatid печатает chat_id и user_id с
пометкой, какое поле куда (MAX_CHANNEL_ID / ADMIN_IDS); три ветки «Access denied.»
логируют access_denied с user_id. Документация: README, ARCHITECTURE (таблица
различий MAX/Telegram + Последствия ADR-001) и .env.example — ADMIN_IDS это user_id.
Отклонения от плана: дополнительно закрыт пробел в тестах — новый test_alerter.py
(алертер не был покрыт), обновлён .env.example. Тесты: 866 passed (861 на main + 5).
Известные долги (не блокируют): разовый флак test_db_backup под нагрузкой полного
набора (тайминг-тест, вне изменённого кода) — чинить отдельно при повторении;
.zcode/project.yaml отсутствует в репозитории (правила брались из AGENTS.md) —
нужен онбординг/zcode-doctor. Коммит: squash-merge feature/fix-admin-dm-addressing
→ main, push выполнен с явного разрешения оператора.
