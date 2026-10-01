"""履约台账：只追加的履行/释放事件。

* 履行（shipment）：已发出/已交付数量，一旦入账不可修改、不可删除。
* 释放（transport_cancel）：运输取消时，尚未履行的份额回流，可重新分配。
* 检验不合格的释放由批次结论自动生成（reason=qc_reject），这里不允许手工补造。
* 事件级 ``idempotency_key``：崩溃重放/重复回调绝不重复入账。
"""
from __future__ import annotations

from ..db import Database
from ..errors import Conflict, NotFound, OvercommitError, PermissionDenied, ValidationError
from ..timeutil import now


class FulfillmentService:
    def __init__(self, db: Database):
        self.db = db

    def record_event(self, commitment_id: str, payload: dict, *, actor: dict) -> dict:
        kind = payload.get("kind")
        reason = payload.get("reason")
        qty = payload.get("quantity")
        idem = payload.get("idempotency_key")
        if kind not in ("fulfill", "release"):
            raise ValidationError("事件 kind 必须是 fulfill 或 release")
        if not isinstance(qty, int) or qty <= 0:
            raise ValidationError("事件数量必须为正整数")
        if not idem:
            raise ValidationError("事件必须带 idempotency_key 以防重复入账")
        allowed_reasons = (
            ("shipment",) if kind == "fulfill" else ("transport_cancel",)
        )
        if reason not in allowed_reasons:
            raise ValidationError(
                f"{kind} 事件的 reason 必须是 {allowed_reasons}；"
                "检验不合格释放由批次检验结论自动产生"
            )

        with self.db.begin_immediate() as conn:
            dup = conn.execute(
                "SELECT commitment_id, kind, quantity FROM commitment_events "
                "WHERE idempotency_key=?",
                (idem,),
            ).fetchone()
            if dup:
                if dup["commitment_id"] != commitment_id or dup["kind"] != kind \
                        or dup["quantity"] != qty:
                    raise Conflict("幂等键已存在但事件内容与原记账不一致")
                # 幂等重放：返回同一承诺当前状态，不再记账。
                return self._load_commitment(conn, commitment_id)

            com = conn.execute(
                "SELECT * FROM commitments WHERE id=?", (commitment_id,)
            ).fetchone()
            if com is None:
                raise NotFound(f"承诺不存在：{commitment_id}")
            if actor["role"] == "enterprise" and actor["enterprise_id"] != com["enterprise_id"]:
                raise PermissionDenied("企业只能登记本企业承诺的履约事件")
            if com["status"] in ("released", "rejected_batch") and kind == "fulfill":
                raise Conflict("承诺已释放/批次不合格，不能再登记履行")

            remaining = com["quantity"] - com["fulfilled_qty"] - com["released_qty"]
            if qty > remaining:
                raise OvercommitError(
                    f"承诺 {commitment_id} 未履行余量仅 {remaining}，"
                    f"不能登记 {qty}（已履行与已释放数量不可改写）"
                )

            conn.execute(
                """
                INSERT INTO commitment_events
                  (commitment_id, ts, kind, quantity, reason, ref, reporter_id,
                   idempotency_key)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    commitment_id, payload.get("ts") or now(), kind, qty, reason,
                    payload.get("ref", ""), actor["id"], idem,
                ),
            )
            if kind == "fulfill":
                conn.execute(
                    "UPDATE commitments SET fulfilled_qty = fulfilled_qty + ? WHERE id=?",
                    (qty, commitment_id),
                )
            else:
                conn.execute(
                    "UPDATE commitments SET released_qty = released_qty + ? WHERE id=?",
                    (qty, commitment_id),
                )

            row = conn.execute(
                "SELECT * FROM commitments WHERE id=?", (commitment_id,)
            ).fetchone()
            if row["fulfilled_qty"] + row["released_qty"] >= row["quantity"]:
                # 全部有了去向：全履行为 completed；含释放则按释放记账完成。
                new_status = "completed" if row["released_qty"] == 0 else "released"
                conn.execute(
                    "UPDATE commitments SET status=? WHERE id=?",
                    (new_status, commitment_id),
                )

            self._refresh_request(conn, com["request_id"])
            return self._load_commitment(conn, commitment_id)

    @staticmethod
    def _refresh_request(conn, request_id: str) -> None:
        # 与确认环节共用同一套申请状态折叠规则，避免两处口径漂移。
        from .allocation import AllocationService

        AllocationService._refresh_request_status(conn, request_id)

    @staticmethod
    def _load_commitment(conn, commitment_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM commitments WHERE id=?", (commitment_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"承诺不存在：{commitment_id}")
        d = dict(row)
        d["events"] = [
            dict(e) for e in conn.execute(
                "SELECT id, ts, kind, quantity, reason, ref FROM commitment_events "
                "WHERE commitment_id=? ORDER BY id",
                (commitment_id,),
            )
        ]
        return d

    def get_commitment(self, commitment_id: str, *, actor: dict) -> dict:
        conn = self.db.reader()
        try:
            row = conn.execute(
                "SELECT enterprise_id FROM commitments WHERE id=?", (commitment_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"承诺不存在：{commitment_id}")
            if actor["role"] == "enterprise" and actor["enterprise_id"] != row["enterprise_id"]:
                raise PermissionDenied("企业只能查看本企业承诺")
            return self._load_commitment(conn, commitment_id)
        finally:
            if self.db.path != ":memory:":
                conn.close()
