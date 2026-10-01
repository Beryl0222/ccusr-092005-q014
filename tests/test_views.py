"""分级可见性与承诺溯源。"""
from __future__ import annotations

from supply.errors import PermissionDenied

from support import PlatformTestCase


class VisibilityTest(PlatformTestCase):
    def test_enterprise_dashboard_contains_only_own_data(self) -> None:
        self._create_requests()
        self._plan_and_confirm_both()
        alpha = self.platform.views.enterprise_dashboard("ent-alpha", actor=self.alpha)
        beta = self.platform.views.enterprise_dashboard("ent-beta", actor=self.beta)
        self.assertTrue(alpha["batches"])
        self.assertTrue(beta["batches"])
        self.assertEqual({b["enterprise_id"] for b in alpha["batches"]}, {"ent-alpha"})
        self.assertEqual({b["enterprise_id"] for b in beta["batches"]}, {"ent-beta"})
        self.assertEqual(
            {c["enterprise_id"] for c in alpha["commitments"]}, {"ent-alpha"}
        )
        self.assertEqual(
            {c["enterprise_id"] for c in beta["commitments"]}, {"ent-beta"}
        )
        # 批次行的可承诺量真实反映锁定情况
        a0928 = next(b for b in alpha["batches"] if b["id"] == "batch-a-rel-0928")
        self.assertEqual(a0928["available_to_promise"], 0)

    def test_enterprise_cannot_open_other_dashboard_or_regulator_view(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.platform.views.enterprise_dashboard("ent-beta", actor=self.alpha)
        with self.assertRaises(PermissionDenied):
            self.platform.views.regulator_overview(actor=self.alpha)

    def test_regulator_sees_cross_enterprise_overview(self) -> None:
        self._create_requests()
        self._plan_and_confirm_both()
        ov = self.platform.views.regulator_overview(actor=self.reg)
        self.assertEqual({e["id"] for e in ov["enterprises"]},
                         {"ent-alpha", "ent-beta"})
        med = next(m for m in ov["medicines"] if m["id"] == "drug-emergency-a")
        self.assertGreater(med["net_locked_qty"], 0)
        self.assertEqual(
            med["free_released_qty"], med["released_qty"] - med["net_locked_qty"]
        )

    def test_commitment_trace_points_to_batch_constraints_and_decision(self) -> None:
        self._create_requests()
        _, decisions = self._plan_and_confirm_both()
        cid = next(
            c["id"] for d in decisions.values() for c in d["commitments"]
            if c["batch_id"] == "batch-a-rel-0928"
        )
        trace = self.platform.views.commitment_trace(cid, actor=self.reg)
        self.assertEqual(trace["batch"]["id"], "batch-a-rel-0928")
        self.assertEqual(trace["batch"]["qc_status"], "released")
        self.assertTrue(trace["license_in_scope"])
        self.assertTrue(trace["decision"]["idempotency_key"])
        self.assertEqual(trace["commitment"]["decision_id"], trace["decision"]["id"])
        self.assertIn("allocation_rationale", trace)
        self.assertTrue(trace["allocation_rationale"])

    def test_enterprise_cannot_trace_other_enterprise_commitment(self) -> None:
        self._create_requests()
        _, decisions = self._plan_and_confirm_both()
        beta_cid = next(
            c["id"] for d in decisions.values() for c in d["commitments"]
            if c["enterprise_id"] == "ent-beta"
        )
        with self.assertRaises(PermissionDenied):
            self.platform.views.commitment_trace(beta_cid, actor=self.alpha)
