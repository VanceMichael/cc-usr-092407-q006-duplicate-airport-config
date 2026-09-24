"""启动门禁与"同一输入始终采用同一份机场定义"的验证。

覆盖：

* 空库启动登记机场事实与配置摘要；
* 相同配置重启通过；修改/删除机场定义被拒绝且旧实例保持可读；
* 修复前版本创建的库（无机场事实表、events 无 config_digest 列）升级；
* 冲突时不留下半初始化数据库；
* 配置记录顺序变化、容器重启、多进程同时启动都得到同一份机场定义。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.bootstrap import bootstrap
from app.config import ROOT, Config, airports_digest, load_airports
from app.errors import ConfigConflictError, ConfigError
from app.repository import Repository
from tests.support import FIXTURES_DIR, base_event

# 修复前版本的 events 表结构（无 config_digest 列，无机场事实表）。
LEGACY_SCHEMA = """
CREATE TABLE events (
    event_id             TEXT PRIMARY KEY,
    event_version        INTEGER NOT NULL,
    event_type           TEXT NOT NULL,
    airport_code         TEXT NOT NULL,
    effective_from       TEXT NOT NULL,
    effective_until      TEXT,
    reported_at          TEXT NOT NULL,
    supersedes_event_id  TEXT,
    reason               TEXT,
    payload_json         TEXT NOT NULL,
    replay_count         INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL
);
"""

LEGACY_EVENT = {
    "event_id": "evt-legacy000001",
    "event_version": 1,
    "event_type": "airport.closed",
    "airport_code": "APS",
    "effective_from": "2026-09-07T15:00:00Z",
    "effective_until": "2026-09-07T19:00:00Z",
    "reported_at": "2026-09-07T14:00:00Z",
    "supersedes_event_id": None,
    "reason": "legacy ash",
}


def make_legacy_db(db_path: Path, airport_code: str = "APS") -> None:
    """按修复前版本的结构与数据创建一个旧库。"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA)
    payload = dict(LEGACY_EVENT, airport_code=airport_code)
    conn.execute(
        """
        INSERT INTO events (event_id, event_version, event_type, airport_code,
                            effective_from, effective_until, reported_at,
                            supersedes_event_id, reason, payload_json,
                            replay_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            payload["event_id"],
            payload["event_version"],
            payload["event_type"],
            payload["airport_code"],
            payload["effective_from"],
            payload["effective_until"],
            payload["reported_at"],
            payload["supersedes_event_id"],
            payload["reason"],
            json.dumps(payload, sort_keys=True),
            "2026-09-07T14:00:01Z",
        ),
    )
    conn.commit()
    conn.close()


class StartupGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "gate.db"
        self.airports = load_airports(FIXTURES_DIR)
        self.digest = airports_digest(self.airports)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self, fixtures_dir: Path = FIXTURES_DIR) -> Config:
        return Config(
            fixtures_dir=fixtures_dir,
            db_path=self.db_path,
            host="127.0.0.1",
            port=0,
        )

    def write_fixtures(
        self, airports: list, name: str = "fixtures-alt", flights: list | None = None
    ) -> Path:
        directory = self.tmp / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "airports.json").write_text(
            json.dumps(airports, ensure_ascii=False), encoding="utf-8"
        )
        if flights is None:
            shutil.copy(FIXTURES_DIR / "flights.json", directory / "flights.json")
        else:
            (directory / "flights.json").write_text(
                json.dumps(flights, ensure_ascii=False), encoding="utf-8"
            )
        return directory

    def airport_rows(self, **overrides) -> list[dict]:
        rows = [
            {
                "code": "APS",
                "name": "Awan Pura International",
                "timezone": "Asia/Makassar",
                "reopen_buffer_minutes": 20,
            },
            {
                "code": "BSR",
                "name": "Basa Raya Airport",
                "timezone": "Asia/Jakarta",
                "reopen_buffer_minutes": 15,
            },
            {
                "code": "KTA",
                "name": "Karta Regional",
                "timezone": "Asia/Jakarta",
                "reopen_buffer_minutes": 10,
            },
        ]
        for row in rows:
            if row["code"] in overrides:
                row.update(overrides[row["code"]])
        return rows

    # ------------------------------------------------------------------ #
    # 登记与重启
    # ------------------------------------------------------------------ #

    def test_empty_db_registers_facts_and_digest(self) -> None:
        context = bootstrap(self.config())
        try:
            facts = context.repo.airport_facts()
            self.assertEqual(sorted(facts), ["APS", "BSR", "KTA"])
            self.assertEqual(facts["APS"]["timezone"], "Asia/Makassar")
            self.assertEqual(facts["APS"]["reopen_buffer_minutes"], 20)
            self.assertEqual(context.repo.registered_digest(), self.digest)
            ready, detail = context.service.ready()
            self.assertTrue(ready)
            self.assertEqual(detail["config_digest"], self.digest)
        finally:
            context.close()

    def test_restart_with_same_config_accepted(self) -> None:
        first = bootstrap(self.config())
        first.service.submit_event(base_event())
        first.close()
        second = bootstrap(self.config())
        try:
            self.assertEqual(second.repo.registered_digest(), self.digest)
            status = second.service.event_status("evt-close0000001")
            self.assertEqual(status["processing"]["config_digest"], self.digest)
        finally:
            second.close()

    def test_changed_timezone_refused_and_old_instance_stays_readable(self) -> None:
        old = bootstrap(self.config())
        old.service.submit_event(base_event())
        try:
            rows = self.airport_rows(APS={"timezone": "America/New_York"})
            alt_dir = self.write_fixtures(rows)
            with self.assertRaises(ConfigConflictError) as ctx:
                bootstrap(self.config(alt_dir))
            conflicts = ctx.exception.details["conflicts"]
            self.assertEqual(conflicts[0]["issue"], "airport_definition_changed")
            self.assertEqual(conflicts[0]["airport_code"], "APS")
            self.assertEqual(
                conflicts[0]["fields"]["timezone"],
                {"registered": "Asia/Makassar", "configured": "America/New_York"},
            )
            # 数据库中的事实未被改写……
            facts = old.repo.airport_facts()
            self.assertEqual(facts["APS"]["timezone"], "Asia/Makassar")
            self.assertEqual(old.repo.registered_digest(), self.digest)
            # ……旧实例保持可读、可写，继续服务。
            status = old.service.event_status("evt-close0000001")
            self.assertEqual(len(status["impacts"]), 3)
            followup = old.service.submit_event(
                base_event(
                    event_id="evt-after-refusal",
                    event_version=2,
                    effective_from="2026-09-08T15:00:00Z",
                    effective_until="2026-09-08T19:00:00Z",
                )
            )
            self.assertEqual(followup["processing_state"], "processed")
        finally:
            old.close()

    def test_changed_buffer_refused(self) -> None:
        bootstrap(self.config()).close()
        rows = self.airport_rows(APS={"reopen_buffer_minutes": 90})
        with self.assertRaises(ConfigConflictError) as ctx:
            bootstrap(self.config(self.write_fixtures(rows)))
        fields = ctx.exception.details["conflicts"][0]["fields"]
        self.assertEqual(
            fields["reopen_buffer_minutes"], {"registered": 20, "configured": 90}
        )

    def test_removed_airport_refused(self) -> None:
        bootstrap(self.config()).close()
        rows = self.airport_rows()[:2]  # 删掉 KTA
        # 航班文件同步移除引用 KTA 的航班，使配置本身有效，
        # 冲突只能来自库中已登记的机场事实。
        flights = json.loads((FIXTURES_DIR / "flights.json").read_text())
        flights = [
            f for f in flights if "KTA" not in (f["origin"], f["destination"])
        ]
        with self.assertRaises(ConfigConflictError) as ctx:
            bootstrap(self.config(self.write_fixtures(rows, flights=flights)))
        issues = [c["issue"] for c in ctx.exception.details["conflicts"]]
        self.assertIn("registered_airport_missing_from_config", issues)

    def test_added_airport_registered(self) -> None:
        bootstrap(self.config()).close()
        rows = self.airport_rows()
        rows.append(
            {
                "code": "XYZ",
                "name": "Xylophone Bay",
                "timezone": "UTC",
                "reopen_buffer_minutes": 5,
            }
        )
        context = bootstrap(self.config(self.write_fixtures(rows)))
        try:
            facts = context.repo.airport_facts()
            self.assertIn("XYZ", facts)
            self.assertEqual(facts["XYZ"]["timezone"], "UTC")
            # 既有机场事实保持不变。
            self.assertEqual(facts["APS"]["timezone"], "Asia/Makassar")
        finally:
            context.close()

    # ------------------------------------------------------------------ #
    # 从修复前版本升级
    # ------------------------------------------------------------------ #

    def test_legacy_db_upgrade_registers_facts(self) -> None:
        make_legacy_db(self.db_path, airport_code="APS")
        context = bootstrap(self.config())
        try:
            facts = context.repo.airport_facts()
            self.assertEqual(sorted(facts), ["APS", "BSR", "KTA"])
            # 旧事件仍可读；其 config_digest 为 NULL（计算时还没有摘要）。
            status = context.service.event_status("evt-legacy000001")
            self.assertIsNone(status["processing"]["config_digest"])
            # 新事件携带当前摘要。
            result = context.service.submit_event(base_event())
            self.assertEqual(result["config_digest"], self.digest)
        finally:
            context.close()

    def test_legacy_db_with_unconfigured_airport_events_refused(self) -> None:
        make_legacy_db(self.db_path, airport_code="ZZZ")
        with self.assertRaises(ConfigConflictError) as ctx:
            bootstrap(self.config())
        issues = [c["issue"] for c in ctx.exception.details["conflicts"]]
        self.assertIn("events_reference_unconfigured_airport", issues)
        # 回滚：不留下任何部分登记的事实。
        repo = Repository(self.db_path)
        try:
            self.assertEqual(repo.airport_facts(), {})
            self.assertIsNone(repo.registered_digest())
        finally:
            repo.close()

    # ------------------------------------------------------------------ #
    # 不产生半初始化数据库
    # ------------------------------------------------------------------ #

    def test_invalid_config_creates_no_database_file(self) -> None:
        rows = self.airport_rows()
        rows.append(dict(rows[0], timezone="America/New_York"))  # 重复 APS
        bad_dir = self.write_fixtures(rows)
        with self.assertRaises(ConfigError):
            bootstrap(self.config(bad_dir))
        self.assertFalse(self.db_path.exists())
        self.assertFalse(Path(str(self.db_path) + "-wal").exists())

    def test_conflict_leaves_no_partial_writes(self) -> None:
        bootstrap(self.config()).close()
        before_facts = None
        repo = Repository(self.db_path)
        before_facts = repo.airport_facts()
        repo.close()
        rows = self.airport_rows(APS={"timezone": "America/New_York"})
        with self.assertRaises(ConfigConflictError):
            bootstrap(self.config(self.write_fixtures(rows)))
        repo = Repository(self.db_path)
        try:
            self.assertEqual(repo.airport_facts(), before_facts)
            self.assertEqual(repo.registered_digest(), self.digest)
        finally:
            repo.close()

    # ------------------------------------------------------------------ #
    # 同一输入始终采用同一份机场定义
    # ------------------------------------------------------------------ #

    def test_record_order_change_yields_same_definition_and_results(self) -> None:
        # 1) 用原始顺序的夹具启动并提交事件。
        first = bootstrap(self.config())
        original = first.service.submit_event(base_event())
        first_digest = first.config_digest
        first.close()

        # 2) 同样的机场定义，仅文件中的记录顺序不同。
        rows = list(reversed(self.airport_rows()))
        shuffled_dir = self.write_fixtures(rows)
        shuffled_airports = load_airports(shuffled_dir)
        self.assertEqual(airports_digest(shuffled_airports), first_digest)

        # 3) 重启：门禁通过（事实一致），重放同一事件返回完全相同的结果。
        second = bootstrap(self.config(shuffled_dir))
        try:
            self.assertEqual(second.config_digest, first_digest)
            replay = second.service.submit_event(dict(base_event()))
            self.assertEqual(replay["processing_state"], "replayed")
            self.assertEqual(replay["impacts"], original["impacts"])
            self.assertEqual(replay["config_digest"], original["config_digest"])
        finally:
            second.close()

    def test_event_results_carry_config_digest_across_restart(self) -> None:
        first = bootstrap(self.config())
        result = first.service.submit_event(base_event())
        self.assertEqual(result["config_digest"], self.digest)
        first.close()

        second = bootstrap(self.config())
        try:
            status = second.service.event_status("evt-close0000001")
            self.assertEqual(status["processing"]["config_digest"], self.digest)
            replay = second.service.submit_event(dict(base_event()))
            self.assertEqual(replay["config_digest"], self.digest)
            self.assertEqual(replay["impacts"], result["impacts"])
        finally:
            second.close()


class MultiProcessStartupTest(unittest.TestCase):
    """两个进程同时启动同一数据库：都必须收敛到同一份机场定义。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "shared.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def _spawn(self, port: int) -> subprocess.Popen:
        env = dict(os.environ)
        env.update(
            {
                "FIXTURES_DIR": str(FIXTURES_DIR),
                "DB_PATH": str(self.db_path),
                "HOST": "127.0.0.1",
                "PORT": str(port),
            }
        )
        return subprocess.Popen(
            [sys.executable, "-m", "app"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    @staticmethod
    def _get(port: int, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=5
            ) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    @staticmethod
    def _post_event(port: int, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/events",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _wait_ready(self, port: int, proc: subprocess.Popen) -> dict:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(
                    f"process on port {port} exited early "
                    f"(rc={proc.returncode}): {proc.stdout.read()}"
                )
            try:
                status, body = self._get(port, "/readyz")
                if status == 200 and body.get("status") == "ready":
                    return body
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(0.1)
        self.fail(f"service on port {port} did not become ready in time")

    def test_concurrent_startup_converges_on_same_airport_definition(self) -> None:
        ports = [self._free_port() for _ in range(2)]
        procs = [self._spawn(port) for port in ports]
        try:
            ready_bodies = [self._wait_ready(port, proc) for port, proc in zip(ports, procs)]
            # 两个进程报告同一份机场定义摘要，且与本地计算一致。
            expected = airports_digest(load_airports(FIXTURES_DIR))
            for body in ready_bodies:
                self.assertEqual(body["config_digest"], expected)
                self.assertEqual(body["airports"], 3)

            # 同一事件并发提交到两个进程：恰好一个 processed、一个 replayed，
            # 影响结果与摘要完全一致。
            payload = base_event(event_id="evt-multiproc0001")
            results: list[tuple[int, dict]] = []

            def submit(port: int) -> None:
                results.append(self._post_event(port, payload))

            threads = [threading.Thread(target=submit, args=(p,)) for p in ports]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertEqual([status for status, _ in results], [201, 201])
            states = sorted(body["processing_state"] for _, body in results)
            self.assertEqual(states, ["processed", "replayed"])
            bodies = [body for _, body in results]
            self.assertEqual(bodies[0]["impacts"], bodies[1]["impacts"])
            self.assertEqual(bodies[0]["config_digest"], expected)
            self.assertEqual(bodies[1]["config_digest"], expected)

            # 两个进程都仍然健康存活（没有谁因锁冲突崩溃退出）。
            for proc in procs:
                self.assertIsNone(proc.poll())
        finally:
            for proc in procs:
                proc.terminate()
            for proc in procs:
                proc.wait(timeout=15)
                if proc.stdout is not None:
                    proc.stdout.close()

    def test_conflicting_instance_is_refused_while_peer_serves(self) -> None:
        # 正常实例先启动；随后一个携带冲突配置的实例必须被拒绝，
        # 且正常实例的服务与数据库内容不受任何影响。
        good_port = self._free_port()
        good = self._spawn(good_port)
        bad = None
        try:
            ready = self._wait_ready(good_port, good)
            status, submitted = self._post_event(good_port, base_event())
            self.assertEqual(status, 201)

            alt_dir = self.tmp / "fixtures-conflict"
            alt_dir.mkdir()
            rows = json.loads((FIXTURES_DIR / "airports.json").read_text())
            rows[0]["timezone"] = "America/New_York"
            (alt_dir / "airports.json").write_text(json.dumps(rows))
            shutil.copy(FIXTURES_DIR / "flights.json", alt_dir / "flights.json")

            env = dict(os.environ)
            env.update(
                {
                    "FIXTURES_DIR": str(alt_dir),
                    "DB_PATH": str(self.db_path),
                    "HOST": "127.0.0.1",
                    "PORT": str(self._free_port()),
                }
            )
            bad = subprocess.Popen(
                [sys.executable, "-m", "app"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            rc = bad.wait(timeout=30)
            output = bad.stdout.read()
            self.assertEqual(rc, 3)
            self.assertIn("startup refused", output)
            self.assertIn("airport_definition_changed", output)

            # 好实例未受影响：数据一致、继续就绪、继续服务。
            status, body = self._get(good_port, "/readyz")
            self.assertEqual(status, 200)
            self.assertEqual(body["config_digest"], ready["config_digest"])
            status, fetched = self._get(
                good_port, f"/api/v1/events/{base_event()['event_id']}"
            )
            self.assertEqual(status, 200)
            self.assertEqual(fetched["impacts"], submitted["impacts"])
        finally:
            good.terminate()
            good.wait(timeout=15)
            if good.stdout is not None:
                good.stdout.close()
            if bad is not None:
                if bad.poll() is None:
                    bad.terminate()
                    bad.wait(timeout=15)
                if bad.stdout is not None:
                    bad.stdout.close()


if __name__ == "__main__":
    unittest.main()
