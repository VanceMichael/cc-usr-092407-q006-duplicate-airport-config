"""配置与领域夹具加载。

加载规则（修复"合并配置时后一行静默覆盖前一行"的缺陷）：

* 机场三字码在文件中出现两次即错误——无论两条记录是否完全一致，都不再
  依赖字典"最后一项获胜"。每条错误都同时给出两处来源位置
  （``文件路径[数组下标]``）。
* 名称必须是非空字符串，不做隐式 ``str()`` 转换；缓冲必须是非负整数
  （布尔值被显式拒绝）；时区必须是可加载的 IANA 名称。
* 同一机场的两条记录使用不同时区拼写但语义相同（IANA 链接别名，例如
  ``Singapore`` 与 ``Asia/Singapore``）会报语义重复；规范名不同则报冲突。
* 所有问题一次收集并按 (location, field, issue) 排序，错误内容与行序无关。
* 规范摘要按三字码排序、时区按规范名归一化后做 SHA-256；调整夹具行序或
  改用别名拼写都不会改变摘要，保证相同输入始终采用同一份机场定义。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from app.errors import AppError, ConfigError, ValidationError
from app.models import Airport, Flight
from app.timeutil import (
    canonical_timezone_name,
    load_timezone,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES_DIR = ROOT / "fixtures"
DEFAULT_DB_PATH = ROOT / "data" / "disruptions.db"

# Bumped into the digest so a future change to the canonical summary shape can
# never be mistaken for an unchanged airport definition set.
CONFIG_SUMMARY_VERSION = "airport-config/v1"


@dataclass(frozen=True)
class Config:
    fixtures_dir: Path
    db_path: Path
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Config":
        fixtures_dir = Path(os.environ.get("FIXTURES_DIR", DEFAULT_FIXTURES_DIR))
        db_path = Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8080"))
        return cls(fixtures_dir=fixtures_dir, db_path=db_path, host=host, port=port)


@dataclass(frozen=True)
class AirportEntry:
    """单个机场的加载结果及其来源位置。"""

    airport: Airport
    configured_timezone: str  # spelling as written in the fixture
    location: str
    index: int

    def fact(self) -> dict[str, Any]:
        """进入规范摘要（与持久化登记）的机场事实。"""
        return {
            "code": self.airport.code,
            "name": self.airport.name,
            "timezone": self.airport.timezone,  # canonical IANA name
            "reopen_buffer_minutes": self.airport.reopen_buffer_minutes,
        }


class AirportConfig(Mapping):
    """已校验的机场集合，外加顺序无关的规范摘要。

    行为是只读的 ``code -> Airport`` 映射；附加的 :attr:`digest` 与
    :meth:`manifest` 供启动门禁和事件结果持久化使用。
    """

    def __init__(self, entries: list[AirportEntry], source: str):
        self._source = source
        self._entries = dict(
            sorted(((e.airport.code, e) for e in entries), key=lambda kv: kv[0])
        )
        self._manifest = [e.fact() for e in self._entries.values()]
        payload = CONFIG_SUMMARY_VERSION + "\n" + json.dumps(
            self._manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        self._digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # Mapping protocol ----------------------------------------------------- #
    def __getitem__(self, code: str) -> Airport:
        return self._entries[code].airport

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # Extras ---------------------------------------------------------------- #
    @property
    def source(self) -> str:
        return self._source

    @property
    def digest(self) -> str:
        return self._digest

    def entries(self) -> list[AirportEntry]:
        return list(self._entries.values())

    def manifest(self) -> list[dict[str, Any]]:
        return [dict(f) for f in self._manifest]

    def canonical_json(self) -> str:
        return json.dumps(
            self._manifest, sort_keys=True, indent=2, ensure_ascii=False
        )


def _load_json(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise AppError(f"Required fixture file is missing: {path}") from None
    except json.JSONDecodeError as exc:
        raise AppError(f"Fixture file {path} is not valid JSON: {exc.msg}") from None


def _require_fields(obj: dict, fields: tuple, source: str) -> list[str]:
    return [f for f in fields if f not in obj]


def _issue(
    path: str,
    index: int | None,
    field: str,
    issue: str,
    *,
    code: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    location = path if index is None else f"{path}[{index}]"
    row: dict[str, Any] = {"location": location, "field": field, "issue": issue}
    if index is not None:
        row["index"] = index
    if code is not None:
        row["code"] = code
    row.update(extra)
    return row


def _check_airport_row(
    item: Any, index: int, path: str, issues: list[dict[str, Any]]
) -> AirportEntry | None:
    """校验单条机场记录；任何字段非法都登记 issue 并返回 None。"""
    if not isinstance(item, dict):
        issues.append(
            _issue(path, index, ".", "must_be_object", received_type=type(item).__name__)
        )
        return None

    missing = _require_fields(
        item, ("code", "name", "timezone", "reopen_buffer_minutes"), path
    )
    if missing:
        issues.append(
            _issue(
                path,
                index,
                ".",
                "missing_fields",
                fields=", ".join(missing),
            )
        )
        return None

    code = item["code"]
    if not isinstance(code, str) or len(code) != 3 or not code.isupper():
        issues.append(
            _issue(
                path,
                index,
                "code",
                "must_be_three_letter_uppercase_string",
                received=code if isinstance(code, str) else None,
                received_type=type(code).__name__,
            )
        )
        return None

    name = item["name"]
    # NOTE: no str() coercion — a number/object/null name is a merge accident,
    # not display data.
    if not isinstance(name, str):
        issues.append(
            _issue(path, index, "name", "must_be_string", received_type=type(name).__name__)
        )
        return None
    name = name.strip()
    if not name:
        issues.append(_issue(path, index, "name", "must_be_non_empty_string"))
        return None

    raw_tz = item["timezone"]
    if not isinstance(raw_tz, str) or not raw_tz.strip():
        issues.append(
            _issue(
                path,
                index,
                "timezone",
                "must_be_non_empty_string",
                received_type=type(raw_tz).__name__,
            )
        )
        return None
    try:
        load_timezone(raw_tz)
    except ValidationError:
        issues.append(
            _issue(
                path,
                index,
                "timezone",
                "invalid_iana_timezone",
                received=raw_tz,
            )
        )
        return None
    canonical_tz = canonical_timezone_name(raw_tz)

    buffer = item["reopen_buffer_minutes"]
    # bool is a subclass of int; reject it explicitly so True no longer means 1.
    if not isinstance(buffer, int) or isinstance(buffer, bool):
        issues.append(
            _issue(
                path,
                index,
                "reopen_buffer_minutes",
                "must_be_integer",
                received=buffer if isinstance(buffer, (int, float, bool, str)) else None,
                received_type=type(buffer).__name__,
            )
        )
        return None
    if buffer < 0:
        issues.append(
            _issue(
                path,
                index,
                "reopen_buffer_minutes",
                "must_be_non_negative_integer",
                received=buffer,
            )
        )
        return None

    return AirportEntry(
        airport=Airport(
            code=code,
            name=name,
            timezone=canonical_tz,
            reopen_buffer_minutes=buffer,
        ),
        configured_timezone=raw_tz,
        location=f"{path}[{index}]",
        index=index,
    )


def load_airports(fixtures_dir: Path | str) -> AirportConfig:
    path = str(Path(fixtures_dir) / "airports.json")
    raw = _load_json(Path(path))
    if not isinstance(raw, list):
        raise ConfigError(
            f"{path} must be a JSON array",
            {"issues": [_issue(path, None, ".", "must_be_array")]},
        )

    issues: list[dict[str, Any]] = []
    entries: list[AirportEntry] = []
    for idx, item in enumerate(raw):
        entry = _check_airport_row(item, idx, path, issues)
        if entry is not None:
            entries.append(entry)

    # Cross-row checks. Group by code; iteration order is the file order so the
    # earliest occurrence is deterministically reported as "first", regardless
    # of where the later duplicate sits.
    groups: dict[str, list[AirportEntry]] = {}
    for entry in entries:  # file order
        groups.setdefault(entry.airport.code, []).append(entry)

    for code, group in groups.items():
        if len(group) == 1:
            continue
        first = group[0]
        for later in group[1:]:
            issues.append(
                _issue(
                    path,
                    later.index,
                    "code",
                    "duplicate_airport_code",
                    code=code,
                    first_location=first.location,
                    later_location=later.location,
                )
            )
            a, b = first.airport, later.airport
            if a.name != b.name:
                issues.append(
                    _issue(
                        path,
                        later.index,
                        "name",
                        "duplicate_airport_name_conflict",
                        code=code,
                        first_location=first.location,
                        registered=a.name,
                        received=b.name,
                    )
                )
            if a.timezone == b.timezone:
                if first.configured_timezone != later.configured_timezone:
                    # Different strings, identical zone rules (IANA link alias):
                    # a semantically duplicate timezone record. The merge must
                    # not silently pick one spelling.
                    issues.append(
                        _issue(
                            path,
                            later.index,
                            "timezone",
                            "semantic_timezone_duplicate",
                            code=code,
                            first_location=first.location,
                            canonical=a.timezone,
                            registered=first.configured_timezone,
                            received=later.configured_timezone,
                        )
                    )
            else:
                issues.append(
                    _issue(
                        path,
                        later.index,
                        "timezone",
                        "duplicate_airport_timezone_conflict",
                        code=code,
                        first_location=first.location,
                        registered=a.timezone,
                        received=b.timezone,
                    )
                )
            if a.reopen_buffer_minutes != b.reopen_buffer_minutes:
                issues.append(
                    _issue(
                        path,
                        later.index,
                        "reopen_buffer_minutes",
                        "duplicate_airport_buffer_conflict",
                        code=code,
                        first_location=first.location,
                        registered=a.reopen_buffer_minutes,
                        received=b.reopen_buffer_minutes,
                    )
                )
            if (
                a.name == b.name
                and a.timezone == b.timezone
                and a.reopen_buffer_minutes == b.reopen_buffer_minutes
            ):
                issues.append(
                    _issue(
                        path,
                        later.index,
                        ".",
                        "duplicate_airport_record",
                        code=code,
                        first_location=first.location,
                    )
                )

    if not raw and not issues:
        issues.append(_issue(path, None, ".", "contains_no_airports"))

    if issues:
        issues.sort(key=lambda i: (i["location"], i["field"], i["issue"]))
        raise ConfigError(
            f"{path} fails airport configuration validation with "
            f"{len(issues)} issue(s)",
            {"file": path, "issues": issues},
        )

    if not entries:
        # Defensive: structural problems should have produced issues above.
        raise ConfigError(
            f"{path} contains no airports",
            {"file": path, "issues": [_issue(path, None, ".", "contains_no_airports")]},
        )

    return AirportConfig(entries, source=path)


def load_flights(
    fixtures_dir: Path | str, airports: Mapping[str, Airport]
) -> dict[str, Flight]:
    flights_path = Path(fixtures_dir) / "flights.json"
    path = str(flights_path)
    raw = _load_json(flights_path)
    if not isinstance(raw, list):
        raise AppError(f"{path} must be a JSON array")
    flights: dict[str, Flight] = {}
    first_index: dict[str, int] = {}
    issues: list[dict[str, Any]] = []
    for idx, item in enumerate(raw):
        source = f"{path}[{idx}]"
        if not isinstance(item, dict):
            issues.append(
                {"location": source, "field": ".", "issue": "must_be_object"}
            )
            continue
        missing = _require_fields(
            item,
            (
                "flight_id",
                "flight_number",
                "origin",
                "destination",
                "scheduled_departure",
                "scheduled_arrival",
                "passenger_count",
                "can_retime",
                "max_delay_minutes",
            ),
            source,
        )
        if missing:
            issues.append(
                {"location": source, "field": ".", "issue": "missing_fields",
                 "fields": ", ".join(missing)}
            )
            continue
        flight_id = item["flight_id"]
        if not isinstance(flight_id, str) or not flight_id:
            issues.append(
                {"location": source, "field": "flight_id",
                 "issue": "must_be_non_empty_string"}
            )
            continue
        if flight_id in flights:
            issues.append(
                {"location": source, "field": "flight_id",
                 "issue": "duplicate_flight_id", "received": flight_id,
                 "first_location": f"{path}[{first_index[flight_id]}]"}
            )
            continue
        first_index[flight_id] = idx
        flight_number = item["flight_number"]
        if not isinstance(flight_number, str) or not flight_number.strip():
            issues.append(
                {"location": source, "field": "flight_number",
                 "issue": "must_be_non_empty_string",
                 "received_type": type(flight_number).__name__}
            )
            continue
        bad_endpoint = False
        for endpoint in ("origin", "destination"):
            code = item[endpoint]
            if not isinstance(code, str) or code not in airports:
                issues.append(
                    {
                        "location": source,
                        "field": endpoint,
                        "issue": "references_unknown_airport",
                        "received": code,
                    }
                )
                bad_endpoint = True
        if bad_endpoint:
            continue
        try:
            departure = parse_fixture_datetime(
                item["scheduled_departure"], f"{source}.scheduled_departure"
            )
            arrival = parse_fixture_datetime(
                item["scheduled_arrival"], f"{source}.scheduled_arrival"
            )
        except AppError as exc:
            issues.append({"location": source, "field": ".", "issue": exc.message})
            continue
        if arrival <= departure:
            issues.append(
                {"location": source, "field": "scheduled_arrival",
                 "issue": "must_be_after_scheduled_departure"}
            )
            continue
        passengers = item["passenger_count"]
        if not isinstance(passengers, int) or isinstance(passengers, bool) or passengers < 0:
            issues.append(
                {"location": source, "field": "passenger_count",
                 "issue": "must_be_non_negative_integer",
                 "received_type": type(passengers).__name__}
            )
            continue
        can_retime = item["can_retime"]
        if not isinstance(can_retime, bool):
            issues.append(
                {"location": source, "field": "can_retime",
                 "issue": "must_be_boolean", "received_type": type(can_retime).__name__}
            )
            continue
        max_delay = item["max_delay_minutes"]
        if not isinstance(max_delay, int) or isinstance(max_delay, bool) or max_delay < 0:
            issues.append(
                {"location": source, "field": "max_delay_minutes",
                 "issue": "must_be_non_negative_integer",
                 "received_type": type(max_delay).__name__}
            )
            continue
        if not can_retime and max_delay != 0:
            issues.append(
                {"location": source, "field": "max_delay_minutes",
                 "issue": "must_be_zero_when_can_retime_false"}
            )
            continue
        flights[flight_id] = Flight(
            flight_id=flight_id,
            flight_number=flight_number.strip(),
            origin=item["origin"],
            destination=item["destination"],
            scheduled_departure=departure,
            scheduled_arrival=arrival,
            passenger_count=passengers,
            can_retime=can_retime,
            max_delay_minutes=max_delay,
        )

    if issues:
        issues.sort(key=lambda i: (i["location"], i["field"], i["issue"]))
        raise ConfigError(
            f"{path} fails flight fixture validation with {len(issues)} issue(s)",
            {"file": path, "issues": issues},
        )
    if not flights:
        raise AppError(f"{path} contains no flights")
    return flights


def parse_fixture_datetime(value: object, field: str) -> datetime:
    from datetime import timezone

    if not isinstance(value, str):
        raise AppError(f"{field} must be a string")
    text = value.strip()
    if not text.endswith(("Z", "+00:00")):
        # Fixture timestamps are documented as ISO 8601 UTC values.
        raise AppError(f"{field} must be a UTC timestamp (end with Z)")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise AppError(f"{field} is not a valid ISO 8601 date-time") from None
    return dt.astimezone(timezone.utc)
