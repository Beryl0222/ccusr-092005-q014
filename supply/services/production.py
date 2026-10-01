"""生产批次登记与检验生命周期。

* 登记批次即执行全部硬约束校验（许可/停机/换线/缺料），违规批次进不了底账。
* 检验放行/不合格/延期是只追加事件；批次状态是事件的折叠结果。
* 批次被判不合格时，其未履行承诺自动释放回流，并留下批次级审计。
"""
from __future__ import annotations

import sqlite3

from .. import timeutil
from ..constraints import validate_batch_window
from ..db import Database
from ..errors import Conflict, NotFound, ValidationError


def _batch_to_dict(conn: sqlite3.Connection, batch_id: str) -> dict:
    row = conn.execute(
        """
        SELECT b.*, (
            SELECT COALESCE(SUM(c.quantity - c.released_qty),0)
            FROM commitments c
            WHERE c.batch_id=b.id AND c.status IN ('active','completed','released')
        ) AS locked_qty
        FROM batches b WHERE b.id=?
        """,
        (batch_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"批次不存在：{batch_id}")
    d = dict(row)
    free = d["quantity"] - d["locked_qty"]
    d["available_qty"] = free if d["qc_status"] == "released" else 0
    return d


class ProductionService:
    def __init__(self, db: Database):
        self.db = db

    # -- 批次登记 ---------------------------------------------------------

    def register_batch(self, payload: dict, *, actor: dict) -> dict:
        required = ("id", "line_id", "medicine_id", "start_ts", "produced_ts", "quantity")
        for key in required:
            if payload.get(key) in (None, ""):
                raise ValidationError(f"批次缺少字段：{key}")
        if not isinstance(payload["quantity"], int) or payload["quantity"] < 0:
            raise ValidationError("批次数量必须是非负整数")

        line = self.db.writer().execute(
            "SELECT enterprise_id FROM production_lines WHERE id=?",
            (payload["line_id"],),
        ).fetchone()
        if line is None:
            raise NotFound(f"生产线不存在：{payload['line_id']}")
        enterprise_id = line["enterprise_id"]
        if actor["role"] != "enterprise" or actor["enterprise_id"] != enterprise_id:
            from ..errors import PermissionDenied

            raise PermissionDenied("只能由所属企业登记本企业生产线批次")

        qc_due = payload.get("qc_due_ts")
        with self.db.begin_immediate() as conn:
            validate_batch_window(
                conn,
                enterprise_id=enterprise_id,
                line_id=payload["line_id"],
                medicine_id=payload["medicine_id"],
                start_ts=payload["start_ts"],
                produced_ts=payload["produced_ts"],
                quantity=payload["quantity"],
            )
            if conn.execute(
                "SELECT 1 FROM batches WHERE id=?", (payload["id"],)
            ).fetchone():
                raise Conflict(f"批次已存在：{payload['id']}")
            conn.execute(
                """
                INSERT INTO batches
                    (id, line_id, enterprise_id, medicine_id, start_ts, produced_ts,
                     qc_due_ts, quantity, qc_status)
                VALUES (?,?,?,?,?,?,?,?,'pending')
                """,
                (
                    payload["id"], payload["line_id"], enterprise_id,
                    payload["medicine_id"], payload["start_ts"], payload["produced_ts"],
                    qc_due, payload["quantity"],
                ),
            )
            return _batch_to_dict(conn, payload["id"])

    # -- 物料到货上报 -----------------------------------------------------

    def report_material_supply(self, payload: dict, *, actor: dict) -> dict:
        """企业上报物料到货：来源时间(available_ts) + 责任人(reporter)。

        记录 ID 由企业给出（业务幂等键），重复上报同一 ID 不会重复增加库存。
        """
        if actor["role"] != "enterprise":
            from ..errors import PermissionDenied

            raise PermissionDenied("物料到货只能由企业账号上报")
        for field in ("id", "material_id", "available_ts", "quantity"):
            if payload.get(field) in (None, ""):
                raise ValidationError(f"物料到货缺少字段：{field}")
        if not isinstance(payload["quantity"], int) or payload["quantity"] < 0:
            raise ValidationError("到货数量必须是非负整数")
        try:
            timeutil.parse(payload["available_ts"])
        except ValueError as exc:
            raise ValidationError(str(exc))
        if not self.db.writer().execute(
            "SELECT 1 FROM materials WHERE id=?", (payload["material_id"],)
        ).fetchone():
            raise NotFound(f"物料不存在：{payload['material_id']}")

        with self.db.begin_immediate() as conn:
            existing = conn.execute(
                "SELECT * FROM material_supply WHERE id=?", (payload["id"],)
            ).fetchone()
            if existing:
                if existing["enterprise_id"] != actor["enterprise_id"]:
                    from ..errors import PermissionDenied

                    raise PermissionDenied("到货记录 ID 已被其他企业使用")
                if (existing["material_id"] != payload["material_id"]
                        or existing["available_ts"] != payload["available_ts"]
                        or existing["quantity"] != payload["quantity"]):
                    raise Conflict("到货记录 ID 已存在但上报内容不一致")
                return dict(existing)
            conn.execute(
                """
                INSERT INTO material_supply
                  (id, enterprise_id, material_id, available_ts, quantity, reporter)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    payload["id"], actor["enterprise_id"], payload["material_id"],
                    payload["available_ts"], payload["quantity"], actor["id"],
                ),
            )
            row = conn.execute(
                "SELECT * FROM material_supply WHERE id=?", (payload["id"],)
            ).fetchone()
            return dict(row)

    # -- 检验事件 ---------------------------------------------------------

    def _get_batch_for_qc(self, conn, batch_id: str, actor: dict):
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound(f"批次不存在：{batch_id}")
        if actor["role"] == "enterprise" and actor["enterprise_id"] != row["enterprise_id"]:
            from ..errors import PermissionDenied

            raise PermissionDenied("企业只能上报本企业批次的检验结果")
        return row

    def record_qc(self, batch_id: str, payload: dict, *, actor: dict) -> dict:
        kind = payload.get("kind")
        if kind not in ("release", "reject", "postpone"):
            raise ValidationError("检验事件 kind 必须是 release/reject/postpone")
        ts = payload.get("ts") or timeutil.now()

        with self.db.begin_immediate() as conn:
            batch = self._get_batch_for_qc(conn, batch_id, actor)
            if batch["qc_status"] == "rejected":
                raise Conflict("批次已判不合格，检验结论不可再变更")
            if kind == batch["qc_status"]:
                raise Conflict(f"批次已是 {kind} 状态，不能重复上报")
            if kind == "postpone":
                if batch["qc_status"] != "pending":
                    raise Conflict("只有待检(pending)批次可以延期检验")
                new_due = payload.get("new_due_ts")
                if not new_due:
                    raise ValidationError("检验延期必须提供 new_due_ts")
                if batch["qc_due_ts"] and timeutil.parse(new_due) <= timeutil.parse(
                    batch["qc_due_ts"]
                ):
                    raise ValidationError("延期后的放行预计时间必须晚于原时间")
            else:
                new_due = None
                if kind == "release" and batch["qc_status"] == "released":
                    raise Conflict("已放行批次不能重复放行")

            conn.execute(
                """
                INSERT INTO qc_events (batch_id, ts, kind, new_due_ts, reporter_id, note)
                VALUES (?,?,?,?,?,?)
                """,
                (batch_id, ts, kind, new_due, actor["id"], payload.get("note", "")),
            )

            if kind == "release":
                conn.execute(
                    """
                    UPDATE batches SET qc_status='released', qc_decided_ts=?,
                                       qc_due_ts=?, qc_reason=NULL
                    WHERE id=?
                    """,
                    (ts, ts, batch_id),
                )
            elif kind == "reject":
                conn.execute(
                    """
                    UPDATE batches SET qc_status='rejected', qc_decided_ts=?,
                                       qc_reason=?
                    WHERE id=?
                    """,
                    (ts, payload.get("note", "检验不合格"), batch_id),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO batch_rejections (batch_id, rejected_ts, reporter_id, reason)
                    VALUES (?,?,?,?)
                    """,
                    (batch_id, ts, actor["id"], payload.get("note", "检验不合格")),
                )
                self._release_commitments_for_rejected_batch(
                    conn, batch_id, ts, actor
                )
                # 释放后同步刷新受影响申请的状态（可能退回 open/partial 重新分配）。
                affected = conn.execute(
                    "SELECT DISTINCT request_id FROM commitments WHERE batch_id=?",
                    (batch_id,),
                ).fetchall()
                from .allocation import AllocationService

                for row in affected:
                    AllocationService._refresh_request_status(conn, row["request_id"])
            else:  # postpone
                conn.execute(
                    "UPDATE batches SET qc_due_ts=? WHERE id=?", (new_due, batch_id)
                )

            return _batch_to_dict(conn, batch_id)

    @staticmethod
    def _release_commitments_for_rejected_batch(conn, batch_id, ts, actor) -> None:
        """整批不合格：所有未履行承诺份额全部释放，已履行部分不动。"""
        rows = conn.execute(
            """
            SELECT id, quantity, fulfilled_qty, released_qty
            FROM commitments
            WHERE batch_id=? AND status IN ('active','completed')
            """,
            (batch_id,),
        ).fetchall()
        for c in rows:
            remaining = c["quantity"] - c["fulfilled_qty"] - c["released_qty"]
            if remaining <= 0:
                continue
            key = f"qc-reject:{batch_id}:{c['id']}"
            if conn.execute(
                "SELECT 1 FROM commitment_events WHERE idempotency_key=?", (key,)
            ).fetchone():
                continue
            conn.execute(
                """
                INSERT INTO commitment_events
                    (commitment_id, ts, kind, quantity, reason, ref, reporter_id,
                     idempotency_key)
                VALUES (?,?, 'release', ?, 'qc_reject', ?, ?, ?)
                """,
                (c["id"], ts, remaining, f"batch:{batch_id}", actor["id"], key),
            )
            conn.execute(
                """
                UPDATE commitments
                SET released_qty = released_qty + ?,
                    status = 'rejected_batch'
                WHERE id=?
                """,
                (remaining, c["id"]),
            )

    def get_batch(self, batch_id: str, *, actor: dict) -> dict:
        conn = self.db.reader()
        try:
            row = conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"批次不存在：{batch_id}")
            if (actor["role"] == "enterprise"
                    and actor["enterprise_id"] != row["enterprise_id"]):
                from ..errors import PermissionDenied

                raise PermissionDenied("企业只能查看本企业批次")
            return _batch_to_dict(conn, batch_id)
        finally:
            if self.db.path != ":memory:":
                conn.close()
