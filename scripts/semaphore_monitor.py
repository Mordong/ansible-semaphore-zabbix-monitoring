#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
semaphore_monitor.py — мониторинг Ansible Semaphore для Zabbix Agent 2.

Режимы запуска (первый аргумент):
  ping       -> "1"/"0": доступность API Semaphore (GET /api/ping, без авторизации)
  discovery  -> LLD JSON для Zabbix: список пар проект/шаблон
  status     -> сводный JSON по последним задачам всех шаблонов (master item)
  runners    -> статус remote-раннеров: активность, heartbeat (нужен admin-токен)

Совместимость (проверено против форматов):
  * Semaphore v2.8+ (REST API v2, авторизация Bearer-токеном)
  * Zabbix 7.4.x (Zabbix Agent 2, UserParameter, LLD)
  * ansible-core 2.13 — парсинг PLAY RECAP с полями
    ok/changed/unreachable/failed/skipped/rescued/ignored
    (пары key=value разбираются обобщённо, ANSI-коды удаляются)

Требования: Python >= 3.7, только стандартная библиотека.

Конфигурация: JSON-файл /etc/zabbix/semaphore_monitor.conf (права 0600,
владелец zabbix). Путь можно переопределить переменной окружения
SEMAPHORE_MONITOR_CONF.
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

CONFIG_PATH = os.environ.get(
    "SEMAPHORE_MONITOR_CONF", "/etc/zabbix/semaphore_monitor.conf"
)

DEFAULTS = {
    "url": "http://127.0.0.1:3000",
    "token": "",
    "timeout": 10,            # сек, на один HTTP-запрос
    "verify_tls": True,
    "fetch_output": True,     # читать вывод последней задачи и парсить PLAY RECAP
    "max_output_fetch": 50,   # максимум задач за прогон, для которых читаем вывод
    "cache_file": "/var/tmp/zbx_semaphore_recap_cache.json",
}

# Статусы задач Semaphore -> числовые коды для Zabbix (value map в шаблоне)
STATUS_CODES = {
    "success": 0,
    "running": 1,
    "starting": 1,
    "waiting": 2,
    "error": 3,
    "stopping": 4,
    "stopped": 4,
}
UNKNOWN_CODE = 5  # нет данных о запусках

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Строка PLAY RECAP (ansible-core 2.13):
#   host : ok=5 changed=2 unreachable=0 failed=0 skipped=1 rescued=0 ignored=0
RECAP_LINE_RE = re.compile(
    r"^(?P<host>[^\s:]+)\s*:\s*(?P<pairs>(?:[a-z]+=\d+\s*)+)$"
)


# ----------------------------------------------------------------- утилиты --

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        die("config error: %s" % exc)
    return cfg


def die(msg):
    print(json.dumps({"api_reachable": 0, "error": str(msg)[:300],
                      "summary": {}, "tasks": []}))
    sys.exit(0)  # 0, чтобы Zabbix получил JSON, а не NOTSUPPORTED без данных


def _open(cfg, path, need_auth=True):
    url = cfg["url"].rstrip("/") + path
    headers = {"Accept": "application/json",
               "User-Agent": "zbx-semaphore-monitor/1.0"}
    if need_auth and cfg.get("token"):
        headers["Authorization"] = "Bearer " + cfg["token"]
    req = urllib.request.Request(url, headers=headers)
    ctx = None
    if url.startswith("https") and not cfg.get("verify_tls", True):
        ctx = ssl._create_unverified_context()
    return urllib.request.urlopen(req, timeout=cfg["timeout"], context=ctx)


def api_get_text(cfg, path, need_auth=True):
    with _open(cfg, path, need_auth) as resp:
        return resp.read().decode("utf-8", "replace")


def api_get(cfg, path, need_auth=True):
    return json.loads(api_get_text(cfg, path, need_auth))


def parse_ts(value):
    """ISO8601 -> unix timestamp; пустые/нулевые даты -> None."""
    if not value or str(value).startswith("0001-01-01"):
        return None
    v = str(value).strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # дробные секунды обрезаем до 6 знаков (ограничение fromisoformat)
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", v)
    if m:
        frac = (m.group(2) or "")[:7]
        v = m.group(1) + frac + (m.group(3) or "")
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


# ---------------------------------------------------- разбор PLAY RECAP ----

def parse_recap(text):
    """Возвращает суммарную статистику по последней секции PLAY RECAP
    или None, если секция не найдена (формат ansible-core 2.13)."""
    lines = [ANSI_RE.sub("", ln).rstrip() for ln in text.splitlines()]
    idx = None
    for i, ln in enumerate(lines):
        if "PLAY RECAP" in ln:
            idx = i  # берём последнюю секцию recap в выводе
    if idx is None:
        return None
    stats = {"ok": 0, "changed": 0, "failed": 0, "unreachable": 0, "hosts": 0}
    for ln in lines[idx + 1:]:
        s = ln.strip()
        if not s:
            if stats["hosts"]:
                break
            continue
        m = RECAP_LINE_RE.match(s)
        if not m:
            if stats["hosts"]:
                break
            continue
        stats["hosts"] += 1
        for pair in m.group("pairs").split():
            key, _, num = pair.partition("=")
            if key in ("ok", "changed", "failed", "unreachable"):
                try:
                    stats[key] += int(num)
                except ValueError:
                    pass
    return stats if stats["hosts"] else None


def load_cache(cfg):
    try:
        with open(cfg["cache_file"], "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def save_cache(cfg, cache):
    # держим не более 1000 последних task_id
    if len(cache) > 1000:
        for key in sorted(cache, key=lambda x: int(x))[: len(cache) - 1000]:
            cache.pop(key, None)
    tmp = cfg["cache_file"] + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, cfg["cache_file"])
    except OSError:
        pass


def task_recap(cfg, cache, project_id, task):
    """Статистика PLAY RECAP последней задачи (с файловым кешем по task_id)."""
    tid = task.get("id")
    key = str(tid)
    if key in cache:
        return cache[key], False
    try:
        rows = api_get(cfg, "/api/project/%d/tasks/%d/output"
                       % (project_id, tid))
        text = "\n".join(str(r.get("output", "")) for r in rows)
        stats = parse_recap(text)
    except Exception:
        stats = None
    cache[key] = stats
    return stats, True


# ------------------------------------------------------------------ режимы --

def mode_ping(cfg):
    try:
        txt = api_get_text(cfg, "/api/ping", need_auth=False)
        return "1" if txt.strip() else "0"
    except Exception:
        return "0"


def mode_runners(cfg):
    """Статус раннеров Semaphore (remote runners): активность и heartbeat.
    Эндпоинт /api/runners требует токен администратора."""
    now = int(time.time())
    try:
        runners = api_get(cfg, "/api/runners")
    except Exception as exc:
        return {"api_reachable": 0, "error": str(exc)[:300],
                "summary": {"total": 0, "active": 0}, "runners": []}
    out = []
    active_n = 0
    for r in runners or []:
        rid = r.get("id")
        if rid is None:
            continue
        acode = 1 if r.get("active") else 0
        active_n += acode
        hb = None
        for field in ("touched", "last_touched", "updated", "created"):
            hb = parse_ts(r.get(field))
            if hb:
                break
        out.append({
            "id": rid,
            "name": r.get("name") or ("runner-%s" % rid),
            "active_code": acode,
            "heartbeat_age_sec": max(0, now - hb) if hb else -1,
        })
    return {"api_reachable": 1, "error": "",
            "summary": {"total": len(out), "active": active_n},
            "runners": out}


def collect(cfg):
    """Проекты -> шаблоны -> последняя задача по каждому шаблону."""
    projects = api_get(cfg, "/api/projects")
    data = []
    for prj in projects:
        pid = prj.get("id")
        pname = prj.get("name", "")
        templates = api_get(cfg, "/api/project/%d/templates" % pid)
        try:
            recent = api_get(cfg, "/api/project/%d/tasks/last" % pid)
        except Exception:
            recent = []
        last_by_tpl = {}
        for t in recent:
            tpl_id = t.get("template_id")
            if tpl_id is None:
                continue
            cur = last_by_tpl.get(tpl_id)
            if cur is None or (t.get("id") or 0) > (cur.get("id") or 0):
                last_by_tpl[tpl_id] = t
        for tpl in templates:
            tpl_id = tpl.get("id")
            task = last_by_tpl.get(tpl_id) or tpl.get("last_task") or None
            data.append({
                "project_id": pid,
                "project_name": pname,
                "template_id": tpl_id,
                "template_name": tpl.get("name", ""),
                "task": task,
            })
    return data


def mode_discovery(cfg):
    rows = []
    for item in collect(cfg):
        rows.append({
            "{#PROJECT_ID}": item["project_id"],
            "{#PROJECT_NAME}": item["project_name"],
            "{#TEMPLATE_ID}": item["template_id"],
            "{#TEMPLATE_NAME}": item["template_name"],
        })
    return rows


def mode_status(cfg):
    now = int(time.time())
    result = {"api_reachable": 1, "error": "", "summary": {}, "tasks": []}
    try:
        data = collect(cfg)
    except Exception as exc:
        return {"api_reachable": 0, "error": str(exc)[:300],
                "summary": {"projects": 0, "templates": 0,
                            "running": 0, "errors_last": 0},
                "tasks": []}

    cache = load_cache(cfg) if cfg.get("fetch_output") else {}
    cache_dirty = False
    fetched = 0
    running = errors_last = 0
    project_ids = set()

    for item in data:
        project_ids.add(item["project_id"])
        entry = {
            "project_id": item["project_id"],
            "project_name": item["project_name"],
            "template_id": item["template_id"],
            "template_name": item["template_name"],
            "task_id": 0,
            "status": "unknown",
            "status_code": UNKNOWN_CODE,
            "duration_sec": 0,
            "age_sec": -1,
            "hosts_ok": -1,
            "hosts_changed": -1,
            "hosts_failed": -1,
            "hosts_unreachable": -1,
            "recap_found": 0,
        }
        task = item["task"]
        if task:
            status = str(task.get("status", "")).lower()
            code = STATUS_CODES.get(status, UNKNOWN_CODE)
            start_ts = parse_ts(task.get("start"))
            end_ts = parse_ts(task.get("end"))
            created_ts = parse_ts(task.get("created"))
            duration = 0
            if start_ts and end_ts:
                duration = max(0, end_ts - start_ts)
            elif start_ts and code == 1:          # выполняется сейчас
                duration = max(0, now - start_ts)
            ref = end_ts or start_ts or created_ts
            entry.update({
                "task_id": task.get("id") or 0,
                "status": status or "unknown",
                "status_code": code,
                "duration_sec": duration,
                "age_sec": max(0, now - ref) if ref else -1,
            })
            if code == 1:
                running += 1
            elif code == 3:
                errors_last += 1
            # PLAY RECAP читаем только для завершённых задач
            if (cfg.get("fetch_output") and code in (0, 3)
                    and fetched < int(cfg.get("max_output_fetch", 50))):
                stats, did_fetch = task_recap(cfg, cache,
                                              item["project_id"], task)
                if did_fetch:
                    fetched += 1
                    cache_dirty = True
                if stats:
                    entry.update({
                        "hosts_ok": stats.get("ok", -1),
                        "hosts_changed": stats.get("changed", -1),
                        "hosts_failed": stats.get("failed", -1),
                        "hosts_unreachable": stats.get("unreachable", -1),
                        "recap_found": 1,
                    })
        result["tasks"].append(entry)

    if cache_dirty:
        save_cache(cfg, cache)

    result["summary"] = {
        "projects": len(project_ids),
        "templates": len(data),
        "running": running,
        "errors_last": errors_last,
    }
    return result


# -------------------------------------------------------------------- main --

def main():
    modes = ("ping", "discovery", "status", "runners")
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        sys.stderr.write("usage: semaphore_monitor.py %s\n" % "|".join(modes))
        sys.exit(1)
    mode = sys.argv[1]
    cfg = load_config()

    if mode == "ping":
        print(mode_ping(cfg))
        return

    if not cfg.get("token"):
        if mode == "discovery":
            print("[]")
            return
        die("token is not set in " + CONFIG_PATH)

    if mode == "runners":
        print(json.dumps(mode_runners(cfg)))
        return

    if mode == "discovery":
        try:
            print(json.dumps(mode_discovery(cfg)))
        except Exception as exc:
            # пустой LLD вместо ошибки, чтобы не удалять найденные объекты
            sys.stderr.write("discovery error: %s\n" % exc)
            print("[]")
        return

    print(json.dumps(mode_status(cfg)))


if __name__ == "__main__":
    main()
