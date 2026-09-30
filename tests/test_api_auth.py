"""数据隔离与 HTTP API 冒烟。"""

import json
import urllib.error
import urllib.request

import pytest

from supply_guard.api import create_server, build_service
from conftest import NOW, make_demand

TOK = {
    "reg": "tok-regulator",
    "hd": "tok-huadong",
    "hb": "tok-huabei",
    "east": "tok-east",
    "central": "tok-central",
}


@pytest.fixture
def server(svc):
    httpd = create_server("127.0.0.1", 0, svc)
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def call(server, method, path, token=None, body=None, idem=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if idem:
        req.add_header("Idempotency-Key", idem)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_requires_token(server):
    status, body = call(server, "GET", "/api/atp?medicine_id=med-a")
    assert status == 403
    assert body["error"] == "PermissionError"


def test_enterprise_sees_only_own_data(server):
    # 华东不能看华北批次
    status, _ = call(server, "GET", "/api/batches/hb-a-1106", TOK["hd"])
    assert status == 403
    status, body = call(server, "GET", "/api/batches/hd-a-2401", TOK["hd"])
    assert status == 200
    assert body["batch"]["enterprise_id"] == "ent-huadong"

    # ATP 中只有本企业
    status, atp = call(server, "GET", "/api/atp?medicine_id=med-a", TOK["hb"])
    assert status == 200
    enterprises = {x["enterprise_id"] for x in atp["released_stock"]}
    assert enterprises <= {"ent-huabei"}


def test_regulator_sees_cross_enterprise_board(server):
    status, body = call(server, "GET", "/api/board", TOK["reg"])
    assert status == 200
    mids = {m["medicine_id"] for m in body["medicines"]}
    assert {"med-a", "med-b"} <= mids
    # 企业不能看态势板
    status, _ = call(server, "GET", "/api/board", TOK["hd"])
    assert status == 403


def test_region_only_manages_own_demand(server):
    payload = {"id": "dem-http-1", "medicine_id": "med-a", "qty": 1000,
               "urgency": "URGENT", "coverage_days": 1.0,
               "needed_by_ts": "2026-10-04T00:00:00+00:00"}
    status, _ = call(server, "POST", "/api/demands", TOK["hd"], payload)
    assert status == 403
    status, created = call(server, "POST", "/api/demands", TOK["east"], payload)
    assert status == 201
    # 华中区不能取消华东需求
    status, _ = call(server, "POST", "/api/demands/dem-http-1/cancel", TOK["central"])
    assert status == 403


def test_region_atp_requires_own_region_filter(server):
    # 区域必须按本区域过滤，不得无条件拉取跨企业库存
    status, _ = call(server, "GET", "/api/atp?medicine_id=med-a", TOK["east"])
    assert status == 403
    status, body = call(server, "GET",
                        "/api/atp?medicine_id=med-a&region_id=region-east", TOK["east"])
    assert status == 200
    # 华东通道：华东已放行批次可见且带运输后交付时间
    assert body["released_stock"][0]["batch_id"] == "hd-a-2401"
    # 不能冒充其他区域
    status, _ = call(server, "GET",
                     "/api/atp?medicine_id=med-a&region_id=region-central", TOK["east"])
    assert status == 403


def test_full_flow_over_http_and_idempotency_header(server):
    demand = {"id": "dem-flow", "medicine_id": "med-a", "qty": 6000,
              "urgency": "URGENT", "coverage_days": 1.0,
              "needed_by_ts": "2026-10-04T00:00:00+00:00"}
    status, _ = call(server, "POST", "/api/demands", TOK["east"], demand,
                     idem="dem-flow-1")
    assert status == 201
    # 同键重放
    status, replay = call(server, "POST", "/api/demands", TOK["east"], demand,
                          idem="dem-flow-1")
    assert status == 201 and replay.get("replayed") is True

    status, sug = call(server, "POST", "/api/suggestions", TOK["reg"],
                       {"demand_id": "dem-flow", "at_ts": NOW})
    assert status == 201
    sid = sug["suggestion_id"]

    # 企业不能确认锁定
    status, _ = call(server, "POST", "/api/commitments/approve", TOK["hd"],
                     {"suggestion_id": sid})
    assert status == 403

    status, appr = call(server, "POST", "/api/commitments/approve", TOK["reg"],
                        {"suggestion_id": sid, "at_ts": NOW,
                         "approval_note": "HTTP调度令"}, idem="appr-flow-1")
    assert status == 201
    cid = appr["commitments"][0]["id"]

    # 同键重放不产生第二把锁
    status, appr2 = call(server, "POST", "/api/commitments/approve", TOK["reg"],
                         {"suggestion_id": sid, "at_ts": NOW,
                          "approval_note": "HTTP调度令"}, idem="appr-flow-1")
    assert status == 201 and appr2.get("replayed") is True
    status, trace = call(server, "GET", f"/api/batches/hd-a-2401", TOK["reg"])
    assert trace["total_locked"] == 6000

    status, ful = call(server, "POST", f"/api/commitments/{cid}/fulfill",
                       TOK["reg"], {"qty": 6000,
                                    "event_ts": "2026-10-02T00:00:00+00:00"})
    assert status == 200
    assert ful["status"] == "FULFILLED"
