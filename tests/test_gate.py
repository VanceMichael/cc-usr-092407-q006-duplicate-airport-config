"""启动门禁、schema 迁移与原子性测试。

覆盖：

* 全新库门禁登记（empty_initialized）、重复门禁对齐（aligned）。
* v1 库升级：配置覆盖所有被引用机场时单事务升级并登记（upgraded）。
* v1 升级冲突：在任何 DDL 之前回滚——文件保持 v1、旧事件仍可读、不出现
  新表，杜绝半初始化数据库。
* v2 已登记事实与配置冲突：拒绝接管、不新增修订、不重写任何事实。
* schema 建立中途失败整体回滚（空库保持空）。
* 修订内容寻址：相同摘要只保留一行。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.config import AirportConfig, AirportEntry, load_airports
from app.errors import ConfigConflictError
from app.models import Airport
from app.repository import SCHEMA_VERSION, Repository
from tests.support import FIXTURES_DIR

V1_SCHEMA = """
CREATE TABLE events (
    event_id TEXT PRIMARY KEY, event_version INTEGER NOT NULL, event_type TEXT NOT NULL,
    airport_code TEXT NOT NULL, effective_from TEXT NOT NULL, effective_until TEXT,
    reported_at TEXT NOT NULL, supersedes_event_id TEXT, reason TEXT,
    payload_json TEXT NOT NULL, replay_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE impacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
    root_event_id TEXT NOT NULL, airport_code TEXT NOT NULL, flight_id TEXT NOT NULL,
    flight_number TEXT NOT NULL, affected_endpoint TEXT NOT NULL, impact_status TEXT NOT NULL,
    overlap_minutes INTEGER, delay_minutes INTEGER, proposed_departure TEXT,
    proposed_arrival TEXT, passenger_count INTEGER NOT NULL, crosses_midnight INTEGER NOT NULL,
    UNIQUE(event_id, flight_id, airport_code)
);
"""

V1_EVENT = {
    "event_id": "evt-v1close00001",
    "event_version": 1,
    "event_type": "airport.closed",
    "airport_code": "APS",
    "effective_from": "2026-09-07T15:00:00Z",
    "effective_until": "2026-09-07T19:00:00Z",
    "reported_at": "2026-09-07T14:00:00Z",
    "supersedes_event_id": None,
    "reason": None,
}


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "g.db"
        self.config = load_airports(FIXTURES_DIR)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def locations(self) -> dict[str, str]:
        return {e.airport.code: e.location for e in self.config.entries()}

    def gate(self, repo: Repository):
        return repo.gate(self.config.digest, self.config.manifest(), self.locations())

    def make_v1_db(self, *, airport: str = "APS") -> None:
        conn = sqlite3.connect(self.db)
        conn.executescript(V1_SCHEMA)
        conn.execute("PRAGMA user_version=1")
        event = dict(V1_EVENT, airport_code=airport)
        conn.execute(
            "INSERT INTO events (event_id,event_version,event_type,airport_code,"
            "effective_from,effective_until,reported_at,supersedes_event_id,reason,"
            "payload_json,replay_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
            (
                event["event_id"], event["event_version"], event["event_type"],
                event["airport_code"], event["effective_from"], event["effective_until"],
                event["reported_at"], event["supersedes_event_id"], event["reason"],
                json.dumps(event), "2026-09-07T14:00:00Z",
            ),
        )
        conn.commit()
        conn.close()

    def table_names(self, path: Path) -> set[str]:
        conn = sqlite3.connect(path)
        rows = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        conn.close()
        return rows

    # ------------------------------------------------------------------ #

    def test_fresh_db_initializes_and_registers(self) -> None:
        repo = Repository(self.db)
        status = self.gate(repo)
        self.assertEqual(status.state, "empty_initialized")
        self.assertEqual(status.newly_registered, ["APS", "BSR", "KTA"])
        self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
        revision = repo.get_config_revision(self.config.digest)
        self.assertIsNotNone(revision)
        self.assertEqual(revision["airport_count"], 3)
        repo.close()

    def test_second_gate_aligns_without_duplicate_revision(self) -> None:
        repo = Repository(self.db)
        self.gate(repo)
        again = self.gate(repo)
        self.assertEqual(again.state, "aligned")
        self.assertEqual(again.newly_registered, [])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM config_revisions").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM airport_registry").fetchone()[0], 3
            )
        repo.close()

    def test_v1_upgrade_registers_facts_in_one_gate(self) -> None:
        self.make_v1_db()
        repo = Repository(self.db)
        self.assertEqual(repo.schema_version(), 1)
        status = self.gate(repo)
        self.assertEqual(status.state, "upgraded")
        self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
        self.assertEqual(status.newly_registered, ["APS", "BSR", "KTA"])
        # Legacy event row survived and now carries a NULL digest (not rewritten).
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT airport_code, config_digest FROM events"
            ).fetchone()
            self.assertEqual(row, ("APS", None))
        repo.close()

    def test_v1_upgrade_conflict_rolls_back_entirely_file_stays_v1(self) -> None:
        self.make_v1_db(airport="ZZZ")  # v1 data references an airport now missing
        repo = Repository(self.db)
        with self.assertRaises(ConfigConflictError) as ctx:
            self.gate(repo)
        issue = ctx.exception.details["issues"][0]
        self.assertEqual(issue["issue"], "referenced_airport_missing_from_config")
        self.assertEqual(issue["code"], "ZZZ")
        repo.close()

        # No DDL happened: file is still v1, no registry/revision tables.
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], 1
        )
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("airport_registry", names)
        self.assertNotIn("config_revisions", names)
        # The old instance's data is fully intact and readable as v1.
        row = conn.execute("SELECT event_id, airport_code FROM events").fetchone()
        self.assertEqual(row, ("evt-v1close00001", "ZZZ"))
        conn.close()

    def test_v2_changed_buffer_blocks_and_changes_nothing(self) -> None:
        repo = Repository(self.db)
        self.gate(repo)
        repo.close()

        # Build a conflicting in-memory config: APS buffer 20 -> 77.
        entries = self.config.entries()
        changed = []
        for e in entries:
            a = e.airport
            if a.code == "APS":
                a = Airport(a.code, a.name, a.timezone, 77)
            changed.append(AirportEntry(a, e.configured_timezone, e.location, e.index))
        bad = AirportConfig(changed, source=self.config.source)

        repo = Repository(self.db)
        with self.assertRaises(ConfigConflictError) as ctx:
            repo.gate(bad.digest, bad.manifest(),
                      {e.airport.code: e.location for e in changed})
        issues = ctx.exception.details["issues"]
        self.assertTrue(
            any(i["issue"] == "airport_definition_changed"
                and i["field"] == "reopen_buffer_minutes"
                and i["registered"] == 20 and i["configured"] == 77
                for i in issues)
        )
        # Each issue points at the config source location.
        self.assertTrue(
            all("configured_location" in i for i in issues if "location" not in i)
        )
        repo.close()

        # Database untouched: still one revision (the good digest), buffer 20.
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM config_revisions").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute(
                    "SELECT reopen_buffer_minutes FROM airport_registry WHERE code='APS'"
                ).fetchone()[0],
                20,
            )

    def test_v2_removed_airport_blocks_takeover(self) -> None:
        repo = Repository(self.db)
        self.gate(repo)
        repo.close()
        kept = [e for e in self.config.entries() if e.airport.code != "KTA"]
        reduced = AirportConfig(kept, source=self.config.source)
        repo = Repository(self.db)
        with self.assertRaises(ConfigConflictError) as ctx:
            repo.gate(reduced.digest, reduced.manifest(),
                      {e.airport.code: e.location for e in kept})
        self.assertTrue(
            any(i["issue"] == "airport_removed_from_config" and i["code"] == "KTA"
                for i in ctx.exception.details["issues"])
        )
        repo.close()

    def test_fresh_init_failure_leaves_no_tables(self) -> None:
        repo = Repository(self.db)
        # Force a failure after DDL begins but before commit; the whole
        # transaction must roll back (no half-initialized database).
        original = repo._register_locked

        def boom(*a, **k):
            raise RuntimeError("simulated crash during registration")

        repo._register_locked = boom  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            self.gate(repo)
        repo.close()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0],
                0,
            )
        # A later, healthy start on the same file initializes cleanly.
        repo2 = Repository(self.db)
        self.assertEqual(self.gate(repo2).state, "empty_initialized")
        repo2.close()
        # silence unused helper
        self.assertTrue(callable(original))


if __name__ == "__main__":
    unittest.main()
