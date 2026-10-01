"""版本化企业日报。

核心规则：
* 理论产能 / 等待检验成品 / 缺料在制品 是监测数字，**不能**相加当作可承诺量。
* ``deliverable``（真正可承诺交付量）必须逐批溯源到已放行真实批次，
  且在同一报告内同一批次不得被两个条目重复引用。
* 迟报（接收晚于时限）打 ``is_late``；更正不覆盖旧版，而是产生新版本，
  历史版本在数据库层不可修改、不可删除。
"""
from __future__ import annotations

from ..db import Database
from ..errors import NotFound, PermissionDenied, ValidationError
from ..timeutil import now, parse

_DUE_HOUR = 23  # 默认应报时限：生产日当天 23:59:59Z（fixture 可显式给 due_ts）


def _default_due(production_date: str) -> str:
    # production_date 形如 2026-09-29
    return f"{production_date}T{_DUE_HOUR:02d}:59:59Z"


class ReportingService:
    def __init__(self, db: Database):
        self.db = db

    def submit_report(self, payload: dict, *, actor: dict) -> dict:
        if actor["role"] != "enterprise":
            raise PermissionDenied("日报只能由企业账号上报")
        enterprise_id = actor["enterprise_id"]

        line_id = payload.get("line_id")
        production_date = payload.get("production_date")
        source_ts = payload.get("source_ts")
        items = payload.get("items")
        if not line_id or not production_date or not source_ts:
            raise ValidationError("日报必须包含 line_id / production_date / source_ts")
        if not isinstance(items, list) or not items:
            raise ValidationError("日报必须包含至少一个 items 条目")

        line = self.db.writer().execute(
            "SELECT enterprise_id FROM production_lines WHERE id=?", (line_id,)
        ).fetchone()
        if line is None:
            raise NotFound(f"生产线不存在：{line_id}")
        if line["enterprise_id"] != enterprise_id:
            raise PermissionDenied("不能为其他企业的生产线上报日报")
        try:
            parse(source_ts)
        except ValueError as exc:
            raise ValidationError(str(exc))

        due_ts = payload.get("due_ts") or _default_due(production_date)
        submitted_ts = payload.get("submitted_ts") or now()
        is_late = parse(submitted_ts) > parse(due_ts)
        report_key = f"{enterprise_id}:{production_date}:{line_id}"

        with self.db.begin_immediate() as conn:
            prev = conn.execute(
                """
                SELECT id, version_no FROM daily_reports
                WHERE report_key=? ORDER BY version_no DESC LIMIT 1
                """,
                (report_key,),
            ).fetchone()
            version_no = (prev["version_no"] + 1) if prev else 1
            report_id = payload.get("id") or f"rep:{report_key}:v{version_no}"
            if conn.execute("SELECT 1 FROM daily_reports WHERE id=?", (report_id,)).fetchone():
                raise ValidationError(f"报告 ID 已存在：{report_id}")

            conn.execute(
                """
                INSERT INTO daily_reports
                  (id, report_key, enterprise_id, version_no, source_ts, submitted_ts,
                   due_ts, reporter_id, line_id, is_late, is_correction, supersedes_id, note)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    report_id, report_key, enterprise_id, version_no, source_ts,
                    submitted_ts, due_ts, actor["id"], line_id, int(is_late),
                    1 if prev else 0, prev["id"] if prev else None,
                    payload.get("note", ""),
                ),
            )

            # 同一报告内逐批累计引用量，防止条目之间重复占用同一批次。
            seen_batch_qty: dict[str, int] = {}
            total_deliverable = 0
            for idx, item in enumerate(items):
                self._validate_item_shape(item, idx)
                item_id = item.get("id") or f"{report_id}:item:{idx}"
                conn.execute(
                    """
                    INSERT INTO daily_report_items
                      (id, report_id, medicine_id, theoretical_capacity, pending_qc,
                       wip_material_short, deliverable)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        item_id, report_id, item["medicine_id"],
                        item["theoretical_capacity"], item["pending_qc"],
                        item["wip_material_short"], item["deliverable"],
                    ),
                )
                refs = item.get("batches", [])
                refs_total = self._validate_and_insert_refs(
                    conn, item_id, enterprise_id, item, refs, seen_batch_qty
                )
                if refs_total != item["deliverable"]:
                    raise ValidationError(
                        f"条目 {item_id} 的可承诺量 {item['deliverable']} 必须等于"
                        f"已放行批次溯源数量之和 {refs_total}；"
                        "理论产能/待检成品/缺料在制品不得计入可承诺量"
                    )
                total_deliverable += item["deliverable"]

            conn.execute(
                """
                INSERT INTO report_current_versions (report_key, report_id, version_no)
                VALUES (?,?,?)
                ON CONFLICT(report_key) DO UPDATE SET
                  report_id=excluded.report_id, version_no=excluded.version_no
                """,
                (report_key, report_id, version_no),
            )
            return self._load_report(conn, report_id)

    @staticmethod
    def _validate_item_shape(item: dict, idx: int) -> None:
        if not item.get("medicine_id"):
            raise ValidationError(f"items[{idx}] 缺少字段 medicine_id")
        for field in ("theoretical_capacity", "pending_qc",
                      "wip_material_short", "deliverable"):
            if field not in item:
                raise ValidationError(f"items[{idx}] 缺少字段 {field}")
            if not isinstance(item[field], int) or item[field] < 0:
                raise ValidationError(f"items[{idx}].{field} 必须是非负整数")

    @staticmethod
    def _validate_and_insert_refs(
        conn, item_id: str, enterprise_id: str, item: dict,
        refs: list, seen_batch_qty: dict[str, int],
    ) -> int:
        if not refs:
            return 0
        total = 0
        med_id = item["medicine_id"]
        for ref in refs:
            batch_id = ref.get("batch_id")
            qty = ref.get("quantity")
            if not batch_id or not isinstance(qty, int) or qty <= 0:
                raise ValidationError("批次溯源必须包含 batch_id 与正整数 quantity")
            batch = conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise ValidationError(f"可承诺量溯源批次不存在：{batch_id}")
            if batch["enterprise_id"] != enterprise_id:
                raise PermissionDenied("不得把其他企业的批次计入本企业可承诺量")
            if batch["medicine_id"] != med_id:
                raise ValidationError(
                    f"批次 {batch_id} 产品与条目药品 {med_id} 不一致"
                )
            if batch["qc_status"] != "released":
                raise ValidationError(
                    f"批次 {batch_id} 检验状态为 {batch['qc_status']}，"
                    "只有 released 批次才能计入可承诺量"
                )
            used = seen_batch_qty.get(batch_id, 0) + qty
            if used > batch["quantity"]:
                raise ValidationError(
                    f"批次 {batch_id} 在本报告内被累计引用 {used}，"
                    f"超过批次总量 {batch['quantity']}"
                )
            # 可承诺量 = 已放行 − 已净锁定；已承诺给其他地区的份额不能再报。
            locked_row = conn.execute(
                """
                SELECT COALESCE(SUM(quantity - released_qty),0) AS q
                FROM commitments
                WHERE batch_id=? AND status IN ('active','completed','released')
                """,
                (batch_id,),
            ).fetchone()
            free_qty = batch["quantity"] - int(locked_row["q"])
            if used > free_qty:
                raise ValidationError(
                    f"批次 {batch_id} 已净锁定 {int(locked_row['q'])}，"
                    f"真正可承诺余量仅 {free_qty}，本报告累计引用 {used}"
                )
            seen_batch_qty[batch_id] = used
            conn.execute(
                """
                INSERT INTO report_deliverable_batches (item_id, batch_id, enterprise_id, quantity)
                VALUES (?,?,?,?)
                """,
                (item_id, batch_id, enterprise_id, qty),
            )
            total += qty
        return total

    # -- 查询 -------------------------------------------------------------

    def _load_report(self, conn, report_id: str) -> dict:
        rep = conn.execute(
            "SELECT * FROM daily_reports WHERE id=?", (report_id,)
        ).fetchone()
        if rep is None:
            raise NotFound(f"日报不存在：{report_id}")
        items = []
        for row in conn.execute(
            "SELECT * FROM daily_report_items WHERE report_id=?", (report_id,)
        ):
            item = dict(row)
            item["batches"] = [
                dict(r) for r in conn.execute(
                    """
                    SELECT rdb.batch_id, rdb.quantity, b.qc_status, b.produced_ts,
                           b.line_id
                    FROM report_deliverable_batches rdb
                    JOIN batches b ON b.id = rdb.batch_id
                    WHERE rdb.item_id=?
                    """,
                    (row["id"],),
                )
            ]
            items.append(item)
        d = dict(rep)
        d["items"] = items
        return d

    def get_report(self, report_id: str, *, actor: dict) -> dict:
        conn = self.db.reader()
        try:
            rep = conn.execute(
                "SELECT * FROM daily_reports WHERE id=?", (report_id,)
            ).fetchone()
            if rep is None:
                raise NotFound(f"日报不存在：{report_id}")
            if (actor["role"] == "enterprise"
                    and actor["enterprise_id"] != rep["enterprise_id"]):
                raise PermissionDenied("企业只能查看本企业日报")
            return self._load_report(conn, report_id)
        finally:
            if self.db.path != ":memory:":
                conn.close()

    def list_reports(self, *, actor: dict, report_key: str | None = None) -> list[dict]:
        conn = self.db.reader()
        try:
            sql = "SELECT id FROM daily_reports WHERE 1=1"
            args: list = []
            if actor["role"] == "enterprise":
                sql += " AND enterprise_id=?"
                args.append(actor["enterprise_id"])
            if report_key:
                sql += " AND report_key=?"
                args.append(report_key)
            sql += " ORDER BY report_key, version_no"
            return [self._load_report(conn, r["id"]) for r in conn.execute(sql, args)]
        finally:
            if self.db.path != ":memory:":
                conn.close()
