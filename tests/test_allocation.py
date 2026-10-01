"""建议计算、监管确认锁定、并发抢锁与运输时限。"""
from __future__ import annotations

import sqlite3
import threading

from supply.errors import (
    Conflict,
    OvercommitError,
    PermissionDenied,
    ValidationError,
)
from supply.timeutil import now

from support import PlatformTestCase


class PlanningTest(PlatformTestCase):
    def test_plan_does_not_double_promise_same_batch_within_one_run(self) -> None:
        self._create_requests()
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        used: dict[str, int] = {}
        for req in plan["plan"]:
            for line in req["lines"]:
                used[line["batch_id"]] = used.get(line["batch_id"], 0) + line["quantity"]
        # 每批次在同一轮计划中的预留不得超过批次总量
        batch_qty = {
            "batch-a-rel-0928": 20000,
            "batch-a-rel-0929": 12000,
            "batch-b-rel-0929": 18000,
        }
        for bid, qty in used.items():
            self.assertLessEqual(qty, batch_qty[bid])
        # 华东优先（覆盖天数更少）吃掉 alpha 两批 25000；华中应转向 beta
        by_req = {r["request_id"]: r for r in plan["plan"]}
        east = by_req["req:tok-east"]
        central = by_req["req:tok-central"]
        east_batches = {l["batch_id"] for l in east["lines"]}
        self.assertEqual(east_batches, {"batch-a-rel-0928", "batch-a-rel-0929"})
        self.assertIn("batch-b-rel-0929", {l["batch_id"] for l in central["lines"]})
        self.assertEqual(east["planned"], 25000)
        self.assertEqual(central["planned"], 20000)

    def test_proposed_plan_locks_nothing(self) -> None:
        self._create_requests()
        self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        locked = self.db.writer().execute(
            "SELECT COUNT(*) c FROM commitments"
        ).fetchone()["c"]
        self.assertEqual(locked, 0)

    def test_transit_deadline_excludes_unreachable_or_too_slow(self) -> None:
        # 场景一：华东要求 09-29 23:00 前到货。
        #   alpha 已放行批次 02:00+6h=08:00 可达；beta 20:00 才放行 +30h = 10-01，超时排除。
        self.platform.requests.create_request(
            {
                "region_id": "region-east",
                "medicine_id": "drug-emergency-a",
                "quantity": 18000,
                "needed_by_ts": "2026-09-29T23:00:00Z",
                "urgency": "critical",
                "stock_days": 0.2,
                "client_token": "tok-tight",
            },
            actor=self.reg,
        )
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-29T12:00:00Z"
        )
        req = plan["plan"][0]
        self.assertEqual(req["request_id"], "req:tok-tight")
        self.assertTrue(req["lines"])
        self.assertEqual(
            {l["enterprise_id"] for l in req["lines"]}, {"ent-alpha"}
        )

    def test_region_without_transit_record_is_unreachable(self) -> None:
        # 场景二：新增一个没有任何运输时限记录的区域 => 不可达、无候选。
        self.db.writer().execute(
            "INSERT INTO regions (id, name) VALUES ('region-remote','边远区')"
        )
        self.platform.requests.create_request(
            {
                "region_id": "region-remote",
                "medicine_id": "drug-emergency-a",
                "quantity": 18000,
                "needed_by_ts": "2026-10-05T00:00:00Z",
                "urgency": "normal",
                "stock_days": 5.0,
                "client_token": "tok-remote",
            },
            actor=self.reg,
        )
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        req = next(r for r in plan["plan"] if r["request_id"] == "req:tok-remote")
        self.assertEqual(req["lines"], [])
        self.assertEqual(req["shortfall"], 18000)

    def test_regulator_confirmation_locks_real_batch_quantities(self) -> None:
        self._create_requests()
        _, decisions = self._plan_and_confirm_both()
        total = sum(
            c["quantity"] for d in decisions.values() for c in d["commitments"]
        )
        self.assertEqual(total, 45000)
        batch = self.platform.production.get_batch(
            "batch-a-rel-0928", actor=self.reg
        )
        self.assertEqual(batch["locked_qty"], 20000)
        self.assertEqual(batch["available_qty"], 0)

    def test_enterprise_cannot_confirm(self) -> None:
        self._create_requests()
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        line = plan["plan"][0]["lines"][0]
        with self.assertRaises(PermissionDenied):
            self.platform.allocations.confirm(
                {
                    "request_id": plan["plan"][0]["request_id"],
                    "idempotency_key": "hack",
                    "lines": [
                        {"allocation_id": line["allocation_id"], "quantity": 1}
                    ],
                },
                actor=self.alpha,
            )

    def test_decision_idempotency_key_makes_replay_a_noop(self) -> None:
        self._create_requests()
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        payload = {
            "request_id": plan["plan"][0]["request_id"],
            "idempotency_key": "dec-same",
            "lines": [
                {"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
                for l in plan["plan"][0]["lines"]
            ],
        }
        d1 = self.platform.allocations.confirm(payload, actor=self.reg)
        d2 = self.platform.allocations.confirm(payload, actor=self.reg)
        self.assertEqual(d1["id"], d2["id"])
        count = self.db.writer().execute(
            "SELECT COUNT(*) c FROM commitments WHERE decision_id=?", (d1["id"],)
        ).fetchone()["c"]
        self.assertEqual(count, len(d1["commitments"]))

    def test_confirmed_allocation_cannot_be_confirmed_again(self) -> None:
        self._create_requests()
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        req = plan["plan"][0]
        lines = [
            {"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
            for l in req["lines"]
        ]
        self.platform.allocations.confirm(
            {
                "request_id": req["request_id"],
                "idempotency_key": "k1",
                "lines": lines,
            },
            actor=self.reg,
        )
        with self.assertRaises(Conflict):
            self.platform.allocations.confirm(
                {
                    "request_id": req["request_id"],
                    "idempotency_key": "k2",
                    "lines": lines,
                },
                actor=self.reg,
            )

    def test_confirm_quantity_bounded_by_proposal(self) -> None:
        self._create_requests()
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        line = plan["plan"][0]["lines"][0]
        with self.assertRaises(ValidationError):
            self.platform.allocations.confirm(
                {
                    "request_id": plan["plan"][0]["request_id"],
                    "idempotency_key": "k-big",
                    "lines": [
                        {
                            "allocation_id": line["allocation_id"],
                            "quantity": line["quantity"] + 1,
                        }
                    ],
                },
                actor=self.reg,
            )

    def test_confirm_cannot_lock_more_than_requested_quantity(self) -> None:
        # 同一申请的两份不同建议累计不得超过申请数量。
        self.platform.requests.create_request(
            {
                "region_id": "region-east",
                "medicine_id": "drug-emergency-a",
                "quantity": 10000,
                "needed_by_ts": "2026-10-02T00:00:00Z",
                "urgency": "critical",
                "stock_days": 0.5,
                "client_token": "tok-small",
            },
            actor=self.reg,
        )
        with self.db.begin_immediate() as conn:
            for aid, bid in [
                ("alloc:small:1", "batch-a-rel-0928"),
                ("alloc:small:2", "batch-a-rel-0929"),
            ]:
                conn.execute(
                    """
                    INSERT INTO allocations
                      (id, request_id, batch_id, enterprise_id, quantity, available_ts,
                       transit_hours, arrives_ts, score, rationale, created_ts, status)
                    VALUES (?, 'req:tok-small', ?, 'ent-alpha', 9000,
                            '2026-09-29T02:00:00Z', 6, '2026-09-29T08:00:00Z', 1,
                            'x', '2026-09-30T05:00:00Z', 'proposed')
                    """,
                    (aid, bid),
                )
        # 6000+6000 > 申请 10000，应被拦截（批次余量本身足够）
        with self.assertRaises(OvercommitError):
            self.platform.allocations.confirm(
                {
                    "request_id": "req:tok-small",
                    "idempotency_key": "dec-small",
                    "lines": [
                        {"allocation_id": "alloc:small:1", "quantity": 6000},
                        {"allocation_id": "alloc:small:2", "quantity": 6000},
                    ],
                },
                actor=self.reg,
            )

    def test_idempotency_key_replay_with_different_payload_conflicts(self) -> None:
        self._create_requests()
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        req = plan["plan"][0]
        full = [
            {"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
            for l in req["lines"]
        ]
        self.platform.allocations.confirm(
            {"request_id": req["request_id"], "idempotency_key": "idem-x",
             "lines": full},
            actor=self.reg,
        )
        with self.assertRaises(Conflict):
            self.platform.allocations.confirm(
                {"request_id": req["request_id"], "idempotency_key": "idem-x",
                 "lines": [full[0]]},  # 载荷不同
                actor=self.reg,
            )


    def test_database_trigger_blocks_overlocking_batch(self) -> None:
        # 即使绕过应用直接插入两条占满同一批次的承诺，触发器也会中止。
        self._create_requests()
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        req = plan["plan"][0]
        lines = [
            {"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
            for l in req["lines"]
        ]
        self.platform.allocations.confirm(
            {"request_id": req["request_id"], "idempotency_key": "k1", "lines": lines},
            actor=self.reg,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.writer().execute(
                """
                INSERT INTO commitments
                  (id, decision_id, allocation_id, request_id, batch_id,
                   enterprise_id, region_id, quantity, locked_ts)
                SELECT 'evil', d.id, a.id, d.request_id, a.batch_id,
                       a.enterprise_id, 'region-central', 1, ?
                FROM allocations a JOIN decisions d ON d.request_id = a.request_id
                WHERE a.batch_id='batch-a-rel-0928' AND a.status='confirmed'
                LIMIT 1
                """,
                (now(),),
            )


class ConcurrentLockTest(PlatformTestCase):
    def _two_duplicate_proposals(self):
        for region, tok in [
            ("region-east", "te"),
            ("region-central", "tc"),
        ]:
            self.platform.requests.create_request(
                {
                    "region_id": region,
                    "medicine_id": "drug-emergency-a",
                    "quantity": 20000,
                    "needed_by_ts": "2026-10-02T00:00:00Z",
                    "urgency": "critical",
                    "stock_days": 0.5,
                    "client_token": tok,
                },
                actor=self.reg,
            )
        w = self.db.writer()
        with self.db.begin_immediate() as conn:
            for rid, aid in [("req:te", "alloc:dup:e"), ("req:tc", "alloc:dup:c")]:
                conn.execute(
                    """
                    INSERT INTO allocations
                      (id, request_id, batch_id, enterprise_id, quantity, available_ts,
                       transit_hours, arrives_ts, score, rationale, created_ts, status)
                    VALUES (?,?, 'batch-a-rel-0928','ent-alpha',20000,
                            '2026-09-29T02:00:00Z',6,'2026-09-29T08:00:00Z',1,'dup',?,'proposed')
                    """,
                    (aid, rid, now()),
                )

    def test_parallel_confirms_of_same_batch_have_single_winner(self) -> None:
        self._two_duplicate_proposals()
        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def confirm(side, req_id, alloc_id, key):
            barrier.wait()
            try:
                d = self.platform.allocations.confirm(
                    {
                        "request_id": req_id,
                        "idempotency_key": key,
                        "lines": [{"allocation_id": alloc_id, "quantity": 20000}],
                    },
                    actor=self.reg,
                )
                results[side] = ("ok", d["total_confirmed_qty"])
            except OvercommitError as exc:
                results[side] = ("overcommit", str(exc))
            except Exception as exc:  # noqa: BLE001
                results[side] = ("error", type(exc).__name__, str(exc))

        threads = [
            threading.Thread(
                target=confirm, args=("east", "req:te", "alloc:dup:e", "ke")
            ),
            threading.Thread(
                target=confirm, args=("central", "req:tc", "alloc:dup:c", "kc")
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        outcomes = {v[0] for v in results.values()}  # type: ignore[index]
        self.assertEqual(outcomes, {"ok", "overcommit"})
        batch = self.platform.production.get_batch(
            "batch-a-rel-0928", actor=self.reg
        )
        self.assertEqual(batch["locked_qty"], 20000)
        self.assertEqual(batch["available_qty"], 0)
