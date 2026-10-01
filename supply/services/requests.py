"""区域保供申请。

申请携带建议计算所需的三类输入：紧急程度、现有覆盖天数、运输时限
（``needed_by_ts`` 为到货截止）。申请本身不锁定任何产能。
``client_token`` 保证网络重试/崩溃重放不会产生重复申请。
"""
from __future__ import annotations

from ..db import Database
from ..errors import NotFound, PermissionDenied, ValidationError
from ..timeutil import now, parse

_URGENCY = ("critical", "urgent", "normal")


class RequestService:
    def __init__(self, db: Database):
        self.db = db

    def create_request(self, payload: dict, *, actor: dict) -> dict:
        if actor["role"] != "regulator":
            raise PermissionDenied("保供申请由监管人员受理登记")
        for field in ("region_id", "medicine_id", "quantity", "needed_by_ts",
                      "urgency", "stock_days", "client_token"):
            if field not in payload:
                raise ValidationError(f"申请缺少字段：{field}")
        if not isinstance(payload["quantity"], int) or payload["quantity"] <= 0:
            raise ValidationError("申请数量必须为正整数")
        if payload["urgency"] not in _URGENCY:
            raise ValidationError(f"紧急程度必须是 {_URGENCY} 之一")
        stock = payload["stock_days"]
        if not isinstance(stock, (int, float)) or stock < 0:
            raise ValidationError("现有覆盖天数必须是非负数")
        try:
            parse(payload["needed_by_ts"])
        except ValueError as exc:
            raise ValidationError(str(exc))
        for ref, label in (("region_id", "区域"), ("medicine_id", "药品")):
            table = "regions" if ref == "region_id" else "medicines"
            if not self.db.writer().execute(
                f"SELECT 1 FROM {table} WHERE id=?", (payload[ref],)
            ).fetchone():
                raise NotFound(f"{label}不存在：{payload[ref]}")

        rid = payload.get("id") or f"req:{payload['client_token']}"
        with self.db.begin_immediate() as conn:
            existing = conn.execute(
                "SELECT id FROM supply_requests WHERE client_token=?",
                (payload["client_token"],),
            ).fetchone()
            if existing:
                # 幂等重试：直接返回已落库的申请，不重复创建。
                return self._load(conn, existing["id"])
            conn.execute(
                """
                INSERT INTO supply_requests
                  (id, region_id, medicine_id, quantity, needed_by_ts, urgency,
                   stock_days, created_ts, status, client_token)
                VALUES (?,?,?,?,?,?,?,?,'open',?)
                """,
                (
                    rid, payload["region_id"], payload["medicine_id"],
                    payload["quantity"], payload["needed_by_ts"], payload["urgency"],
                    float(stock), now(), payload["client_token"],
                ),
            )
            return self._load(conn, rid)

    def get_request(self, request_id: str, *, actor: dict) -> dict:
        conn = self.db.reader()
        try:
            row = conn.execute(
                "SELECT * FROM supply_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"申请不存在：{request_id}")
            return self._load(conn, request_id)
        finally:
            if self.db.path != ":memory:":
                conn.close()

    def list_requests(self, *, actor: dict, status: str | None = None) -> list[dict]:
        if actor["role"] != "regulator":
            raise PermissionDenied("跨企业需求态势仅监管人员可见")
        conn = self.db.reader()
        try:
            sql = "SELECT id FROM supply_requests"
            args: list = []
            if status:
                sql += " WHERE status=?"
                args.append(status)
            sql += " ORDER BY created_ts"
            return [self._load(conn, r["id"]) for r in conn.execute(sql, args)]
        finally:
            if self.db.path != ":memory:":
                conn.close()

    @staticmethod
    def _load(conn, request_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM supply_requests WHERE id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"申请不存在：{request_id}")
        d = dict(row)
        d["allocations"] = [
            dict(a) for a in conn.execute(
                "SELECT * FROM allocations WHERE request_id=? ORDER BY score DESC, id",
                (request_id,),
            )
        ]
        return d
