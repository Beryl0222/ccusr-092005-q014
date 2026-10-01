"""ISO8601 时间工具。库内统一存 UTC（``...Z``），比较可按字符串字典序进行。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse(ts: str) -> datetime:
    """解析 ISO8601；接受 ``Z`` 与显式偏移，返回带时区的 datetime。"""
    if not isinstance(ts, str) or not ts:
        raise ValueError(f"非法时间戳: {ts!r}")
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def add_hours(ts: str, hours: float) -> str:
    return format(parse(ts) + timedelta(hours=float(hours)))


def diff_hours(a: str, b: str) -> float:
    """a - b，单位小时。"""
    return (parse(a) - parse(b)).total_seconds() / 3600.0
