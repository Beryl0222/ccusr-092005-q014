"""履行台账、运输取消释放、检验延期与不合格自动释放、只追加不可变。"""
from __future__ import annotations

import sqlite3

from supply.errors import Conflict, OvercommitError, PermissionDenied

from support import PlatformTestCase


class FulfillmentLifecycleTest(PlatformTestCase):
    def _one_locked_batch(self, qty=18000, token="t1", region="region-central"):
        self.platform.requests.create_request(
            {
                "region_id": region,
                "medicine_id": "drug-emergency-a",
                "quantity": qty,
                "needed_by_ts": "2026-10-01T02:00:00Z",
                "urgency": "critical",
                "stock_days": 1.0,
                "client_token": token,
            },
            actor=self.reg,
        )
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T05:10:00Z"
        )
        req = plan["plan"][0]
        decision = self.platform.allocations.confirm(
            {
                "request_id": req["request_id"],
                "idempotency_key": f"dec:{token}",
                "lines": [
                    {"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
                    for l in req["lines"]
                ],
            },
            actor=self.reg,
        )
        return req["request_id"], decision["commitments"][0]

    def test_fulfillment_events_are_append_only(self) -> None:
        _, com = self._one_locked_batch()
        self.platform.fulfillment.record_event(
            com["id"],
            {"kind": "fulfill", "reason": "shipment", "quantity": 5000,
             "idempotency_key": "e1", "ref": "WB1"},
            actor=self.alpha,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.writer().execute(
                "UPDATE commitment_events SET quantity=4 WHERE idempotency_key='e1'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.writer().execute(
                "DELETE FROM commitment_events WHERE idempotency_key='e1'"
            )

    def test_event_idempotency_replay_does_not_double_count(self) -> None:
        _, com = self._one_locked_batch()
        payload = {"kind": "fulfill", "reason": "shipment", "quantity": 5000,
                   "idempotency_key": "e1"}
        self.platform.fulfillment.record_event(com["id"], payload, actor=self.alpha)
        again = self.platform.fulfillment.record_event(
            com["id"], payload, actor=self.alpha
        )
        self.assertEqual(again["fulfilled_qty"], 5000)

    def test_cannot_fulfill_beyond_commitment(self) -> None:
        _, com = self._one_locked_batch()
        with self.assertRaises(OvercommitError):
            self.platform.fulfillment.record_event(
                com["id"],
                {"kind": "fulfill", "reason": "shipment", "quantity": 18001,
                 "idempotency_key": "e1"},
                actor=self.alpha,
            )

    def test_transport_cancel_releases_unfulfilled_share_for_reallocation(self) -> None:
        req_id, com = self._one_locked_batch()
        self.platform.fulfillment.record_event(
            com["id"],
            {"kind": "fulfill", "reason": "shipment", "quantity": 10000,
             "idempotency_key": "e1", "ref": "WB9"},
            actor=self.alpha,
        )
        released = self.platform.fulfillment.record_event(
            com["id"],
            {"kind": "release", "reason": "transport_cancel", "quantity": 8000,
             "idempotency_key": "e2"},
            actor=self.alpha,
        )
        self.assertEqual(released["fulfilled_qty"], 10000)
        self.assertEqual(released["released_qty"], 8000)
        # 已履行部分不能被改写：再履行无剩余
        with self.assertRaises(Conflict):
            self.platform.fulfillment.record_event(
                com["id"],
                {"kind": "fulfill", "reason": "shipment", "quantity": 1,
                 "idempotency_key": "e3"},
                actor=self.alpha,
            )
        # 释放份额回流：重新生成建议时该批次重新出现
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T08:00:00Z"
        )
        req_plan = next(r for r in plan["plan"] if r["request_id"] == req_id)
        self.assertEqual(req_plan["already_locked"], 10000)
        self.assertGreaterEqual(req_plan["planned"], 8000)
        self.assertTrue(
            any(l["batch_id"] == com["batch_id"] for l in req_plan["lines"])
        )

    def test_qc_postpone_then_release(self) -> None:
        b = self.platform.production.record_qc(
            "batch-a-pend-0930",
            {"kind": "postpone", "new_due_ts": "2026-10-01T18:00:00Z",
             "ts": "2026-09-30T11:00:00Z", "note": "仪器排队"},
            actor=self.alpha,
        )
        self.assertEqual(b["qc_status"], "pending")
        self.assertEqual(b["qc_due_ts"], "2026-10-01T18:00:00Z")
        # 延期必须晚于原时间
        from supply.errors import ValidationError

        with self.assertRaises(ValidationError):
            self.platform.production.record_qc(
                "batch-a-pend-0930",
                {"kind": "postpone", "new_due_ts": "2026-09-30T10:00:00Z"},
                actor=self.alpha,
            )
        b2 = self.platform.production.record_qc(
            "batch-a-pend-0930",
            {"kind": "release", "ts": "2026-10-01T16:00:00Z"},
            actor=self.alpha,
        )
        self.assertEqual(b2["qc_status"], "released")

    def test_qc_reject_auto_releases_unfulfilled_and_keeps_fulfilled(self) -> None:
        _, com = self._one_locked_batch()
        self.platform.fulfillment.record_event(
            com["id"],
            {"kind": "fulfill", "reason": "shipment", "quantity": 5000,
             "idempotency_key": "e1"},
            actor=self.alpha,
        )
        self.platform.production.record_qc(
            com["batch_id"],
            {"kind": "reject", "ts": "2026-09-30T09:00:00Z", "note": "热原不合格"},
            actor=self.alpha,
        )
        after = self.platform.fulfillment.get_commitment(com["id"], actor=self.reg)
        self.assertEqual(after["status"], "rejected_batch")
        self.assertEqual(after["fulfilled_qty"], 5000)
        self.assertEqual(after["released_qty"], 13000)
        kinds = {(e["kind"], e["reason"]) for e in after["events"]}
        self.assertIn(("release", "qc_reject"), kinds)
        # 不合格批次不再进入任何建议
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T10:00:00Z"
        )
        for r in plan["plan"]:
            for l in r["lines"]:
                self.assertNotEqual(l["batch_id"], com["batch_id"])
        # 已判不合格不可翻案
        with self.assertRaises(Conflict):
            self.platform.production.record_qc(
                com["batch_id"],
                {"kind": "release", "ts": "2026-09-30T11:00:00Z"},
                actor=self.alpha,
            )

    def test_released_batch_share_can_be_relocked_after_cancel(self) -> None:
        # 取消释放后该批次份额应能被新决定再次锁定，且不重复占用。
        req_id, com = self._one_locked_batch()
        self.platform.fulfillment.record_event(
            com["id"],
            {"kind": "release", "reason": "transport_cancel", "quantity": 18000,
             "idempotency_key": "e1"},
            actor=self.alpha,
        )
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T08:00:00Z"
        )
        new_lines = [
            {"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
            for r in plan["plan"] if r["request_id"] == req_id
            for l in r["lines"]
        ]
        self.assertTrue(new_lines)
        d = self.platform.allocations.confirm(
            {"request_id": req_id, "idempotency_key": "dec:t1:v2",
             "lines": new_lines},
            actor=self.reg,
        )
        batch = self.platform.production.get_batch(com["batch_id"], actor=self.reg)
        self.assertLessEqual(batch["locked_qty"], batch["quantity"])
        self.assertEqual(batch["available_qty"], batch["quantity"] - batch["locked_qty"])
        self.assertTrue(d["commitments"])

    def test_other_enterprise_cannot_record_events(self) -> None:
        _, com = self._one_locked_batch()
        with self.assertRaises(PermissionDenied):
            self.platform.fulfillment.record_event(
                com["id"],
                {"kind": "fulfill", "reason": "shipment", "quantity": 1,
                 "idempotency_key": "e1"},
                actor=self.beta,
            )

    def test_request_status_tracks_lock_release_relock(self) -> None:
        req_id, com = self._one_locked_batch()
        self.assertEqual(
            self.platform.requests.get_request(req_id, actor=self.reg)["status"],
            "locked",
        )
        # 部分履行后全部运输取消：净锁定归零，需求重新开放
        self.platform.fulfillment.record_event(
            com["id"],
            {"kind": "fulfill", "reason": "shipment", "quantity": 8000,
             "idempotency_key": "e1"},
            actor=self.alpha,
        )
        self.platform.fulfillment.record_event(
            com["id"],
            {"kind": "release", "reason": "transport_cancel", "quantity": 10000,
             "idempotency_key": "e2"},
            actor=self.alpha,
        )
        self.assertEqual(
            self.platform.requests.get_request(req_id, actor=self.reg)["status"],
            "partial",  # 已履行 8000 仍计入净锁定
        )
        # 回流份额重新建议并确认后回到 locked
        plan = self.platform.allocations.build_plan(
            actor=self.reg, plan_ts="2026-09-30T08:00:00Z"
        )
        rp = next(r for r in plan["plan"] if r["request_id"] == req_id)
        self.platform.allocations.confirm(
            {"request_id": req_id, "idempotency_key": "dec:t1:relock",
             "lines": [{"allocation_id": l["allocation_id"], "quantity": l["quantity"]}
                       for l in rp["lines"]]},
            actor=self.reg,
        )
        self.assertEqual(
            self.platform.requests.get_request(req_id, actor=self.reg)["status"],
            "locked",
        )
        # 全部履行后关闭
        new_coms = self.db.writer().execute(
            "SELECT id FROM commitments WHERE request_id=? AND status='active'",
            (req_id,),
        ).fetchall()
        for i, row in enumerate(new_coms):
            c = self.platform.fulfillment.get_commitment(row["id"], actor=self.reg)
            self.platform.fulfillment.record_event(
                row["id"],
                {"kind": "fulfill", "reason": "shipment",
                 "quantity": c["quantity"], "idempotency_key": f"e-fill-{i}"},
                actor=self.alpha,
            )
        self.assertEqual(
            self.platform.requests.get_request(req_id, actor=self.reg)["status"],
            "closed",
        )
