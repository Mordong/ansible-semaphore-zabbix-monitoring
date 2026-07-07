#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автотесты semaphore_monitor.py: юнит-тесты парсеров PLAY RECAP
(формат ansible-core 2.13) и timestamp'ов + интеграционный прогон трёх
режимов против встроенного мок-API Semaphore.

Запуск:  python3 tests/test_semaphore_monitor.py
Зависимости: только стандартная библиотека.
"""

import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "semaphore_monitor.py")

spec = importlib.util.spec_from_file_location("mon", SCRIPT)
mon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mon)


# ------------------------------------------------------------- юнит-тесты --

def test_parse_recap():
    recap = (
        "PLAY [all] *****\n"
        "\x1b[0;31mfatal: [db1]: FAILED!\x1b[0m\n"
        "PLAY RECAP *****\n"
        "\x1b[0;32mweb1\x1b[0m : ok=5 changed=2 unreachable=0 failed=0 "
        "skipped=1 rescued=0 ignored=0\n"
        "db1  : ok=3 changed=0 unreachable=1 failed=1 "
        "skipped=0 rescued=0 ignored=0\n"
        "\n"
        "trailing semaphore line\n"
    )
    stats = mon.parse_recap(recap)
    assert stats == {"ok": 8, "changed": 2, "failed": 1,
                     "unreachable": 1, "hosts": 2}, stats
    assert mon.parse_recap("no recap here") is None


def test_parse_ts():
    assert mon.parse_ts("2026-07-07T10:00:00Z") is not None
    assert mon.parse_ts("2026-07-07T10:00:00.123456789+03:00") is not None
    assert mon.parse_ts("0001-01-01T00:00:00Z") is None
    assert mon.parse_ts(None) is None
    assert mon.parse_ts("garbage") is None


# ------------------------------------------------------------- мок-сервер --

ROUTES = {
    "/api/ping": "pong",
    "/api/projects": [{"id": 1, "name": "Infra"}],
    "/api/project/1/templates": [
        {"id": 10, "name": "deploy-web"},
        {"id": 11, "name": "patch-db"},
        {"id": 12, "name": "never-run"},
    ],
    "/api/project/1/tasks/last": [
        {"id": 101, "template_id": 10, "status": "success",
         "created": "2026-01-01T09:00:00Z",
         "start": "2026-01-01T09:00:01Z", "end": "2026-01-01T09:02:31Z"},
        {"id": 102, "template_id": 11, "status": "error",
         "created": "2026-01-01T08:00:00Z",
         "start": "2026-01-01T08:00:02Z", "end": "2026-01-01T08:01:00Z"},
        {"id": 99, "template_id": 10, "status": "error",
         "created": "2025-12-31T09:00:00Z",
         "start": "2025-12-31T09:00:01Z", "end": "2025-12-31T09:02:00Z"},
    ],
    "/api/project/1/tasks/101/output": [
        {"task_id": 101, "output": "PLAY RECAP ****"},
        {"task_id": 101, "output": "web1 : ok=4 changed=1 unreachable=0 "
                                   "failed=0 skipped=0 rescued=0 ignored=0"},
    ],
    "/api/project/1/tasks/102/output": [
        {"task_id": 102, "output": "PLAY RECAP ****"},
        {"task_id": 102, "output": "db1 : ok=2 changed=0 unreachable=1 "
                                   "failed=1 skipped=0 rescued=0 ignored=0"},
    ],
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if (self.path != "/api/ping"
                and self.headers.get("Authorization") != "Bearer testtoken"):
            self.send_response(401)
            self.end_headers()
            return
        body = ROUTES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body if isinstance(body, str) else json.dumps(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data.encode())

    def log_message(self, *args):
        pass


def run_script(mode, conf_path):
    proc = subprocess.run(
        [sys.executable, SCRIPT, mode],
        env={"SEMAPHORE_MONITOR_CONF": conf_path,
             "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# ------------------------------------------------------ интеграционный тест --

def test_integration():
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "monitor.conf")
            cache = os.path.join(tmp, "cache.json")
            with open(conf, "w") as fh:
                json.dump({"url": "http://127.0.0.1:%d" % port,
                           "token": "testtoken",
                           "cache_file": cache}, fh)

            assert run_script("ping", conf) == "1"

            lld = json.loads(run_script("discovery", conf))
            assert len(lld) == 3
            assert {r["{#TEMPLATE_ID}"] for r in lld} == {10, 11, 12}
            assert all("{#PROJECT_NAME}" in r for r in lld)

            st = json.loads(run_script("status", conf))
            assert st["api_reachable"] == 1
            assert st["summary"] == {"projects": 1, "templates": 3,
                                     "running": 0, "errors_last": 1}
            by_tpl = {t["template_id"]: t for t in st["tasks"]}
            ok = by_tpl[10]
            assert (ok["status_code"], ok["task_id"]) == (0, 101)
            assert ok["duration_sec"] == 150 and ok["hosts_failed"] == 0
            err = by_tpl[11]
            assert err["status_code"] == 3
            assert err["hosts_failed"] == 1 and err["hosts_unreachable"] == 1
            nodata = by_tpl[12]
            assert nodata["status_code"] == 5 and nodata["hosts_failed"] == -1

            # кеш: второй прогон читает recap из файла, а не из API
            run_script("status", conf)
            with open(cache) as fh:
                assert set(json.load(fh)) == {"101", "102"}

            # деградация: неверный токен -> api_reachable 0, discovery -> []
            bad = os.path.join(tmp, "bad.conf")
            with open(bad, "w") as fh:
                json.dump({"url": "http://127.0.0.1:%d" % port,
                           "token": "wrong", "cache_file": cache}, fh)
            st_bad = json.loads(run_script("status", bad))
            assert st_bad["api_reachable"] == 0
            assert run_script("discovery", bad) == "[]"
    finally:
        srv.shutdown()


if __name__ == "__main__":
    for fn in (test_parse_recap, test_parse_ts, test_integration):
        fn()
        print("PASS %s" % fn.__name__)
    print("ALL TESTS PASSED")
