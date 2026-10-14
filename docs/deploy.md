# Деплой alertsbot

Канонический путь — push в main: Gitea Actions проверяет ruff, mypy и тесты,
затем автоматически выкладывает на mts. Прод: `/alertsbot`, пользователь/юнит
`alertsbot`, Python 3.10.12, runner на том же хосте. Push требует разрешения владельца.

## Выпуск

CI держит отдельный venv и под flock выполняет:

1. Запоминает предыдущий SHA, устанавливает целевой git HEAD и зависимости.
2. Копирует systemd-юнит, daemon-reload, restart.
3. Запоминает InvocationID нового запуска, проверяет health (до 10 попыток), active
   и отсутствие перезапуска между проверками.
4. Читает только журнал нового InvocationID. `scripts/check_startup.py` требует
   корректный JSON, `startup` с целевой версией и отсутствие ERROR/CRITICAL.
   Версия в приложении и CI — `git rev-parse --short=12 HEAD`.
5. Любой сбой вызывает существующий откат: предыдущий git HEAD, зависимости/юнит,
   daemon-reload, restart и healthcheck. Журнал выводится при провале job.

## Данные и настройки

`.env` уже содержит заполненные секреты и `ALERTS_APP_HOST`/`ALERTS_APP_PORT` для
uvicorn. Settings host/port не читает. Продовый `.env` не изменяется при выкладке.
`ALERTS_ENV=prod` запрещает старт с пустыми/placeholder секретами.

Новый юнит: `StateDirectory=alertsbot`, `StateDirectoryMode=0700`, `UMask=0077`.
systemd создаёт `/var/lib/alertsbot` для пользователя alertsbot. По умолчанию
`ALERTS_DB_PATH=/var/lib/alertsbot/alerts.db`; если `.env` задаёт путь, он имеет
приоритет над Environment юнита и должен быть доступен пользователю alertsbot.
На существующем хосте добавлять настройку в `.env` для стандартного пути не требуется.
Схема SQLite создаётся приложением при первом доступе, без отдельного мигратора.
Данные хранятся 90 дней и переживают git reset/деплой/откат.

## Проверки на хосте

```bash
systemctl status alertsbot --no-pager
curl -fsS http://127.0.0.1:9100/healthz
curl -fsS http://127.0.0.1:9100/readyz
journalctl -u alertsbot -n 100 --no-pager -o cat
diff /etc/systemd/system/alertsbot.service /alertsbot/systemd/alertsbot.service
```

Живость: `healthz` → ok. Готовность: `readyz` → ready (конфиг и БД; не Telegram).
Обязательно startup с SHA выпуска, отсутствие ERROR до записи startup, установленный
юнит совпадает с репо. Чтение последних алертов через python3+sqlite3 — [AGENTS.md](../AGENTS.md).
Политика journald на хосте должна хранить JSON-логи минимум 14 дней; репо её не меняет.

## Резервный путь и откат

`bash scripts/restart.sh` создаёт/обновляет venv, устанавливает юнит и рестартит
сервис. Это резервный путь при недоступном CI, без параллельной выкладки.
После него те же post-check обязательны вручную.

Ручной откат по разрешению: `sudo -u alertsbot git -C /alertsbot reset --hard <SHA>`,
затем `bash scripts/restart.sh` и post-check. SQLite не удалять: старый код может
не читать историю, но файл останется для последующего обновления/разбора.
Автоматический откат проверяет health, а ручная проверка журнала отката остаётся
за оператором (старые выпуски могут писать прежний формат).

## Пересыльщик во вкладку (web)

Отдельный stdlib-скрипт читает новые строки SQLite с mts по SSH, только `mode=ro`,
и раз в минуту отправляет одну сводку в `admin/alerts`. Токен tpanel нужен только
на web; mts его не получает. Устанавливается вручную на web, независимо от
выкладки FastAPI на mts:

```bash
sudo install -m 0755 scripts/tab_forwarder.py /usr/local/bin/alerts-tab-forwarder
sudo install -m 0644 systemd/web/alerts-tab-forwarder.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alerts-tab-forwarder.timer
systemctl status alerts-tab-forwarder.timer --no-pager
journalctl -u alerts-tab-forwarder -n 100 --no-pager -o cat
```

Пользователю server должны быть доступны SSH-ключ и алиас `mts`, бинарь
`/usr/local/bin/tpanel` и его `~/.config/tpanel/api.env`. На mts требуется
беспарольный `sudo -n -u alertsbot python3` для чтения истории. Каталог
`/var/lib/prometheus/node-exporter` должен уже существовать и собираться
textfile-коллектором node-exporter.

systemd создаёт `/var/lib/alerts-tab-forwarder` (0700); файл `last_id` (0600)
содержит последний обработанный id. Первый запуск начинает с текущего max(id),
старые алерты не пересылает. За проход — до 100 строк по возрастанию id;
pending младше пяти минут останавливает префикс, более старый показывается как
`pending (итог неизвестен)`. cached=1 не показывается, но позиция продвигается.
Сводка до 6000 символов: начало message до 300 символов; не вместившиеся алерты
уходят следующей сводкой, позиция продвигается только до последнего показанного.
Позицию сначала проверяет запись, потом идёт отправка: иначе сбой записи повторял бы
сводку каждую минуту.
Маршрут: ssh напрямую по tailnet (`100.89.26.10`), при недоступности — алиас `mts`
через vpn. Недоступен mts, alertsbot не готов (`readyz`) или история не читается
дольше 3 минут — одно сообщение во вкладку и одно о восстановлении
(`/var/lib/alerts-tab-forwarder/outage`).
При сбое SSH/tpanel позиция сохраняется для повтора. Если доставка уже произошла,
но запись позиции не удалась или процесс оборвался, следующая попытка может
повторить сводку.

Вкладка задаётся через `Environment=ALERTS_TAB=…` в override сервиса
(`sudo systemctl edit alerts-tab-forwarder.service`). Для обновления скрипта
достаточно повторить install; при изменении юнитов выполнить daemon-reload.
Остановка: `sudo systemctl disable --now alerts-tab-forwarder.timer`;
уже запущенный проход завершится. Позицию при обновлении/откате сохранять.

После каждого успешного ExecStart, включая пустой проход, ExecStartPost от root
атомарно обновляет `/var/lib/prometheus/node-exporter/alerts_tab_forwarder.prom`
(0644). Метрика `alerts_tab_forwarder_last_success_timestamp_seconds` — Unix-время
последнего успеха; устаревание более пяти минут означает пропавший пульс.
Сбой прохода оставляет прежний пульс. Пустые проходы молчат, работа даёт один
JSON-итог с числом, диапазоном id и длительностью; ошибка — ERROR с этапом и
безопасной причиной. Версия в журнале — первые 12 символов SHA256 скрипта.
