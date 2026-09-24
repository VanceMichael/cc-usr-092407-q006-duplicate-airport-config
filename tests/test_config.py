"""配置装载器的严格校验与顺序确定性测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import load_airports, load_flights
from app.errors import ConfigError

AIRPORTS = [
    {"code": "APS", "name": "Awan Pura International", "timezone": "Asia/Makassar",
     "reopen_buffer_minutes": 20},
    {"code": "BSR", "name": "Basa Raya Airport", "timezone": "Asia/Jakarta",
     "reopen_buffer_minutes": 15},
    {"code": "KTA", "name": "Karta Regional", "timezone": "Asia/Jakarta",
     "reopen_buffer_minutes": 10},
]

FLIGHTS = [
    {"flight_id": "AX-1", "flight_number": "AX1", "origin": "APS", "destination": "BSR",
     "scheduled_departure": "2026-09-07T15:30:00Z", "scheduled_arrival": "2026-09-07T17:10:00Z",
     "passenger_count": 100, "can_retime": True, "max_delay_minutes": 60},
]


class LoaderCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_airports(self, data) -> Path:
        p = self.dir / "airports.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def write_flights(self, data=FLIGHTS) -> Path:
        p = self.dir / "flights.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def issues_for(self, data):
        path = self.write_airports(data)
        with self.assertRaises(ConfigError) as ctx:
            load_airports(self.dir)
        issues = ctx.exception.details["issues"]
        for issue in issues:
            self.assertTrue(
                issue["location"].startswith(str(path)),
                f"issue lacks file location: {issue}",
            )
        return issues

    # ------------------------------------------------------------------ #
    # Duplicate airport code: never "last dict entry wins"
    # ------------------------------------------------------------------ #

    def test_duplicate_code_with_conflicting_tz_and_buffer_rejected(self) -> None:
        dup = AIRPORTS + [
            {"code": "APS", "name": "Awan Pura International",
             "timezone": "Asia/Jakarta", "reopen_buffer_minutes": 45}
        ]
        issues = self.issues_for(dup)
        kinds = {(i["field"], i["issue"]) for i in issues}
        self.assertIn(("code", "duplicate_airport_code"), kinds)
        self.assertIn(("timezone", "duplicate_airport_timezone_conflict"), kinds)
        self.assertIn(("reopen_buffer_minutes", "duplicate_airport_buffer_conflict"), kinds)
        code_issue = next(i for i in issues if i["issue"] == "duplicate_airport_code")
        # Both source locations are reported deterministically.
        self.assertEqual(code_issue["first_location"], str(self.dir / "airports.json[0]"))
        self.assertEqual(code_issue["later_location"], str(self.dir / "airports.json[3]"))
        tz_issue = next(i for i in issues if i["field"] == "timezone")
        self.assertEqual(tz_issue["registered"], "Asia/Makassar")
        self.assertEqual(tz_issue["received"], "Asia/Jakarta")

    def test_exact_duplicate_row_rejected(self) -> None:
        issues = self.issues_for(AIRPORTS + [dict(AIRPORTS[0])])
        kinds = {i["issue"] for i in issues}
        self.assertIn("duplicate_airport_code", kinds)
        self.assertIn("duplicate_airport_record", kinds)

    def test_semantic_timezone_duplicate_via_iana_link(self) -> None:
        # Singapore is an IANA link to Asia/Singapore: same rules, different
        # string. Two rows for one code using both spellings is a semantic
        # duplicate, not something a dict merge may resolve by overwrite.
        rows = [
            {"code": "SIN", "name": "Singapura", "timezone": "Asia/Singapore",
             "reopen_buffer_minutes": 20},
            {"code": "BSR", "name": "Basa Raya", "timezone": "Asia/Jakarta",
             "reopen_buffer_minutes": 15},
            {"code": "SIN", "name": "Singapura", "timezone": "Singapore",
             "reopen_buffer_minutes": 20},
        ]
        issues = self.issues_for(rows)
        sem = [i for i in issues if i["issue"] == "semantic_timezone_duplicate"]
        self.assertEqual(len(sem), 1)
        self.assertEqual(sem[0]["canonical"], "Asia/Singapore")
        self.assertEqual(sem[0]["registered"], "Asia/Singapore")
        self.assertEqual(sem[0]["received"], "Singapore")

    def test_zulu_utc_link_pair_detected(self) -> None:
        rows = [
            {"code": "XYZ", "name": "X", "timezone": "Etc/UTC", "reopen_buffer_minutes": 0},
            {"code": "XYZ", "name": "X", "timezone": "Zulu", "reopen_buffer_minutes": 0},
        ]
        issues = self.issues_for(rows)
        self.assertIn("semantic_timezone_duplicate", {i["issue"] for i in issues})

    # ------------------------------------------------------------------ #
    # Field type validation
    # ------------------------------------------------------------------ #

    def test_non_string_name_rejected_without_coercion(self) -> None:
        for bad in (123, None, ["x"], {"a": 1}, True):
            data = [dict(a) for a in AIRPORTS]
            data[1]["name"] = bad
            issues = self.issues_for(data)
            self.assertTrue(
                any(i["field"] == "name" and i["issue"] == "must_be_string" for i in issues),
                f"name={bad!r} should be rejected",
            )

    def test_blank_name_rejected(self) -> None:
        data = [dict(a) for a in AIRPORTS]
        data[0]["name"] = "   "
        issues = self.issues_for(data)
        self.assertTrue(any(i["issue"] == "must_be_non_empty_string" for i in issues))

    def test_invalid_buffers_rejected(self) -> None:
        for bad in (True, False, 1.5, -1, "20", None):
            data = [dict(a) for a in AIRPORTS]
            data[2]["reopen_buffer_minutes"] = bad
            issues = self.issues_for(data)
            self.assertTrue(
                any(i["field"] == "reopen_buffer_minutes" for i in issues),
                f"buffer={bad!r} should be rejected",
            )

    def test_invalid_timezone_rejected(self) -> None:
        data = [dict(a) for a in AIRPORTS]
        data[0]["timezone"] = "Mars/Olympus"
        issues = self.issues_for(data)
        self.assertTrue(any(i["issue"] == "invalid_iana_timezone" for i in issues))

    def test_non_object_and_bad_code_reported(self) -> None:
        issues = self.issues_for([42, {"code": "bad", "name": "N",
                                       "timezone": "Asia/Jakarta",
                                       "reopen_buffer_minutes": 1}])
        self.assertTrue(any(i["issue"] == "must_be_object" for i in issues))
        self.assertTrue(
            any(i["issue"] == "must_be_three_letter_uppercase_string" for i in issues)
        )

    def test_issues_are_deterministically_sorted(self) -> None:
        data = [
            {"code": "ZZZ", "name": 9, "timezone": "nope", "reopen_buffer_minutes": True},
            AIRPORTS[0],
            {"code": "APS", "name": "Other", "timezone": "Asia/Jakarta",
             "reopen_buffer_minutes": 5},
        ]
        i1 = self.issues_for(data)
        i2 = self.issues_for(data)
        key = lambda i: (i["location"], i["field"], i["issue"])
        self.assertEqual(i1, i2)
        self.assertEqual(i1, sorted(i1, key=key))

    # ------------------------------------------------------------------ #
    # Order-independent canonical digest
    # ------------------------------------------------------------------ #

    def test_reordering_rows_keeps_digest(self) -> None:
        self.write_airports(AIRPORTS)
        first = load_airports(self.dir).digest
        self.write_airports([AIRPORTS[2], AIRPORTS[0], AIRPORTS[1]])
        second = load_airports(self.dir).digest
        self.assertEqual(first, second)

    def test_alias_spelling_keeps_digest(self) -> None:
        rows = [
            {"code": "SIN", "name": "Singapura", "timezone": "Asia/Singapore",
             "reopen_buffer_minutes": 20},
            {"code": "BSR", "name": "Basa Raya", "timezone": "Asia/Jakarta",
             "reopen_buffer_minutes": 15},
        ]
        self.write_airports(rows)
        first = load_airports(self.dir).digest
        aliased = [dict(r) for r in rows]
        aliased[0]["timezone"] = "Singapore"
        self.write_airports(aliased)
        second = load_airports(self.dir).digest
        self.assertEqual(first, second)
        # Canonical spelling is what the loaded model carries.
        self.assertEqual(load_airports(self.dir)["SIN"].timezone, "Asia/Singapore")

    def test_changed_buffer_changes_digest(self) -> None:
        self.write_airports(AIRPORTS)
        first = load_airports(self.dir).digest
        changed = [dict(a) for a in AIRPORTS]
        changed[1]["reopen_buffer_minutes"] = 99
        self.write_airports(changed)
        self.assertNotEqual(first, load_airports(self.dir).digest)

    # ------------------------------------------------------------------ #
    # Flights are cross-checked against the airport config as one unit
    # ------------------------------------------------------------------ #

    def test_flight_referencing_unknown_airport_rejected_with_location(self) -> None:
        self.write_airports(AIRPORTS)
        bad = [dict(FLIGHTS[0], origin="ZZZ")]
        self.write_flights(bad)
        airports = load_airports(self.dir)
        with self.assertRaises(ConfigError) as ctx:
            load_flights(self.dir, airports)
        issue = ctx.exception.details["issues"][0]
        self.assertEqual(issue["issue"], "references_unknown_airport")
        self.assertEqual(issue["received"], "ZZZ")
        self.assertTrue(issue["location"].endswith("flights.json[0]"))

    def test_duplicate_flight_id_reported_with_both_locations(self) -> None:
        self.write_airports(AIRPORTS)
        self.write_flights([FLIGHTS[0], dict(FLIGHTS[0])])
        with self.assertRaises(ConfigError) as ctx:
            load_flights(self.dir, load_airports(self.dir))
        issue = ctx.exception.details["issues"][0]
        self.assertEqual(issue["issue"], "duplicate_flight_id")
        self.assertTrue(issue["first_location"].endswith("flights.json[0]"))
        self.assertTrue(issue["location"].endswith("flights.json[1]"))


if __name__ == "__main__":
    unittest.main()
