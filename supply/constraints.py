"""不可突破的生产约束：许可范围、换线清洁、设备停机、原料短缺。

这些约束在批次登记（开工）时强制校验，任何调用路径都不能绕过：
服务层先给可读的领域错误，数据库触发器再对锁定环节兜底。
"""
from __future__ import annotations

import sqlite3

from .errors import ConstraintViolation
from .timeutil import add_hours, parse


def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return parse(start_a) < parse(end_b) and parse(start_b) < parse(end_a)


def assert_license(conn: sqlite3.Connection, line_id: str, medicine_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM line_licenses WHERE line_id=? AND medicine_id=?",
        (line_id, medicine_id),
    ).fetchone()
    if row is None:
        raise ConstraintViolation(
            f"生产线 {line_id} 不在 {medicine_id} 的许可生产范围内"
        )


def assert_no_downtime(
    conn: sqlite3.Connection, line_id: str, start_ts: str, end_ts: str
) -> None:
    rows = conn.execute(
        "SELECT start_ts, end_ts, reason FROM downtime_blocks WHERE line_id=?",
        (line_id,),
    ).fetchall()
    for row in rows:
        if _overlaps(start_ts, end_ts, row["start_ts"], row["end_ts"]):
            raise ConstraintViolation(
                f"生产窗口({start_ts}~{end_ts})与设备停机/清洁块冲突：{row['reason']}"
            )


def assert_changeover_ready(
    conn: sqlite3.Connection, line_id: str, medicine_id: str, start_ts: str
) -> None:
    """跨日换线：取该生产线开工时间之前最后一个批次，清洁完成前不得开工。

    按绝对时间戳而不是自然日查找，因此跨日（含跨多日）换线同样被拦截。
    """
    prev = conn.execute(
        """
        SELECT b.medicine_id, b.produced_ts, b.start_ts
        FROM batches b
        WHERE b.line_id = ? AND b.start_ts < ?
        ORDER BY b.start_ts DESC, b.produced_ts DESC
        LIMIT 1
        """,
        (line_id, start_ts),
    ).fetchone()
    if prev is None:
        return  # 冷启动：无换线要求（许可仍单独校验）

    co = conn.execute(
        """
        SELECT hours FROM line_changeovers
        WHERE line_id=? AND from_medicine=? AND to_medicine=?
        """,
        (line_id, prev["medicine_id"], medicine_id),
    ).fetchone()
    if co is None:
        # 未配置具体组合时，尝试“任意来源产品”的换线时长。
        co = conn.execute(
            """
            SELECT hours FROM line_changeovers
            WHERE line_id=? AND from_medicine IS NULL AND to_medicine=?
            """,
            (line_id, medicine_id),
        ).fetchone()
    hours = co["hours"] if co is not None else 0.0
    if hours <= 0:
        return
    ready_ts = add_hours(prev["produced_ts"], hours)
    if parse(start_ts) < parse(ready_ts):
        raise ConstraintViolation(
            f"换线清洁未完成：{line_id} 上一批次 {prev['medicine_id']} "
            f"{prev['produced_ts']} 下线，需清洁 {hours} 小时，"
            f"最早 {ready_ts} 才能开工 {medicine_id}"
        )


def assert_materials_available(
    conn: sqlite3.Connection,
    enterprise_id: str,
    medicine_id: str,
    start_ts: str,
    quantity: int,
) -> None:
    """原料短缺约束：开工时点库存须覆盖本批次消耗。

    可用量 = 开工前已到货总量；已扣减 = 开工时点不晚于本批次的其他批次需求。
    """
    reqs = conn.execute(
        "SELECT material_id, per_unit FROM material_requirements WHERE medicine_id=?",
        (medicine_id,),
    ).fetchall()
    for req in reqs:
        mat_id, per_unit = req["material_id"], req["per_unit"]
        available = conn.execute(
            """
            SELECT COALESCE(SUM(quantity),0) AS q
            FROM material_supply
            WHERE enterprise_id=? AND material_id=? AND available_ts <= ?
            """,
            (enterprise_id, mat_id, start_ts),
        ).fetchone()["q"]
        consumed = conn.execute(
            """
            SELECT COALESCE(SUM(CAST(b.quantity AS REAL) * r.per_unit),0) AS q
            FROM batches b
            JOIN material_requirements r ON r.medicine_id = b.medicine_id
            WHERE b.enterprise_id=? AND r.material_id=? AND b.start_ts <= ?
            """,
            (enterprise_id, mat_id, start_ts),
        ).fetchone()["q"]
        needed = per_unit * quantity
        if available - consumed < needed:
            short = needed - (available - consumed)
            raise ConstraintViolation(
                f"原料 {mat_id} 短缺：开工 {start_ts} 前缺口约 {short:g}，"
                f"批次不得登记/承诺"
            )


def validate_batch_window(
    conn: sqlite3.Connection,
    *,
    enterprise_id: str,
    line_id: str,
    medicine_id: str,
    start_ts: str,
    produced_ts: str,
    quantity: int,
) -> None:
    """登记批次前的全套硬约束校验。"""
    if parse(produced_ts) < parse(start_ts):
        raise ConstraintViolation("下线时间不得早于开工时间")
    line = conn.execute(
        "SELECT enterprise_id FROM production_lines WHERE id=? AND active=1",
        (line_id,),
    ).fetchone()
    if line is None:
        raise ConstraintViolation(f"生产线 {line_id} 不存在或已停用")
    if line["enterprise_id"] != enterprise_id:
        raise ConstraintViolation("不能在其他企业的生产线上登记批次")
    assert_license(conn, line_id, medicine_id)
    assert_no_downtime(conn, line_id, start_ts, produced_ts)
    assert_changeover_ready(conn, line_id, medicine_id, start_ts)
    assert_materials_available(
        conn, enterprise_id, medicine_id, start_ts, quantity
    )
