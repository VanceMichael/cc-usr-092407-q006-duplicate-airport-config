"""各层共享的稳定结构化错误类型。

所有 HTTP 错误均使用相同的响应结构：

    {"error": {"code": "...", "message": "...", "details": {...}}}

错误代码属于 API 契约，修改时必须保持兼容。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """可转换为结构化 HTTP 响应的错误基类。"""

    code = "internal_error"
    status = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}


class ConfigError(AppError):
    """配置/夹具本身非法：进程不得进入就绪状态。

    每条问题都携带来源位置（文件路径与数组下标），details["issues"]
    按 (location, field, issue) 确定性排序，与文件行序无关。
    """

    code = "config_invalid"
    status = 500  # not an HTTP-input error; mapped at the startup gate


class ConfigConflictError(AppError):
    """已登记的机场事实与当前配置冲突。

    新实例必须保持旧数据可读但拒绝接管流量；绝不覆盖或部分重写数据库。
    """

    code = "config_conflict"
    status = 503


class ValidationError(AppError):
    code = "validation_error"
    status = 422


class UnknownAirportError(ValidationError):
    code = "unknown_airport"


class EventConflictError(AppError):
    code = "event_conflict"
    status = 409


class NotFoundError(AppError):
    code = "not_found"
    status = 404


class BadRequestError(AppError):
    code = "bad_request"
    status = 400


class UnsupportedMediaTypeError(BadRequestError):
    code = "unsupported_media_type"


class MethodNotAllowedError(AppError):
    code = "method_not_allowed"
    status = 405
