"""HTTP API 端到端测试（标准库 urllib + 内存库 + 随机端口）。"""
from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from supply.db import Database
from supply.httpapi import create_server, serve_in_thread
from supply.platform import SupplyPlatform


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.initialize()
        self.platform = SupplyPlatform(self.db)
        self.platform.load_seed("fixtures/seed.json")
        self.server = create_server(self.platform, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = serve_in_thread(self.server)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.db.close()

    def call(self, method, path, body=None, user=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        req.add_header("Content-Type", "application/json")
        if user:
            req.add_header("X-User-Id", user)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_open(self) -> None:
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

    def test_request_plan_confirm_trace_flow(self) -> None:
        status, _ = self.call(
            "POST", "/requests",
            {
                "region_id": "region-east",
                "medicine_id": "drug-emergency-a",
                "quantity": 20000,
                "needed_by_ts": "2026-10-01T06:00:00Z",
                "urgency": "critical",
                "stock_days": 0.5,
                "client_token": "te",
            },
            "u-reg",
        )
        self.assertEqual(status, 201)
        status, plan = self.call("POST", "/allocations/plan",
                                 {"plan_ts": "2026-09-30T05:10:00Z"}, "u-reg")
        self.assertEqual(status, 200)
        lines = [
            {"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
            for l in plan["plan"][0]["lines"]
        ]
        status, decision = self.call(
            "POST", "/decisions/confirm",
            {
                "request_id": plan["plan"][0]["request_id"],
                "idempotency_key": "dec-te",
                "lines": lines,
            },
            "u-reg",
        )
        self.assertEqual(status, 200)
        self.assertEqual(decision["total_confirmed_qty"], 20000)
        cid = decision["commitments"][0]["id"]
        status, trace = self.call("GET", f"/commitments/{cid}/trace", None, "u-reg")
        self.assertEqual(status, 200)
        self.assertTrue(trace["license_in_scope"])
        self.assertEqual(trace["decision"]["idempotency_key"], "dec-te")

    def test_auth_and_role_enforcement(self) -> None:
        status, body = self.call("GET", "/dashboard/regulator")
        self.assertEqual(status, 403)
        status, body = self.call("GET", "/dashboard/regulator", None, "u-alpha")
        self.assertEqual(status, 403)
        status, body = self.call(
            "POST", "/allocations/plan", {}, "u-alpha"
        )
        self.assertEqual(status, 403)
        status, body = self.call("GET", "/requests", None, "u-alpha")
        self.assertEqual(status, 403)

    def test_enterprise_dashboard_is_scoped(self) -> None:
        status, dash = self.call("GET", "/dashboard/enterprise", None, "u-beta")
        self.assertEqual(status, 200)
        self.assertTrue(dash["batches"])
        self.assertEqual(
            {b["enterprise_id"] for b in dash["batches"]}, {"ent-beta"}
        )

    def test_bad_json_returns_400(self) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/requests",
            data=b"{not-json", method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("X-User-Id", "u-reg")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)

    def test_report_versioning_over_http(self) -> None:
        payload = {
            "line_id": "line-beta-01",
            "production_date": "2026-09-29",
            "source_ts": "2026-09-29T23:30:00Z",
            "items": [
                {
                    "medicine_id": "drug-emergency-a",
                    "theoretical_capacity": 22000,
                    "pending_qc": 0,
                    "wip_material_short": 0,
                    "deliverable": 18000,
                    "batches": [{"batch_id": "batch-b-rel-0929", "quantity": 18000}],
                }
            ],
        }
        s, v1 = self.call("POST", "/reports", payload, "u-beta")
        self.assertEqual(s, 201)
        s, v2 = self.call("POST", "/reports", payload, "u-beta")
        self.assertEqual(s, 201)
        self.assertEqual((v1["version_no"], v2["version_no"]), (1, 2))
        self.assertEqual(v2["is_correction"], 1)
        # 监管可以跨企业读取，alpha 企业不能读 beta 日报
        s, _ = self.call("GET", f"/reports?id={v1['id']}", None, "u-reg")
        self.assertEqual(s, 200)
        s, body = self.call("GET", f"/reports?id={v1['id']}", None, "u-alpha")
        self.assertEqual(s, 403)


if __name__ == "__main__":
    unittest.main()
