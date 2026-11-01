# Task: egress-proxy-foreign-apis

- Дата: 2026-11-01
- Проект: content-fabric-max
- Ветка: feature/egress-proxy-foreign-apis
- Статус: done

## Задача
OpenRouter (первичный LLM-канал) и fal.ai (генерация картинок) нестабильны из
РФ напрямую. Нужно пустить их исходящий трафик через локальный socks5h-прокси
stelthVPN (`bot_socks_in`, loopback VPS), не затронув MAX API, резервный
Yandex LLM, RSS и погоду. План прошёл adversarial-review (2 раунда, APPROVED).

## Требования
- Прокси строго `socks5h://` (rdns=True, DNS на стороне foreign) — требование
  брифинга stelthVPN; `socks5://` (DNS-лик на RU-резолверы) — fail-fast.
- Проксируется только отдельная сессия для иностранных клиентов; общая
  прямая сессия (MAX/Yandex/RSS/погода) не меняется.
- Контейнер должен достучаться до loopback хоста → `network_mode: host`.
- Пустая/отсутствующая `EGRESS_PROXY_URL` сохраняет текущее поведение
  (локальная разработка без прокси).
- Решение зафиксировано как ADR-005, формулировки ADR-001 скорректированы.

## Ограничения
- Секреты — только в `.env`; `EGRESS_PROXY_URL` не секрет, живое значение —
  только в `.env` на VPS (`/opt/maxnewsbot/.env`).
- RSS-only MVP; конфиги stelthVPN не трогаем.
- Возврат socks-зависимости — через новый ADR (откат тоже).

## Acceptance criteria
- [x] `EGRESS_PROXY_URL=socks5h://127.0.0.1:1080` → OpenRouter, fal.ai и проба
      primary-канала идут через `ProxyConnector(rdns=True)`.
- [x] Любое отклонение от `socks5h://host:port` (включая `socks5://`, userinfo,
      отсутствие порта) — ValueError при старте.
- [x] Fallback Yandex, MAX, RSS, погода — на прямой сессии.
- [x] Оба compose-файла на `network_mode: host`; CI-workflow не менялся.
- [x] ADR-005 в ARCHITECTURE.md (security trade-off host-сети, отвергнутые
      альтернативы, режимы отказа), ADR-001 скорректирован.
- [x] `uv run pytest` — зелёной (808 passed).

## Изменяемые файлы
- `max_news_bot/utils/proxy.py` (новый), `main.py`
- `bot/rubrics_runtime.py`, `writers/readiness.py`
- `infra/compose.prod.yml`, `infra/compose.prod.vps.yml`
- `pyproject.toml`, `uv.lock` (aiohttp-socks)
- `max_news_bot/.env.example`, `README.md`, `ARCHITECTURE.md`
- `tests/test_proxy.py` (новый), `tests/test_rubrics_runtime.py`,
  `tests/test_architecture.py`, `tests/test_main_contour.py`

## Итог
Сделано по плану без отклонений. Ключевое: отдельная `outbound_session`
с `ProxyConnector(rdns=True)` в `main.py`, передаётся только первичному
LLM-клиенту, fal-генератору и primary-пробе (`probe_llm_channels(channels,
primary_session, fallback_session=None)`); fallback Yandex — на прямой
сессии (решение пользователя: независимость failover). Compose переведён на
`network_mode: host`. ADR-005 добавлен, ADR-001 скорректирован,
`test_architecture.py` ограничивает импорт `aiohttp_socks` модулем
`utils/proxy.py`. Тесты: `uv run pytest` — 808 passed, 1 deselected (network).
Находки adversarial-review (2 medium + 5 low) закрыты в плане и реализации.

Долги/ручные шаги на VPS (вне репо):
- добавить `EGRESS_PROXY_URL=socks5h://127.0.0.1:1080` в `/opt/maxnewsbot/.env`
  (порт = `STELTHVPN_RU_BOT_SOCKS_PORT`); без переменной бот ходит напрямую,
  как раньше;
- проверить прокси с хоста: `curl --socks5-hostname 127.0.0.1:1080
  https://api64.ipify.org` и отсутствие битого IPv6;
- после деплоя убедиться по логам: `egress_proxy_enabled`,
  `llm_probe_ok channel=primary`.
