"""在临时端口启动真实服务执行 HTTP 端到端测试。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.server import build_server, AppState
from app.errors import ConfigConflictError
from tests.support import ServiceTestCase, base_event


def _request(method: str, url: str, body=None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        state = AppState(service=self.service, gate_status=self.gate_status)
        self.server: ThreadingHTTPServer = build_server("127.0.0.1", 0, state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def test_health(self) -> None:
        status, body = _request("GET", f"{self.base}/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_valid_event_round_trip(self) -> None:
        status, body = _request("POST", f"{self.base}/api/v1/events", base_event())
        self.assertEqual(status, 201)
        self.assertEqual(body["processing_state"], "processed")
        self.assertGreater(body["impact_count"], 0)

        status, fetched = _request(
            "GET", f"{self.base}/api/v1/events/{body['event_id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(fetched["impacts"], body["impacts"])

    def test_invalid_event_structured_error_and_no_write(self) -> None:
        bad = base_event(airport_code="ZZZ")
        status, body = _request("POST", f"{self.base}/api/v1/events", bad)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "unknown_airport")
        self.assertIn("message", body["error"])
        self.assertIn("details", body["error"])

        status, _ = _request(
            "GET", f"{self.base}/api/v1/events/{bad['event_id']}"
        )
        self.assertEqual(status, 404)

    def test_malformed_json_is_bad_request(self) -> None:
        req = urllib.request.Request(
            f"{self.base}/api/v1/events",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            body = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(body["error"]["code"], "bad_request")

    def test_wrong_content_type(self) -> None:
        req = urllib.request.Request(
            f"{self.base}/api/v1/events",
            data=b"{}",
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertEqual(
                json.loads(exc.read())["error"]["code"], "unsupported_media_type"
            )

    def test_idempotent_replay_over_http(self) -> None:
        payload = base_event()
        s1, b1 = _request("POST", f"{self.base}/api/v1/events", payload)
        s2, b2 = _request("POST", f"{self.base}/api/v1/events", dict(payload))
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(b1["impacts"], b2["impacts"])
        self.assertEqual(b2["processing_state"], "replayed")
        _, status = _request("GET", f"{self.base}/api/v1/events/{payload['event_id']}")
        self.assertEqual(status["processing"]["replay_count"], 1)

    def test_conflicting_submission_409(self) -> None:
        payload = base_event()
        _request("POST", f"{self.base}/api/v1/events", payload)
        changed = dict(payload)
        changed["reason"] = "ash cloud update"
        status, body = _request("POST", f"{self.base}/api/v1/events", changed)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "event_conflict")

    def test_unknown_route_404(self) -> None:
        status, body = _request("GET", f"{self.base}/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_method_not_allowed(self) -> None:
        status, body = _request("POST", f"{self.base}/healthz")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "method_not_allowed")

    def test_airport_summary_endpoint(self) -> None:
        _request("POST", f"{self.base}/api/v1/events", base_event())
        status, body = _request(
            "GET", f"{self.base}/api/v1/airports/APS/summary"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["affected_flights"], 3)

    def test_pagination_endpoint(self) -> None:
        _request("POST", f"{self.base}/api/v1/events", base_event())
        status, body = _request(
            "GET", f"{self.base}/api/v1/flights/affected?limit=2&offset=0"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["pagination"]["total"], 3)
        self.assertEqual(len(body["flights"]), 2)

        status, body = _request(
            "GET", f"{self.base}/api/v1/flights/affected?limit=bogus"
        )
        self.assertEqual(status, 400)

    def test_cross_midnight_event_over_http(self) -> None:
        # Submitted with a +08:00 offset; equivalent to the 15:00-19:00Z window.
        payload = base_event(
            event_id="evt-midnight0001",
            effective_from="2026-09-07T23:00:00+08:00",
            effective_until="2026-09-08T03:00:00+08:00",
        )
        status, body = _request("POST", f"{self.base}/api/v1/events", payload)
        self.assertEqual(status, 201)
        self.assertTrue(all(i["crosses_midnight"] for i in body["impacts"]))
        self.assertEqual(body["impact_count"], 3)

    def test_healthz_and_readyz_distinct(self) -> None:
        status, health = _request("GET", f"{self.base}/healthz")
        self.assertEqual((status, health["status"]), (200, "ok"))
        status, ready = _request("GET", f"{self.base}/readyz")
        self.assertEqual(status, 200)
        self.assertEqual(ready["status"], "ready")
        self.assertIn("config_digest", ready["gate"])
        self.assertEqual(ready["gate"]["state"], "empty_initialized")

    def test_event_result_and_status_carry_config_summary(self) -> None:
        status, body = _request("POST", f"{self.base}/api/v1/events", base_event())
        self.assertEqual(status, 201)
        digest = body["config"]["config_digest"]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(body["config"]["airport"]["code"], "APS")
        self.assertEqual(
            body["config"]["airport"]["reopen_buffer_minutes"], 20
        )
        status, fetched = _request(
            "GET", f"{self.base}/api/v1/events/{body['event_id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(fetched["config"]["config_digest"], digest)
        self.assertEqual(fetched["config"]["airport"]["timezone"], "Asia/Makassar")


class GateFailedHttpTest(unittest.TestCase):
    """门禁冲突时：进程存活、就绪失败、所有业务请求 503。"""

    def setUp(self) -> None:
        conflict = ConfigConflictError(
            "Airport configuration conflicts with registered facts",
            {
                "config_digest": "deadbeef",
                "issues": [
                    {
                        "field": "reopen_buffer_minutes",
                        "issue": "airport_definition_changed",
                        "code": "APS",
                        "registered": 20,
                        "configured": 99,
                        "configured_location": "fixtures/airports.json[0]",
                    }
                ],
            },
        )
        state = AppState(service=None, gate_status=None, gate_error=conflict)
        self.server: ThreadingHTTPServer = build_server("127.0.0.1", 0, state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_liveness_ok_but_not_ready(self) -> None:
        status, health = _request("GET", f"{self.base}/healthz")
        self.assertEqual((status, health["status"]), (200, "ok"))
        status, ready = _request("GET", f"{self.base}/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(ready["status"], "not_ready")
        self.assertEqual(ready["reason"], "config_conflict")
        self.assertEqual(
            ready["error"]["details"]["issues"][0]["configured"], 99
        )

    def test_business_traffic_refused_with_location(self) -> None:
        status, body = _request("GET", f"{self.base}/api/v1/events/evt-x")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "service_not_ready")
        self.assertEqual(
            body["error"]["details"]["issues"][0]["configured_location"],
            "fixtures/airports.json[0]",
        )
        status, _ = _request("GET", f"{self.base}/api/v1/flights/affected")
        self.assertEqual(status, 503)
        status, _ = _request(
            "POST", f"{self.base}/api/v1/events", base_event()
        )
        self.assertEqual(status, 503)


if __name__ == "__main__":
    unittest.main()
