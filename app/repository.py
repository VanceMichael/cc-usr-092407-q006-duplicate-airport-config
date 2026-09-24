"""SQLite 持久化层。

单一数据库文件同时保存事件、计算结果和已登记的机场事实，因此一次提交
可以原子写入事件及其全部影响，失败时也不会留下部分数据。数据库位于
挂载卷时，WAL 模式可在容器重启后继续保留数据。

机场事实（``airport_facts`` / ``config_registry``）是启动门禁的一部分：
服务只有在当前配置与库中已登记的事实整体核对通过后才会登记摘要并
开始接收流量；核对失败时事务回滚，数据库保持旧实例可读的状态。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.errors import ConfigConflictError
from app.models import Airport

# 逐条执行的建表语句：sqlite3 的 executescript 会先隐式提交，无法在
# 事务中保证"全部建表成功或全部回滚"，因此改为显式事务内逐条执行。
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS events (
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
    CREATE TABLE IF NOT EXISTS impacts (
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
    # 启动时登记的机场事实：每个机场一份定义，登记后只允许新增机场，
    # 不允许修改或删除（修改/删除即与历史事件的计算基础冲突）。
    """
    CREATE TABLE IF NOT EXISTS airport_facts (
        airport_code           TEXT PRIMARY KEY,
        name                   TEXT NOT NULL,
        timezone               TEXT NOT NULL,
        reopen_buffer_minutes  INTEGER NOT NULL,
        registered_at          TEXT NOT NULL
    )
    """,
    # 单行注册表：当前生效配置摘要（airports_digest 的结果）。
    """
    CREATE TABLE IF NOT EXISTS config_registry (
        singleton      INTEGER PRIMARY KEY CHECK (singleton = 1),
        digest         TEXT NOT NULL,
        airport_count  INTEGER NOT NULL,
        registered_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_impacts_root    ON impacts(root_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_impacts_airport ON impacts(airport_code, impact_status)",
    "CREATE INDEX IF NOT EXISTS idx_impacts_flight  ON impacts(flight_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_airport  ON events(airport_code, event_version)",
)

BUSY_TIMEOUT_MS = 30_000


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Repository:
    """对单一 SQLite 连接提供线程安全封装。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        created = not self._db_path.exists()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.execute("PRAGMA synchronous=FULL")
                self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                with _Transaction(self._conn, self._lock):
                    for statement in SCHEMA_STATEMENTS:
                        self._conn.execute(statement)
                    self._migrate(self._conn)
        except Exception:
            self._conn.close()
            if created:
                # 初始化失败不得留下半初始化数据库文件。
                for suffix in ("", "-wal", "-shm"):
                    try:
                        Path(str(self._db_path) + suffix).unlink(missing_ok=True)
                    except OSError:
                        pass
            raise

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """对修复前版本创建的库做就地迁移（在初始化事务内执行）。"""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "config_digest" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN config_digest TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1").fetchone()
            return row is not None and row[0] == 1

    # ------------------------------------------------------------------ #
    # 启动门禁：机场事实核对与登记
    # ------------------------------------------------------------------ #

    def register_airport_facts(
        self, airports: dict[str, Airport], digest: str
    ) -> None:
        """把当前配置与库中已登记的机场事实作为一个整体核对并登记。

        规则（全部在单个事务内判定，任一冲突即整体回滚）：

        * 已登记机场的 ``name`` / ``timezone`` / ``reopen_buffer_minutes``
          必须与当前配置完全一致；
        * 已登记事实的机场不得从配置中消失；
        * 已有事件引用的机场必须仍存在于配置中；
        * 配置中新增的机场会被登记为事实，并刷新注册表摘要。
        """
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT airport_code, name, timezone, reopen_buffer_minutes "
                "FROM airport_facts"
            ).fetchall()
            registered = {r["airport_code"]: dict(r) for r in rows}
            referenced = {
                r["airport_code"]
                for r in conn.execute("SELECT DISTINCT airport_code FROM events")
            }

            conflicts: list[dict[str, Any]] = []
            for code in sorted(set(registered) & set(airports)):
                fact = registered[code]
                airport = airports[code]
                fields: dict[str, dict[str, Any]] = {}
                if fact["name"] != airport.name:
                    fields["name"] = {
                        "registered": fact["name"],
                        "configured": airport.name,
                    }
                if fact["timezone"] != airport.timezone:
                    fields["timezone"] = {
                        "registered": fact["timezone"],
                        "configured": airport.timezone,
                    }
                if fact["reopen_buffer_minutes"] != airport.reopen_buffer_minutes:
                    fields["reopen_buffer_minutes"] = {
                        "registered": fact["reopen_buffer_minutes"],
                        "configured": airport.reopen_buffer_minutes,
                    }
                if fields:
                    conflicts.append(
                        {
                            "issue": "airport_definition_changed",
                            "airport_code": code,
                            "fields": fields,
                        }
                    )
            for code in sorted(set(registered) - set(airports)):
                conflicts.append(
                    {
                        "issue": "registered_airport_missing_from_config",
                        "airport_code": code,
                    }
                )
            for code in sorted(referenced - set(airports)):
                conflicts.append(
                    {
                        "issue": "events_reference_unconfigured_airport",
                        "airport_code": code,
                    }
                )
            if conflicts:
                raise ConfigConflictError(
                    "Configured airports conflict with the airport facts already "
                    "recorded in the database; refusing to take over traffic",
                    {"conflicts": conflicts},
                )

            now = utcnow_iso()
            for code in sorted(set(airports) - set(registered)):
                airport = airports[code]
                conn.execute(
                    "INSERT INTO airport_facts (airport_code, name, timezone, "
                    "reopen_buffer_minutes, registered_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        airport.code,
                        airport.name,
                        airport.timezone,
                        airport.reopen_buffer_minutes,
                        now,
                    ),
                )
            row = conn.execute(
                "SELECT digest FROM config_registry WHERE singleton = 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO config_registry (singleton, digest, airport_count, "
                    "registered_at) VALUES (1, ?, ?, ?)",
                    (digest, len(airports), now),
                )
            elif row["digest"] != digest:
                conn.execute(
                    "UPDATE config_registry SET digest = ?, airport_count = ?, "
                    "registered_at = ? WHERE singleton = 1",
                    (digest, len(airports), now),
                )

    def registered_digest(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT digest FROM config_registry WHERE singleton = 1"
            ).fetchone()
            return row["digest"] if row else None

    def airport_facts(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT airport_code, name, timezone, reopen_buffer_minutes, "
                "registered_at FROM airport_facts ORDER BY airport_code"
            ).fetchall()
            return {r["airport_code"]: dict(r) for r in rows}

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
        config_digest: str | None,
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
