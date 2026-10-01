"""态势视图：企业只见自身商业资料；监管人员可见跨企业全局。

视图一律由批次、承诺、只追加事件实时汇总，任何数字都能逐批溯源，
不接受应用层传入的“合计产能”。
"""
from __future__ import annotations

from ..db import Database
from ..errors import PermissionDenied
from ..timeutil import now


class ViewService:
    def __init__(self, db: Database):
        self.db = db

    # -- 企业视图 ---------------------------------------------------------

    def enterprise_dashboard(self, enterprise_id: str, *, actor: dict) -> dict:
        if actor["role"] != "enterprise" or actor["enterprise_id"] != enterprise_id:
            raise PermissionDenied("企业只能查看本企业商业资料")
        conn = self.db.reader()
        try:
            batches = [
                self._batch_row(conn, r)
                for r in conn.execute(
                    """
                    SELECT b.* FROM batches b
                    WHERE b.enterprise_id=? ORDER BY b.produced_ts
                    """,
                    (enterprise_id,),
                )
            ]
            commitments = [
                dict(r) for r in conn.execute(
                    """
                    SELECT c.id, c.request_id, c.enterprise_id, c.region_id, c.batch_id,
                           c.quantity, c.fulfilled_qty, c.released_qty, c.status,
                           c.locked_ts
                    FROM commitments c WHERE c.enterprise_id=?
                    ORDER BY c.locked_ts
                    """,
                    (enterprise_id,),
                )
            ]
            reports = [
                dict(r) for r in conn.execute(
                    """
                    SELECT rc.report_key, rc.version_no, r.submitted_ts, r.is_late,
                           r.is_correction, r.source_ts, r.line_id
                    FROM report_current_versions rc
                    JOIN daily_reports r ON r.id = rc.report_id
                    WHERE r.enterprise_id=?
                    ORDER BY r.submitted_ts DESC
                    """,
                    (enterprise_id,),
                )
            ]
            return {
                "enterprise_id": enterprise_id,
                "generated_ts": now(),
                "batches": batches,
                "commitments": commitments,
                "current_reports": reports,
            }
        finally:
            if self.db.path != ":memory:":
                conn.close()

    @staticmethod
    def _batch_row(conn, row) -> dict:
        d = dict(row)
        agg = conn.execute(
            """
            SELECT COALESCE(SUM(quantity - released_qty),0) AS locked,
                   COALESCE(SUM(CASE WHEN status='active' THEN quantity - released_qty
                                     ELSE 0 END),0) AS active_locked
            FROM commitments
            WHERE batch_id=? AND status IN ('active','completed','released')
            """,
            (row["id"],),
        ).fetchone()
        d["locked_qty"] = agg["locked"]
        # 只有已放行批次才存在“真正可承诺量”；待检/不合格一律为 0。
        free = row["quantity"] - agg["locked"]
        d["available_to_promise"] = free if row["qc_status"] == "released" else 0
        return d

    # -- 监管跨企业态势 ---------------------------------------------------

    def regulator_overview(self, *, actor: dict) -> dict:
        if actor["role"] != "regulator":
            raise PermissionDenied("跨企业态势仅监管人员可见")
        conn = self.db.reader()
        try:
            enterprises = [
                dict(r) for r in conn.execute(
                    "SELECT id, name FROM enterprises ORDER BY id"
                )
            ]
            med_rows = conn.execute(
                """
                SELECT m.id, m.name,
                       COALESCE(SUM(CASE WHEN b.qc_status='released'
                                         THEN b.quantity ELSE 0 END),0) AS released_qty,
                       COALESCE(SUM(CASE WHEN b.qc_status='pending'
                                         THEN b.quantity ELSE 0 END),0) AS pending_qty,
                       COALESCE(SUM(CASE WHEN b.qc_status='rejected'
                                         THEN b.quantity ELSE 0 END),0) AS rejected_qty
                FROM medicines m
                LEFT JOIN batches b ON b.medicine_id = m.id
                GROUP BY m.id ORDER BY m.id
                """
            ).fetchall()
            medicines = []
            for r in med_rows:
                d = dict(r)
                # 已放行但已净锁定的数量不算余量（跨企业合计）。
                locked = conn.execute(
                    """
                    SELECT COALESCE(SUM(c.quantity - c.released_qty),0) AS q
                    FROM commitments c JOIN batches b ON b.id = c.batch_id
                    WHERE b.medicine_id=?
                      AND c.status IN ('active','completed','released')
                    """,
                    (r["id"],),
                ).fetchone()["q"]
                d["net_locked_qty"] = locked
                d["free_released_qty"] = d["released_qty"] - locked
                medicines.append(d)

            requests = [
                self._request_row(conn, r)
                for r in conn.execute(
                    "SELECT * FROM supply_requests ORDER BY created_ts"
                )
            ]
            return {
                "generated_ts": now(),
                "enterprises": enterprises,
                "medicines": medicines,
                "requests": requests,
            }
        finally:
            if self.db.path != ":memory:":
                conn.close()

    @staticmethod
    def _request_row(conn, row) -> dict:
        d = dict(row)
        agg = conn.execute(
            """
            SELECT COALESCE(SUM(quantity - released_qty),0) AS net_locked,
                   COALESCE(SUM(fulfilled_qty),0) AS fulfilled
            FROM commitments WHERE request_id=?
            """,
            (row["id"],),
        ).fetchone()
        d["net_locked_qty"] = agg["net_locked"]
        d["fulfilled_qty"] = agg["fulfilled"]
        d["open_qty"] = max(0, row["quantity"] - agg["net_locked"])
        return d

    # -- 承诺溯源：任一承诺指向真实批次、约束与批准依据 -------------------

    def commitment_trace(self, commitment_id: str, *, actor: dict) -> dict:
        conn = self.db.reader()
        try:
            com = conn.execute(
                "SELECT * FROM commitments WHERE id=?", (commitment_id,)
            ).fetchone()
            if com is None:
                from ..errors import NotFound

                raise NotFound(f"承诺不存在：{commitment_id}")
            if actor["role"] == "enterprise" and actor["enterprise_id"] != com["enterprise_id"]:
                raise PermissionDenied("企业只能查看本企业承诺")
            batch = conn.execute(
                "SELECT * FROM batches WHERE id=?", (com["batch_id"],)
            ).fetchone()
            dec = conn.execute(
                "SELECT * FROM decisions WHERE id=?", (com["decision_id"],)
            ).fetchone()
            alloc = conn.execute(
                "SELECT * FROM allocations WHERE id=?", (com["allocation_id"],)
            ).fetchone()
            line = conn.execute(
                "SELECT * FROM production_lines WHERE id=?", (batch["line_id"],)
            ).fetchone()
            licensed = conn.execute(
                "SELECT 1 FROM line_licenses WHERE line_id=? AND medicine_id=?",
                (batch["line_id"], batch["medicine_id"]),
            ).fetchone() is not None
            events = [
                dict(e) for e in conn.execute(
                    """
                    SELECT id, ts, kind, quantity, reason, ref
                    FROM commitment_events WHERE commitment_id=? ORDER BY id
                    """,
                    (commitment_id,),
                )
            ]
            qc_events = [
                dict(e) for e in conn.execute(
                    "SELECT ts, kind, new_due_ts, note FROM qc_events "
                    "WHERE batch_id=? ORDER BY id",
                    (batch["id"],),
                )
            ]
            return {
                "commitment": dict(com),
                "batch": dict(batch),
                "production_line": dict(line),
                "license_in_scope": licensed,
                "decision": dict(dec),
                "allocation_rationale": alloc["rationale"],
                "allocation_arrives_ts": alloc["arrives_ts"],
                "events": events,
                "qc_events": qc_events,
                "trace_summary": (
                    f"承诺 {com['id']} 锁定批次 {batch['id']}（检验 {batch['qc_status']} "
                    f"@{batch['qc_decided_ts']}）{com['quantity']} 单位，去向区域 "
                    f"{com['region_id']}；依据监管决定 {dec['id']}（{dec['decided_ts']}）；"
                    f"许可范围内={licensed}"
                ),
            }
        finally:
            if self.db.path != ":memory:":
                conn.close()
