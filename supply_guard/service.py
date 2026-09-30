"""领域服务：上报版本、ATP、建议计算、确认锁定、履行与释放。

关键不变量：
1. 理论产能 / 待检成品 / 缺料在制品 / 已放行库存是四类来源，绝不直接相加；
   只有已放行库存，或已落到「具体批次 + 时间槽」并通过全部约束的生产计划，
   才进入可承诺量（ATP）。
2. 建议只计算不锁定；锁定发生在监管确认时刻，并在同一写事务内重新校验
   全部约束（许可、换线清洁、停机、原料、检验状态、剩余可用量、运输时限）。
3. 承诺与事件只追加；履行数量不可改写；释放只作用于未履行部分，
   被释放的真实数量回到对应批次可重新分配。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from . import timeutil
from .errors import (
    ConflictError,
    IdempotencyReplayed,
    NotFoundError,
    PermissionError,
    ValidationError,
)
from .store import Store, dumps, log_event

URGENCY_WEIGHT = {"URGENT": 100.0, "NORMAL": 40.0}
LATE_AFTER_HOURS = 24.0
EPS = 1e-6
_TERMINAL_BATCH = ("REJECTED", "CANCELLED")
_ACTIVE_COMMITMENT = ("ACTIVE", "PARTIALLY_FULFILLED")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _row(r) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


def _hours(x: float) -> timedelta:
    return timedelta(hours=x)


def _request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()


@dataclass
class Actor:
    username: str
    role: str
    enterprise_id: str | None = None
    region_id: str | None = None

    def require_regulator(self) -> None:
        if self.role != "REGULATOR":
            raise PermissionError("仅监管人员可以执行该操作")

    def require_enterprise(self) -> None:
        if self.role != "ENTERPRISE":
            raise PermissionError("仅企业账号可以执行该操作")

    def require_region(self) -> None:
        if self.role != "REGION":
            raise PermissionError("仅区域账号可以执行该操作")

    def check_enterprise(self, enterprise_id: str) -> None:
        if self.role == "ENTERPRISE" and self.enterprise_id != enterprise_id:
            raise PermissionError("企业只能操作本企业的商业资料")


class Service:
    def __init__(self, store: Store):
        self.store = store

    # ================================================================== #
    # 身份与幂等
    # ================================================================== #
    def authenticate(self, token: str) -> Actor:
        if not token:
            raise PermissionError("缺少访问令牌")
        with self.store.read() as conn:
            r = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
        if r is None:
            raise PermissionError("令牌无效")
        return Actor(r["username"], r["role"], r["enterprise_id"], r["region_id"])

    def _idem_begin(self, conn, idem_key: str | None, payload: dict[str, Any]):
        if not idem_key:
            return None
        row = conn.execute("SELECT * FROM idempotency WHERE idem_key=?", (idem_key,)).fetchone()
        if row is not None:
            if row["request_hash"] != _request_hash(payload):
                raise IdempotencyReplayed("幂等键已用于不同请求")
            return row
        return None

    def _idem_finish(self, conn, idem_key: str | None, actor: Actor,
                     payload: dict[str, Any], ref_kind: str, ref_id: str) -> None:
        if not idem_key:
            return
        conn.execute(
            "INSERT INTO idempotency(idem_key, actor, request_hash, ref_kind, ref_id, created_ts)"
            " VALUES(?,?,?,?,?,?)",
            (idem_key, actor.username, _request_hash(payload), ref_kind, str(ref_id),
             timeutil.now_iso()),
        )

    # ================================================================== #
    # 企业日报（版本化、迟报、更正）
    # ================================================================== #
    def submit_report(self, actor: Actor, payload: dict[str, Any], *,
                      idem_key: str | None = None,
                      received_ts: str | None = None) -> dict[str, Any]:
        """企业上报日报或更正版本。

        payload:
          report_date, source_ts, kind(DAILY|CORRECTION), reporter, note?,
          lines: [{line_id, theoretical_capacity, awaiting_qc_qty, wip_qty}],
          materials: [{material_id, on_hand_qty}],
          batches: [{id, line_id, medicine_id, status, planned_qty,
                     planned_start, planned_finish, expected_release_ts,
                     qty_released, material_short}]
        """
        actor.require_enterprise()
        self._validate_report_payload(payload)
        received = received_ts or timeutil.now_iso()
        late = timeutil.hours_between(payload["source_ts"], received) > LATE_AFTER_HOURS

        with self.store.transaction() as conn:
            existing = self._idem_begin(conn, idem_key, payload)
            if existing is not None:
                return self._get_report(conn, int(existing["ref_id"]), replayed=True)

            if conn.execute("SELECT 1 FROM enterprises WHERE id=?",
                            (actor.enterprise_id,)).fetchone() is None:
                raise ValidationError("企业不存在")

            supersedes = None
            if payload["kind"] == "CORRECTION":
                prev = conn.execute(
                    "SELECT id FROM report_versions WHERE enterprise_id=? AND report_date=?"
                    " ORDER BY id DESC LIMIT 1",
                    (actor.enterprise_id, payload["report_date"]),
                ).fetchone()
                if prev is None:
                    raise ValidationError("更正必须针对已存在的日报")
                supersedes = prev["id"]

            cur = conn.execute(
                "INSERT INTO report_versions(enterprise_id, report_date, source_ts, submitted_ts,"
                " reporter, kind, supersedes_id, late, note)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (actor.enterprise_id, payload["report_date"], payload["source_ts"], received,
                 payload["reporter"], payload["kind"], supersedes, 1 if late else 0,
                 payload.get("note")),
            )
            version_id = cur.lastrowid

            for line in payload["lines"]:
                if conn.execute(
                    "SELECT 1 FROM production_lines WHERE id=? AND enterprise_id=?",
                    (line["line_id"], actor.enterprise_id),
                ).fetchone() is None:
                    if conn.execute("SELECT 1 FROM production_lines WHERE id=?",
                                    (line["line_id"],)).fetchone() is not None:
                        raise PermissionError(
                            f"产线 {line['line_id']} 属于其他企业，禁止上报其商业资料")
                    raise ValidationError(f"产线 {line['line_id']} 不存在")
                for key in ("theoretical_capacity", "awaiting_qc_qty", "wip_qty"):
                    if line[key] < -EPS:
                        raise ValidationError(f"产线 {line['line_id']} 的 {key} 不能为负")
                conn.execute(
                    "INSERT INTO report_line_summaries(version_id, line_id, theoretical_capacity,"
                    " awaiting_qc_qty, wip_qty) VALUES(?,?,?,?,?)",
                    (version_id, line["line_id"], line["theoretical_capacity"],
                     line["awaiting_qc_qty"], line["wip_qty"]),
                )

            for mat in payload["materials"]:
                if conn.execute("SELECT 1 FROM materials WHERE id=?",
                                (mat["material_id"],)).fetchone() is None:
                    raise ValidationError(f"物料 {mat['material_id']} 不存在")
                if mat["on_hand_qty"] < -EPS:
                    raise ValidationError("物料库存不能为负")
                conn.execute(
                    "INSERT INTO report_material_stock(version_id, enterprise_id, material_id,"
                    " on_hand_qty) VALUES(?,?,?,?)",
                    (version_id, actor.enterprise_id, mat["material_id"], mat["on_hand_qty"]),
                )
                conn.execute(
                    "INSERT INTO material_stock_current(enterprise_id, material_id, on_hand_qty,"
                    " version_id) VALUES(?,?,?,?) ON CONFLICT(enterprise_id, material_id)"
                    " DO UPDATE SET on_hand_qty=excluded.on_hand_qty,"
                    " version_id=excluded.version_id",
                    (actor.enterprise_id, mat["material_id"], mat["on_hand_qty"], version_id),
                )

            for b in payload["batches"]:
                self._upsert_reported_batch(conn, b, version_id, payload)

            log_event(conn, ts=received, actor=actor.username, action="REPORT_SUBMITTED",
                      entity_type="report_version", entity_id=version_id,
                      payload={"kind": payload["kind"], "late": late,
                               "report_date": payload["report_date"]})
            self._idem_finish(conn, idem_key, actor, payload, "report_version", version_id)
            return self._get_report(conn, version_id)

    def _validate_report_payload(self, p: dict[str, Any]) -> None:
        for key in ("report_date", "source_ts", "kind", "reporter"):
            if not p.get(key):
                raise ValidationError(f"缺少字段 {key}")
        if p["kind"] not in ("DAILY", "CORRECTION"):
            raise ValidationError("kind 必须为 DAILY 或 CORRECTION")
        timeutil.parse(p["source_ts"])
        if not isinstance(p.get("lines"), list) or not p["lines"]:
            raise ValidationError("lines 至少包含一条产线")
        for line in p["lines"]:
            for key in ("line_id", "theoretical_capacity", "awaiting_qc_qty", "wip_qty"):
                if key not in line:
                    raise ValidationError(f"产线汇总缺少字段 {key}")
        if not isinstance(p.get("materials"), list):
            raise ValidationError("materials 必须为数组")
        for mat in p["materials"]:
            for key in ("material_id", "on_hand_qty"):
                if key not in mat:
                    raise ValidationError(f"物料上报缺少字段 {key}")
        if not isinstance(p.get("batches"), list):
            raise ValidationError("batches 必须为数组")
        allowed = {"PLANNED", "IN_PROGRESS", "AWAIT_QC", "RELEASED", "REJECTED"}
        for b in p["batches"]:
            for key in ("id", "line_id", "medicine_id", "status", "planned_qty"):
                if key not in b:
                    raise ValidationError(f"批次缺少字段 {key}")
            if b["status"] not in allowed:
                raise ValidationError(f"批次 {b['id']} 状态非法")

    def _upsert_reported_batch(self, conn, b: dict, version_id: int, payload: dict) -> None:
        ent_id = conn.execute(
            "SELECT enterprise_id FROM report_versions WHERE id=?", (version_id,)
        ).fetchone()["enterprise_id"]
        line = conn.execute("SELECT enterprise_id FROM production_lines WHERE id=?",
                            (b["line_id"],)).fetchone()
        if line is None or line["enterprise_id"] != ent_id:
            raise ValidationError(f"批次 {b['id']} 的产线不属于本企业")
        if conn.execute("SELECT 1 FROM medicines WHERE id=?",
                        (b["medicine_id"],)).fetchone() is None:
            raise ValidationError(f"批次 {b['id']} 的药品不存在")

        existing = conn.execute("SELECT * FROM batches WHERE id=?", (b["id"],)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO batches(id, enterprise_id, line_id, medicine_id, source, status,"
                " planned_qty, planned_start, planned_finish, expected_release_ts,"
                " qty_released, material_short, created_version_id, updated_version_id)"
                " VALUES(?,?,?,?,'REPORT',?,?,?,?,?,?,?,?,?)",
                (b["id"], ent_id, b["line_id"], b["medicine_id"], b["status"],
                 b["planned_qty"], b.get("planned_start"), b.get("planned_finish"),
                 b.get("expected_release_ts"), b.get("qty_released", 0.0),
                 1 if b.get("material_short") else 0, version_id, version_id),
            )
            self._add_batch_event(conn, b["id"], "CREATED", None, b["status"],
                                  b["planned_qty"], payload["source_ts"],
                                  payload["reporter"], version_id)
            return

        if existing["enterprise_id"] != ent_id:
            raise ValidationError(f"批号 {b['id']} 已被其他企业占用")
        if existing["source"] == "SYSTEM_PLAN":
            raise ValidationError(f"批次 {b['id']} 为系统排产批次，不能由日报覆盖")
        old_status = existing["status"]
        conn.execute(
            "UPDATE batches SET status=?, planned_qty=?, planned_start=?, planned_finish=?,"
            " expected_release_ts=?, qty_released=?, material_short=?, updated_version_id=?"
            " WHERE id=?",
            (b["status"], b["planned_qty"], b.get("planned_start"), b.get("planned_finish"),
             b.get("expected_release_ts"), b.get("qty_released", existing["qty_released"]),
             1 if b.get("material_short") else 0, version_id, b["id"]),
        )
        etype = "STATUS"
        if old_status != "RELEASED" and b["status"] == "RELEASED":
            etype = "QC_RELEASED"
        elif old_status != "REJECTED" and b["status"] == "REJECTED":
            etype = "QC_REJECTED"
        self._add_batch_event(conn, b["id"], etype, old_status, b["status"],
                              b.get("qty_released"), payload["source_ts"],
                              payload["reporter"], version_id)

    def _add_batch_event(self, conn, batch_id, event_type, from_status, to_status,
                         qty, event_ts, actor, version_id, payload=None) -> None:
        conn.execute(
            "INSERT INTO batch_events(batch_id, event_type, from_status, to_status, qty,"
            " event_ts, recorded_ts, actor, version_id, payload)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (batch_id, event_type, from_status, to_status, qty, event_ts,
             timeutil.now_iso(), actor, version_id,
             dumps(payload) if payload is not None else None),
        )

    def _get_report(self, conn, version_id: int, *, replayed: bool = False) -> dict[str, Any]:
        v = conn.execute("SELECT * FROM report_versions WHERE id=?", (version_id,)).fetchone()
        if v is None:
            raise NotFoundError("上报版本不存在")
        lines = [_row(r) for r in conn.execute(
            "SELECT * FROM report_line_summaries WHERE version_id=? ORDER BY line_id",
            (version_id,))]
        materials = [_row(r) for r in conn.execute(
            "SELECT * FROM report_material_stock WHERE version_id=? ORDER BY material_id",
            (version_id,))]
        batches = [_row(r) for r in conn.execute(
            "SELECT * FROM batches WHERE created_version_id=? OR updated_version_id=? ORDER BY id",
            (version_id, version_id))]
        out = _row(v)
        out.update(lines=lines, materials=materials, batches=batches, replayed=replayed)
        return out

    def get_report_versions(self, actor: Actor, report_date: str) -> list[dict[str, Any]]:
        actor.require_enterprise()
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT id, report_date, source_ts, submitted_ts, reporter, kind,"
                " supersedes_id, late, note FROM report_versions"
                " WHERE enterprise_id=? AND report_date=? ORDER BY id",
                (actor.enterprise_id, report_date),
            ).fetchall()
            return [_row(r) for r in rows]

    # ================================================================== #
    # 检验放行 / 不合格 / 延期
    # ================================================================== #
    def record_qc_event(self, actor: Actor, payload: dict[str, Any], *,
                        idem_key: str | None = None) -> dict[str, Any]:
        """payload: batch_id, event(RELEASE|REJECT|DELAY), event_ts, reporter,
                   qty_released?(RELEASE), new_expected_release_ts?(DELAY), reason?"""
        actor.require_enterprise()
        event = payload.get("event")
        if event not in ("RELEASE", "REJECT", "DELAY"):
            raise ValidationError("event 必须为 RELEASE/REJECT/DELAY")
        for key in ("batch_id", "event_ts", "reporter"):
            if not payload.get(key):
                raise ValidationError(f"{key} 必填")

        with self.store.transaction() as conn:
            existing = self._idem_begin(conn, idem_key, payload)
            if existing is not None:
                return {"replayed": True, "ref_id": existing["ref_id"]}

            b = conn.execute("SELECT * FROM batches WHERE id=?",
                             (payload["batch_id"],)).fetchone()
            if b is None:
                raise NotFoundError("批次不存在")
            if b["enterprise_id"] != actor.enterprise_id:
                raise PermissionError("只能上报本企业批次")
            if b["status"] in _TERMINAL_BATCH:
                raise ConflictError(f"批次已处于 {b['status']}，检验事件不可再改")
            old_status = b["status"]
            extra = {"reason": payload.get("reason")}

            if event == "RELEASE":
                qty = float(payload.get("qty_released", b["planned_qty"]))
                if qty <= 0 or qty > b["planned_qty"] + EPS:
                    raise ValidationError("放行数量超出批次计划数量")
                new_ets = payload.get("new_expected_release_ts")
                conn.execute(
                    "UPDATE batches SET status='RELEASED', qty_released=?,"
                    " expected_release_ts=COALESCE(?, expected_release_ts), material_short=0"
                    " WHERE id=?",
                    (qty, new_ets, b["id"]),
                )
                self._add_batch_event(conn, b["id"], "QC_RELEASED", old_status, "RELEASED",
                                      qty, payload["event_ts"], payload["reporter"], None, extra)
            elif event == "REJECT":
                conn.execute("UPDATE batches SET status='REJECTED' WHERE id=?", (b["id"],))
                self._add_batch_event(conn, b["id"], "QC_REJECTED", old_status, "REJECTED",
                                      None, payload["event_ts"], payload["reporter"], None, extra)
            else:
                new_ets = payload.get("new_expected_release_ts")
                if not new_ets:
                    raise ValidationError("检验延期必须提供 new_expected_release_ts")
                base = b["expected_release_ts"] or payload["event_ts"]
                if timeutil.parse(new_ets) <= timeutil.parse(base):
                    raise ValidationError("新的预计放行时间必须晚于原预计时间")
                conn.execute("UPDATE batches SET expected_release_ts=? WHERE id=?",
                             (new_ets, b["id"]))
                self._add_batch_event(conn, b["id"], "QC_DELAY", old_status, old_status, None,
                                      payload["event_ts"], payload["reporter"], None,
                                      {**extra, "new_expected_release_ts": new_ets})

            released = self._release_impacted(conn, b, event, payload, actor)
            log_event(conn, ts=timeutil.now_iso(), actor=actor.username,
                      action=f"QC_{event}", entity_type="batch", entity_id=b["id"],
                      payload={"released_commitments": released, **extra})
            self._idem_finish(conn, idem_key, actor, payload, "batch", b["id"])
            return {"batch_id": b["id"], "event": event,
                    "released_commitments": released}

    # ================================================================== #
    # 区域需求
    # ================================================================== #
    def create_demand(self, actor: Actor, payload: dict[str, Any], *,
                      idem_key: str | None = None) -> dict[str, Any]:
        actor.require_region()
        for key in ("id", "medicine_id", "qty", "urgency", "coverage_days", "needed_by_ts"):
            if key not in payload:
                raise ValidationError(f"需求缺少字段 {key}")
        if payload["urgency"] not in URGENCY_WEIGHT:
            raise ValidationError("urgency 必须为 URGENT/NORMAL")
        if payload["qty"] <= 0:
            raise ValidationError("需求数量必须为正")
        timeutil.parse(payload["needed_by_ts"])

        with self.store.transaction() as conn:
            existing = self._idem_begin(conn, idem_key, payload)
            if existing is not None:
                d = conn.execute("SELECT * FROM demands WHERE id=?",
                                 (existing["ref_id"],)).fetchone()
                return {**_row(d), "replayed": True}
            if conn.execute("SELECT 1 FROM medicines WHERE id=?",
                            (payload["medicine_id"],)).fetchone() is None:
                raise ValidationError("药品不存在")
            if conn.execute("SELECT 1 FROM demands WHERE id=?",
                            (payload["id"],)).fetchone() is not None:
                raise ConflictError("需求编号已存在")
            conn.execute(
                "INSERT INTO demands(id, region_id, medicine_id, qty, urgency, coverage_days,"
                " needed_by_ts, created_ts, created_by) VALUES(?,?,?,?,?,?,?,?,?)",
                (payload["id"], actor.region_id, payload["medicine_id"], payload["qty"],
                 payload["urgency"], payload["coverage_days"], payload["needed_by_ts"],
                 timeutil.now_iso(), actor.username),
            )
            log_event(conn, ts=timeutil.now_iso(), actor=actor.username,
                      action="DEMAND_CREATED", entity_type="demand", entity_id=payload["id"])
            self._idem_finish(conn, idem_key, actor, payload, "demand", payload["id"])
            return _row(conn.execute("SELECT * FROM demands WHERE id=?",
                                     (payload["id"],)).fetchone())

    def cancel_demand(self, actor: Actor, demand_id: str, *,
                     idem_key: str | None = None) -> dict:
        """运输取消：尚未履行的承诺全部释放并可重新分配；已履行不动。"""
        actor.require_region()
        idem_payload = {"demand_id": demand_id}
        with self.store.transaction() as conn:
            existing = self._idem_begin(conn, idem_key, idem_payload)
            if existing is not None:
                return {"replayed": True, "ref_id": existing["ref_id"]}
            d = conn.execute("SELECT * FROM demands WHERE id=?", (demand_id,)).fetchone()
            if d is None:
                raise NotFoundError("需求不存在")
            if d["region_id"] != actor.region_id:
                raise PermissionError("只能取消本区域需求")
            if d["status"] == "CANCELLED":
                raise ConflictError("需求已取消")
            if d["status"] == "FULFILLED":
                raise ConflictError("需求已全部履行，不能取消（已履行不可改写）")
            conn.execute("UPDATE demands SET status='CANCELLED' WHERE id=?", (demand_id,))
            released = []
            for c in conn.execute(
                "SELECT * FROM commitments WHERE demand_id=? AND status IN (?,?)",
                (demand_id, *_ACTIVE_COMMITMENT),
            ).fetchall():
                released.append(self._release_commitment(
                    conn, c, qty=None, reason="TRANSPORT_CANCELLED", actor=actor)["id"])
            log_event(conn, ts=timeutil.now_iso(), actor=actor.username,
                      action="DEMAND_CANCELLED", entity_type="demand", entity_id=demand_id,
                      payload={"released": released})
            self._idem_finish(conn, idem_key, actor, idem_payload, "demand", demand_id)
            return {"demand_id": demand_id, "released": released}

    # ================================================================== #
    # ATP 视图（四类来源分列）
    # ================================================================== #
    def _batch_locked_qty(self, conn, batch_id: str) -> float:
        return conn.execute(
            "SELECT COALESCE(SUM(qty),0) s FROM batch_locks WHERE batch_id=?",
            (batch_id,)).fetchone()["s"]

    def _batch_available(self, conn, b) -> float:
        """仅已放行批次有成品 ATP：放行量 − 仍锁定量（锁定含已交付消耗）。"""
        if b["status"] != "RELEASED":
            return 0.0
        return max(0.0, b["qty_released"] - self._batch_locked_qty(conn, b["id"]))

    def _lane(self, conn, enterprise_id: str, region_id: str | None):
        if region_id is None:
            return None
        return conn.execute(
            "SELECT transport_hours FROM lanes WHERE enterprise_id=? AND region_id=?",
            (enterprise_id, region_id)).fetchone()

    def _deliver_ts(self, conn, enterprise_id, ready_ts, region_id):
        lane = self._lane(conn, enterprise_id, region_id)
        if lane is None:
            return ready_ts if region_id is None else None
        return timeutil.add_hours(ready_ts, lane["transport_hours"])

    def atp_overview(self, actor: Actor, medicine_id: str, *,
                     region_id: str | None = None, at_ts: str | None = None) -> dict:
        at = at_ts or timeutil.now_iso()
        with self.store.read() as conn:
            if actor.role == "ENTERPRISE":
                ent_ids = [actor.enterprise_id]
            elif actor.role == "REGULATOR":
                ent_ids = [r["id"] for r in conn.execute("SELECT id FROM enterprises")]
            elif actor.role == "REGION":
                # 区域只能按「本区域运输通道」看可达的交付量，不能无条件拉跨企业商业底账
                if not region_id:
                    raise PermissionError("区域查询必须指定本区域 region_id 过滤")
                if region_id != actor.region_id:
                    raise PermissionError("只能按本区域运输通道过滤")
                ent_ids = [r["id"] for r in conn.execute("SELECT id FROM enterprises")]
            else:
                raise PermissionError("身份无效")

            stock, qc, wip, cap = [], [], [], []
            for ent_id in ent_ids:
                for b in conn.execute(
                    "SELECT * FROM batches WHERE enterprise_id=? AND medicine_id=? AND status='RELEASED'"
                    " ORDER BY expected_release_ts, id", (ent_id, medicine_id)):
                    deliver = self._deliver_ts(conn, ent_id,
                                               b["expected_release_ts"] or at, region_id)
                    if region_id and deliver is None:
                        continue
                    stock.append({
                        "enterprise_id": ent_id, "batch_id": b["id"], "line_id": b["line_id"],
                        "qty_available": round(self._batch_available(conn, b), 6),
                        "qty_released": b["qty_released"],
                        "qty_locked": round(self._batch_locked_qty(conn, b["id"]), 6),
                        "deliverable_ts": deliver,
                    })
                for b in conn.execute(
                    "SELECT * FROM batches WHERE enterprise_id=? AND medicine_id=?"
                    " AND status IN ('AWAIT_QC','IN_PROGRESS')", (ent_id, medicine_id)):
                    row = {"enterprise_id": ent_id, "batch_id": b["id"], "line_id": b["line_id"],
                           "planned_qty": b["planned_qty"],
                           "expected_release_ts": b["expected_release_ts"],
                           "material_short": bool(b["material_short"]),
                           "committable": False}
                    if b["status"] == "AWAIT_QC":
                        row["reason"] = "等待检验放行，放行前不可承诺"
                        qc.append(row)
                    else:
                        row["reason"] = ("在制品缺料，不可承诺" if b["material_short"]
                                         else "在制品未完工未检验，不可承诺")
                        wip.append(row)
                latest = conn.execute(
                    "SELECT id FROM report_versions WHERE enterprise_id=? ORDER BY id DESC LIMIT 1",
                    (ent_id,)).fetchone()
                if latest:
                    for ls in conn.execute(
                        "SELECT ls.* FROM report_line_summaries ls"
                        " JOIN line_licenses ll ON ll.line_id=ls.line_id"
                        " WHERE ls.version_id=? AND ll.medicine_id=?",
                        (latest["id"], medicine_id)):
                        cap.append({
                            "enterprise_id": ent_id, "line_id": ls["line_id"],
                            "theoretical_capacity": ls["theoretical_capacity"],
                            "committable": False,
                            "reason": "理论产能，须排产并通过换线/许可/物料/停机/检验约束",
                        })
            return {
                "medicine_id": medicine_id, "at_ts": at,
                "released_stock": stock,
                "awaiting_qc": qc,
                "wip": wip,
                "theoretical_capacity": cap,
                "atp_total": round(sum(i["qty_available"] for i in stock), 6),
            }

    # ================================================================== #
    # 排产：许可 + 跨日换线清洁 + 停机 + 时限
    # ================================================================== #
    def _line_intervals(self, conn, line_id: str):
        intervals = []
        for b in conn.execute(
            "SELECT planned_start, planned_finish, id FROM batches WHERE line_id=?"
            " AND status NOT IN ('REJECTED','CANCELLED') AND planned_start IS NOT NULL"
            " AND planned_finish IS NOT NULL", (line_id,)):
            intervals.append((b["planned_start"], b["planned_finish"], f"batch:{b['id']}"))
        for d in conn.execute(
            "SELECT start_ts, end_ts, reason FROM line_downtimes WHERE line_id=?", (line_id,)):
            intervals.append((d["start_ts"], d["end_ts"], f"downtime:{d['reason']}"))
        intervals.sort(key=lambda x: x[0])
        return intervals

    def _changeover_before(self, conn, line, medicine_id: str, cursor) -> float:
        """换线清洁只取决于该产线最近一次生产的品种：

        同品种继续生产免清洁；切换到不同品种必须完成 changeover_hours 清洁。
        空闲不会免除也不会重复清洁（清洁随品种切换发生）。
        """
        r = conn.execute(
            "SELECT medicine_id FROM batches WHERE line_id=?"
            " AND status NOT IN ('REJECTED','CANCELLED') AND planned_finish<=?"
            " ORDER BY planned_finish DESC LIMIT 1",
            (line["id"], timeutil.iso(cursor))).fetchone()
        if r and r["medicine_id"] == medicine_id:
            return 0.0
        return line["changeover_hours"]

    def _find_slot(self, conn, line, medicine_id: str, qty: float,
                   after_ts: str, must_ready_ts: str | None):
        if conn.execute("SELECT 1 FROM line_licenses WHERE line_id=? AND medicine_id=?",
                        (line["id"], medicine_id)).fetchone() is None:
            return None
        hours_needed = qty / line["daily_capacity"] * 24.0
        intervals = self._line_intervals(conn, line["id"])
        cursor = timeutil.parse(after_ts)
        while True:
            co = self._changeover_before(conn, line, medicine_id, cursor)
            prod_start = cursor + _hours(co)
            prod_end = prod_start + _hours(hours_needed)
            hit = next((iv for iv in intervals
                        if timeutil.parse(iv[0]) < prod_end
                        and timeutil.parse(iv[1]) > prod_start), None)
            if hit is None:
                break
            cursor = timeutil.parse(hit[1])
        ready = prod_end + _hours(line["qc_lead_hours"])
        if must_ready_ts is not None and ready > timeutil.parse(must_ready_ts):
            return None
        return {
            "slot_start": timeutil.iso(cursor),
            "production_start": timeutil.iso(prod_start),
            "production_finish": timeutil.iso(prod_end),
            "changeover_hours": co,
            "hours_needed": round(hours_needed, 6),
            "qc_lead_hours": line["qc_lead_hours"],
            "ready_ts": timeutil.iso(ready),
        }

    def _max_fit_qty(self, conn, line, medicine_id: str, after_ts: str,
                     must_ready_ts: str) -> float:
        """该产线在截止时间前（含换线、检验）最多能生产的数量（粗估，不含停机占用）。"""
        window_h = timeutil.hours_between(after_ts, must_ready_ts)
        usable = max(0.0, window_h - line["changeover_hours"] - line["qc_lead_hours"])
        return line["daily_capacity"] / 24.0 * usable

    def _largest_fitting_qty(self, conn, line, medicine_id: str, after_ts: str,
                             must_ready_ts: str, upper: float) -> float:
        """考虑停机/既有批次占用后，二分求截止前实际可容纳的最大批量。"""
        lo, hi = 0.0, upper
        for _ in range(40):
            mid = (lo + hi) / 2.0
            if mid <= EPS:
                break
            if self._find_slot(conn, line, medicine_id, mid, after_ts,
                               must_ready_ts) is not None:
                lo = mid
            else:
                hi = mid
        # 留极小余量，避免浮点边界导致确认时放不下
        return max(0.0, lo - 1e-6)

    def _check_materials(self, conn, enterprise_id: str, medicine_id: str,
                         qty: float) -> tuple[bool, list]:
        short = []
        for bom in conn.execute("SELECT * FROM bom WHERE medicine_id=?",
                                (medicine_id,)).fetchall():
            stock = conn.execute(
                "SELECT on_hand_qty FROM material_stock_current WHERE enterprise_id=? AND"
                " material_id=?", (enterprise_id, bom["material_id"])).fetchone()
            reserved = conn.execute(
                "SELECT COALESCE(SUM(mr.qty),0) s FROM material_reservations mr"
                " JOIN batches b ON b.id=mr.batch_id WHERE b.enterprise_id=?"
                " AND mr.material_id=? AND b.status NOT IN ('REJECTED','CANCELLED')",
                (enterprise_id, bom["material_id"])).fetchone()["s"]
            available = (stock["on_hand_qty"] if stock else 0.0) - reserved
            need = bom["qty_per_unit"] * qty
            if available + EPS < need:
                short.append({"material_id": bom["material_id"],
                              "need": round(need, 6), "available": round(available, 6)})
        return (not short), short

    # ================================================================== #
    # 建议计算（只算不锁）
    # ================================================================== #
    def compute_suggestion(self, actor: Actor, demand_id: str, *,
                           at_ts: str | None = None) -> dict[str, Any]:
        at = at_ts or timeutil.now_iso()
        with self.store.transaction() as conn:
            d = conn.execute("SELECT * FROM demands WHERE id=?", (demand_id,)).fetchone()
            if d is None:
                raise NotFoundError("需求不存在")
            if actor.role == "REGION":
                if actor.region_id != d["region_id"]:
                    raise PermissionError("只能针对本区域需求计算建议")
            elif actor.role != "REGULATOR":
                raise PermissionError("企业不能计算调配建议")
            if d["status"] == "CANCELLED":
                raise ConflictError("需求已取消")

            items, remaining = [], d["qty"]

            # 1) 已放行且运输可达的库存，按交付时间先后
            stock_candidates = []
            for b in conn.execute(
                "SELECT * FROM batches WHERE medicine_id=? AND status='RELEASED'",
                (d["medicine_id"],)):
                avail = self._batch_available(conn, b)
                if avail <= EPS:
                    continue
                lane = self._lane(conn, b["enterprise_id"], d["region_id"])
                if lane is None:
                    continue
                deliver = timeutil.add_hours(b["expected_release_ts"] or at,
                                             lane["transport_hours"])
                if timeutil.parse(deliver) > timeutil.parse(d["needed_by_ts"]):
                    continue
                stock_candidates.append((deliver, b, avail, lane))
            for deliver, b, avail, lane in sorted(stock_candidates, key=lambda c: c[0]):
                if remaining <= EPS:
                    break
                take = min(avail, remaining)
                remaining -= take
                items.append({
                    "source_type": "STOCK", "enterprise_id": b["enterprise_id"],
                    "batch_id": b["id"], "line_id": b["line_id"],
                    "qty": round(take, 6), "deliverable_ts": deliver,
                    "detail": {"basis": "已放行未锁定成品",
                               "qty_released": b["qty_released"],
                               "already_locked": self._batch_locked_qty(conn, b["id"]),
                               "transport_hours": lane["transport_hours"]},
                })

            # 2) 不足部分排产：逐产线贪心，按可交付时间
            if remaining > EPS:
                items.extend(self._production_candidates(conn, d, remaining, at)[0])
            planned_qty = sum(i["qty"] for i in items if i["source_type"] == "PRODUCTION")
            unmet = round(d["qty"] - sum(i["qty"] for i in items), 6)

            score = self._score(d, items, unmet)
            rationale = self._rationale(d, items, unmet, score)

            conn.execute("UPDATE suggestions SET superseded=1 WHERE demand_id=?", (demand_id,))
            sid = conn.execute(
                "INSERT INTO suggestions(demand_id, computed_ts, score, rationale)"
                " VALUES(?,?,?,?)",
                (demand_id, timeutil.now_iso(), score, rationale)).lastrowid
            for seq, it in enumerate(items):
                conn.execute(
                    "INSERT INTO suggestion_items(suggestion_id, seq, source_type,"
                    " enterprise_id, batch_id, line_id, qty, deliverable_ts, detail)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (sid, seq, it["source_type"], it["enterprise_id"], it.get("batch_id"),
                     it.get("line_id"), it["qty"], it["deliverable_ts"],
                     dumps(it.get("detail", {})))),
            log_event(conn, ts=timeutil.now_iso(), actor=actor.username,
                      action="SUGGESTION_COMPUTED", entity_type="suggestion", entity_id=sid,
                      payload={"demand_id": demand_id, "items": len(items), "unmet": unmet})
            return {"suggestion_id": sid, "demand_id": demand_id, "score": score,
                    "rationale": rationale, "items": items, "unmet_qty": unmet,
                    "locked": False}

    def _production_candidates(self, conn, d, remaining: float, at: str):
        candidates = []
        used_lines: set[str] = set()
        lines = conn.execute(
            "SELECT pl.* FROM production_lines pl JOIN line_licenses ll ON ll.line_id=pl.id"
            " WHERE ll.medicine_id=? ORDER BY pl.id", (d["medicine_id"],)).fetchall()
        while remaining > EPS:
            best = None
            for line in lines:
                if line["id"] in used_lines:
                    continue
                lane = self._lane(conn, line["enterprise_id"], d["region_id"])
                if lane is None:
                    continue
                must_ready = timeutil.iso(
                    timeutil.parse(d["needed_by_ts"]) - _hours(lane["transport_hours"]))
                if timeutil.parse(must_ready) <= timeutil.parse(at):
                    continue
                qty_cap = self._max_fit_qty(conn, line, d["medicine_id"], at, must_ready)
                qty = min(remaining, qty_cap)
                if qty <= EPS:
                    continue
                slot = self._find_slot(conn, line, d["medicine_id"], qty, at, must_ready)
                if slot is None:
                    # 停机/既有批次占用使整量放不下：二分求实际可容纳的最大批量
                    qty = self._largest_fitting_qty(
                        conn, line, d["medicine_id"], at, must_ready, qty_cap)
                    if qty <= EPS:
                        continue
                    slot = self._find_slot(conn, line, d["medicine_id"], qty,
                                           at, must_ready)
                    if slot is None:
                        continue
                ok, _short = self._check_materials(
                    conn, line["enterprise_id"], d["medicine_id"], qty)
                if not ok:
                    continue
                deliver = timeutil.add_hours(slot["ready_ts"], lane["transport_hours"])
                cand = (deliver, line, lane, qty, slot)
                if best is None or deliver < best[0]:
                    best = cand
            if best is None:
                break
            deliver, line, lane, qty, slot = best
            used_lines.add(line["id"])
            remaining -= qty
            candidates.append({
                "source_type": "PRODUCTION", "enterprise_id": line["enterprise_id"],
                "line_id": line["id"], "qty": round(qty, 6),
                "deliverable_ts": deliver,
                "detail": {"basis": "理论产能经排产落到具体批次（确认时创建并重检约束）",
                           **slot, "transport_hours": lane["transport_hours"],
                           "hard_constraints": ["许可范围", "换线清洁", "设备停机",
                                                "原料BOM", "检验周期", "运输时限"]},
            })
        return candidates, remaining

    def _score(self, d, items, unmet: float) -> float:
        base = URGENCY_WEIGHT[d["urgency"]]
        coverage = max(0.0, 7.0 - d["coverage_days"]) * 5.0
        fill = (d["qty"] - unmet) / d["qty"] if d["qty"] else 0.0
        slack = 0.0
        for it in items:
            slack += min(max(timeutil.hours_between(it["deliverable_ts"],
                                                    d["needed_by_ts"]), 0.0), 120.0)
        slack = slack / len(items) if items else 0.0
        return round(base + coverage + fill * 30.0 + slack * 0.2, 3)

    def _rationale(self, d, items, unmet: float, score: float) -> str:
        parts = [f"紧急度={d['urgency']}", f"现有覆盖{d['coverage_days']}天",
                 f"需求截止{d['needed_by_ts']}",
                 f"建议满足{d['qty'] - unmet:g}/{d['qty']:g}"]
        if unmet > EPS:
            parts.append("缺口受许可/换线/停机/原料/检验/运输硬约束限制")
        return "；".join(parts) + f"；综合评分{score}（建议仅供参考，监管确认后才锁定）"

    # ================================================================== #
    # 监管确认 → 锁定真实批次
    # ================================================================== #
    def approve_commitment(self, actor: Actor, payload: dict[str, Any], *,
                           idem_key: str | None = None,
                           at_ts: str | None = None) -> dict[str, Any]:
        """确认建议：payload 提供 suggestion_id（整单）或 items（逐项）+ demand_id。

        at_ts 为排产起算的决策时刻（默认当前时间）；批准留痕时间始终取系统时钟。
        """
        actor.require_regulator()

        with self.store.transaction() as conn:
            # 先解析为标准明细（建议可能已过期，锁定时全部重检）
            if payload.get("items"):
                demand_id = payload.get("demand_id")
                if not demand_id:
                    raise ValidationError("逐项确认必须提供 demand_id")
                raw_items = payload["items"]
            elif payload.get("suggestion_id") is not None:
                sid = payload["suggestion_id"]
                sug = conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
                if sug is None:
                    raise NotFoundError(f"建议 {sid} 不存在")
                if sug["superseded"]:
                    raise ConflictError("建议已被新版本替代，请重新计算后确认")
                demand_id = sug["demand_id"]
                raw_items = [{
                    "source_type": r["source_type"], "enterprise_id": r["enterprise_id"],
                    "batch_id": r["batch_id"], "line_id": r["line_id"], "qty": r["qty"],
                    "deliverable_ts": r["deliverable_ts"], "detail": json.loads(r["detail"]),
                } for r in conn.execute(
                    "SELECT * FROM suggestion_items WHERE suggestion_id=? ORDER BY seq",
                    (sid,))]
            else:
                raise ValidationError("必须提供 suggestion_id 或 items")
            if not raw_items:
                raise ValidationError("没有可确认的建议项")

            idem_payload = {"demand_id": demand_id, "items": raw_items}
            existing = self._idem_begin(conn, idem_key, idem_payload)
            if existing is not None:
                ids = [x for x in str(existing["ref_id"]).split(",") if x]
                return {"commitments": [self._commitment_view(conn, i) for i in ids],
                        "replayed": True}

            d = conn.execute("SELECT * FROM demands WHERE id=?", (demand_id,)).fetchone()
            if d is None:
                raise NotFoundError("需求不存在")
            if d["status"] == "CANCELLED":
                raise ConflictError("需求已取消，不能锁定")

            created = []
            for item in raw_items:
                created.append(self._lock_one(conn, actor, d, item,
                                              payload.get("suggestion_id"),
                                              payload.get("approval_note"),
                                              at_ts or timeutil.now_iso()))
            self._refresh_demand_status(conn, d["id"])
            ids = [c["id"] for c in created]
            self._idem_finish(conn, idem_key, actor, idem_payload,
                              "commitments", ",".join(ids))
            log_event(conn, ts=timeutil.now_iso(), actor=actor.username,
                      action="COMMITMENTS_APPROVED", entity_type="demand", entity_id=demand_id,
                      payload={"commitments": ids, "note": payload.get("approval_note")})
            return {"commitments": created, "replayed": False}

    def _lock_one(self, conn, actor: Actor, d, item: dict,
                  suggestion_id, approval_note, at_ts: str) -> dict:
        qty = float(item["qty"])
        if qty <= 0:
            raise ValidationError("锁定数量必须为正")
        ent_id = item["enterprise_id"]
        lane = self._lane(conn, ent_id, d["region_id"])
        if lane is None:
            raise ConflictError(f"企业 {ent_id} 到需求区域无运输通道")

        if item["source_type"] == "STOCK":
            b = conn.execute("SELECT * FROM batches WHERE id=?",
                             (item["batch_id"],)).fetchone()
            if b is None or b["enterprise_id"] != ent_id or \
                    b["medicine_id"] != d["medicine_id"]:
                raise ValidationError(f"库存批次 {item['batch_id']} 与需求不符")
            if b["status"] != "RELEASED":
                raise ConflictError(f"批次 {b['id']} 未放行，不能锁定成品")
            avail = self._batch_available(conn, b)
            if avail + EPS < qty:
                raise ConflictError(
                    f"批次 {b['id']} 可承诺量不足：需要 {qty:g}，剩余 {avail:g}"
                    "（可能已被另一地区承诺）")
            deliver = timeutil.add_hours(b["expected_release_ts"], lane["transport_hours"]) \
                if b["expected_release_ts"] else item["deliverable_ts"]
            if timeutil.parse(deliver) > timeutil.parse(d["needed_by_ts"]):
                raise ConflictError("计入运输时限后无法在需求截止前交付")
            snapshot = {
                "source_type": "STOCK",
                "basis": "已放行未锁定成品，确认时重检批次余量",
                "batch": {"id": b["id"], "status": b["status"],
                          "qty_released": b["qty_released"],
                          "locked_before_this": self._batch_locked_qty(conn, b["id"])},
                "transport_hours": lane["transport_hours"], "qty": qty,
                "approval": {"by": actor.username, "note": approval_note},
                "hard_constraints": ["检验已放行", "批次剩余可承诺量", "运输时限"]}
            cid = self._insert_commitment(conn, d, ent_id, b["id"], qty, deliver,
                                          suggestion_id, snapshot, actor, approval_note)
            conn.execute("INSERT INTO batch_locks(commitment_id, batch_id, qty)"
                         " VALUES(?,?,?)", (cid, b["id"], qty))
            return self._commitment_view(conn, cid)

        if item["source_type"] == "PRODUCTION":
            return self._lock_production(conn, actor, d, item, suggestion_id, lane,
                                         approval_note, at_ts)
        raise ValidationError(f"不支持的建议来源类型 {item['source_type']}")

    def _lock_production(self, conn, actor, d, item, suggestion_id, lane, approval_note,
                         at_ts: str):
        qty = float(item["qty"])
        line = conn.execute("SELECT * FROM production_lines WHERE id=?",
                            (item["line_id"],)).fetchone()
        if line is None or line["enterprise_id"] != item["enterprise_id"]:
            raise ValidationError("产线与企业不符")
        # 硬约束：许可
        if conn.execute("SELECT 1 FROM line_licenses WHERE line_id=? AND medicine_id=?",
                        (line["id"], d["medicine_id"])).fetchone() is None:
            raise ConflictError("该产线许可范围不含此药品（不可突破）")
        must_ready = timeutil.iso(
            timeutil.parse(d["needed_by_ts"]) - _hours(lane["transport_hours"]))
        # 硬约束：换线/停机/跨日/检验（建议可能已过期，全部重算）
        slot = self._find_slot(conn, line, d["medicine_id"], qty,
                               at_ts, must_ready)
        if slot is None:
            raise ConflictError("当前排产已无法在时限内完成（换线清洁/设备停机/跨日）")
        # 硬约束：原料
        ok, short = self._check_materials(conn, item["enterprise_id"],
                                          d["medicine_id"], qty)
        if not ok:
            raise ConflictError(f"原料不足，不能排产（不可突破）：{short}")

        batch_id = _new_id("batch")
        deliver = timeutil.add_hours(slot["ready_ts"], lane["transport_hours"])
        conn.execute(
            "INSERT INTO batches(id, enterprise_id, line_id, medicine_id, source, status,"
            " planned_qty, planned_start, planned_finish, expected_release_ts,"
            " qty_released, material_short) VALUES(?,?,?,?,'SYSTEM_PLAN','PLANNED',"
            "?,?,?,?,0,0)",
            (batch_id, item["enterprise_id"], line["id"], d["medicine_id"], qty,
             slot["production_start"], slot["production_finish"], slot["ready_ts"]))
        self._add_batch_event(conn, batch_id, "CREATED", None, "PLANNED", qty,
                              timeutil.now_iso(), actor.username, None,
                              {"reason": "监管确认排产", "slot": slot})
        for bom in conn.execute("SELECT * FROM bom WHERE medicine_id=?",
                                (d["medicine_id"],)).fetchall():
            conn.execute(
                "INSERT INTO material_reservations(batch_id, material_id, qty, commitment_id)"
                " VALUES(?,?,?,NULL)",
                (batch_id, bom["material_id"], bom["qty_per_unit"] * qty))
        downtimes = [{"start_ts": r["start_ts"], "end_ts": r["end_ts"], "reason": r["reason"]}
                     for r in conn.execute(
                         "SELECT start_ts,end_ts,reason FROM line_downtimes WHERE line_id=?",
                         (line["id"],))]
        snapshot = {
            "source_type": "PRODUCTION",
            "basis": "理论产能经排产落到新建批次，确认时重检全部硬约束",
            "line": {"id": line["id"], "daily_capacity": line["daily_capacity"],
                     "changeover_hours": line["changeover_hours"],
                     "qc_lead_hours": line["qc_lead_hours"]},
            "slot": slot, "transport_hours": lane["transport_hours"], "qty": qty,
            "downtimes": downtimes,
            "approval": {"by": actor.username, "note": approval_note},
            "hard_constraints": ["许可范围", "换线清洁", "设备停机", "原料BOM库存",
                                 "检验周期", "运输时限"]}
        cid = self._insert_commitment(conn, d, item["enterprise_id"], batch_id, qty,
                                      deliver, suggestion_id, snapshot, actor, approval_note)
        conn.execute("UPDATE material_reservations SET commitment_id=? WHERE batch_id=?",
                     (cid, batch_id))
        conn.execute("INSERT INTO batch_locks(commitment_id, batch_id, qty)"
                     " VALUES(?,?,?)", (cid, batch_id, qty))
        return self._commitment_view(conn, cid)

    def _insert_commitment(self, conn, d, ent_id, batch_id, qty, deliver,
                           suggestion_id, snapshot, actor, approval_note) -> str:
        cid = _new_id("cmt")
        conn.execute(
            "INSERT INTO commitments(id, demand_id, enterprise_id, medicine_id, batch_id,"
            " qty_committed, deliverable_ts, source_suggestion_id, constraint_snapshot,"
            " approved_by, approved_ts, approval_note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, d["id"], ent_id, d["medicine_id"], batch_id, qty, deliver,
             suggestion_id, dumps(snapshot), actor.username, timeutil.now_iso(),
             approval_note))
        conn.execute(
            "INSERT INTO commitment_events(commitment_id, event_type, qty, reason, actor, event_ts)"
            " VALUES(?,?,?,?,?,?)",
            (cid, "LOCKED", qty, snapshot["basis"], actor.username, timeutil.now_iso()))
        return cid

    # ================================================================== #
    # 履行 / 释放（只追加）
    # ================================================================== #
    def fulfill_commitment(self, actor: Actor, payload: dict[str, Any], *,
                           idem_key: str | None = None) -> dict:
        """登记已履行数量。已履行永久不可改写、不可释放。
        payload: commitment_id, qty, event_ts, note?"""
        actor.require_regulator()
        cid = payload.get("commitment_id")
        qty = float(payload.get("qty", 0))
        if qty <= 0:
            raise ValidationError("履行数量必须为正")
        with self.store.transaction() as conn:
            if self._idem_begin(conn, idem_key, payload) is not None:
                return {**self._commitment_view(conn, cid), "replayed": True}
            c = self._get_commitment(conn, cid)
            outstanding = c["qty_committed"] - c["qty_fulfilled"] - c["qty_released"]
            if qty > outstanding + EPS:
                raise ConflictError(f"履行数量超出未履行余额 {outstanding:g}（已履行不可改写）")
            conn.execute("UPDATE commitments SET qty_fulfilled=qty_fulfilled+? WHERE id=?",
                         (qty, cid))
            conn.execute(
                "INSERT INTO commitment_events(commitment_id, event_type, qty, reason,"
                " actor, event_ts) VALUES(?,?,?,?,?,?)",
                (cid, "FULFILLED", qty, payload.get("note"), actor.username,
                 payload.get("event_ts", timeutil.now_iso())))
            self._finalize_status(conn, cid)
            self._refresh_demand_status(conn, c["demand_id"])
            self._idem_finish(conn, idem_key, actor, payload, "commitment", cid)
            log_event(conn, ts=timeutil.now_iso(), actor=actor.username,
                      action="COMMITMENT_FULFILLED", entity_type="commitment",
                      entity_id=cid, payload={"qty": qty})
            return self._commitment_view(conn, cid)

    def release_commitment(self, actor: Actor, payload: dict[str, Any], *,
                           idem_key: str | None = None) -> dict:
        """释放尚未履行部分。payload: commitment_id, qty?(默认全部), reason, event_ts?"""
        actor.require_regulator()
        cid = payload.get("commitment_id")
        with self.store.transaction() as conn:
            if self._idem_begin(conn, idem_key, payload) is not None:
                return {**self._commitment_view(conn, cid), "replayed": True}
            c = self._get_commitment(conn, cid)
            outstanding = c["qty_committed"] - c["qty_fulfilled"] - c["qty_released"]
            if outstanding <= EPS:
                raise ConflictError("承诺没有可释放的未履行数量")
            qty = float(payload["qty"]) if payload.get("qty") is not None else outstanding
            if qty <= 0:
                raise ValidationError("释放数量必须为正")
            if qty > outstanding + EPS:
                raise ConflictError(f"释放数量超出未履行余额 {outstanding:g}")
            view = self._release_commitment(conn, c, qty=qty,
                                            reason=payload.get("reason", "MANUAL_RELEASE"),
                                            actor=actor)
            self._refresh_demand_status(conn, c["demand_id"])
            self._idem_finish(conn, idem_key, actor, payload, "commitment", cid)
            return view

    def _release_commitment(self, conn, c, *, qty: float | None, reason: str, actor) -> dict:
        cid = c["id"]
        outstanding = c["qty_committed"] - c["qty_fulfilled"] - c["qty_released"]
        if outstanding <= EPS:
            return self._commitment_view(conn, cid)
        rel = outstanding if qty is None else min(qty, outstanding)
        conn.execute("UPDATE commitments SET qty_released=qty_released+? WHERE id=?",
                     (rel, cid))
        conn.execute(
            "INSERT INTO commitment_events(commitment_id, event_type, qty, reason,"
            " actor, event_ts) VALUES(?,?,?,?,?,?)",
            (cid, "RELEASED", rel, reason, getattr(actor, "username", "system"),
             timeutil.now_iso()))
        # 未履行锁回到可承诺池；已履行对应锁保留（成品已交付消耗，不可再分配）
        lock = conn.execute(
            "SELECT qty FROM batch_locks WHERE commitment_id=?", (cid,)).fetchone()
        if lock is not None:
            remaining_lock = lock["qty"] - rel
            if remaining_lock <= EPS:
                conn.execute("DELETE FROM batch_locks WHERE commitment_id=?", (cid,))
            else:
                conn.execute("UPDATE batch_locks SET qty=? WHERE commitment_id=?",
                             (remaining_lock, cid))

        b = conn.execute("SELECT * FROM batches WHERE id=?", (c["batch_id"],)).fetchone()
        if b["source"] == "SYSTEM_PLAN":
            still_locked = self._batch_locked_qty(conn, b["id"])
            cur = conn.execute("SELECT * FROM commitments WHERE id=?", (cid,)).fetchone()
            if still_locked <= EPS and cur["qty_fulfilled"] <= EPS:
                conn.execute("UPDATE batches SET status='CANCELLED' WHERE id=?", (b["id"],))
                conn.execute("DELETE FROM material_reservations WHERE batch_id=?", (b["id"],))
                self._add_batch_event(conn, b["id"], "STATUS", b["status"], "CANCELLED",
                                      None, timeutil.now_iso(),
                                      getattr(actor, "username", "system"), None,
                                      {"reason": reason, "commitment_released": cid})
            elif still_locked <= EPS:
                ratio = cur["qty_fulfilled"] / c["qty_committed"]
                conn.execute("UPDATE material_reservations SET qty=qty*? WHERE batch_id=?",
                             (ratio, b["id"]))
        self._finalize_status(conn, cid)
        log_event(conn, ts=timeutil.now_iso(), actor=getattr(actor, "username", "system"),
                  action="COMMITMENT_RELEASED", entity_type="commitment", entity_id=cid,
                  payload={"qty": rel, "reason": reason})
        return self._commitment_view(conn, cid)

    def _release_impacted(self, conn, b, event: str, payload, actor) -> list[str]:
        """检验不合格：释放全部未履行；检验延期：仅释放新交付时间赶不上截止的承诺。"""
        released = []
        cs = conn.execute(
            "SELECT * FROM commitments WHERE batch_id=? AND status IN (?,?)",
            (b["id"], *_ACTIVE_COMMITMENT)).fetchall()
        for c in cs:
            if event == "REJECT":
                released.append(self._release_commitment(
                    conn, c, qty=None, reason="QC_REJECTED", actor=actor)["id"])
            elif event == "DELAY":
                d = conn.execute("SELECT * FROM demands WHERE id=?",
                                 (c["demand_id"],)).fetchone()
                lane = self._lane(conn, c["enterprise_id"], d["region_id"])
                new_deliver = timeutil.add_hours(payload["new_expected_release_ts"],
                                                 lane["transport_hours"] if lane else 0.0)
                if timeutil.parse(new_deliver) > timeutil.parse(d["needed_by_ts"]):
                    released.append(self._release_commitment(
                        conn, c, qty=None, reason="QC_DELAY_MISSES_DEADLINE",
                        actor=actor)["id"])
        return released

    def _get_commitment(self, conn, cid: str):
        c = conn.execute("SELECT * FROM commitments WHERE id=?", (cid,)).fetchone()
        if c is None:
            raise NotFoundError("承诺不存在")
        return c

    def _finalize_status(self, conn, cid: str) -> None:
        c = conn.execute("SELECT * FROM commitments WHERE id=?", (cid,)).fetchone()
        q = c["qty_committed"]
        if c["qty_fulfilled"] >= q - EPS:
            status = "FULFILLED"
        elif c["qty_released"] >= q - EPS:
            status = "RELEASED"
        elif c["qty_fulfilled"] > EPS and c["qty_released"] > EPS:
            status = "PARTIALLY_RELEASED"
        elif c["qty_fulfilled"] > EPS:
            status = "PARTIALLY_FULFILLED"
        else:
            status = "ACTIVE"
        conn.execute("UPDATE commitments SET status=? WHERE id=?", (status, cid))

    def _refresh_demand_status(self, conn, demand_id: str) -> None:
        d = conn.execute("SELECT * FROM demands WHERE id=?", (demand_id,)).fetchone()
        if d["status"] == "CANCELLED":
            return
        live = conn.execute(
            "SELECT COALESCE(SUM(qty_committed - qty_released),0) s FROM commitments"
            " WHERE demand_id=?", (demand_id,)).fetchone()["s"]
        fulfilled = conn.execute(
            "SELECT COALESCE(SUM(qty_fulfilled),0) s FROM commitments WHERE demand_id=?",
            (demand_id,)).fetchone()["s"]
        if fulfilled >= d["qty"] - EPS:
            status = "FULFILLED"
        elif live > EPS:
            status = "PARTIAL"
        else:
            status = "OPEN"
        conn.execute("UPDATE demands SET status=? WHERE id=?", (status, demand_id))

    # ================================================================== #
    # 查询与追溯（角色隔离）
    # ================================================================== #
    def _commitment_view(self, conn, cid: str) -> dict:
        c = conn.execute("SELECT * FROM commitments WHERE id=?", (cid,)).fetchone()
        out = _row(c)
        out["constraint_snapshot"] = json.loads(c["constraint_snapshot"])
        out["events"] = [_row(r) for r in conn.execute(
            "SELECT event_type, qty, reason, actor, event_ts FROM commitment_events"
            " WHERE commitment_id=? ORDER BY id", (cid,))]
        out["qty_outstanding"] = round(
            c["qty_committed"] - c["qty_fulfilled"] - c["qty_released"], 6)
        return out

    def list_commitments(self, actor: Actor, *, demand_id: str | None = None,
                         enterprise_id: str | None = None) -> list[dict]:
        sql = "SELECT id FROM commitments WHERE 1=1"
        args: list[Any] = []
        if demand_id:
            sql += " AND demand_id=?"
            args.append(demand_id)
        if enterprise_id:
            sql += " AND enterprise_id=?"
            args.append(enterprise_id)
        with self.store.read() as conn:
            if actor.role == "ENTERPRISE":
                sql += " AND enterprise_id=?"
                args.append(actor.enterprise_id)
            elif actor.role == "REGION":
                sql += " AND demand_id IN (SELECT id FROM demands WHERE region_id=?)"
                args.append(actor.region_id)
            elif actor.role != "REGULATOR":
                raise PermissionError("身份无效")
            rows = conn.execute(sql + " ORDER BY approved_ts, id", args).fetchall()
            return [self._commitment_view(conn, r["id"]) for r in rows]

    def situation_board(self, actor: Actor, medicine_id: str | None = None) -> dict:
        """跨企业保供态势，仅监管人员。"""
        actor.require_regulator()
        with self.store.read() as conn:
            if medicine_id:
                meds = [medicine_id] if conn.execute(
                    "SELECT 1 FROM medicines WHERE id=?", (medicine_id,)).fetchone() else []
            else:
                meds = [r["id"] for r in conn.execute("SELECT id FROM medicines")]
            board = {"medicines": []}
            for mid in meds:
                demands = [dict(region_id=r["region_id"], urgency=r["urgency"],
                                count=r["n"], qty=r["qty"], open_count=r["open_n"])
                           for r in conn.execute(
                    "SELECT region_id, urgency, COUNT(*) n, SUM(qty) qty,"
                    " SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) open_n"
                    " FROM demands WHERE medicine_id=? GROUP BY region_id, urgency",
                    (mid,))]
                enterprises = []
                for r in conn.execute(
                    "SELECT enterprise_id, COALESCE(SUM(qty_released),0) released"
                    " FROM batches WHERE medicine_id=? AND status='RELEASED'"
                    " GROUP BY enterprise_id", (mid,)):
                    locked = conn.execute(
                        "SELECT COALESCE(SUM(l.qty),0) s FROM batch_locks l"
                        " JOIN batches b ON b.id=l.batch_id WHERE b.enterprise_id=?"
                        " AND b.medicine_id=?", (r["enterprise_id"], mid)).fetchone()["s"]
                    enterprises.append({"enterprise_id": r["enterprise_id"],
                                        "released": r["released"],
                                        "locked": round(locked, 6),
                                        "free_atp": round(max(0.0, r["released"] - locked), 6)})
                qc_pending = [dict(enterprise_id=r["enterprise_id"], count=r["n"], qty=r["q"])
                              for r in conn.execute(
                    "SELECT enterprise_id, COUNT(*) n, SUM(planned_qty) q FROM batches"
                    " WHERE medicine_id=? AND status IN ('AWAIT_QC','IN_PROGRESS')"
                    " GROUP BY enterprise_id", (mid,))]
                short_batches = [r["id"] for r in conn.execute(
                    "SELECT id FROM batches WHERE medicine_id=? AND material_short=1"
                    " AND status IN ('PLANNED','IN_PROGRESS')", (mid,))]
                board["medicines"].append({
                    "medicine_id": mid, "demands": demands, "enterprises": enterprises,
                    "atp_total": round(sum(e["free_atp"] for e in enterprises), 6),
                    "qc_pending": qc_pending,
                    "material_short_batches": short_batches})
            return board

    def batch_trace(self, actor: Actor, batch_id: str) -> dict:
        with self.store.read() as conn:
            b = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if b is None:
                raise NotFoundError("批次不存在")
            if actor.role == "ENTERPRISE" and actor.enterprise_id != b["enterprise_id"]:
                raise PermissionError("企业只能查看本企业批次")
            if actor.role == "REGION":
                ok = conn.execute(
                    "SELECT 1 FROM commitments c JOIN demands d ON d.id=c.demand_id"
                    " WHERE c.batch_id=? AND d.region_id=?",
                    (batch_id, actor.region_id)).fetchone()
                if not ok:
                    raise PermissionError("区域只能查看与本区域需求相关的批次")
            events = [_row(r) for r in conn.execute(
                "SELECT event_type, from_status, to_status, qty, event_ts, actor, version_id"
                " FROM batch_events WHERE batch_id=? ORDER BY id", (batch_id,))]
            locks = []
            for l in conn.execute(
                "SELECT c.id commitment_id, c.demand_id, c.approved_by, c.approved_ts,"
                " c.status, l.qty, c.constraint_snapshot FROM batch_locks l"
                " JOIN commitments c ON c.id=l.commitment_id WHERE l.batch_id=?",
                (batch_id,)):
                locks.append({"commitment_id": l["commitment_id"],
                              "demand_id": l["demand_id"],
                              "approved_by": l["approved_by"],
                              "approved_ts": l["approved_ts"], "status": l["status"],
                              "qty": l["qty"],
                              "constraint_snapshot": json.loads(l["constraint_snapshot"])})
            return {"batch": _row(b), "events": events, "locks": locks,
                    "total_locked": self._batch_locked_qty(conn, batch_id)}
