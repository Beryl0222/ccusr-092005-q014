"""企业日报：来源时间、责任人、迟报、更正新版本、批次台账联动。"""

import pytest

from supply_guard.errors import PermissionError, ValidationError
from conftest import NOW


def _report(**over):
    base = {
        "report_date": "2026-09-30",
        "source_ts": "2026-09-30T08:00:00+00:00",
        "kind": "DAILY",
        "reporter": "王工",
        "lines": [{"line_id": "line-hd-07", "theoretical_capacity": 26000,
                   "awaiting_qc_qty": 5000, "wip_qty": 3000}],
        "materials": [{"material_id": "active-a", "on_hand_qty": 210.0}],
        "batches": [],
    }
    base.update(over)
    return base


def test_report_records_source_time_reporter_and_late_flag(svc, actors):
    # 来源时间比接收时间早 30 小时 → 迟报
    r = svc.submit_report(actors["hd"], _report(
        source_ts="2026-09-29T00:00:00+00:00"),
        received_ts="2026-09-30T06:00:00+00:00")
    assert r["late"] == 1
    assert r["reporter"] == "王工"
    assert r["source_ts"] == "2026-09-29T00:00:00+00:00"

    r2 = svc.submit_report(actors["hd"], _report(
        report_date="2026-09-30", source_ts="2026-09-30T05:00:00+00:00"),
        received_ts="2026-09-30T06:00:00+00:00")
    assert r2["late"] == 0


def test_correction_creates_new_version_and_chain(svc, actors):
    r1 = svc.submit_report(actors["hd"], _report(),
                           received_ts="2026-09-30T09:00:00+00:00")
    r2 = svc.submit_report(actors["hd"], _report(
        kind="CORRECTION",
        lines=[{"line_id": "line-hd-07", "theoretical_capacity": 24000,
                "awaiting_qc_qty": 5000, "wip_qty": 3000}],
        note="复核后理论产能修正"),
        received_ts="2026-09-30T15:00:00+00:00")
    assert r2["id"] > r1["id"]
    assert r2["supersedes_id"] == r1["id"]
    assert r2["kind"] == "CORRECTION"

    versions = svc.get_report_versions(actors["hd"], "2026-09-30")
    assert [v["kind"] for v in versions] == ["DAILY", "CORRECTION"]

    # 历史版本明细不被改写
    with svc.store.read() as conn:
        old = conn.execute(
            "SELECT theoretical_capacity FROM report_line_summaries WHERE version_id=?",
            (r1["id"],)).fetchone()
        new = conn.execute(
            "SELECT theoretical_capacity FROM report_line_summaries WHERE version_id=?",
            (r2["id"],)).fetchone()
    assert old["theoretical_capacity"] == 26000
    assert new["theoretical_capacity"] == 24000


def test_correction_without_prior_report_rejected(svc, actors):
    with pytest.raises(ValidationError, match="更正必须针对已存在"):
        svc.submit_report(actors["hd"], _report(
            report_date="2026-10-01", kind="CORRECTION"))


def test_enterprise_cannot_report_other_enterprise_line(svc, actors):
    with pytest.raises(PermissionError, match="属于其他企业"):
        svc.submit_report(actors["hd"], _report(
            lines=[{"line_id": "line-hb-02", "theoretical_capacity": 1,
                    "awaiting_qc_qty": 0, "wip_qty": 0}]))


def test_region_and_other_enterprise_cannot_report(svc, actors):
    with pytest.raises(PermissionError):
        svc.submit_report(actors["east"], _report())
    with pytest.raises(PermissionError):
        svc.submit_report(actors["hb"], _report())


def test_report_batch_lifecycle_feeds_atp_only_when_released(svc, actors):
    # 新批次先上报为在制 → 不可承诺
    svc.submit_report(actors["hd"], _report(
        batches=[{"id": "hd-a-2501", "line_id": "line-hd-07", "medicine_id": "med-a",
                  "status": "IN_PROGRESS", "planned_qty": 5000,
                  "planned_start": "2026-09-30T00:00:00+00:00",
                  "planned_finish": "2026-10-01T00:00:00+00:00",
                  "expected_release_ts": "2026-10-02T00:00:00+00:00"}]),
        received_ts="2026-09-30T09:00:00+00:00")
    atp1 = svc.atp_overview(actors["hd"], "med-a")
    assert atp1["atp_total"] == 10000  # 仍只有 2401

    # 企业更正：批次放行 5000
    svc.submit_report(actors["hd"], _report(
        kind="CORRECTION",
        batches=[{"id": "hd-a-2501", "line_id": "line-hd-07", "medicine_id": "med-a",
                  "status": "RELEASED", "planned_qty": 5000,
                  "planned_start": "2026-09-30T00:00:00+00:00",
                  "planned_finish": "2026-10-01T00:00:00+00:00",
                  "expected_release_ts": "2026-10-01T06:00:00+00:00",
                  "qty_released": 5000}]),
        received_ts="2026-10-01T08:00:00+00:00")
    atp2 = svc.atp_overview(actors["hd"], "med-a")
    assert atp2["atp_total"] == 15000

    # 批次事件流水可追溯
    trace = svc.batch_trace(actors["hd"], "hd-a-2501")
    types = [e["event_type"] for e in trace["events"]]
    assert types[0] == "CREATED"
    assert "QC_RELEASED" in types


def test_system_plan_batch_cannot_be_overwritten_by_report(svc, actors):
    from conftest import make_demand
    # 先占用完放行库存，使华中需求只能走排产
    make_demand(svc, actors["east"], id="dem-e0", qty=10000, coverage=0.0,
                needed="2026-10-04T00:00:00+00:00")
    s0 = svc.compute_suggestion(actors["reg"], "dem-e0", at_ts=NOW)
    svc.approve_commitment(actors["reg"], {"suggestion_id": s0["suggestion_id"]},
                           at_ts=NOW)
    make_demand(svc, actors["central"], id="dem-c", qty=8000, coverage=1.0,
                needed="2026-10-08T00:00:00+00:00")
    s = svc.compute_suggestion(actors["reg"], "dem-c", at_ts=NOW)
    appr = svc.approve_commitment(actors["reg"],
                                  {"suggestion_id": s["suggestion_id"]}, at_ts=NOW)
    plan_batch = appr["commitments"][0]["batch_id"]
    assert appr["commitments"][0]["constraint_snapshot"]["source_type"] == "PRODUCTION"
    with pytest.raises(ValidationError, match="系统排产批次"):
        svc.submit_report(actors["hd"], _report(
            batches=[{"id": plan_batch, "line_id": "line-hd-07",
                      "medicine_id": "med-a", "status": "RELEASED",
                      "planned_qty": 99999, "qty_released": 99999}]))


def test_idempotent_report_resubmit_returns_same_version(svc, actors):
    payload = _report()
    r1 = svc.submit_report(actors["hd"], payload, idem_key="rep-1",
                           received_ts="2026-09-30T09:00:00+00:00")
    r2 = svc.submit_report(actors["hd"], payload, idem_key="rep-1",
                           received_ts="2026-09-30T10:00:00+00:00")
    assert r1["id"] == r2["id"]
    assert r2["replayed"] is True
