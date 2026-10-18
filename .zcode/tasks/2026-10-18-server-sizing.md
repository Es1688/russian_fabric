# Task: server-sizing

- Дата: 2026-10-18
- Проект: content-fabric-max
- Ветка: feature/server-sizing
- Статус: done

## Задача
Рассчитать минимальные характеристики VPS для нормальной работы бота.
План согласован в сессии (plan mode): расчёт по фактическому профилю нагрузки
из кода + фикс в документе + закрытие трёх рисков минимальной конфигурации
правками инфраструктуры.

## Требования
- Расчёт CPU/RAM/диск/сеть на основании фактического профиля нагрузки
  (частоты опроса, LLM-вызовы, рост SQLite, Docker-окружение), с вердиктом
  «минимум / рекомендуется».
- Ротация логов Docker (json-file без max-size растёт неограниченно).
- Остановка накопления старых образов при деплоях.
- Явный лимит памяти контейнера.

## Ограничения
- Правки только docs/ и инфраструктуры (compose, deploy workflow); код бота
  не трогать. Деплой/push — вне задачи (Level C).

## Acceptance criteria
- [x] docs/server-sizing.md: профиль нагрузки со ссылками на код, расчёт по
  компонентам, вердикт, риски, команды проверки на сервере.
- [x] Ротация логов (json-file, max-size 10m, max-file 3) и mem_limit 256m
  во всех трёх compose-файлах.
- [x] docker image prune -a -f в deploy workflow после успешного подъёма
  контейнера.
- [x] uv run pytest зелёный (954 passed на дереве dual-channel; повторный
  прогон на базе main после переключения ветки).

## Изменяемые файлы
docs/server-sizing.md (новый), infra/compose.prod.vps.yml,
infra/compose.prod.yml, max_news_bot/docker-compose.yml,
.github/workflows/docker.yml, .zcode/tasks/2026-10-18-server-sizing.md.

## Итог
Вердикт: 1 vCPU / 1 ГиБ RAM / 10 ГиБ SSD (комфортный минимум, самый дешёвый
тариф), абсолютный пол — 512 МиБ + swap 512 МиБ при закрытых рисках.
Отклонение от плана: docker image prune -a -f вместо -f — без -a prune не
удаляет тегированные старые sha-образы, а копятся именно они.
Ветка построена от main, не от feature/dual-channel-llm-images: изменения
файлов не пересекаются; docs/server-sizing.md ссылается на файлы ветки
dual-channel (writers/fallback.py, images/base.py, weather/) — ссылки
разрешатся в main после её мержа. Долги: merge/push — по разрешению
оператора; compose-изменения применятся при следующем деплое.
