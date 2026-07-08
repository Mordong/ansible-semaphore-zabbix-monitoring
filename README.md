# Мониторинг Ansible Semaphore средствами Zabbix 7.4

Решение контролирует бэкенд Ansible (Semaphore в контейнере Docker/Podman + PostgreSQL) и выполнение задач (плейбуков): доступность API и контейнеров, статусы и длительность запусков, failed/unreachable хосты из PLAY RECAP.

## Структура репозитория

```
zabbix-semaphore-monitoring/
├── README.md
├── CHANGELOG.md
├── scripts/
│   └── semaphore_monitor.py            # опрос REST API Semaphore (ping/discovery/status)
├── config/
│   └── semaphore_monitor.conf.example  # пример конфига скрипта (токен — НЕ коммитить)
├── zabbix_agent2.d/
│   ├── userparameter_semaphore.conf    # UserParameter для Zabbix Agent 2
│   └── podman.conf.example             # эндпоинт плагина Docker для Podman
├── templates/
│   └── zbx_template_semaphore_agent2.yaml  # шаблон Zabbix 7.4 (импорт через веб)
├── deploy/
│   ├── deploy_monitoring.yml           # плейбук развёртывания (ansible-core 2.13)
│   ├── templates/semaphore_monitor.conf.j2
│   └── inventory.example
└── tests/
    └── test_semaphore_monitor.py       # юнит + интеграционные тесты (stdlib)
```

Куда попадают файлы на хосте: скрипт → `/etc/zabbix/script/semaphore_monitor.py` (0755); конфиг → `/etc/zabbix/semaphore_monitor.conf` (0600, zabbix); UserParameter и podman.conf → каталог из директивы `Include` агента (типично `/etc/zabbix/zabbix_agent2.d/plugins.d/`).

## Совместимость

- **Zabbix 7.4.5**: формат экспорта шаблона `7.4`; per-item таймауты (30s на master-итем и LLD) — возможность Zabbix 7.x; используются встроенные плагины Agent 2 (`docker.container_info`, `net.tcp.port`) и препроцессинг JSONPath / Boolean to decimal.
- **ansible-core 2.13**: парсер PLAY RECAP разбирает строку формата `host : ok= changed= unreachable= failed= skipped= rescued= ignored=` (пары key=value читаются обобщённо, поэтому rescued/ignored не ломают разбор); ANSI-коды цвета из вывода Semaphore удаляются. Код плейбуков решение не изменяет.
- **Semaphore 2.8+** (REST API v2, Bearer-токен), Python ≥ 3.7 на хосте (только стандартная библиотека).

## Развёртывание через Ansible (альтернатива ручным шагам 2–3)

Плейбук `deploy/deploy_monitoring.yml` выполняет шаги 2–3 автоматически: раскладывает скрипт, генерирует конфиг с токеном (0600), ставит UserParameter в include-каталог агента, настраивает доступ к Docker- или Podman-сокету и перезапускает агент. Совместим с ansible-core 2.13 — только модули `ansible.builtin`, FQCN. Токен передавайте через Vault:

```bash
ansible-playbook -i deploy/inventory.example deploy/deploy_monitoring.yml \
  -e semaphore_api_token='ТОКЕН' -e container_runtime=podman
```

Переменные: `semaphore_url`, `container_runtime` (docker/podman/none), `zbx_script_dir` (по умолчанию `/etc/zabbix/script`), `zbx_include_dir` (по умолчанию `/etc/zabbix/zabbix_agent2.d/plugins.d`).

## Тесты

```bash
python3 tests/test_semaphore_monitor.py
```

Юнит-тесты парсеров (PLAY RECAP ansible-core 2.13, timestamps) и интеграционный прогон всех трёх режимов против встроенного мок-API Semaphore, включая кеширование вывода и деградацию при неверном токене. Зависимости — только стандартная библиотека Python.

## Развёртывание

### 1. API-токен Semaphore

Создайте отдельного пользователя (достаточно прав на чтение проектов) и получите токен:

```bash
# логин (кука сохраняется в файл)
curl -s -c /tmp/sem.cookie -H 'Content-Type: application/json' \
  -d '{"auth":"monitoring","password":"ПАРОЛЬ"}' \
  http://127.0.0.1:3000/api/auth/login

# выпуск токена
curl -s -b /tmp/sem.cookie -X POST http://127.0.0.1:3000/api/user/tokens
# в ответе поле "id" — это и есть токен
```

В новых версиях Semaphore токен также можно создать в веб-интерфейсе (меню пользователя).

### 2. Скрипт и его конфиг

```bash
install -m 755 scripts/semaphore_monitor.py /etc/zabbix/script/

cat > /etc/zabbix/semaphore_monitor.conf <<'EOF'
{
  "url": "http://127.0.0.1:3000",
  "token": "ВАШ_ТОКЕН",
  "timeout": 10,
  "verify_tls": true,
  "fetch_output": true,
  "max_output_fetch": 50,
  "cache_file": "/var/tmp/zbx_semaphore_recap_cache.json"
}
EOF
chown zabbix:zabbix /etc/zabbix/semaphore_monitor.conf
chmod 600 /etc/zabbix/semaphore_monitor.conf
```

`url` — адрес Semaphore с точки зрения хоста (порт, опубликованный из контейнера).

### 3. Zabbix Agent 2

```bash
cp zabbix_agent2.d/userparameter_semaphore.conf /etc/zabbix/zabbix_agent2.d/plugins.d/
```

**Важно про Include.** В пакетном конфиге RHEL-подобных систем по умолчанию
`Include=./zabbix_agent2.d/plugins.d/*.conf` — файлы прямо в `zabbix_agent2.d/`
не читаются (симптом: `ZBX_NOTSUPPORTED [Unknown metric semaphore.ping]`).
Либо добавьте в `/etc/zabbix/zabbix_agent2.conf` строку
`Include=/etc/zabbix/zabbix_agent2.d/*.conf`, либо положите файл в
`zabbix_agent2.d/plugins.d/`.

**Доступ к контейнерному рантайму** (для итемов `docker.container_info`;
плагин Docker в Agent 2 совместим с Podman, так как API Podman
Docker-совместимый):

```bash
# --- Docker ---
usermod -aG docker zabbix

# --- Podman (rootful) ---
# 1) включить API-сокет
systemctl enable --now podman.socket
# 2) выдать доступ пользователю zabbix (drop-in для юнита сокета)
systemctl edit podman.socket
#   [Socket]
#   SocketMode=0660
#   SocketGroup=zabbix
#   ExecStartPost=/usr/bin/chgrp zabbix /run/podman
#   ExecStartPost=/usr/bin/chmod 0750 /run/podman
# (ExecStartPost обязателен: systemd создаёт /run/podman как root:root,
#  без прохода по каталогу доступ к сокету даст permission denied)
systemctl daemon-reload && systemctl restart podman.socket
# 3) указать плагину эндпоинт — файл /etc/zabbix/zabbix_agent2.d/plugins.d/podman.conf:
#   Plugins.Docker.Endpoint=unix:///run/podman/podman.sock

systemctl restart zabbix-agent2
```

Для rootless Podman сокет находится в `/run/user/<uid>/podman/podman.sock`
и принадлежит пользователю контейнеров — агенту от zabbix он недоступен;
в этом случае либо запустите `podman.socket` в rootful-режиме параллельно,
либо отключите docker-итемы в шаблоне (доступность Semaphore останется
под контролем через `/api/ping`, порт PostgreSQL — через `net.tcp.port`).
Имена контейнеров в Podman могут отличаться от docker-compose — сверьте
`podman ps --format '{{.Names}}'` и поправьте макросы
`{$SEMAPHORE.CONTAINER.APP}` / `{$SEMAPHORE.CONTAINER.DB}`.

Проверка с хоста (`-c` укажите тот же конфиг, с которым запущен агент):

```bash
zabbix_agent2 -c /etc/zabbix/zabbix_agent2.conf -t semaphore.ping        # ожидается 1
zabbix_agent2 -c /etc/zabbix/zabbix_agent2.conf -t semaphore.discovery  # JSON-массив {#PROJECT_ID}...
zabbix_agent2 -c /etc/zabbix/zabbix_agent2.conf -t semaphore.status     # сводный JSON
zabbix_agent2 -c /etc/zabbix/zabbix_agent2.conf -t 'docker.container_info["semaphore",full]'
```

### 4. Импорт шаблона

Data collection → Templates → Import → `templates/zbx_template_semaphore_agent2.yaml`. Привяжите шаблон **Semaphore by Zabbix agent 2** к хосту с Agent 2 и задайте макросы:

| Макрос | По умолчанию | Смысл |
|---|---|---|
| `{$SEMAPHORE.CONTAINER.APP}` | `semaphore` | имя контейнера приложения (`docker ps --format '{{.Names}}'`) |
| `{$SEMAPHORE.CONTAINER.DB}` | `postgres` | имя контейнера PostgreSQL |
| `{$SEMAPHORE.PG.HOST}` / `{$SEMAPHORE.PG.PORT}` | `127.0.0.1` / `5432` | адрес проверки порта БД с хоста |
| `{$SEMAPHORE.TASK.MAXTIME}` | `3600` | порог «задача выполняется слишком долго», сек |
| `{$SEMAPHORE.RUNNER.MATCHES}` | `^/?podman_runner-.*` | регэксп имён контейнеров раннеров для LLD |
| `{$SEMAPHORE.RUNNER.HEARTBEAT.MAX}` | `300` | порог возраста heartbeat раннера, сек |

Итем «PostgreSQL: TCP-порт доступен» по умолчанию ВЫКЛЮЧЕН: в типовой установке порт БД не публикуется из контейнера, и проверка давала бы ложный триггер. Включите его на хосте только при опубликованном порте; состояние БД и без него контролируется проверкой контейнера.

## Что контролируется

**Бэкенд:** `/api/ping`; успешность опроса API (отдельный триггер при невалидном токене); контейнеры Semaphore и PostgreSQL (Docker-плагин Agent 2); порт PostgreSQL; отсутствие данных мониторинга (nodata 30m).

**Задачи (LLD по каждому шаблону Semaphore):** статус последнего запуска (success/running/waiting/error/stopped), длительность, время с последнего запуска, failed- и unreachable-хосты из PLAY RECAP. Триггеры: запуск завершился с ошибкой (AVERAGE); есть failed/unreachable хосты (WARNING); задача выполняется дольше порога (WARNING).

**Раннеры (v1.1.0+):** два независимых контура. (1) Контейнеры раннеров — LLD `docker.containers.discovery[true]` с фильтром имени по макросу `{$SEMAPHORE.RUNNER.MATCHES}` (по умолчанию `^/?podman_runner-.*`); на каждый контейнер: running-состояние, exit code, рестарты, CPU (ядер) и память; триггер HIGH «контейнер раннера не запущен». Остановленные контейнеры включены в обнаружение намеренно — упавший раннер даёт триггер, а не исчезает из LLD. (2) Heartbeat из API — master-итем `semaphore.runners` (режим `runners` скрипта, эндпоинт `/api/runners`, требуется admin-токен), зависимое LLD по раннерам: активность и возраст heartbeat, триггеры «раннер неактивен» (AVERAGE) и «нет heartbeat дольше `{$SEMAPHORE.RUNNER.HEARTBEAT.MAX}` сек» (WARNING, по умолчанию 300). При недоступности `/api/runners` — отдельный триггер «не удаётся опросить /api/runners» без ложных сработок по самим раннерам.

Дашборд «Semaphore: обзор» входит в шаблон (проблемы + счётчики). Если при импорте в вашей минорной версии возникнет ошибка на секции `dashboards` — удалите её из YAML, на остальное это не влияет.

## Совместимость с Podman 4.9.x (проверено)

Против compat-API Podman 4.9 подтверждено: ping на версии API 1.28 (её использует
плагин Docker Agent 2); список контейнеров `/containers/json?all=true` отдаёт
имена с ведущим слэшем (`/podman_runner-01`) — регэксп макроса
`{$SEMAPHORE.RUNNER.MATCHES}` (`^/?podman_runner-.*`) это учитывает; в inspect
присутствуют все поля, используемые шаблоном: `State.Running`, `State.ExitCode`,
`RestartCount`. Пути статистики (`cpu_stats.cpu_usage.total_usage`,
`memory_stats.usage`) совпадают с официальным шаблоном «Docker by Zabbix
agent 2». Расчёт CPU намеренно построен на Change per second от счётчика
`total_usage`, а не на `precpu_stats` — у Podman известен баг с нулевыми
`precpu_stats` (containers/podman#24730), наш способ его не задевает.

Особенность Podman: для ОСТАНОВЛЕННОГО контейнера эндпоинт `/stats` возвращает
ошибку (Docker отдаёт нули), поэтому итем «статистика контейнера (raw)» у
неработающего раннера уходит в unsupported — это ожидаемо и не влияет на
триггер HIGH «контейнер раннера не запущен», который строится по inspect.
После старта контейнера итем восстанавливается сам.

## Тестовая проверка триггеров (шаг 4 плана)

1. Создайте в Semaphore три тестовых шаблона на ansible-core 2.13: заведомо успешный плейбук; плейбук с `ansible.builtin.fail`; инвентарь с несуществующим хостом.
2. Запустите их и убедитесь: статусы 0/3/3, для второго `hosts_failed>0`, для третьего `hosts_unreachable>0`, сработали соответствующие триггеры.
3. Остановите контейнер (`docker stop semaphore`) — должны сработать триггеры «контейнер не запущен» и «API недоступен»; после `docker start` проблемы закрываются.
4. Для проверки «долгого выполнения» временно уменьшите `{$SEMAPHORE.TASK.MAXTIME}` (например, до 30) и запустите плейбук с `ansible.builtin.pause`.

## Эксплуатационные заметки

- Вывод задачи (для RECAP) читается **один раз** на task_id и кешируется в `cache_file`, поэтому ежеминутный опрос не нагружает Semaphore повторным чтением логов.
- Сводка по проекту строится из `GET /api/project/{id}/tasks/last` (окно последних задач Semaphore) с дополнением из поля `last_task` списка шаблонов; шаблон без запусков получает статус `no data` (код 5).
- Значение `-1` в метриках failed/unreachable/age означает «нет данных» (recap не найден или запусков не было) — триггеры на `>0` при этом не срабатывают.
- При смене пароля/токена мониторингового пользователя обновите `/etc/zabbix/semaphore_monitor.conf`; при ошибке авторизации сработает триггер «ошибка опроса API (проверьте токен)».
