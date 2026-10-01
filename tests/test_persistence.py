"""持久化与故障恢复：进程重开后数据一致，幂等重放不产生重复锁定。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from supply.db import Database
from supply.platform import SupplyPlatform


class PersistenceRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)  # Database 需要全新文件以执行建库
        self.db = Database(self.path)
        self.db.initialize()
        self.platform = SupplyPlatform(self.db)
        self.platform.load_seed("fixtures/seed.json")
        self.reg = self.platform.actor("u-reg")

    def tearDown(self) -> None:
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(self.path + suffix)
            if p.exists():
                p.unlink()

    def _lock_east(self):
        self.platform.requests.create_request(
            {
                "region_id": "region-east",
                "medicine_id": "drug-emergency-a",
                "quantity": 18000,
                "needed_by_ts": "2026-10-01T06:00:00Z",
                "urgency": "critical",
                "stock_days": 0.5,
                "client_token": "tok-east",
            },
            actor=self.reg,
        )
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        lines = [
            {"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
            for l in plan["plan"][0]["lines"]
        ]
        decision = self.platform.allocations.confirm(
            {
                "request_id": "req:tok-east",
                "idempotency_key": "dec-crash-1",
                "lines": lines,
            },
            actor=self.reg,
        )
        return plan, decision

    def test_committed_locks_survive_reopen(self) -> None:
        plan, decision = self._lock_east()
        self.assertEqual(len(decision["commitments"]), 1)
        self.db.close()

        db2 = Database(self.path)
        p2 = SupplyPlatform(db2)
        try:
            batch = p2.production.get_batch(
                "batch-a-rel-0928", actor=p2.actor("u-reg")
            )
            self.assertEqual(batch["locked_qty"], 18000)
            self.assertEqual(batch["available_qty"], 2000)
        finally:
            db2.close()

    def test_replaying_request_and_decision_after_crash_does_not_duplicate(self) -> None:
        self._lock_east()
        self.db.close()

        db2 = Database(self.path)
        p2 = SupplyPlatform(db2)
        try:
            req = p2.requests.create_request(
                {
                    "region_id": "region-east",
                    "medicine_id": "drug-emergency-a",
                    "quantity": 18000,
                    "needed_by_ts": "2026-10-01T06:00:00Z",
                    "urgency": "critical",
                    "stock_days": 0.5,
                    "client_token": "tok-east",
                },
                actor=p2.actor("u-reg"),
            )
            self.assertEqual(req["id"], "req:tok-east")

            # 旧建议已随崩溃前事务持久化；重放时引用它（幂等命中后不会重复锁定）。
            alloc_id = db2.writer().execute(
                "SELECT id FROM allocations WHERE request_id='req:tok-east' "
                "AND status='confirmed'"
            ).fetchone()["id"]
            decision = p2.allocations.confirm(
                {
                    "request_id": "req:tok-east",
                    "idempotency_key": "dec-crash-1",
                    "lines": [{"allocation_id": alloc_id, "quantity": 18000}],
                },
                actor=p2.actor("u-reg"),
            )
            # 幂等命中：返回崩溃前的同一条承诺，而非新锁定
            self.assertEqual(len(decision["commitments"]), 1)
            total = db2.writer().execute(
                "SELECT COUNT(*) c FROM commitments"
            ).fetchone()["c"]
            self.assertEqual(total, 1)
            locked = db2.writer().execute(
                """
                SELECT COALESCE(SUM(quantity - released_qty),0) q
                FROM commitments WHERE batch_id='batch-a-rel-0928'
                """
            ).fetchone()["q"]
            self.assertEqual(locked, 18000)
        finally:
            db2.close()

    def test_fulfillment_event_replay_after_crash_is_idempotent(self) -> None:
        _, decision = self._lock_east()
        cid = decision["commitments"][0]["id"]
        self.platform.fulfillment.record_event(
            cid,
            {"kind": "fulfill", "reason": "shipment", "quantity": 9000,
             "idempotency_key": "ship-1"},
            actor=self.platform.actor("u-alpha"),
        )
        self.db.close()

        db2 = Database(self.path)
        p2 = SupplyPlatform(db2)
        try:
            p2.fulfillment.record_event(
                cid,
                {"kind": "fulfill", "reason": "shipment", "quantity": 9000,
                 "idempotency_key": "ship-1"},
                actor=p2.actor("u-alpha"),
            )
            qty = db2.writer().execute(
                "SELECT fulfilled_qty FROM commitments WHERE id=?", (cid,)
            ).fetchone()["fulfilled_qty"]
            self.assertEqual(qty, 9000)
            events = db2.writer().execute(
                "SELECT COUNT(*) c FROM commitment_events"
            ).fetchone()["c"]
            self.assertEqual(events, 1)
        finally:
            db2.close()
