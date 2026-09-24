"""配置与领域夹具加载。

机场配置是计算的唯一事实来源：装载阶段必须拒绝一切歧义，而不是让
"字典最后一项获胜"。以下情况都会抛出带来源位置（如 ``airports[2]``）
的确定性 :class:`ConfigError`，且与记录在文件中的先后顺序无关：

* 同一三字码出现多次——无论两条记录字段冲突（时区/缓冲不同）还是
  语义重复（内容完全一致），都不允许静默覆盖；
* ``name`` / ``timezone`` 不是字符串，或时区不是有效的 IANA 名称；
* ``reopen_buffer_minutes`` 不是非负整数；
* 航班引用了配置中不存在的机场代码。

装载成功后，:func:`airports_digest` 为全部机场定义计算与文件顺序无关的
确定性摘要，随启动门禁登记到 SQLite，并随每个事件的处理结果保存。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.errors import AppError, ConfigError
from app.models import Airport, Flight
from app.timeutil import load_timezone

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES_DIR = ROOT / "fixtures"
DEFAULT_DB_PATH = ROOT / "data" / "disruptions.db"

_AIRPORT_CODE_RE = re.compile(r"^[A-Z]{3}$")
_FLIGHT_NUMBER_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,15}$")


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


def _load_json(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"Required fixture file is missing: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Fixture file {path} is not valid JSON: {exc.msg}") from None


def _require_fields(obj: dict, fields: tuple, source: str) -> None:
    missing = [f for f in fields if f not in obj]
    if missing:
        raise ConfigError(f"{source} is missing required field(s): {', '.join(missing)}")


def load_airports(fixtures_dir: Path) -> dict[str, Airport]:
    """装载并严格校验机场配置。

    任何重复、类型错误或无效值都抛出带 ``airports[i]`` 来源位置的
    :class:`ConfigError`；绝不靠"后一条覆盖前一条"消解歧义。
    """
    raw = _load_json(fixtures_dir / "airports.json")
    if not isinstance(raw, list):
        raise ConfigError("fixtures/airports.json must be a JSON array")

    parsed: list[tuple[str, Airport]] = []
    for idx, item in enumerate(raw):
        source = f"airports[{idx}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{source} must be an object")
        _require_fields(
            item, ("code", "name", "timezone", "reopen_buffer_minutes"), source
        )
        code = item["code"]
        if not isinstance(code, str) or not _AIRPORT_CODE_RE.fullmatch(code):
            raise ConfigError(
                f"{source}.code must be a 3-letter uppercase string",
                {"source": f"{source}.code", "received": code},
            )
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                f"{source}.name must be a non-empty string",
                {"source": f"{source}.name", "received": name},
            )
        timezone = item["timezone"]
        if not isinstance(timezone, str) or not timezone.strip():
            raise ConfigError(
                f"{source}.timezone must be a non-empty string",
                {"source": f"{source}.timezone", "received": timezone},
            )
        buffer = item["reopen_buffer_minutes"]
        if not isinstance(buffer, int) or isinstance(buffer, bool) or buffer < 0:
            raise ConfigError(
                f"{source}.reopen_buffer_minutes must be a non-negative integer",
                {"source": f"{source}.reopen_buffer_minutes", "received": buffer},
            )
        try:
            tz = load_timezone(timezone)
        except AppError as exc:
            # 时区校验错误归一化为配置错误，并补上来源位置。
            raise ConfigError(
                f"{source}.timezone: {exc.message}",
                {"source": f"{source}.timezone", "received": timezone},
            ) from None
        parsed.append(
            (
                source,
                Airport(
                    code=code,
                    name=name,
                    timezone=str(tz),
                    reopen_buffer_minutes=buffer,
                ),
            )
        )

    if not parsed:
        raise ConfigError("fixtures/airports.json contains no airports")

    airports: dict[str, Airport] = {}
    sources: dict[str, str] = {}
    for source, airport in parsed:
        first_source = sources.get(airport.code)
        if first_source is not None:
            first = airports[airport.code]
            if first == airport:
                raise ConfigError(
                    f"Duplicate airport record for code '{airport.code}': "
                    f"{first_source} and {source} are semantically identical; "
                    "remove one of them instead of relying on override order",
                    {
                        "code": airport.code,
                        "issue": "duplicate_airport_record",
                        "sources": [first_source, source],
                    },
                )
            differing = sorted(
                field
                for field, before, after in (
                    ("name", first.name, airport.name),
                    ("timezone", first.timezone, airport.timezone),
                    (
                        "reopen_buffer_minutes",
                        first.reopen_buffer_minutes,
                        airport.reopen_buffer_minutes,
                    ),
                )
                if before != after
            )
            raise ConfigError(
                f"Conflicting airport records for code '{airport.code}': "
                f"{first_source} and {source} disagree on "
                f"{', '.join(differing)}; refusing to let the last record win",
                {
                    "code": airport.code,
                    "issue": "conflicting_airport_records",
                    "sources": [first_source, source],
                    "differing_fields": differing,
                },
            )
        sources[airport.code] = source
        airports[airport.code] = airport
    return airports


def airports_digest(airports: dict[str, Airport]) -> str:
    """计算机场定义的确定性摘要。

    摘要只取决于机场集合本身（代码、名称、时区、恢复缓冲），与记录在
    文件中的顺序无关：同一批机场定义无论以何种顺序合并，摘要都相同。
    """
    canonical = [
        {
            "code": airport.code,
            "name": airport.name,
            "timezone": airport.timezone,
            "reopen_buffer_minutes": airport.reopen_buffer_minutes,
        }
        for airport in sorted(airports.values(), key=lambda a: a.code)
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_flights(fixtures_dir: Path, airports: dict[str, Airport]) -> dict[str, Flight]:
    raw = _load_json(fixtures_dir / "flights.json")
    if not isinstance(raw, list):
        raise ConfigError("fixtures/flights.json must be a JSON array")
    flights: dict[str, Flight] = {}
    for idx, item in enumerate(raw):
        source = f"flights[{idx}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{source} must be an object")
        _require_fields(
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
        flight_id = item["flight_id"]
        if not isinstance(flight_id, str) or not flight_id:
            raise ConfigError(f"{source}.flight_id must be a non-empty string")
        if flight_id in flights:
            raise ConfigError(
                f"Duplicate flight_id in fixtures: {flight_id}",
                {"flight_id": flight_id, "issue": "duplicate_flight_id"},
            )
        flight_number = item["flight_number"]
        if not isinstance(flight_number, str) or not _FLIGHT_NUMBER_RE.fullmatch(
            flight_number
        ):
            raise ConfigError(
                f"{source}.flight_number must match {_FLIGHT_NUMBER_RE.pattern}",
                {"source": f"{source}.flight_number", "received": flight_number},
            )
        for endpoint in ("origin", "destination"):
            code = item[endpoint]
            if not isinstance(code, str) or code not in airports:
                raise ConfigError(
                    f"{source}.{endpoint} references unknown airport code '{code}'",
                    {"source": f"{source}.{endpoint}", "received": code},
                )
        departure = parse_fixture_datetime(item["scheduled_departure"], f"{source}.scheduled_departure")
        arrival = parse_fixture_datetime(item["scheduled_arrival"], f"{source}.scheduled_arrival")
        if arrival <= departure:
            raise ConfigError(f"{source}: scheduled_arrival must be after scheduled_departure")
        passengers = item["passenger_count"]
        if not isinstance(passengers, int) or isinstance(passengers, bool) or passengers < 0:
            raise ConfigError(f"{source}.passenger_count must be a non-negative integer")
        can_retime = item["can_retime"]
        if not isinstance(can_retime, bool):
            raise ConfigError(f"{source}.can_retime must be a boolean")
        max_delay = item["max_delay_minutes"]
        if not isinstance(max_delay, int) or isinstance(max_delay, bool) or max_delay < 0:
            raise ConfigError(f"{source}.max_delay_minutes must be a non-negative integer")
        if not can_retime and max_delay != 0:
            raise ConfigError(f"{source}: max_delay_minutes must be 0 when can_retime is false")
        flights[flight_id] = Flight(
            flight_id=flight_id,
            flight_number=flight_number,
            origin=item["origin"],
            destination=item["destination"],
            scheduled_departure=departure,
            scheduled_arrival=arrival,
            passenger_count=passengers,
            can_retime=can_retime,
            max_delay_minutes=max_delay,
        )
    if not flights:
        raise ConfigError("fixtures/flights.json contains no flights")
    return flights


def parse_fixture_datetime(value: object, field: str) -> datetime:
    from datetime import timezone

    if not isinstance(value, str):
        raise ConfigError(f"{field} must be a string")
    text = value.strip()
    if not text.endswith(("Z", "+00:00")):
        # Fixture timestamps are documented as ISO 8601 UTC values.
        raise ConfigError(f"{field} must be a UTC timestamp (end with Z)")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ConfigError(f"{field} is not a valid ISO 8601 date-time") from None
    return dt.astimezone(timezone.utc)
