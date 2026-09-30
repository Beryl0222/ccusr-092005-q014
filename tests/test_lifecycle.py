"""承诺生命周期：履行不可改写；检验不合格/延期、运输取消释放未履行部分并可再分配。"""

import pytest

from supply_guard.errors import ConflictError
from conftest import NOW, make_demand


def _lock_stock(svc, actors, demand_id, qty, *, region="east", urgency="URGENT",
                coverage=1.0, needed="2026-10-04T00:00:00+00:00"):
    make_demand(svc, actors[region], id=demand_id, qty=qty, urgency=urgency,
                coverage=coverage, needed=needed)
    s = svc.compute_suggestion(actors["reg"], demand_id, at_ts=NOW)
    return svc.approve_commitment(actors["reg"],
                                  {"suggestion_id": s["suggestion_id"]}, at_ts=NOW)


def test_qc_reject_releases_unfulfilled_and_stock_is_reallocatable(svc, actors):
    # 先放行更多库存：华北待检批次 hb-a-1106（6000）放行
    svc.record_qc_event(actors["hb"], {
        "batch_id": "hb-a-1106", "event": "RELEASE", "qty_released": 6000,
        "event_ts": "2026-10-01T02:00:00+00:00", "reporter": "李工"})
    make_demand(svc, actors["east"], id="dem-east", qty=6000,
                needed="2026-10-04T00:00:00+00:00")
    # 逐项锁定华北批次（验证检验联动针对具体批次）
    appr = svc.approve_commitment(actors["reg"], {
        "demand_id": "dem-east",
        "items": [{"source_type": "STOCK", "enterprise_id": "ent-huabei",
                   "batch_id": "hb-a-1106", "line_id": "line-hb-02",
                   "qty": 6000,
                   "deliverable_ts": "2026-10-01T20:00:00+00:00"}]}, at_ts=NOW)
    cmt = appr["commitments"][0]
    assert cmt["batch_id"] == "hb-a-1106"

    # 后续抽检不合格 → 未履行部分全部释放
    res = svc.record_qc_event(actors["hb"], {
        "batch_id": "hb-a-1106", "event": "REJECT",
        "event_ts": "2026-10-01T08:00:00+00:00", "reporter": "李工",
        "reason": "无菌检查不合格"})
    assert cmt["id"] in res["released_commitments"]

    view = svc.list_commitments(actors["reg"], demand_id="dem-east")[0]
    assert view["status"] == "RELEASED"
    assert view["qty_released"] == 6000
    assert view["qty_fulfilled"] == 0
    # REJECTED 批次不再有 ATP、锁已清空
    trace = svc.batch_trace(actors["reg"], "hb-a-1106")
    assert trace["batch"]["status"] == "REJECTED"
    assert trace["total_locked"] == 0
    atp = svc.atp_overview(actors["reg"], "med-a")
    assert all(x["batch_id"] != "hb-a-1106" for x in atp["released_stock"])


def test_fulfilled_quantity_survives_release_and_cannot_be_rewritten(svc, actors):
    appr = _lock_stock(svc, actors, "dem-east", 10000)
    cmt = appr["commitments"][0]
    # 先履行 4000
    svc.fulfill_commitment(actors["reg"], {
        "commitment_id": cmt["id"], "qty": 4000,
        "event_ts": "2026-10-01T00:00:00+00:00", "note": "首批发运"})
    # 运输取消，只能释放剩余 6000
    res = svc.cancel_demand(actors["east"], "dem-east")
    assert res["released"] == [cmt["id"]]
    view = svc.list_commitments(actors["reg"], demand_id="dem-east")[0]
    assert view["qty_fulfilled"] == 4000
    assert view["qty_released"] == 6000
    assert view["qty_outstanding"] == 0
    assert view["status"] == "PARTIALLY_RELEASED"

    # 已履行不能再被释放或冲减
    with pytest.raises(ConflictError, match="没有可释放"):
        svc.release_commitment(actors["reg"],
                               {"commitment_id": cmt["id"], "reason": "try-again"})
    with pytest.raises(ConflictError, match="超出未履行余额"):
        svc.fulfill_commitment(actors["reg"],
                               {"commitment_id": cmt["id"], "qty": 1})


def test_released_stock_can_be_reallocated_to_other_region(svc, actors):
    appr = _lock_stock(svc, actors, "dem-east", 10000)
    cmt = appr["commitments"][0]
    svc.release_commitment(actors["reg"], {
        "commitment_id": cmt["id"], "reason": "TRANSPORT_CANCELLED"})

    # 华中需求重新计算建议，应重新拿到已释放的同一批次
    make_demand(svc, actors["central"], id="dem-central", qty=10000,
                urgency="NORMAL", coverage=4.0, needed="2026-10-06T00:00:00+00:00")
    s = svc.compute_suggestion(actors["reg"], "dem-central", at_ts=NOW)
    assert s["items"][0]["source_type"] == "STOCK"
    assert s["items"][0]["batch_id"] == "hd-a-2401"
    appr2 = svc.approve_commitment(actors["reg"],
                                   {"suggestion_id": s["suggestion_id"]}, at_ts=NOW)
    assert appr2["commitments"][0]["batch_id"] == "hd-a-2401"
    trace = svc.batch_trace(actors["reg"], "hd-a-2401")
    assert trace["total_locked"] == 10000
    # 旧决定仍在台账中（RELEASED），没有被删除或改写
    statuses = {c["id"]: c["status"] for c in svc.list_commitments(actors["reg"])}
    assert statuses[cmt["id"]] == "RELEASED"


def test_qc_delay_past_deadline_releases_but_within_deadline_keeps(svc, actors):
    svc.record_qc_event(actors["hb"], {
        "batch_id": "hb-a-1106", "event": "RELEASE", "qty_released": 6000,
        "event_ts": "2026-09-30T18:00:00+00:00", "reporter": "李工",
        "new_expected_release_ts": "2026-09-30T18:00:00+00:00"})
    hb_item = {"source_type": "STOCK", "enterprise_id": "ent-huabei",
               "batch_id": "hb-a-1106", "line_id": "line-hb-02", "qty": 6000,
               "deliverable_ts": "2026-10-01T02:00:00+00:00"}

    # 情形一：华中截止 10-02T00，延期到 10-01T20（+8h 运输 = 10-02T04）赶不上 → 释放
    make_demand(svc, actors["central"], id="dem-c", qty=6000, coverage=1.0,
                needed="2026-10-02T00:00:00+00:00")
    appr = svc.approve_commitment(actors["reg"],
                                  {"demand_id": "dem-c", "items": [dict(hb_item)]},
                                  at_ts=NOW)
    cid = appr["commitments"][0]["id"]
    res = svc.record_qc_event(actors["hb"], {
        "batch_id": "hb-a-1106", "event": "DELAY",
        "new_expected_release_ts": "2026-10-01T20:00:00+00:00",
        "event_ts": "2026-10-01T00:00:00+00:00", "reporter": "李工"})
    assert cid in res["released_commitments"]

    # 情形二：另一地区截止 10-05，重新锁定同一批；小幅延期到 10-03（+8h = 10-03T08）仍可达 → 保留
    make_demand(svc, actors["central"], id="dem-c2", qty=6000, coverage=3.0,
                needed="2026-10-05T00:00:00+00:00")
    appr2 = svc.approve_commitment(actors["reg"],
                                   {"demand_id": "dem-c2", "items": [dict(hb_item)]},
                                   at_ts=NOW)
    cid2 = appr2["commitments"][0]["id"]
    res2 = svc.record_qc_event(actors["hb"], {
        "batch_id": "hb-a-1106", "event": "DELAY",
        "new_expected_release_ts": "2026-10-03T00:00:00+00:00",
        "event_ts": "2026-10-02T00:00:00+00:00", "reporter": "李工"})
    assert cid2 not in res2["released_commitments"]
    view = next(c for c in svc.list_commitments(actors["reg"]) if c["id"] == cid2)
    assert view["status"] == "ACTIVE"
    assert svc.batch_trace(actors["reg"], "hb-a-1106")["total_locked"] == 6000


def test_production_commitment_full_release_cancels_plan_and_returns_materials(svc, actors):
    # 先占用完已放行库存，迫使建议进入排产
    _lock_stock(svc, actors, "dem-east-stock", 10000, region="east",
                needed="2026-10-04T00:00:00+00:00")
    make_demand(svc, actors["central"], id="dem-c", qty=20000, coverage=1.0,
                needed="2026-10-08T00:00:00+00:00")
    s = svc.compute_suggestion(actors["reg"], "dem-c", at_ts=NOW)
    appr = svc.approve_commitment(actors["reg"],
                                  {"suggestion_id": s["suggestion_id"]}, at_ts=NOW)
    prod = [c for c in appr["commitments"]
            if c["constraint_snapshot"]["source_type"] == "PRODUCTION"]
    assert prod, f"应包含生产承诺，实际：{[c['constraint_snapshot']['source_type'] for c in appr['commitments']]}"
    cmt = prod[0]
    plan_batch = cmt["batch_id"]

    svc.release_commitment(actors["reg"], {
        "commitment_id": cmt["id"], "reason": "TRANSPORT_CANCELLED"})
    trace = svc.batch_trace(actors["reg"], plan_batch)
    assert trace["batch"]["status"] == "CANCELLED"
    assert trace["total_locked"] == 0
    with svc.store.read() as conn:
        reserved = conn.execute(
            "SELECT COUNT(*) n FROM material_reservations WHERE batch_id=?",
            (plan_batch,)).fetchone()["n"]
    assert reserved == 0

    # 物料归还后可以再次排产同样数量
    s2 = svc.compute_suggestion(actors["reg"], "dem-c", at_ts=NOW)
    assert any(i["source_type"] == "PRODUCTION" for i in s2["items"])
