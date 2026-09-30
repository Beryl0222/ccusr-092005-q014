"""建议评分、承诺可追溯（真实批次+约束+批准依据）与建议/锁定分离。"""

from conftest import NOW, make_demand


def test_urgency_coverage_transport_drive_score(svc, actors):
    make_demand(svc, actors["east"], id="dem-urgent", qty=5000,
                urgency="URGENT", coverage=0.5,
                needed="2026-10-02T00:00:00+00:00")
    make_demand(svc, actors["central"], id="dem-normal", qty=5000,
                urgency="NORMAL", coverage=6.0,
                needed="2026-10-10T00:00:00+00:00")
    s_urg = svc.compute_suggestion(actors["reg"], "dem-urgent", at_ts=NOW)
    s_nor = svc.compute_suggestion(actors["reg"], "dem-normal", at_ts=NOW)
    assert s_urg["score"] > s_nor["score"]
    # 评分依据可读
    assert "紧急度=URGENT" in s_urg["rationale"]
    assert "建议仅供参考" in s_urg["rationale"]


def test_suggestion_does_not_lock(svc, actors):
    make_demand(svc, actors["east"], id="dem-e", qty=5000, coverage=1.0,
                needed="2026-10-04T00:00:00+00:00")
    svc.compute_suggestion(actors["reg"], "dem-e", at_ts=NOW)
    svc.compute_suggestion(actors["reg"], "dem-e", at_ts=NOW)
    # 多次计算建议，库存仍 100% 可承诺
    atp = svc.atp_overview(actors["reg"], "med-a")
    assert atp["atp_total"] == 10000
    assert svc.list_commitments(actors["reg"]) == []


def test_recomputing_suggestion_supersedes_old(svc, actors):
    make_demand(svc, actors["east"], id="dem-e", qty=5000, coverage=1.0,
                needed="2026-10-04T00:00:00+00:00")
    s1 = svc.compute_suggestion(actors["reg"], "dem-e", at_ts=NOW)
    s2 = svc.compute_suggestion(actors["reg"], "dem-e", at_ts=NOW)
    from supply_guard.errors import ConflictError
    import pytest
    with pytest.raises(ConflictError, match="建议已被新版本替代"):
        svc.approve_commitment(actors["reg"],
                               {"suggestion_id": s1["suggestion_id"]}, at_ts=NOW)
    svc.approve_commitment(actors["reg"],
                           {"suggestion_id": s2["suggestion_id"]}, at_ts=NOW)


def test_commitment_points_to_real_batch_constraints_and_approval(svc, actors):
    make_demand(svc, actors["east"], id="dem-e", qty=6000, coverage=1.0,
                needed="2026-10-04T00:00:00+00:00")
    s = svc.compute_suggestion(actors["reg"], "dem-e", at_ts=NOW)
    appr = svc.approve_commitment(actors["reg"], {
        "suggestion_id": s["suggestion_id"],
        "approval_note": "国家调度令第7号"}, at_ts=NOW)
    cmt = appr["commitments"][0]

    # 真实批次
    trace = svc.batch_trace(actors["reg"], cmt["batch_id"])
    assert trace["batch"]["id"] == cmt["batch_id"]

    # 约束与批准依据快照
    snap = cmt["constraint_snapshot"]
    assert snap["approval"]["by"] == "regulator"
    assert snap["approval"]["note"] == "国家调度令第7号"
    assert "运输时限" in snap["hard_constraints"]
    assert cmt["approved_by"] == "regulator"
    assert cmt["source_suggestion_id"] == s["suggestion_id"]

    # 事件链：LOCKED 首事件
    assert cmt["events"][0]["event_type"] == "LOCKED"
    assert cmt["events"][0]["qty"] == 6000


def test_demand_status_progresses(svc, actors):
    make_demand(svc, actors["east"], id="dem-e", qty=10000, coverage=1.0,
                needed="2026-10-04T00:00:00+00:00")
    s = svc.compute_suggestion(actors["reg"], "dem-e", at_ts=NOW)
    svc.approve_commitment(actors["reg"],
                           {"suggestion_id": s["suggestion_id"]}, at_ts=NOW)
    with svc.store.read() as conn:
        st = conn.execute("SELECT status FROM demands WHERE id='dem-e'").fetchone()["status"]
    assert st == "PARTIAL"  # 已锁定未履行
    cid = svc.list_commitments(actors["reg"])[0]["id"]
    svc.fulfill_commitment(actors["reg"],
                           {"commitment_id": cid, "qty": 10000,
                            "event_ts": "2026-10-02T00:00:00+00:00"})
    with svc.store.read() as conn:
        st = conn.execute("SELECT status FROM demands WHERE id='dem-e'").fetchone()["status"]
    assert st == "FULFILLED"
