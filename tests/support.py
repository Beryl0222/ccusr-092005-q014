"""测试公共夹具：内存库 + 种子装载。"""
from __future__ import annotations

import unittest

from supply.db import Database
from supply.platform import SupplyPlatform

SEED = "fixtures/seed.json"


class PlatformTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.initialize()
        self.platform = SupplyPlatform(self.db)
        self.platform.load_seed(SEED)
        self.reg = self.platform.actor("u-reg")
        self.alpha = self.platform.actor("u-alpha")
        self.beta = self.platform.actor("u-beta")

    def tearDown(self) -> None:
        self.db.close()

    # -- 便捷构造 ---------------------------------------------------------

    def _create_requests(self) -> dict[str, str]:
        """两地区同一小时提出急救针A保供申请，返回 request_id 映射。"""
        east = self.platform.requests.create_request(
            {
                "region_id": "region-east",
                "medicine_id": "drug-emergency-a",
                "quantity": 25000,
                "needed_by_ts": "2026-10-01T06:00:00Z",
                "urgency": "critical",
                "stock_days": 0.5,
                "client_token": "tok-east",
            },
            actor=self.reg,
        )
        central = self.platform.requests.create_request(
            {
                "region_id": "region-central",
                "medicine_id": "drug-emergency-a",
                "quantity": 20000,
                "needed_by_ts": "2026-10-01T02:00:00Z",
                "urgency": "critical",
                "stock_days": 1.0,
                "client_token": "tok-central",
            },
            actor=self.reg,
        )
        return {"east": east["id"], "central": central["id"]}

    def _plan_and_confirm_both(self, plan_ts: str = "2026-09-30T05:10:00Z"):
        """生成建议并让监管确认两地区全部建议行，返回 (plan, decisions)。"""
        plan = self.platform.allocations.build_plan(actor=self.reg, plan_ts=plan_ts)
        decisions = {}
        for req in plan["plan"]:
            lines = [
                {"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
                for l in req["lines"]
            ]
            key = f"dec:{req['request_id']}"
            decisions[req["request_id"]] = self.platform.allocations.confirm(
                {
                    "request_id": req["request_id"],
                    "idempotency_key": key,
                    "lines": lines,
                },
                actor=self.reg,
            )
        return plan, decisions
