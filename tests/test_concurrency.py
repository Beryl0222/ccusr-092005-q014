"""并发承诺：同一批次/产线绝不被两个地区锁定两次。"""

import threading

import pytest

from supply_guard.errors import ConflictError
from conftest import NOW, make_demand


def test_same_released_batch_cannot_be_promised_twice(svc, actors):
    """两地区同一小时都请求 10000，已放行批次只有 10000。"""
    make_demand(svc, actors["east"], id="dem-e", qty=10000, coverage=1.0,
                needed="2026-10-04T00:00:00+00:00")
    make_demand(svc, actors["central"], id="dem-c", qty=10000, coverage=5.0,
                urgency="NORMAL", needed="2026-10-06T00:00:00+00:00")
    s_e = svc.compute_suggestion(actors["reg"], "dem-e", at_ts=NOW)
    s_c = svc.compute_suggestion(actors["reg"], "dem-c", at_ts=NOW)
    # 建议阶段两条都指向同一批次（建议不锁定）
    assert {i["batch_id"] for i in s_e["items"]} == {"hd-a-2401"}
    assert {i["batch_id"] for i in s_c["items"]} == {"hd-a-2401"}

    svc.approve_commitment(actors["reg"], {"suggestion_id": s_e["suggestion_id"]},
                           at_ts=NOW)
    # 第二个确认在锁定期重检余量，必须失败而不是超卖
    with pytest.raises(ConflictError, match="可承诺量不足"):
        svc.approve_commitment(actors["reg"], {"suggestion_id": s_c["suggestion_id"]},
                               at_ts=NOW)

    # 重新计算后，第二地区改为生产承诺，批次不再重复
    s_c2 = svc.compute_suggestion(actors["reg"], "dem-c", at_ts=NOW)
    approved = svc.approve_commitment(actors["reg"],
                                      {"suggestion_id": s_c2["suggestion_id"]}, at_ts=NOW)
    batch_ids = [c["batch_id"] for c in approved["commitments"]]
    assert "hd-a-2401" not in batch_ids
    assert len(batch_ids) == len(set(batch_ids))

    # 台账层面：每个批次的锁定总量不超过放行量
    traced = svc.batch_trace(actors["reg"], "hd-a-2401")
    assert traced["total_locked"] <= traced["batch"]["qty_released"] + 1e-6
    assert traced["total_locked"] == 10000


def test_concurrent_approval_threads_never_double_lock(svc, actors):
    """多线程同时确认同一批次，恰好一个成功，其余冲突，总锁定不超量。"""
    make_demand(svc, actors["east"], id="dem-e", qty=6000, coverage=1.0,
                needed="2026-10-04T00:00:00+00:00")
    make_demand(svc, actors["central"], id="dem-c", qty=6000, coverage=2.0,
                needed="2026-10-04T00:00:00+00:00")
    s_e = svc.compute_suggestion(actors["reg"], "dem-e", at_ts=NOW)
    s_c = svc.compute_suggestion(actors["reg"], "dem-c", at_ts=NOW)

    results = []

    def approve(sid, barrier):
        barrier.wait()
        try:
            r = svc.approve_commitment(
                actors["reg"], {"suggestion_id": sid}, at_ts=NOW)
            results.append(("ok", [c["id"] for c in r["commitments"]]))
        except ConflictError as exc:
            results.append(("conflict", str(exc)))

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=approve, args=(s_e["suggestion_id"], barrier))
    t2 = threading.Thread(target=approve, args=(s_c["suggestion_id"], barrier))
    t1.start(); t2.start(); t1.join(); t2.join()

    oks = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "conflict"]
    assert len(oks) == 1
    assert len(conflicts) == 1

    traced = svc.batch_trace(actors["reg"], "hd-a-2401")
    assert traced["total_locked"] <= 10000 + 1e-6
    # 成功方只锁 6000，剩余 4000 仍可再分配
    assert traced["total_locked"] == 6000
    atp = svc.atp_overview(actors["reg"], "med-a")
    free = [x for x in atp["released_stock"] if x["batch_id"] == "hd-a-2401"][0]
    assert free["qty_available"] == 4000


def test_partial_batch_split_between_regions(svc, actors):
    """同一批次可拆分给两地区，但合计不超量。"""
    make_demand(svc, actors["east"], id="dem-e", qty=4000, coverage=1.0,
                needed="2026-10-04T00:00:00+00:00")
    make_demand(svc, actors["central"], id="dem-c", qty=4000, coverage=1.0,
                needed="2026-10-04T00:00:00+00:00")
    s_e = svc.compute_suggestion(actors["reg"], "dem-e", at_ts=NOW)
    svc.approve_commitment(actors["reg"], {"suggestion_id": s_e["suggestion_id"]},
                           at_ts=NOW)
    s_c = svc.compute_suggestion(actors["reg"], "dem-c", at_ts=NOW)
    assert s_c["items"][0]["batch_id"] == "hd-a-2401"
    assert s_c["items"][0]["qty"] == 4000
    svc.approve_commitment(actors["reg"], {"suggestion_id": s_c["suggestion_id"]},
                           at_ts=NOW)
    traced = svc.batch_trace(actors["reg"], "hd-a-2401")
    assert traced["total_locked"] == 8000
    assert len(traced["locks"]) == 2
