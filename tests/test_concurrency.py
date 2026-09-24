"""真实子进程级别的确定性验证。

这些测试启动多个独立的 ``python -m app`` 进程（等价于多个容器实例），让它们
共享同一个 SQLite 文件，证明：

* 夹具行序变化不会改变采用的机场定义（顺序无关摘要）。
* 容器/进程重启后门禁对齐到同一份已登记定义。
* 多个进程在 **空库上同时启动** 时，经 ``BEGIN IMMEDIATE`` 串行化只登记一次；
  全部就绪且服务同一份 config_digest，并发写入同一事件得到完全一致的结果。
* 携带不同机场定义的进程同时启动时，恰好一种定义被登记，其余被门禁阻塞
  （503），绝不会出现覆盖或"后启动者获胜"。
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
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.config import ROOT, load_airports

REPO_ROOT = ROOT
FIXTURES = ROOT / "fixtures"


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method: str, url: str, body=None, timeout: float = 3.0):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


CLOSE_EVENT = {
    "event_id": "evt-multi0000001",
    "event_version": 1,
    "event_type": "airport.closed",
    "airport_code": "APS",
    "effective_from": "2026-09-07T15:00:00Z",
    "effective_until": "2026-09-07T19:00:00Z",
    "reported_at": "2026-09-07T14:00:00Z",
}


class ServerProcess:
    def __init__(self, fixtures_dir: Path, db_path: Path, port: int):
        self.port = port
        env = dict(os.environ)
        env.update(
            {
                "FIXTURES_DIR": str(fixtures_dir),
                "DB_PATH": str(db_path),
                "HOST": "127.0.0.1",
                "PORT": str(port),
                "PYTHONPATH": str(REPO_ROOT),
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
        )

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def probe(self, timeout: float = 12.0):
        """Return (ready: bool, body) once /readyz answers or timeout elapses."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode("utf-8", "replace") if self.proc.stderr else ""
                raise AssertionError(f"server exited early: {err}")
            try:
                status, body = http("GET", f"{self.base}/readyz", timeout=1.5)
                return status == 200, body
            except (urllib.error.URLError, ConnectionError, OSError):
                last = sys.exc_info()
                time.sleep(0.1)
        raise AssertionError(f"server never answered on port {self.port}: {last}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class MultiProcessCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "data" / "shared.db"
        self.db.parent.mkdir(parents=True)
        self.procs: list[ServerProcess] = []
        self.canonical_digest = load_airports(FIXTURES).digest

    def tearDown(self) -> None:
        for p in self.procs:
            p.stop()
        self._tmp.cleanup()

    def make_fixtures(self, name: str, *, shuffle: bool = False,
                      aps_buffer: int | None = None) -> Path:
        dst = self.dir / name
        dst.mkdir(parents=True)
        shutil.copy(FIXTURES / "flights.json", dst / "flights.json")
        airports = json.loads((FIXTURES / "airports.json").read_text())
        if aps_buffer is not None:
            for row in airports:
                if row["code"] == "APS":
                    row["reopen_buffer_minutes"] = aps_buffer
        if shuffle:
            airports = [airports[2], airports[0], airports[1]]
        (dst / "airports.json").write_text(json.dumps(airports))
        return dst

    def start(self, fixtures: Path) -> ServerProcess:
        proc = ServerProcess(fixtures, self.db, free_port())
        self.procs.append(proc)
        return proc

    def registry_digests(self) -> list[str]:
        import sqlite3

        con = sqlite3.connect(self.db)
        rows = [r[0] for r in con.execute(
            "SELECT config_digest FROM config_revisions ORDER BY config_digest")]
        con.close()
        return rows

    # ------------------------------------------------------------------ #

    def test_row_order_change_adopts_same_definition(self) -> None:
        fixtures = self.make_fixtures("shuffled", shuffle=True)
        proc = self.start(fixtures)
        ready, body = proc.probe()
        self.assertTrue(ready, body)
        self.assertEqual(body["gate"]["config_digest"], self.canonical_digest)
        self.assertEqual(
            body["gate"]["registered_airports"], ["APS", "BSR", "KTA"]
        )
        # The registry stores canonical, code-sorted facts regardless of order.
        self.assertEqual(self.registry_digests(), [self.canonical_digest])

    def test_restart_realigns_to_registered_definition(self) -> None:
        fixtures = self.make_fixtures("good")
        first = self.start(fixtures)
        ready, _ = first.probe()
        self.assertTrue(ready)
        status, created = http("POST", f"{first.base}/api/v1/events", CLOSE_EVENT)
        self.assertEqual(status, 201)
        first.stop()
        self.procs.remove(first)

        second = self.start(fixtures)
        ready, body = second.probe()
        self.assertTrue(ready)
        self.assertEqual(body["gate"]["state"], "aligned")
        self.assertEqual(body["gate"]["config_digest"], self.canonical_digest)

        status, fetched = http(
            "GET", f"{second.base}/api/v1/events/{CLOSE_EVENT['event_id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            fetched["config"]["config_digest"], self.canonical_digest
        )
        self.assertEqual(fetched["config"]["airport"]["code"], "APS")
        self.assertEqual(fetched["config"]["airport"]["reopen_buffer_minutes"], 20)
        self.assertEqual(len(fetched["impacts"]), created["impact_count"])

    def test_simultaneous_starts_register_once_and_share_digest(self) -> None:
        fixtures = self.make_fixtures("good")
        n = 6
        procs = [self.start(fixtures) for _ in range(n)]
        # All starters race on an EMPTY database.
        results = [p.probe() for p in procs]
        digests = set()
        states = []
        for ready, body in results:
            self.assertTrue(ready, f"a same-config starter was rejected: {body}")
            digests.add(body["gate"]["config_digest"])
            states.append(body["gate"]["state"])
        self.assertEqual(digests, {self.canonical_digest})
        # Exactly one process performed the initial registration...
        self.assertEqual(states.count("empty_initialized"), 1)
        # ...every other process aligned to the committed facts.
        self.assertEqual(states.count("aligned"), n - 1)
        self.assertEqual(self.registry_digests(), [self.canonical_digest])

        # Concurrent submissions of the SAME event serialize; one is
        # "processed", the rest are idempotent "replayed", and every response
        # carries the identical airport definition and impact set.
        responses = []
        for p in procs:
            status, body = http("POST", f"{p.base}/api/v1/events", CLOSE_EVENT)
            self.assertEqual(status, 201, body)
            responses.append(body)
        states_seen = {r["processing_state"] for r in responses}
        self.assertTrue(states_seen <= {"processed", "replayed"})
        self.assertIn("processed", states_seen)
        baseline = responses[0]
        for r in responses[1:]:
            self.assertEqual(r["impacts"], baseline["impacts"])
            self.assertEqual(r["config"], baseline["config"])
            self.assertEqual(
                r["config"]["config_digest"], self.canonical_digest
            )

    def test_simultaneous_divergent_starts_one_definition_wins(self) -> None:
        good = self.make_fixtures("good")
        bad = self.make_fixtures("bad", aps_buffer=77)
        good_procs = [self.start(good) for _ in range(3)]
        bad_procs = [self.start(bad) for _ in range(3)]
        # Launch probes concurrently-ish: they were all Popen'd before probing,
        # so the gates genuinely race on an empty database.
        outcomes = []
        for p in good_procs + bad_procs:
            ready, body = p.probe()
            outcomes.append((ready, body))

        ready_bodies = [b for ready, b in outcomes if ready]
        blocked = [b for ready, b in outcomes if not ready]

        # The database holds exactly one coherent definition.
        registered = self.registry_digests()
        self.assertEqual(len(registered), 1)

        if ready_bodies:
            winner = ready_bodies[0]["gate"]["config_digest"]
            # Every instance that took traffic serves that one definition.
            self.assertEqual(
                {b["gate"]["config_digest"] for b in ready_bodies}, {winner}
            )
            self.assertEqual(registered, [winner])

        # Every loser reports a deterministic config_conflict and did not serve.
        for body in blocked:
            self.assertEqual(body["reason"], "config_conflict")
            issue = body["error"]["details"]["issues"][0]
            self.assertEqual(issue["code"], "APS")
            self.assertEqual(issue["field"], "reopen_buffer_minutes")

        # Whichever definition won, the registry row is internally consistent
        # (never a mix of the two buffers).
        import sqlite3

        con = sqlite3.connect(self.db)
        buffers = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT code, reopen_buffer_minutes FROM airport_registry"
            )
        }
        con.close()
        self.assertIn(buffers["APS"], (20, 77))
        self.assertEqual(len(outcomes), 6)


if __name__ == "__main__":
    unittest.main()
