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
