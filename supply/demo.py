"""连续保供场景演示：

  python3 -m supply.demo

依次展示：两地区同小时申请 -> 建议（不重复承诺）-> 监管确认锁定 ->
日报三类数量分离 -> 运输取消释放回流 -> 检验延期后放行 -> 重新分配 ->
全链溯源。使用内存库，不写磁盘。
"""
from __future__ import annotations

import json

from .db import Database
from .platform import SupplyPlatform


def main() -> None:
    db = Database(":memory:")
    db.initialize()
    p = SupplyPlatform(db)
    p.load_seed("fixtures/seed.json")
    reg = p.actor("u-reg")
    alpha = p.actor("u-alpha")

    def show(title, obj):
        print(f"\n=== {title} ===")
        print(json.dumps(obj, ensure_ascii=False, indent=2))

    # 1) 两地区同一小时提出申请
    p.requests.create_request(
        {"region_id": "region-east", "medicine_id": "drug-emergency-a",
         "quantity": 25000, "needed_by_ts": "2026-10-01T06:00:00Z",
         "urgency": "critical", "stock_days": 0.5, "client_token": "demo-east"},
        actor=reg,
    )
    p.requests.create_request(
        {"region_id": "region-central", "medicine_id": "drug-emergency-a",
         "quantity": 20000, "needed_by_ts": "2026-10-01T02:00:00Z",
         "urgency": "critical", "stock_days": 1.0, "client_token": "demo-central"},
        actor=reg,
    )

    # 2) 建议：同一批次不会被两个地区重复承诺
    plan = p.allocations.build_plan(actor=reg, plan_ts="2026-09-30T05:10:00Z")
    show("调配建议（计划内预留，无重复承诺）", [
        {"request": r["request_id"], "planned": r["planned"],
         "shortfall": r["shortfall"],
         "lines": [{"batch": l["batch_id"], "enterprise": l["enterprise_id"],
                    "qty": l["quantity"]} for l in r["lines"]]}
        for r in plan["plan"]
    ])

    # 3) 监管确认 -> 锁定
    decisions = []
    for r in plan["plan"]:
        d = p.allocations.confirm(
            {"request_id": r["request_id"],
             "idempotency_key": f"demo-dec:{r['request_id']}",
             "lines": [{"allocation_id": l["allocation_id"],
                        "quantity": l["quantity"]} for l in r["lines"]]},
            actor=reg,
        )
        decisions.append(d)
    show("锁定结果", [
        {"decision": d["id"],
         "commitments": [{"batch": c["batch_id"], "region": c["region_id"],
                          "qty": c["quantity"]} for d in [d] for c in d["commitments"]]}
        for d in decisions
    ])

    # 4) 企业日报：理论产能/待检/缺料在制品与可承诺量分列
    report = p.reporting.submit_report(
        {"line_id": "line-alpha-01", "production_date": "2026-09-29",
         "source_ts": "2026-09-29T23:30:00Z", "submitted_ts": "2026-09-29T23:50:00Z",
         "items": [{"medicine_id": "drug-emergency-a",
                    "theoretical_capacity": 26000, "pending_qc": 10000,
                    "wip_material_short": 0, "deliverable": 5000,
                    "batches": [{"batch_id": "batch-a-rel-0929", "quantity": 5000}]}]},
        actor=alpha,
    )
    show("企业日报 v1（可承诺量逐批溯源）", {
        "version": report["version_no"], "is_late": report["is_late"],
        "items": report["items"]})

    # 5) 运输取消：华中 beta 批次 18000 中已发 10000，剩余 8000 释放回流
    beta_com = next(
        c for d in decisions for c in d["commitments"]
        if c["batch_id"] == "batch-b-rel-0929"
    )
    p.fulfillment.record_event(
        beta_com["id"],
        {"kind": "fulfill", "reason": "shipment", "quantity": 10000,
         "idempotency_key": "demo-ship-1", "ref": "WB-100"},
        actor=p.actor("u-beta"),
    )
    released = p.fulfillment.record_event(
        beta_com["id"],
        {"kind": "release", "reason": "transport_cancel", "quantity": 8000,
         "idempotency_key": "demo-cancel-1"},
        actor=p.actor("u-beta"),
    )
    show("运输取消后（已履行保留、未履行回流）",
         {"fulfilled": released["fulfilled_qty"],
          "released": released["released_qty"], "status": released["status"]})

    # 6) 检验延期后放行：新批次 10000 加入可承诺池
    p.production.record_qc(
        "batch-a-pend-0930",
        {"kind": "postpone", "new_due_ts": "2026-10-01T18:00:00Z",
         "ts": "2026-09-30T11:00:00Z", "note": "检验仪器排队"},
        actor=alpha,
    )
    p.production.record_qc(
        "batch-a-pend-0930",
        {"kind": "release", "ts": "2026-10-01T16:00:00Z"}, actor=alpha)

    # 7) 重新计算建议：回流的 8000 被再次分配。
    #    新放行的 10000（10-01 16:00 + alpha->华中 18h）晚于华中到货截止，
    #    被运输时限正确排除。
    plan2 = p.allocations.build_plan(actor=reg, plan_ts="2026-10-01T16:30:00Z")
    show("释放与放行后的重新建议", [
        {"request": r["request_id"], "already_locked": r["already_locked"],
         "planned": r["planned"], "shortfall": r["shortfall"],
         "lines": [{"batch": l["batch_id"], "qty": l["quantity"]}
                   for l in r["lines"]]}
        for r in plan2["plan"]])

    # 8) 任一承诺可全链溯源
    trace = p.views.commitment_trace(beta_com["id"], actor=reg)
    print("\n=== 承诺溯源 ===")
    print(trace["trace_summary"])
    print("建议依据：", trace["allocation_rationale"])

    db.close()


if __name__ == "__main__":
    main()
