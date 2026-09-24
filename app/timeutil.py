"""时间处理工具。

事件输入可以携带不同 UTC 偏移，区间比较前统一转换为带时区的 UTC 时间。
机场本地日期只用于判断是否跨午夜。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError

from app.errors import ValidationError

# Accept ISO 8601 date-time with Z, +HH:MM or explicit timezone.
# Naive timestamps are rejected: a disruption window without a zone is ambiguous.
_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def parse_event_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValidationError(
            f"Field '{field}' must be an ISO 8601 date-time string",
            {"field": field},
        )
    text = value.strip()
    if not _OFFSET_RE.search(text):
        raise ValidationError(
            f"Field '{field}' must include a timezone designator (Z or ±HH:MM)",
            {"field": field, "received": value},
        )
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValidationError(
            f"Field '{field}' is not a valid ISO 8601 date-time",
            {"field": field, "received": value},
        ) from None
    if dt.tzinfo is None:  # pragma: no cover - guarded by regex above
        raise ValidationError(
            f"Field '{field}' must include a timezone designator (Z or ±HH:MM)",
            {"field": field, "received": value},
        )
    return dt.astimezone(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(
            f"Airport timezone '{name}' is not a valid IANA timezone",
            {"timezone": name},
        ) from None


def canonical_timezone_name(name: str) -> str:
    """把 IANA 链接别名（如 ``Zulu``、``Singapore``）解析为目标时区名。

    tzdata 用符号链接表达"别名指向同一时区规则"（``Singapore`` ->
    ``Asia/Singapore``、``Zulu`` -> ``Etc/UTC``）。配置合并时两条记录可能用
    不同字符串写出语义相同的时区；这里统一按 ZoneInfo 的搜索路径
    (``TZPATH``) 逐级跟随链接，使跨环境的规范化结果一致。

    当时区数据库不通过符号链接暴露链接关系（例如仅安装了 ``tzdata`` PyPI
    包）时，原样返回名称——调用方已用 :func:`load_timezone` 确认其可加载。
    """
    for base in TZPATH:
        root = Path(base)
        candidate = root / name
        try:
            if not (candidate.exists() or candidate.is_symlink()):
                continue
            resolved = candidate.resolve(strict=True)
            root_real = root.resolve()
            target = resolved.relative_to(root_real)
        except (OSError, ValueError):
            continue
        canonical = target.as_posix()
        try:
            ZoneInfo(canonical)
        except (ZoneInfoNotFoundError, ValueError):
            return name
        return canonical
    return name


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """计算左闭右开区间的重叠，端点相接不算重叠。"""
    return start_a < end_b and start_b < end_a


def overlap_minutes(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> int:
    start = max(start_a, start_b)
    end = min(end_a, end_b)
    return max(0, int((end - start).total_seconds() // 60))


def crosses_local_midnight(
    start: datetime, end: datetime, tz: ZoneInfo
) -> bool:
    """判断左闭右开区间在指定时区内是否跨越两个自然日。"""
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    return local_start.date() != local_end.date()


def minutes_until(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)
