# Changelog

## 1.0.0 — 2026-07-07

Первый релиз.

- Скрипт `semaphore_monitor.py` (ping / discovery / status): REST API
  Semaphore, парсинг PLAY RECAP ansible-core 2.13, файловый кеш вывода задач.
- Шаблон Zabbix 7.4 «Semaphore by Zabbix agent 2»: доступность API и
  контейнеров (Docker/Podman), LLD по шаблонам задач, триггеры на error /
  failed / unreachable / долгое выполнение, дашборд.
- UserParameter для Zabbix Agent 2, пример конфига плагина Docker под Podman.
- Деплой-плейбук Ansible (модули ansible.builtin, совместим с ansible-core 2.13).
- Автотесты: юнит-тесты парсеров + интеграционный прогон против мок-API.

## 1.0.1 — 2026-07-07

- Все триггеры и прототипы триггеров: Allow manual close = Yes.
- Тег `application: ansible-semaphore` на всех итемах, триггерах и прототипах
  (LLD-правило тегов в Zabbix не имеет — теги наследуются через прототипы).
- Итем «PostgreSQL: TCP-порт доступен» выключен по умолчанию: в типовой
  установке порт БД не публикуется из контейнера, проверка давала ложный
  триггер. Включать только при опубликованном порте.
- README/deploy: ExecStartPost-права на каталог /run/podman (доступ группы
  zabbix к сокету требует прохода по каталогу).

## 1.1.0 — 2026-07-07

Мониторинг раннеров Semaphore.

- Скрипт: новый режим `runners` (`/api/runners`, admin-токен) — активность и
  возраст heartbeat каждого раннера; мягкая деградация при недоступности.
- Новый UserParameter `semaphore.runners`.
- Шаблон: LLD контейнеров раннеров (`docker.containers.discovery[true]`,
  фильтр `{$SEMAPHORE.RUNNER.MATCHES}`, по умолчанию `^/?podman_runner-.*`) —
  running / exit code / рестарты / CPU / память, триггер HIGH; зависимое LLD
  раннеров из API — активность и heartbeat, триггеры AVERAGE/WARNING; итемы
  `semaphore.runners.total|active|reachable`. Все новые триггеры — manual
  close, все сущности — тег `application: ansible-semaphore`. UUID сущностей
  v1.0.x не менялись — импорт поверх безопасен.
- Тесты: мок `/api/runners`, проверки активности/heartbeat/деградации.

## 1.1.1 — 2026-07-07

- Проверена совместимость с Podman 4.9.x (compat-API 1.28): LLD, inspect-поля
  State.Running/ExitCode/RestartCount, формат имён с ведущим слэшем.
  Задокументирована особенность: /stats остановленного контейнера в Podman
  возвращает ошибку (итем raw-статистики в unsupported до старта контейнера).
- Подтверждена работа на боевом стенде: Zabbix 7.4.5, Zabbix Agent 2,
  Semaphore в Podman 4.9.5 (rootful), ansible-core 2.13 — обнаружение задач
  и раннеров, статусы, PLAY RECAP, container-метрики поступают.
