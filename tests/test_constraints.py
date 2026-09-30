"""不可突破约束：许可范围、换线清洁、设备停机、原料、检验周期、运输时限。"""

import pytest

from supply_guard import timeutil
from supply_guard.errors import ConflictError, ValidationError
from conftest import NOW, make_demand


def _line(svc, line_id):
    with svc.store.read() as conn:
        return conn.execute("SELECT * FROM production_lines WHERE id=?",
                            (line_id,)).fetchone()


def test_license_scope_is_hard_constraint(svc, actors):
    """med-b 只有 line-hd-07 有许可，其他产线排产必须拒绝。"""
    with svc.store.read() as conn:
        for line_id in ("line-hd-08", "line-hb-02"):
            slot = svc._find_slot(conn, _line(svc, line_id), "med-b", 1000, NOW,
                                  "2026-10-10T00:00:00+00:00")
            assert slot is None
        slot = svc._find_slot(conn, _line(svc, "line-hd-07"), "med-b", 1000, NOW,
                              "2026-10-10T00:00:00+00:00")
        assert slot is not None


def test_changeover_cleaning_is_counted(svc, actors):
    """line-hd-07 当前最后批次为 med-a，紧邻排 med-a 免清洁；
    若游标越过 med-b 批次后再排 med-a，必须计入 14h 换线清洁。"""
    with svc.store.read() as conn:
        line = _line(svc, "line-hd-07")
        # 紧接现在：上一批是 med-a（hd-a-2402），换线 0
        slot = svc._find_slot(conn, line, "med-a", 1000, NOW,
                              "2026-10-10T00:00:00+00:00")
        assert slot["changeover_hours"] == 0
        # 从 10-03T00（med-b 批次与检修窗口之后）起排：最近生产品种是 med-b → 14h 清洁
        after = "2026-10-03T00:00:00+00:00"
        slot_b = svc._find_slot(conn, line, "med-a", 1000, after,
                                "2026-10-08T00:00:00+00:00")
        assert slot_b["changeover_hours"] == 14
        assert timeutil.parse(slot_b["production_start"]) == \
            timeutil.parse(after) + __import__("datetime").timedelta(hours=14)


def test_downtime_blocks_slot(svc, actors):
    """line-hd-07 在 10-02 全天检修：生产槽必须整体避开。"""
    with svc.store.read() as conn:
        line = _line(svc, "line-hd-07")
        after = "2026-10-01T12:00:00+00:00"
        # 26000 支需要 24h 生产 + 14h 换线 + 24h 检验，检修把槽推到 10-03 之后
        slot = svc._find_slot(conn, line, "med-a", 26000, after,
                              "2026-10-08T00:00:00+00:00")
        assert slot is not None
        start = timeutil.parse(slot["production_start"])
        finish = timeutil.parse(slot["production_finish"])
        dt_s = timeutil.parse("2026-10-02T00:00:00+00:00")
        dt_e = timeutil.parse("2026-10-03T00:00:00+00:00")
        assert not (start < dt_e and finish > dt_s), "生产槽与检修窗口相交"


def test_capacity_spans_multiple_days_and_deadline(svc, actors):
    """跨日生产：40000 支在 26000/日 产线上需 36.9h；截止太紧则不可承诺。"""
    with svc.store.read() as conn:
        line = _line(svc, "line-hd-08")  # 20000/日，换线 8h，检验 24h
        # 40000 支 = 48h 生产
        feasible = svc._find_slot(conn, line, "med-a", 40000, NOW,
                                  "2026-10-06T00:00:00+00:00")
        assert feasible is not None
        assert timeutil.hours_between(NOW, feasible["production_finish"]) > 40
        too_tight = svc._find_slot(conn, line, "med-a", 40000, NOW,
                                   "2026-10-02T00:00:00+00:00")
        assert too_tight is None


def test_material_bom_is_hard_constraint(svc, actors):
    """华北 active-a 仅 30kg（0.001/支 → 最多 30000 支），40000 支缺料不可排。"""
    with svc.store.read() as conn:
        ok, short = svc._check_materials(conn, "ent-huabei", "med-a", 30000)
        assert ok and short == []
        ok, short = svc._check_materials(conn, "ent-huabei", "med-a", 40000)
        assert not ok
        assert short[0]["material_id"] == "active-a"
        # 华东库存充足
        ok, _ = svc._check_materials(conn, "ent-huadong", "med-a", 40000)
        assert ok


def test_reported_material_short_wip_is_not_committable(svc, actors):
    """缺料在制品 hb-a-1107 只能出现在 wip 并标注缺料，绝不进入 ATP。"""
    atp = svc.atp_overview(actors["reg"], "med-a")
    wip = {w["batch_id"]: w for w in atp["wip"]}
    assert "hb-a-1107" in wip
    assert wip["hb-a-1107"]["material_short"] is True
    assert wip["hb-a-1107"]["committable"] is False
    assert atp["atp_total"] == 10000


def test_transport_lane_and_deadline_gate_suggestions(svc, actors):
    """无运输通道或加运输后赶不上截止的候选不得进入建议。"""
    # 华东→华东 6h；已放行批次 09-29 就绪，截止 09-29（过去式紧）则不可达
    make_demand(svc, actors["east"], id="dem-tight", qty=5000, coverage=0.0,
                needed="2026-09-29T09:00:00+00:00")
    s = svc.compute_suggestion(actors["reg"], "dem-tight", at_ts=NOW)
    assert all(i["batch_id"] != "hd-a-2401" for i in s["items"])


def test_confirm_revalidates_all_constraints(svc, actors):
    """建议生成后情况变化（原料被扣减），确认时必须重新校验并拒绝。"""
    make_demand(svc, actors["central"], id="dem-c", qty=20000, coverage=1.0,
                needed="2026-10-08T00:00:00+00:00")
    s = svc.compute_suggestion(actors["reg"], "dem-c", at_ts=NOW)
    prod = [i for i in s["items"] if i["source_type"] == "PRODUCTION"]
    assert prod
    # 调度期间该候选企业原料被挪走（建议过期），确认时必须重新校验并拒绝
    item = prod[0]
    with svc.store.transaction() as conn:
        conn.execute(
            "UPDATE material_stock_current SET on_hand_qty=0"
            " WHERE enterprise_id=? AND material_id='active-a'",
            (item["enterprise_id"],))
    with pytest.raises(ConflictError, match="原料不足"):
        svc.approve_commitment(actors["reg"], {
            "demand_id": "dem-c",
            "items": [item],
        }, at_ts=NOW)
