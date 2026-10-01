"""调配建议计算与监管确认锁定。

建议（allocations, status=proposed）只是计算结果，**不锁定任何产能**；
只有监管人员确认（decisions + commitments）后才锁定批次数量与去向。

建议计算的输入与排除规则：
* 需求排序：紧急程度 → 现有覆盖天数（越少越急）→ 运输截止时间。
* 候选只取检验已放行(released)、未判不合格的真实批次。
* 企业-区域无运输时限记录 => 不可达，排除。
* 放行时间 + 运输时长 > 到货截止 => 时限不满足，排除。
* 许可/换线/停机/缺料在批次登记时已被强制拦截，候选批次天然合规。
* 批次余量 = 批次数量 − 已净锁定量 − 本次计划中更高优先级申请的建议占用。

确认在单个 ``BEGIN IMMEDIATE`` 事务内重算余量后落库；
触发器、allocation 唯一占用、decision 幂等键三重防重复锁定。
"""
from __future__ import annotations

from ..db import Database
from ..errors import Conflict, NotFound, OvercommitError, PermissionDenied, ValidationError
from ..timeutil import add_hours, diff_hours, now, parse

_URGENCY_RANK = {"critical": 3, "urgent": 2, "normal": 1}


class AllocationService:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # 建议计算
    # ------------------------------------------------------------------

    def build_plan(
        self, *, actor: dict, medicine_id: str | None = None, plan_ts: str | None = None
    ) -> dict:
        """为所有未满足的申请重新计算建议。旧的 proposed 建议整体失效。"""
        if actor["role"] != "regulator":
            raise PermissionDenied("只有监管人员可以生成跨企业调配建议")
        ts = plan_ts or now()

        with self.db.begin_immediate() as conn:
            req_sql = """
                SELECT * FROM supply_requests
                WHERE status IN ('open','planning','partial')
            """
            args: list = []
            if medicine_id:
                req_sql += " AND medicine_id=?"
                args.append(medicine_id)
            requests = list(conn.execute(req_sql, args))
            requests.sort(key=self._request_priority_key)

            plan: list[dict] = []
            # 计划级预留：同一轮计算中，更高优先级申请的建议已经占用的批次余量。
            # 不入库（建议不锁定任何产能），但必须在本次计算内可见，
            # 否则两个地区同小时申请会把同一批次重复承诺。
            plan_reserved: dict[str, int] = {}
            for req in requests:
                # 重新生成前，让该申请上一轮的建议失效（已确认的不动）。
                conn.execute(
                    "UPDATE allocations SET status='expired' "
                    "WHERE request_id=? AND status='proposed'",
                    (req["id"],),
                )
                locked = self._net_locked_for_request(conn, req["id"])
                remaining = req["quantity"] - locked
                if remaining <= 0:
                    continue

                candidates = self._candidates(
                    conn,
                    region_id=req["region_id"],
                    medicine_id=req["medicine_id"],
                    needed_by=req["needed_by_ts"],
                    plan_reserved=plan_reserved,
                )
                lines: list[dict] = []
                for cand in candidates:
                    if remaining <= 0:
                        break
                    qty = min(remaining, cand["available_qty"])
                    if qty <= 0:
                        continue
                    alloc_id = f"alloc:{req['id']}:{cand['batch_id']}:{ts.replace(':','').replace('.','')}"
                    arrives = add_hours(cand["available_ts"], cand["transit_hours"])
                    slack = diff_hours(req["needed_by_ts"], arrives)
                    score = round(slack * 10 - cand["transit_hours"], 3)
                    rationale = (
                        f"批次 {cand['batch_id']} 已放行@{cand['available_ts']}；"
                        f"运输 {cand['transit_hours']:g}h，预计 {arrives} 到达，"
                        f"距截止余量 {slack:.1f}h；批次余量 {cand['available_qty']}"
                    )
                    conn.execute(
                        """
                        INSERT INTO allocations
                          (id, request_id, batch_id, enterprise_id, quantity,
                           available_ts, transit_hours, arrives_ts, score,
                           rationale, created_ts, status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,'proposed')
                        """,
                        (
                            alloc_id, req["id"], cand["batch_id"], cand["enterprise_id"],
                            qty, cand["available_ts"], cand["transit_hours"], arrives,
                            score, rationale, ts,
                        ),
                    )
                    plan_reserved[cand["batch_id"]] = (
                        plan_reserved.get(cand["batch_id"], 0) + qty
                    )
                    remaining -= qty
                    lines.append(
                        {
                            "allocation_id": alloc_id,
                            "batch_id": cand["batch_id"],
                            "enterprise_id": cand["enterprise_id"],
                            "quantity": qty,
                            "arrives_ts": arrives,
                            "score": score,
                            "rationale": rationale,
                        }
                    )

                conn.execute(
                    "UPDATE supply_requests SET status='planning' WHERE id=?",
                    (req["id"],),
                )
                plan.append(
                    {
                        "request_id": req["id"],
                        "region_id": req["region_id"],
                        "medicine_id": req["medicine_id"],
                        "requested": req["quantity"],
                        "already_locked": locked,
                        "planned": sum(l["quantity"] for l in lines),
                        "shortfall": max(0, remaining),
                        "lines": lines,
                    }
                )
            return {
                "generated_ts": ts,
                "request_order": [r["id"] for r in requests],
                "plan": plan,
            }

    @staticmethod
    def _request_priority_key(row) -> tuple:
        return (
            -_URGENCY_RANK[row["urgency"]],
            row["stock_days"],
            row["needed_by_ts"],
            row["created_ts"],
        )

    @staticmethod
    def _net_locked_for_request(conn, request_id: str) -> int:
        """净锁定 = 承诺数量 − 已释放（取消/不合格回流的份额不算占用）。"""
        row = conn.execute(
            """
            SELECT COALESCE(SUM(quantity - released_qty),0) AS q
            FROM commitments
            WHERE request_id=? AND status IN ('active','completed','released')
            """,
            (request_id,),
        ).fetchone()
        return int(row["q"])

    def _candidates(
        self, conn, *, region_id: str, medicine_id: str, needed_by: str,
        plan_reserved: dict[str, int] | None = None,
    ) -> list[dict]:
        plan_reserved = plan_reserved or {}
        rows = conn.execute(
            """
            SELECT b.id AS batch_id, b.enterprise_id, b.quantity,
                   b.qc_decided_ts AS available_ts,
                   t.hours AS transit_hours
            FROM batches b
            JOIN transit_times t
              ON t.enterprise_id = b.enterprise_id AND t.region_id = ?
            WHERE b.medicine_id = ?
              AND b.qc_status = 'released'
              AND NOT EXISTS (
                  SELECT 1 FROM batch_rejections br WHERE br.batch_id = b.id
              )
            ORDER BY b.qc_decided_ts, b.id
            """,
            (region_id, medicine_id),
        ).fetchall()

        candidates: list[dict] = []
        for row in rows:
            arrives = add_hours(row["available_ts"], row["transit_hours"])
            if parse(arrives) > parse(needed_by):
                continue  # 运输时限不可突破
            locked = conn.execute(
                """
                SELECT COALESCE(SUM(c.quantity - c.released_qty),0) AS q
                FROM commitments c
                WHERE c.batch_id=? AND c.status IN ('active','completed','released')
                """,
                (row["batch_id"],),
            ).fetchone()["q"]
            # 本批次仍处于 proposed 的建议数量（上一轮未确认、尚未过期的）
            # 不参与余量计算：build_plan 已在入口统一过期；这里仅防御性清零。
            available = row["quantity"] - int(locked) - plan_reserved.get(row["batch_id"], 0)
            if available <= 0:
                continue
            candidates.append(
                {
                    "batch_id": row["batch_id"],
                    "enterprise_id": row["enterprise_id"],
                    "available_ts": row["available_ts"],
                    "transit_hours": row["transit_hours"],
                    "arrives_ts": arrives,
                    "available_qty": available,
                }
            )

        # 同申请内：到货越早、运输越短、余量越大者优先。
        candidates.sort(
            key=lambda c: (c["arrives_ts"], c["transit_hours"], -c["available_qty"])
        )
        return candidates

    # ------------------------------------------------------------------
    # 监管确认：锁定产能与去向
    # ------------------------------------------------------------------

    def confirm(self, payload: dict, *, actor: dict) -> dict:
        if actor["role"] != "regulator":
            raise PermissionDenied("只有监管人员确认后才能锁定产能")
        request_id = payload.get("request_id")
        idem = payload.get("idempotency_key")
        lines = payload.get("lines")
        if not request_id or not idem:
            raise ValidationError("确认必须包含 request_id 与 idempotency_key")
        if not isinstance(lines, list) or not lines:
            raise ValidationError("确认必须包含至少一条 lines")

        with self.db.begin_immediate() as conn:
            # 崩溃恢复/重复提交：幂等键命中则原样返回既有决定，绝不二次锁定。
            existed = conn.execute(
                """
                SELECT d.id, d.request_id FROM decisions d WHERE d.idempotency_key=?
                """,
                (idem,),
            ).fetchone()
            if existed:
                if existed["request_id"] != request_id:
                    raise Conflict("幂等键已用于另一申请，不能复用")
                # 载荷一致性：同一幂等键必须锁定同样的建议行与数量。
                existing = {
                    (c["allocation_id"], c["quantity"])
                    for c in conn.execute(
                        "SELECT allocation_id, quantity FROM commitments "
                        "WHERE decision_id=?",
                        (existed["id"],),
                    )
                }
                sent = {(l.get("allocation_id"), l.get("quantity")) for l in lines}
                if existing != sent:
                    raise Conflict("幂等键命中但确认内容与原决定不一致")
                return self._load_decision(conn, existed["id"])

            req = conn.execute(
                "SELECT * FROM supply_requests WHERE id=?", (request_id,)
            ).fetchone()
            if req is None:
                raise NotFound(f"申请不存在：{request_id}")

            decision_id = payload.get("decision_id") or f"dec:{idem}"
            conn.execute(
                """
                INSERT INTO decisions (id, request_id, regulator_id, decided_ts, note,
                                       idempotency_key)
                VALUES (?,?,?,?,?,?)
                """,
                (decision_id, request_id, actor["id"], now(),
                 payload.get("note", ""), idem),
            )

            confirmed: list[dict] = []
            total_qty = 0
            seen_allocations: set[str] = set()
            for line in lines:
                alloc_id = line.get("allocation_id")
                qty = line.get("quantity")
                if alloc_id in seen_allocations:
                    raise ValidationError(f"建议 {alloc_id} 在本次确认中重复出现")
                seen_allocations.add(alloc_id)
                alloc = conn.execute(
                    "SELECT * FROM allocations WHERE id=?", (alloc_id,)
                ).fetchone()
                if alloc is None:
                    raise ValidationError(f"建议不存在：{alloc_id}")
                if alloc["request_id"] != request_id:
                    raise ValidationError("建议与申请不匹配")
                if alloc["status"] != "proposed":
                    raise Conflict(
                        f"建议 {alloc_id} 状态为 {alloc['status']}，"
                        "已确认/已失效的建议不能再次锁定"
                    )
                if not isinstance(qty, int) or qty <= 0 or qty > alloc["quantity"]:
                    raise ValidationError(
                        f"确认数量必须为 1..建议数量({alloc['quantity']}) 的整数"
                    )

                # 同一申请累计净锁定不得超过申请数量（释放份额已扣除）。
                # 同事务内前几行已插入的承诺对本连接可见，无需再加累计变量。
                net_for_request = self._net_locked_for_request(conn, request_id)
                if net_for_request + qty > req["quantity"]:
                    raise OvercommitError(
                        f"申请 {request_id} 数量 {req['quantity']}，"
                        f"本次再锁 {qty} 后累计 {net_for_request + qty} 将超过申请数量"
                    )

                # 锁定前在事务内重算批次余量（并发的另一个确认可能已先行落库）。
                batch = conn.execute(
                    "SELECT * FROM batches WHERE id=?", (alloc["batch_id"],)
                ).fetchone()
                if batch["qc_status"] != "released":
                    raise Conflict(
                        f"批次 {batch['id']} 当前检验状态 {batch['qc_status']}，"
                        "不能锁定（请重新生成建议）"
                    )
                if conn.execute(
                    "SELECT 1 FROM batch_rejections WHERE batch_id=?",
                    (batch["id"],),
                ).fetchone():
                    raise Conflict(f"批次 {batch['id']} 已判不合格，不能锁定")
                net_locked = conn.execute(
                    """
                    SELECT COALESCE(SUM(c.quantity - c.released_qty),0) AS q
                    FROM commitments c
                    WHERE c.batch_id=?
                      AND c.status IN ('active','completed','released')
                    """,
                    (batch["id"],),
                ).fetchone()["q"]
                if int(net_locked) + qty > batch["quantity"]:
                    raise OvercommitError(
                        f"批次 {batch['id']} 余量仅 "
                        f"{batch['quantity'] - int(net_locked)}，"
                        f"无法再锁定 {qty}：另一个地区可能已锁定该批次，请重新计算建议"
                    )

                commitment_id = f"com:{alloc_id}"
                conn.execute(
                    """
                    INSERT INTO commitments
                      (id, decision_id, allocation_id, request_id, batch_id,
                       enterprise_id, region_id, quantity, locked_ts, status)
                    VALUES (?,?,?,?,?,?,?,?,?,'active')
                    """,
                    (
                        commitment_id, decision_id, alloc_id, request_id,
                        alloc["batch_id"], alloc["enterprise_id"], req["region_id"],
                        qty, now(),
                    ),
                )
                conn.execute(
                    "UPDATE allocations SET status='confirmed' WHERE id=?",
                    (alloc_id,),
                )
                total_qty += qty
                confirmed.append(
                    {
                        "commitment_id": commitment_id,
                        "allocation_id": alloc_id,
                        "batch_id": alloc["batch_id"],
                        "enterprise_id": alloc["enterprise_id"],
                        "region_id": req["region_id"],
                        "quantity": qty,
                    }
                )

            self._refresh_request_status(conn, request_id)
            result = self._load_decision(conn, decision_id)
            result["newly_confirmed"] = confirmed
            result["total_confirmed_qty"] = total_qty
            return result

    @staticmethod
    def _refresh_request_status(conn, request_id: str) -> None:
        req = conn.execute(
            "SELECT quantity FROM supply_requests WHERE id=?", (request_id,)
        ).fetchone()
        net_locked = AllocationService._net_locked_for_request(conn, request_id)
        fulfilled = conn.execute(
            """
            SELECT COALESCE(SUM(e.quantity),0) AS q
            FROM commitment_events e
            JOIN commitments c ON c.id = e.commitment_id
            WHERE c.request_id=? AND e.kind='fulfill'
            """,
            (request_id,),
        ).fetchone()["q"]
        if net_locked >= req["quantity"]:
            status = "locked"
        elif net_locked > 0:
            status = "partial"
        else:
            status = "open"
        if fulfilled >= req["quantity"]:
            status = "closed"
        conn.execute(
            "UPDATE supply_requests SET status=? WHERE id=?", (status, request_id)
        )

    @staticmethod
    def _load_decision(conn, decision_id: str) -> dict:
        dec = conn.execute(
            "SELECT * FROM decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if dec is None:
            raise NotFound(f"决定不存在：{decision_id}")
        d = dict(dec)
        d["commitments"] = [
            dict(c) for c in conn.execute(
                "SELECT * FROM commitments WHERE decision_id=? ORDER BY id",
                (decision_id,),
            )
        ]
        return d
