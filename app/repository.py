"""SQLite 持久化层与启动门禁。

单一数据库文件同时保存事件、计算结果和机场配置事实，因此：

* 一次提交原子写入事件及其全部影响，失败不留下部分数据。
* schema 建立/迁移、机场事实首次登记和配置对账在 **同一个
  ``BEGIN IMMEDIATE`` 事务** 内完成；任一步失败整体回滚，不会产生半初始化
  数据库，旧版本实例随后仍能打开同一文件（v1 升级冲突时文件保持 v1）。
* 多进程/多容器同时挂同一卷启动时，``busy_timeout`` 让后来的进程在写锁上
  排队：先提交的进程登记机场定义，后来的进程必须与已提交事实一致，否则
  收到 :class:`ConfigConflictError` 且不修改任何数据。不存在"最后一个进程
  的内存字典获胜"。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 2

# Version 2 schema. Fresh databases are created directly at SCHEMA_VERSION;
# version-1 files (events without config_digest, no registry) are migrated in
# gate()'s single transaction.
#
# IMPORTANT: statements run one at a time with ``Connection.execute`` inside one
# explicit transaction. ``Connection.executescript`` issues a COMMIT before
# running, which would make schema creation non-atomic and defeat the
# all-or-nothing startup guarantee.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
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
    config_digest        TEXT,
    replay_count         INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL
)
""",
    """
CREATE TABLE impacts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id           TEXT NOT NULL REFERENCES events(event_id),
    root_event_id      TEXT NOT NULL,
    airport_code       TEXT NOT NULL,
    flight_id          TEXT NOT NULL,
    flight_number      TEXT NOT NULL,
    affected_endpoint  TEXT NOT NULL,
    impact_status      TEXT NOT NULL,
    overlap_minutes    INTEGER,
    delay_minutes      INTEGER,
    proposed_departure TEXT,
    proposed_arrival   TEXT,
    passenger_count    INTEGER NOT NULL,
    crosses_midnight   INTEGER NOT NULL,
    UNIQUE(event_id, flight_id, airport_code)
)
""",
    """
CREATE TABLE config_revisions (
    config_digest  TEXT PRIMARY KEY,
    manifest_json  TEXT NOT NULL,
    airport_count  INTEGER NOT NULL,
    parent_digest  TEXT,
    created_at     TEXT NOT NULL
)
""",
    """
CREATE TABLE airport_registry (
    code                      TEXT PRIMARY KEY,
    name                      TEXT NOT NULL,
    timezone                  TEXT NOT NULL,
    reopen_buffer_minutes     INTEGER NOT NULL,
    registered_config_digest  TEXT NOT NULL
                                          REFERENCES config_revisions(config_digest),
    updated_at                TEXT NOT NULL
)
""",
    "CREATE INDEX idx_impacts_root    ON impacts(root_event_id)",
    "CREATE INDEX idx_impacts_airport ON impacts(airport_code, impact_status)",
    "CREATE INDEX idx_impacts_flight  ON impacts(flight_id)",
    "CREATE INDEX idx_events_airport  ON events(airport_code, event_version)",
)

# How long a second starter waits behind the process that owns the write lock
# during concurrent startup. Long enough to ride out a first-time migration.
STARTUP_BUSY_TIMEOUT_MS = 30_000


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class GateStatus:
    """启动门禁的确定性结果。"""

    state: str  # "empty_initialized" | "upgraded" | "aligned"
    digest: str
    registered: list[str] = field(default_factory=list)
    newly_registered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "config_digest": self.digest,
            "registered_airports": list(self.registered),
            "newly_registered_airports": list(self.newly_registered),
        }


class Repository:
    """对单一 SQLite 连接提供线程安全封装。

    构造器只建立连接并设置连接级 PRAGMA，不创建/升级任何表——schema 的
    建立、迁移和机场事实登记全部在 :meth:`gate` 的单事务内完成，从而保证
    升级冲突时数据库字节不变、旧版本实例仍可打开。
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # Set the retry policy before anything that could take a write lock
            # so concurrent starters queue on BEGIN IMMEDIATE instead of erroring.
            self._conn.execute(f"PRAGMA busy_timeout={STARTUP_BUSY_TIMEOUT_MS}")
            # journal_mode is a persistent property of the database file and
            # cannot be changed inside a transaction; it is idempotent once set.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=FULL")

    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def gate(
        self,
        digest: str,
        manifest: list[dict[str, Any]],
        locations: Mapping[str, str],
    ) -> GateStatus:
        """在单事务内完成 schema 建立/迁移、配置对账与机场事实登记。

        成功返回 :class:`GateStatus`；发现已登记事实（或 v1 库中事件/影响
        已引用的机场）与当前配置冲突时抛出 :class:`ConfigConflictError` 并
        回滚——v1 文件保持 v1，旧实例继续可读，新实例不得接管流量。绝不
        留下半初始化数据库。
        """
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = self._user_version(conn)
                if version == 0:
                    for statement in _SCHEMA_STATEMENTS:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    status = self._register_locked(
                        conn, digest, manifest, existing=set(), had_prior_data=False
                    )
                elif version == 1:
                    # Reconcile against the only facts v1 recorded — the codes
                    # referenced by stored events/impacts — BEFORE any DDL.
                    self._check_referenced_locked(conn, manifest)
                    self._migrate_v1_to_v2(conn)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    had_prior_data = (
                        conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
                        is not None
                    )
                    status = self._register_locked(
                        conn,
                        digest,
                        manifest,
                        existing=set(),
                        had_prior_data=had_prior_data,
                    )
                elif version == SCHEMA_VERSION:
                    status = self._reconcile_v2_locked(conn, digest, manifest, locations)
                else:
                    raise RuntimeError(
                        f"Unsupported database schema version {version}; "
                        f"this build supports up to {SCHEMA_VERSION}"
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return status

    # ------------------------------------------------------------------ #
    # Schema migration (runs inside gate()'s transaction)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _user_version(conn: sqlite3.Connection) -> int:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
        # Add the per-event configuration pointer (legacy rows stay NULL) and
        # introduce the fact registry + revision log. Each statement runs in
        # gate()'s transaction (no executescript — it would auto-commit).
        conn.execute("ALTER TABLE events ADD COLUMN config_digest TEXT")
        conn.execute(
            """
            CREATE TABLE config_revisions (
                config_digest  TEXT PRIMARY KEY,
                manifest_json  TEXT NOT NULL,
                airport_count  INTEGER NOT NULL,
                parent_digest  TEXT,
                created_at     TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE airport_registry (
                code                      TEXT PRIMARY KEY,
                name                      TEXT NOT NULL,
                timezone                  TEXT NOT NULL,
                reopen_buffer_minutes     INTEGER NOT NULL,
                registered_config_digest  TEXT NOT NULL
                                                  REFERENCES config_revisions(config_digest),
                updated_at                TEXT NOT NULL
            )
            """
        )
        # v1 created identical indexes; no index changes are needed.

    # ------------------------------------------------------------------ #
    # Reconciliation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _referenced_codes(conn: sqlite3.Connection) -> set[str]:
        return {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT airport_code FROM events "
                "UNION SELECT DISTINCT airport_code FROM impacts"
            ).fetchall()
        }

    def _check_referenced_locked(
        self, conn, manifest: list[dict[str, Any]]
    ) -> None:
        """v1 升级门禁：每个已被事件/影响引用的机场都必须仍在配置中。

        v1 未持久化名称/时区/缓冲，无法逐字段比对；保留"被引用代码必须
        存在"这一可由旧数据证明的约束。冲突时在任何 DDL 之前抛出。
        """
        from app.errors import ConfigConflictError

        configured = {f["code"] for f in manifest}
        issues: list[dict[str, Any]] = []
        for code in sorted(self._referenced_codes(conn) - configured):
            refs: list[str] = []
            if conn.execute(
                "SELECT 1 FROM events WHERE airport_code = ? LIMIT 1", (code,)
            ).fetchone():
                refs.append("events")
            if conn.execute(
                "SELECT 1 FROM impacts WHERE airport_code = ? LIMIT 1", (code,)
            ).fetchone():
                refs.append("impacts")
            issues.append(
                {
                    "field": "code",
                    "issue": "referenced_airport_missing_from_config",
                    "code": code,
                    "referenced_by": refs,
                }
            )
        if issues:
            raise ConfigConflictError(
                "Airport configuration drops airports referenced by stored "
                "events; refusing to migrate or take over traffic",
                {"issues": issues},
            )

    def _reconcile_v2_locked(
        self, conn, digest: str, manifest: list[dict[str, Any]],
        locations: Mapping[str, str],
    ) -> GateStatus:
        from app.errors import ConfigConflictError

        configured = {f["code"]: f for f in manifest}
        registered_rows = conn.execute(
            "SELECT code, name, timezone, reopen_buffer_minutes, "
            "registered_config_digest FROM airport_registry"
        ).fetchall()
        registered = {r["code"]: dict(r) for r in registered_rows}

        issues: list[dict[str, Any]] = []

        # 1) Every airport already registered must keep its exact definition.
        for code in sorted(registered):
            stored = registered[code]
            new = configured.get(code)
            if new is None:
                issues.append(
                    {
                        "field": "code",
                        "issue": "airport_removed_from_config",
                        "code": code,
                        "registered": _fact(stored),
                        "registered_config_digest": stored["registered_config_digest"],
                    }
                )
                continue
            for attr in ("name", "timezone", "reopen_buffer_minutes"):
                if stored[attr] != new[attr]:
                    issues.append(
                        {
                            "field": attr,
                            "issue": "airport_definition_changed",
                            "code": code,
                            "configured_location": locations.get(code),
                            "registered": stored[attr],
                            "configured": new[attr],
                            "registered_config_digest": stored[
                                "registered_config_digest"
                            ],
                        }
                    )

        # 2) Every airport referenced by stored rows must still be configured.
        for code in sorted(self._referenced_codes(conn) - set(configured)):
            issues.append(
                {
                    "field": "code",
                    "issue": "referenced_airport_missing_from_config",
                    "code": code,
                    "referenced_by": ["events/impacts"],
                }
            )

        if issues:
            issues.sort(key=lambda i: (i["code"], i["field"], i["issue"]))
            raise ConfigConflictError(
                "Airport configuration conflicts with facts already registered "
                "in the database; refusing to take over traffic",
                {
                    "config_digest": digest,
                    "configured_locations": dict(locations),
                    "issues": issues,
                },
            )

        return self._register_locked(
            conn,
            digest,
            manifest,
            existing=set(registered),
            had_prior_data=(
                conn.execute("SELECT 1 FROM events LIMIT 1").fetchone() is not None
            ),
        )

    def _register_locked(
        self,
        conn,
        digest: str,
        manifest: list[dict[str, Any]],
        *,
        existing: set[str],
        had_prior_data: bool,
    ) -> GateStatus:
        """插入内容寻址修订与新增机场；已登记事实绝不重写。"""
        parent_row = conn.execute(
            "SELECT config_digest FROM config_revisions "
            "ORDER BY created_at DESC, config_digest DESC LIMIT 1"
        ).fetchone()
        parent_digest = (
            parent_row[0] if parent_row and parent_row[0] != digest else None
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO config_revisions
                (config_digest, manifest_json, airport_count, parent_digest, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                digest,
                json.dumps(manifest, sort_keys=True, ensure_ascii=False),
                len(manifest),
                parent_digest,
                utcnow_iso(),
            ),
        )

        now = utcnow_iso()
        newly: list[str] = []
        for fact in manifest:  # already sorted by code
            code = fact["code"]
            if code in existing:
                continue
            newly.append(code)
            conn.execute(
                """
                INSERT INTO airport_registry
                    (code, name, timezone, reopen_buffer_minutes,
                     registered_config_digest, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    fact["name"],
                    fact["timezone"],
                    fact["reopen_buffer_minutes"],
                    digest,
                    now,
                ),
            )

        registered_all = sorted(existing | set(newly))
        if not existing:
            state = "upgraded" if had_prior_data else "empty_initialized"
        else:
            state = "aligned"
        return GateStatus(
            state=state,
            digest=digest,
            registered=registered_all,
            newly_registered=newly,
        )

    def get_config_revision(self, digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM config_revisions WHERE config_digest = ?",
                (digest,),
            ).fetchone()
            return dict(row) if row else None

    def registered_airports(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT code, name, timezone, reopen_buffer_minutes, "
                    "registered_config_digest, updated_at "
                    "FROM airport_registry ORDER BY code"
                )
            ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1").fetchone()
            return row is not None and row[0] == 1

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def get_event_row(self, event_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()

    def get_impacts(self, event_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM impacts WHERE event_id = ? ORDER BY flight_id",
                    (event_id,),
                )
            )

    def events_for_airport(self, airport_code: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM events WHERE airport_code = ? "
                    "ORDER BY event_version, effective_from",
                    (airport_code,),
                )
            )

    def count_replays(self, event_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT replay_count FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return int(row["replay_count"]) if row else 0

    def latest_impacts(
        self,
        *,
        airport: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回每个航班与机场组合的最新影响。

        同一机场内，每条事件链采用最新事件的快照；航班同时出现在多条链时，
        采用最后生成的快照。`resolved` 墓碑参与排序，使恢复开放后释放的航班
        不再出现在结果中。最终结果按 flight_id 稳定排序。
        """
        where = ["i.airport_code = ?"] if airport else []
        params: list[Any] = [airport] if airport else []
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        outer = ["rn = 1", "impact_status != 'resolved'"]
        outer_params: list[Any] = []
        if status:
            outer.append("impact_status = ?")
            outer_params.append(status)

        sql = f"""
        WITH ranked AS (
            SELECT i.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY i.flight_id, i.airport_code
                       ORDER BY e.created_at DESC,
                                i.id DESC
                   ) AS rn
            FROM impacts i
            JOIN events e ON e.event_id = i.event_id
            {where_sql}
        )
        SELECT * FROM ranked
        WHERE {' AND '.join(outer)}
        ORDER BY flight_id, airport_code
        """
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params + outer_params)]

    def prior_chain_impact_ids(self, conn, root_event_id: str) -> set[str]:
        rows = conn.execute(
            "SELECT DISTINCT flight_id FROM impacts WHERE root_event_id = ? "
            "AND impact_status != 'resolved'",
            (root_event_id,),
        ).fetchall()
        return {r["flight_id"] for r in rows}

    # ------------------------------------------------------------------ #
    # Writes (all callers run inside ``transaction``)
    # ------------------------------------------------------------------ #

    def transaction(self):
        return _Transaction(self._conn, self._lock)

    def insert_event(
        self,
        conn: sqlite3.Connection,
        event_dict: dict[str, Any],
        config_digest: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO events (event_id, event_version, event_type, airport_code,
                                effective_from, effective_until, reported_at,
                                supersedes_event_id, reason, payload_json,
                                config_digest, replay_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                event_dict["event_id"],
                event_dict["event_version"],
                event_dict["event_type"],
                event_dict["airport_code"],
                event_dict["effective_from"],
                event_dict["effective_until"],
                event_dict["reported_at"],
                event_dict["supersedes_event_id"],
                event_dict["reason"],
                json.dumps(event_dict, sort_keys=True, ensure_ascii=False),
                config_digest,
                utcnow_iso(),
            ),
        )

    def insert_impacts(
        self, conn: sqlite3.Connection, impacts: Iterable[dict[str, Any]]
    ) -> None:
        conn.executemany(
            """
            INSERT INTO impacts (event_id, root_event_id, airport_code, flight_id,
                                 flight_number, affected_endpoint, impact_status,
                                 overlap_minutes, delay_minutes, proposed_departure,
                                 proposed_arrival, passenger_count, crosses_midnight)
            VALUES (:event_id, :root_event_id, :airport_code, :flight_id,
                    :flight_number, :affected_endpoint, :impact_status,
                    :overlap_minutes, :delay_minutes, :proposed_departure,
                    :proposed_arrival, :passenger_count, :crosses_midnight)
            """,
            list(impacts),
        )

    def increment_replay(self, conn: sqlite3.Connection, event_id: str) -> None:
        conn.execute(
            "UPDATE events SET replay_count = replay_count + 1 WHERE event_id = ?",
            (event_id,),
        )


def _fact(stored: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": stored["code"],
        "name": stored["name"],
        "timezone": stored["timezone"],
        "reopen_buffer_minutes": stored["reopen_buffer_minutes"],
    }


class _Transaction:
    """管理 BEGIN IMMEDIATE、COMMIT 与 ROLLBACK 的事务上下文。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()
