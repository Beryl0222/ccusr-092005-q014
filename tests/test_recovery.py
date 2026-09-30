"""故障恢复：原子提交 + 幂等重放，恢复后不得重复锁定。"""

import threading

import pytest

from supply_guard.seed import load_seed, seed_store
from supply_guard.service import Actor, Service
from supply_guard.store import Store
from conftest import NOW

REG = Actor("regulator", "REGULATOR")
EAST = Actor("region-east", "REGION", region_id="region-east")


def _fresh_service(db_path, seed_path):
    """模拟进程重启：打开同一个文件数据库，不重新播种。"""
    store = Store(db_path)
    return Service(store)


def test_replay_after_crash_does_not_double_lock(tmp_path):
    db = tmp_path / "guard.db"
    seed = str(tmp_path / "seed.json")
    import shutil
    shutil.copy("fixtures/seed.json", seed)

    store = Store(db)
    seed_store(store, load_seed(seed))
    svc1 = Service(store)
    svc1.create_demand(EAST, {
        "id": "dem-crash", "medicine_id": "med-a", "qty": 7000,
        "urgency": "URGENT", "coverage_days": 1.0,
        "needed_by_ts": "2026-10-04T00:00:00+00:00"})
    s = svc1.compute_suggestion(REG, "dem-crash", at_ts=NOW)
    payload = {"suggestion_id": s["suggestion_id"], "approval_note": "崩溃前确认"}

    # 进程 1 提交成功，但响应在送达客户端前“崩溃”
    r1 = svc1.approve_commitment(REG, payload, idem_key="approve-crash-1", at_ts=NOW)
    first_ids = [c["id"] for c in r1["commitments"]]

    # 进程重启：客户端用同一幂等键重放
    svc2 = _fresh_service(db, seed)
    r2 = svc2.approve_commitment(REG, payload, idem_key="approve-crash-1", at_ts=NOW)
    assert r2["replayed"] is True
    assert [c["id"] for c in r2["commitments"]] == first_ids

    # 只有一把锁、锁定量不超过批次放行量
    trace = svc2.batch_trace(REG, "hd-a-2401")
    assert trace["total_locked"] == 7000
    assert len(trace["locks"]) == 1
    with svc2.store.read() as conn:
        n_cmt = conn.execute("SELECT COUNT(*) n FROM commitments").fetchone()["n"]
        n_locks = conn.execute("SELECT COUNT(*) n FROM batch_locks").fetchone()["n"]
        n_idem = conn.execute("SELECT COUNT(*) n FROM idempotency").fetchone()["n"]
    assert n_cmt == 1
    assert n_locks == 1
    assert n_idem == 1  # 仅承诺使用了幂等键，重放不新增记录


def test_same_idempotency_key_different_payload_rejected(tmp_path):
    from supply_guard.errors import IdempotencyReplayed
    db = tmp_path / "guard2.db"
    store = Store(db)
    seed_store(store, load_seed("fixtures/seed.json"))
    svc = Service(store)
    svc.create_demand(EAST, {
        "id": "dem-x", "medicine_id": "med-a", "qty": 1000,
        "urgency": "URGENT", "coverage_days": 1.0,
        "needed_by_ts": "2026-10-04T00:00:00+00:00"}, idem_key="d1")
    with pytest.raises(IdempotencyReplayed):
        svc.create_demand(EAST, {
            "id": "dem-x2", "medicine_id": "med-a", "qty": 2000,
            "urgency": "NORMAL", "coverage_days": 2.0,
            "needed_by_ts": "2026-10-05T00:00:00+00:00"}, idem_key="d1")


def test_concurrent_threads_against_file_db_after_reopen(tmp_path):
    """重启后的文件库上，两线程并发确认同一批次，仍然恰好一把锁。"""
    from supply_guard.errors import ConflictError
    db = tmp_path / "guard3.db"
    store = Store(db)
    seed_store(store, load_seed("fixtures/seed.json"))
    svc = _fresh_service(db, "fixtures/seed.json")
    svc.create_demand(Actor("region-east", "REGION", region_id="region-east"), {
        "id": "dem-fe", "medicine_id": "med-a", "qty": 8000,
        "urgency": "URGENT", "coverage_days": 1.0,
        "needed_by_ts": "2026-10-04T00:00:00+00:00"})
    svc.create_demand(Actor("region-central", "REGION", region_id="region-central"), {
        "id": "dem-fc", "medicine_id": "med-a", "qty": 8000,
        "urgency": "URGENT", "coverage_days": 1.0,
        "needed_by_ts": "2026-10-04T00:00:00+00:00"})
    s1 = svc.compute_suggestion(REG, "dem-fe", at_ts=NOW)
    s2 = svc.compute_suggestion(REG, "dem-fc", at_ts=NOW)

    outcomes = []

    def worker(sid, key):
        try:
            r = svc.approve_commitment(REG, {"suggestion_id": sid},
                                       idem_key=key, at_ts=NOW)
            outcomes.append(("ok", r["commitments"][0]["id"]))
        except ConflictError:
            outcomes.append(("conflict", None))

    barrier = threading.Barrier(2)
    def w(sid, key):
        barrier.wait()
        worker(sid, key)
    t1 = threading.Thread(target=w, args=(s1["suggestion_id"], "k1"))
    t2 = threading.Thread(target=w, args=(s2["suggestion_id"], "k2"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(o[0] for o in outcomes) == ["conflict", "ok"]
    assert svc.batch_trace(REG, "hd-a-2401")["total_locked"] == 8000
