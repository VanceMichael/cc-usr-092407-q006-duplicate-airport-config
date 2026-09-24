#!/usr/bin/env python3
"""升级门禁冲突的黑盒验证（在业务容器内运行）。

主服务已经在 ``DB_PATH`` 上登记了一套机场定义并处理过事件。本脚本：

1. 复制夹具到临时目录，把 APS 的恢复缓冲改成另一个值（模拟部署时合入冲突配置）；
2. 用**同一个 SQLite 文件**、不同端口启动第二个 ``python -m app`` 实例；
3. 断言新实例 ``/healthz`` 仍为 200（进程存活）但 ``/readyz`` 与 ``/api`` 为
   503 config_conflict / service_not_ready，且错误带 APS 行的来源位置；
4. 终止新实例，断言主实例依旧 ready、数据库只含原修订、APS 缓冲未被改写。

全程只使用标准库，不访问任何外部服务。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "/data/disruptions.db")
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", "/srv/fixtures"))
MAIN_BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILURES.append(msg)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode())


def wait_for(path: str, port: int, want_status: int, timeout: float = 15.0):
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, body = get(base + path)
            last = (status, body)
            if status == want_status:
                return status, body
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(0.15)
    raise AssertionError(f"never got {want_status} on {path}: {last}")


def launch(fixtures_dir: Path, db_path: str):
    port = free_port()
    env = dict(os.environ)
    env.update(
        {
            "FIXTURES_DIR": str(fixtures_dir),
            "DB_PATH": db_path,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "PYTHONUNBUFFERED": "1",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "app"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc, port


def main() -> int:
    print("== gate ordering: a shuffled-but-identical config aligns ==")

    status, ready = get(f"{MAIN_BASE}/readyz")
    check(status == 200 and ready["status"] == "ready",
          f"primary instance is ready before probe (got {status})")
    good_digest = ready["gate"]["config_digest"]

    tmp = Path(tempfile.mkdtemp(prefix="gate-ordering-"))
    try:
        shuffled = tmp / "fixtures"
        shutil.copytree(FIXTURES_DIR, shuffled)
        airports = json.loads((shuffled / "airports.json").read_text())
        # Reverse row order; the canonical, code-sorted digest must be identical.
        (shuffled / "airports.json").write_text(json.dumps(list(reversed(airports))))
        proc, port = launch(shuffled, DB_PATH)
        try:
            status, body = wait_for("/readyz", port, 200)
            check(status == 200 and body["status"] == "ready",
                  f"shuffled-config instance becomes ready (got {status})")
            check(body["gate"]["config_digest"] == good_digest,
                  "shuffled row order adopts the same airport definition digest")
            check(body["gate"]["state"] == "aligned",
                  "shuffled-config instance aligns to the registered facts")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("== gate conflict: divergent second instance against the seeded DB ==")

    # Re-confirm the baseline after the aligned second instance exits.
    status, ready = get(f"{MAIN_BASE}/readyz")
    check(status == 200 and ready["status"] == "ready",
          f"primary instance is ready before conflict probe (got {status})")
    good_digest = ready["gate"]["config_digest"]

    tmp = Path(tempfile.mkdtemp(prefix="gate-conflict-"))
    try:
        bad_fixtures = tmp / "fixtures"
        shutil.copytree(FIXTURES_DIR, bad_fixtures)
        airports_path = bad_fixtures / "airports.json"
        airports = json.loads(airports_path.read_text())
        changed = False
        for row in airports:
            if row["code"] == "APS":
                row["reopen_buffer_minutes"] = 777
                changed = True
        assert changed, "fixture must contain APS"
        airports_path.write_text(json.dumps(airports))

        proc, port = launch(bad_fixtures, DB_PATH)
        try:
            # Liveness comes up even though the gate fails.
            status, _ = wait_for("/healthz", port, 200)
            check(status == 200, "conflicting instance stays alive (/healthz 200)")

            status, body = wait_for("/readyz", port, 503)
            check(status == 503 and body.get("reason") == "config_conflict",
                  f"conflicting instance is not ready (got {status}, {body.get('reason')})")
            issues = body.get("error", {}).get("details", {}).get("issues", [])
            aps_issue = next(
                (i for i in issues
                 if i.get("code") == "APS"
                 and i.get("field") == "reopen_buffer_minutes"),
                None,
            )
            check(aps_issue is not None,
                  "conflict names APS reopen_buffer_minutes")
            if aps_issue:
                check(aps_issue.get("registered") != 777
                      and aps_issue.get("configured") == 777,
                      f"conflict keeps registered value, rejects 777 (got {aps_issue})")
                check(str(aps_issue.get("configured_location", "")).endswith(
                    "airports.json[0]"),
                      f"conflict carries the source location (got {aps_issue.get('configured_location')})")

            status, body = get(f"http://127.0.0.1:{port}/api/v1/flights/affected")
            check(status == 503
                  and body.get("error", {}).get("code") == "service_not_ready",
                  f"conflicting instance refuses business traffic (got {status})")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Primary instance is untouched and still owns traffic.
    status, ready = get(f"{MAIN_BASE}/readyz")
    check(status == 200 and ready["status"] == "ready",
          "primary instance still ready after the conflicting attempt")
    check(ready["gate"]["config_digest"] == good_digest,
          "primary instance still serves the original config digest")

    import sqlite3

    con = sqlite3.connect(DB_PATH)
    try:
        revisions = [r[0] for r in con.execute(
            "SELECT config_digest FROM config_revisions")]
        buffer_min = con.execute(
            "SELECT reopen_buffer_minutes FROM airport_registry WHERE code='APS'"
        ).fetchone()[0]
    finally:
        con.close()
    check(revisions == [good_digest],
          f"no extra revision was registered by the rejected instance ({revisions})")
    check(buffer_min != 777,
          f"registered APS buffer was not overwritten (still {buffer_min})")

    if FAILURES:
        print(f"\nGATE CONFLICT CHECK FAILED: {len(FAILURES)} assertion(s)")
        return 1
    print("\nGATE CONFLICT CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
