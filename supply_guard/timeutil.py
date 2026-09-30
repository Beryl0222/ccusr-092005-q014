"""时间工具：统一 UTC ISO8601（秒）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat()


def parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def add_hours(ts: str, hours: float) -> str:
    return iso(parse(ts) + timedelta(hours=hours))


def hours_between(a: str, b: str) -> float:
    return (parse(b) - parse(a)).total_seconds() / 3600.0
