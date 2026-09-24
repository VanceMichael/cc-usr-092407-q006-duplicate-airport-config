"""基于标准库 ThreadingHTTPServer 的 JSON HTTP 接口层。"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.errors import (
    AppError,
    BadRequestError,
    ConfigConflictError,
    MethodNotAllowedError,
    NotFoundError,
    UnsupportedMediaTypeError,
)
from app.repository import GateStatus
from app.service import DisruptionService

MAX_BODY_BYTES = 64 * 1024
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class AppState:
    """进程级运行状态。

    ``service`` 为 None 表示启动门禁未通过（配置与已登记机场事实冲突）。
    此时进程仍然存活、数据库仍可打开（健康检查为 ok），但既不报告就绪也
    不处理任何业务请求——新实例不得接管流量，旧实例在同一卷上继续可读。
    """

    def __init__(
        self,
        service: DisruptionService | None = None,
        *,
        gate_status: GateStatus | None = None,
        gate_error: ConfigConflictError | None = None,
    ):
        self.service = service
        self.gate_status = gate_status
        self.gate_error = gate_error

    @property
    def ready(self) -> bool:
        return self.service is not None and self.gate_error is None

    def storage_ok(self) -> bool:
        if self.service is None:
            # Gate failure leaves the database intact and openable; liveness
            # only fails when the process itself is gone.
            return True
        return self.service.healthy()


def make_handler(state: AppState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DisruptionService/1.0"
        protocol_version = "HTTP/1.1"

        # Quieter access log; comment out to restore defaults.
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

        # ------------------------------------------------------------------ #
        # Routing
        # ------------------------------------------------------------------ #

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch("PATCH")

        def _dispatch(self, method: str) -> None:
            try:
                parts = urlsplit(self.path)
                path = parts.path.rstrip("/") or "/"
                query = parse_qs(parts.query)

                # Liveness: the process is up. Independent of the startup gate
                # so an orchestrator does not kill an instance that is
                # deliberately holding an old, readable database.
                if path == "/healthz":
                    self._require_method(method, "GET", path)
                    if not state.storage_ok():
                        self._send_json(
                            503,
                            {"status": "degraded", "detail": "storage unavailable"},
                        )
                        return
                    self._send_json(200, {"status": "ok"})
                    return

                # Readiness: the configuration gate passed and this instance is
                # allowed to serve traffic. Distinct from /healthz by contract.
                if path == "/readyz":
                    self._require_method(method, "GET", path)
                    if state.gate_error is not None:
                        self._send_json(
                            503,
                            {
                                "status": "not_ready",
                                "reason": "config_conflict",
                                "error": state.gate_error.to_dict()["error"],
                            },
                        )
                        return
                    if state.service is None or not state.storage_ok():
                        self._send_json(
                            503,
                            {"status": "not_ready", "reason": "starting"},
                        )
                        return
                    self._send_json(
                        200,
                        {
                            "status": "ready",
                            "gate": state.gate_status.to_dict()
                            if state.gate_status
                            else None,
                        },
                    )
                    return

                # Business traffic is refused until the gate has passed.
                if path.startswith("/api/") and not state.ready:
                    self._send_json(
                        503,
                        {
                            "error": {
                                "code": "service_not_ready",
                                "message": (
                                    "Service is not ready: airport configuration "
                                    "conflicts with the registered database facts"
                                ),
                                "details": (
                                    state.gate_error.details
                                    if state.gate_error is not None
                                    else {}
                                ),
                            }
                        },
                    )
                    return

                if path == "/api/v1" or path == "/":
                    self._require_method(method, "GET", path)
                    self._send_json(
                        200,
                        {
                            "service": "airport-disruption",
                            "endpoints": [
                                "POST /api/v1/events",
                                "GET  /api/v1/events/{event_id}",
                                "GET  /api/v1/airports/{airport_code}/summary",
                                "GET  /api/v1/flights/affected",
                                "GET  /healthz",
                                "GET  /readyz",
                            ],
                        },
                    )
                    return

                match = re.fullmatch(r"/api/v1/events/([A-Za-z0-9-]+)", path)
                if match:
                    self._require_method(method, "GET", path)
                    self._send_json(200, state.service.event_status(match.group(1)))
                    return

                match = re.fullmatch(
                    r"/api/v1/airports/([A-Z]{3})/summary", path
                )
                if match:
                    self._require_method(method, "GET", path)
                    self._send_json(200, state.service.airport_summary(match.group(1)))
                    return

                if path == "/api/v1/flights/affected":
                    self._require_method(method, "GET", path)
                    self._send_json(200, self._affected_flights(query))
                    return

                if path == "/api/v1/events":
                    self._require_method(method, "POST", path)
                    payload = self._read_json_body()
                    self._send_json(201, state.service.submit_event(payload))
                    return

                raise NotFoundError(f"No route for {method} {path}")

            except AppError as exc:
                self._send_json(exc.status, exc.to_dict())
            except Exception as exc:  # never leak a stack trace to clients
                import traceback

                traceback.print_exc()
                self._send_json(
                    500,
                    {"error": {"code": "internal_error", "message": "Internal server error"}},
                )

        def _require_method(self, method: str, expected: str, path: str) -> None:
            if method != expected:
                raise MethodNotAllowedError(
                    f"{method} is not allowed for {path}; use {expected}",
                    {"allowed": expected},
                )

        # ------------------------------------------------------------------ #
        # Request/response helpers
        # ------------------------------------------------------------------ #

        def _read_json_body(self) -> Any:
            ctype = self.headers.get("Content-Type", "")
            if not ctype.split(";")[0].strip().lower() == "application/json":
                raise UnsupportedMediaTypeError(
                    "Content-Type must be application/json",
                    {"received_content_type": ctype or None},
                )
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.close_connection = True
                raise BadRequestError("Invalid Content-Length header") from None
            if length <= 0:
                raise BadRequestError("Request body is empty")
            if length > MAX_BODY_BYTES:
                # Do not drain an oversized body; drop the connection so
                # unread bytes cannot corrupt the next keep-alive request.
                self.close_connection = True
                raise BadRequestError(
                    f"Request body exceeds {MAX_BODY_BYTES} bytes",
                    {"max_bytes": MAX_BODY_BYTES},
                )
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BadRequestError(
                    "Request body is not valid JSON", {"detail": str(exc)}
                ) from None
            return payload

        def _affected_flights(self, query: dict[str, list[str]]) -> dict[str, Any]:
            def one(name: str) -> str | None:
                values = query.get(name)
                if values is None:
                    return None
                if len(values) > 1:
                    raise BadRequestError(
                        f"Query parameter '{name}' must be provided once"
                    )
                return values[0]

            limit = self._parse_int(one("limit"), DEFAULT_LIMIT, "limit", 1, MAX_LIMIT)
            offset = self._parse_int(one("offset"), 0, "offset", 0, 100_000)
            return state.service.affected_flights(
                airport=one("airport"),
                status=one("status"),
                limit=limit,
                offset=offset,
            )

        @staticmethod
        def _parse_int(
            raw: str | None, default: int, name: str, minimum: int, maximum: int
        ) -> int:
            if raw is None:
                return default
            try:
                value = int(raw)
            except ValueError:
                raise BadRequestError(
                    f"Query parameter '{name}' must be an integer",
                    {"received": raw},
                ) from None
            if not minimum <= value <= maximum:
                raise BadRequestError(
                    f"Query parameter '{name}' must be between {minimum} and {maximum}",
                    {"received": value},
                )
            return value

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False, sort_keys=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def build_server(
    host: str, port: int, service_or_state: DisruptionService | AppState
) -> ThreadingHTTPServer:
    state = (
        service_or_state
        if isinstance(service_or_state, AppState)
        else AppState(service_or_state)
    )
    server = ThreadingHTTPServer((host, port), make_handler(state))
    return server
