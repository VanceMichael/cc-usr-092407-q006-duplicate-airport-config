"""机场/航班配置装载的确定性校验测试。

覆盖部署事故的直接根因：合并两份机场配置时保留了相同三字码的两行，
后一行的时区与恢复缓冲曾静默覆盖前一行。现在任何重复、类型错误或
无效值都必须形成带来源位置的确定性错误。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import airports_digest, load_airports, load_flights
from app.errors import ConfigError
from tests.support import FIXTURES_DIR


def airport(
    code: str = "APS",
    name: str = "Awan Pura International",
    timezone: str = "Asia/Makassar",
    buffer: int = 20,
) -> dict:
    return {
        "code": code,
        "name": name,
        "timezone": timezone,
        "reopen_buffer_minutes": buffer,
    }


def flight(origin: str = "APS", destination: str = "BSR", **overrides) -> dict:
    payload = {
        "flight_id": "AX-410-20260907",
        "flight_number": "AX410",
        "origin": origin,
        "destination": destination,
        "scheduled_departure": "2026-09-07T15:30:00Z",
        "scheduled_arrival": "2026-09-07T17:10:00Z",
        "passenger_count": 168,
        "can_retime": True,
        "max_delay_minutes": 120,
    }
    payload.update(overrides)
    return payload


def write_fixtures(directory: Path, airports: list, flights: list | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "airports.json").write_text(
        json.dumps(airports, ensure_ascii=False), encoding="utf-8"
    )
    if flights is not None:
        (directory / "flights.json").write_text(
            json.dumps(flights, ensure_ascii=False), encoding="utf-8"
        )
    return directory


class LoadAirportsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def load(self, airports: list):
        write_fixtures(self.dir, airports)
        return load_airports(self.dir)

    # ------------------------------------------------------------------ #
    # 重复三字码：绝不靠"最后一项获胜"
    # ------------------------------------------------------------------ #

    def test_conflicting_duplicate_records_rejected_with_both_sources(self) -> None:
        # 事故场景：合并两份配置后同一三字码保留两行，时区与缓冲不同。
        records = [
            airport("APS", timezone="Asia/Makassar", buffer=20),
            airport("BSR", name="Basa Raya Airport", timezone="Asia/Jakarta", buffer=15),
            airport("APS", timezone="America/New_York", buffer=90),
        ]
        with self.assertRaises(ConfigError) as ctx:
            self.load(records)
        message = ctx.exception.message
        self.assertIn("airports[0]", message)
        self.assertIn("airports[2]", message)
        self.assertIn("APS", message)
        details = ctx.exception.details
        self.assertEqual(details["issue"], "conflicting_airport_records")
        self.assertEqual(details["sources"], ["airports[0]", "airports[2]"])
        self.assertEqual(
            details["differing_fields"], ["reopen_buffer_minutes", "timezone"]
        )

    def test_duplicate_error_does_not_depend_on_record_order(self) -> None:
        # 无论冲突行以什么顺序出现，结果都是同一个确定性错误，
        # 而不是"某一行静默生效"。
        first = airport("APS", timezone="Asia/Makassar", buffer=20)
        second = airport("APS", timezone="America/New_York", buffer=90)
        for records in ([first, second], [second, first]):
            with self.subTest(order=records[0]["timezone"]):
                with self.assertRaises(ConfigError) as ctx:
                    self.load(records)
                self.assertEqual(
                    ctx.exception.details["issue"], "conflicting_airport_records"
                )

    def test_semantically_identical_duplicate_rejected(self) -> None:
        # 内容完全一致的重复行同样是错误：配置里不允许存在"靠覆盖
        # 顺序决定结果"的歧义记录。
        records = [
            airport("APS"),
            airport("BSR", name="Basa Raya Airport", timezone="Asia/Jakarta", buffer=15),
            airport("APS"),
        ]
        with self.assertRaises(ConfigError) as ctx:
            self.load(records)
        self.assertEqual(ctx.exception.details["issue"], "duplicate_airport_record")
        self.assertEqual(ctx.exception.details["sources"], ["airports[0]", "airports[2]"])
        self.assertIn("airports[0]", ctx.exception.message)
        self.assertIn("airports[2]", ctx.exception.message)

    def test_last_record_never_wins(self) -> None:
        # 直接针对事故本身：装载绝不能静默采用后一行的时区/缓冲。
        records = [
            airport("APS", timezone="Asia/Makassar", buffer=20),
            airport("APS", timezone="America/New_York", buffer=90),
        ]
        with self.assertRaises(ConfigError):
            self.load(records)

    # ------------------------------------------------------------------ #
    # 字段类型与取值
    # ------------------------------------------------------------------ #

    def test_non_string_name_rejected_with_source(self) -> None:
        for bad in (123, None, ["x"], {"n": 1}, True):
            with self.subTest(name=bad):
                with self.assertRaises(ConfigError) as ctx:
                    self.load([airport(name=bad)])
                self.assertIn("airports[0].name", ctx.exception.message)

    def test_blank_name_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load([airport(name="   ")])

    def test_non_string_timezone_rejected_with_source(self) -> None:
        for bad in (8, None, ["Asia/Makassar"], False):
            with self.subTest(timezone=bad):
                with self.assertRaises(ConfigError) as ctx:
                    self.load([airport(timezone=bad)])
                self.assertIn("airports[0].timezone", ctx.exception.message)

    def test_unknown_iana_timezone_rejected_with_source(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self.load([airport(timezone="Mars/Olympus_Mons")])
        self.assertIn("airports[0].timezone", ctx.exception.message)
        self.assertEqual(ctx.exception.details["received"], "Mars/Olympus_Mons")

    def test_invalid_buffer_rejected_with_source(self) -> None:
        for bad in (-1, "20", 1.5, True, None):
            with self.subTest(buffer=bad):
                with self.assertRaises(ConfigError) as ctx:
                    self.load([airport(buffer=bad)])
                self.assertIn("airports[0].reopen_buffer_minutes", ctx.exception.message)

    def test_invalid_code_rejected_with_source(self) -> None:
        for bad in ("aps", "AP1", "AP", "APSS", "A S", 123, None):
            with self.subTest(code=bad):
                with self.assertRaises(ConfigError) as ctx:
                    self.load([airport(code=bad)])
                self.assertIn("airports[0].code", ctx.exception.message)

    def test_missing_required_field_rejected_with_source(self) -> None:
        record = airport()
        del record["timezone"]
        with self.assertRaises(ConfigError) as ctx:
            self.load([record])
        self.assertIn("airports[0]", ctx.exception.message)
        self.assertIn("timezone", ctx.exception.message)

    def test_non_object_record_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self.load([["APS"]])
        self.assertIn("airports[0]", ctx.exception.message)

    def test_error_position_tracks_file_order(self) -> None:
        # 来源位置必须指向出错的实际行，而不是某个固定位置。
        records = [
            airport("BSR", name="Basa Raya Airport", timezone="Asia/Jakarta", buffer=15),
            airport("APS", buffer=-5),
        ]
        with self.assertRaises(ConfigError) as ctx:
            self.load(records)
        self.assertIn("airports[1].reopen_buffer_minutes", ctx.exception.message)

    # ------------------------------------------------------------------ #
    # 摘要：与记录顺序无关、对定义内容敏感
    # ------------------------------------------------------------------ #

    def test_digest_independent_of_record_order(self) -> None:
        records = [
            airport("APS", timezone="Asia/Makassar", buffer=20),
            airport("BSR", name="Basa Raya Airport", timezone="Asia/Jakarta", buffer=15),
            airport("KTA", name="Karta Regional", timezone="Asia/Jakarta", buffer=10),
        ]
        forward = self.load(records)
        reversed_dir = self.dir / "reversed"
        write_fixtures(reversed_dir, list(reversed(records)))
        backward = load_airports(reversed_dir)
        self.assertEqual(forward, backward)
        self.assertEqual(airports_digest(forward), airports_digest(backward))

    def test_digest_changes_with_any_definition_field(self) -> None:
        base = self.load([airport("APS")])
        base_digest = airports_digest(base)
        variants = [
            airport("APS", timezone="America/New_York"),
            airport("APS", buffer=21),
            airport("APS", name="Awan Pura Intl"),
            airport("XYZ"),
        ]
        for record in variants:
            with self.subTest(record=record):
                other_dir = self.dir / f"variant-{record['code']}-{record['timezone']}"
                other = load_airports(write_fixtures(other_dir, [record]))
                self.assertNotEqual(base_digest, airports_digest(other))

    def test_shipped_fixtures_load_and_digest_is_stable(self) -> None:
        first = load_airports(FIXTURES_DIR)
        second = load_airports(FIXTURES_DIR)
        self.assertEqual(airports_digest(first), airports_digest(second))
        self.assertEqual(len(airports_digest(first)), 64)


class LoadFlightsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.airports = load_airports(FIXTURES_DIR)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def load_flights(self, flights: list):
        # 机场配置直接使用仓库夹具的拷贝，航班文件写入临时目录。
        import shutil

        shutil.copy(FIXTURES_DIR / "airports.json", self.dir / "airports.json")
        (self.dir / "flights.json").write_text(
            json.dumps(flights, ensure_ascii=False), encoding="utf-8"
        )
        return load_flights(self.dir, self.airports)

    def test_flight_referencing_unknown_airport_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self.load_flights([flight(destination="ZZZ")])
        self.assertIn("flights[0].destination", ctx.exception.message)
        self.assertIn("ZZZ", ctx.exception.message)

    def test_flight_non_string_endpoint_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self.load_flights([flight(origin=123)])
        self.assertIn("flights[0].origin", ctx.exception.message)

    def test_duplicate_flight_id_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            self.load_flights([flight(), flight()])
        self.assertEqual(ctx.exception.details["issue"], "duplicate_flight_id")

    def test_shipped_flights_load(self) -> None:
        flights = load_flights(FIXTURES_DIR, self.airports)
        self.assertEqual(len(flights), 4)


if __name__ == "__main__":
    unittest.main()
