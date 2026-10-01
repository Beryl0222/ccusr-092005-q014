"""版本化日报：三类数量分离、逐批溯源、迟报、更正新版本、历史不可变。"""
from __future__ import annotations

import sqlite3

from supply.errors import PermissionDenied, ValidationError

from support import PlatformTestCase


def _item(deliverable: int, batches=None, **overrides) -> dict:
    item = {
        "medicine_id": "drug-emergency-a",
        "theoretical_capacity": 26000,
        "pending_qc": 10000,
        "wip_material_short": 4000,
        "deliverable": deliverable,
    }
    item.update(overrides)
    if batches is not None:
        item["batches"] = batches
    return item


class ReportingTest(PlatformTestCase):
    def _report(self, date_key="2026-09-29", **overrides):
        payload = {
            "line_id": "line-alpha-01",
            "production_date": date_key,
            "source_ts": f"{date_key}T23:30:00Z",
            "items": [
                _item(
                    12000,
                    batches=[{"batch_id": "batch-a-rel-0929", "quantity": 12000}],
                )
            ],
        }
        payload.update(overrides)
        return self.platform.reporting.submit_report(payload, actor=self.alpha)

    def test_three_phantom_categories_cannot_be_summed(self) -> None:
        # 理论产能 26000 + 待检 10000 + 缺料在制品 4000，若无逐批溯源则拒绝。
        with self.assertRaises(ValidationError):
            self.platform.reporting.submit_report(
                {
                    "line_id": "line-alpha-01",
                    "production_date": "2026-09-28",
                    "source_ts": "2026-09-28T23:30:00Z",
                    "items": [_item(26000 + 10000 + 4000)],
                },
                actor=self.alpha,
            )

    def test_pending_batch_cannot_back_deliverable(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.reporting.submit_report(
                {
                    "line_id": "line-alpha-01",
                    "production_date": "2026-09-30",
                    "source_ts": "2026-09-30T12:00:00Z",
                    "items": [
                        _item(
                            10000,
                            medicine_id="drug-emergency-a",
                            batches=[
                                {"batch_id": "batch-a-pend-0930", "quantity": 10000}
                            ],
                        )
                    ],
                },
                actor=self.alpha,
            )

    def test_rejected_batch_cannot_back_deliverable(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.reporting.submit_report(
                {
                    "line_id": "line-beta-01",
                    "production_date": "2026-09-29",
                    "source_ts": "2026-09-29T23:30:00Z",
                    "items": [
                        _item(
                            6000,
                            batches=[
                                {"batch_id": "batch-b-rej-0928", "quantity": 6000}
                            ],
                        )
                    ],
                },
                actor=self.beta,
            )

    def test_deliverable_must_equal_released_batch_refs(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.reporting.submit_report(
                {
                    "line_id": "line-alpha-01",
                    "production_date": "2026-09-28",
                    "source_ts": "2026-09-28T23:30:00Z",
                    "items": [
                        _item(
                            20000,
                            batches=[
                                {"batch_id": "batch-a-rel-0928", "quantity": 19999}
                            ],
                        )
                    ],
                },
                actor=self.alpha,
            )

    def test_deliverable_cannot_exceed_batch_free_qty_after_locking(self) -> None:
        self._create_requests()
        _, decisions = self._plan_and_confirm_both()
        # batch-a-rel-0929 确认后净锁定 7000，可承诺余量 5000。
        with self.assertRaises(ValidationError):
            self.platform.reporting.submit_report(
                {
                    "line_id": "line-alpha-01",
                    "production_date": "2026-09-30",
                    "source_ts": "2026-09-30T23:30:00Z",
                    "items": [
                        _item(
                            12000,
                            batches=[
                                {"batch_id": "batch-a-rel-0929", "quantity": 12000}
                            ],
                        )
                    ],
                },
                actor=self.alpha,
            )

    def test_late_submission_flagged(self) -> None:
        rep = self._report(submitted_ts="2026-10-01T02:00:00Z")
        self.assertEqual(rep["version_no"], 1)
        self.assertEqual(rep["is_late"], 1)
        rep_on_time = self.platform.reporting.submit_report(
            {
                "line_id": "line-beta-01",
                "production_date": "2026-09-29",
                "source_ts": "2026-09-29T20:00:00Z",
                "submitted_ts": "2026-09-29T20:30:00Z",
                "items": [
                    _item(
                        18000,
                        batches=[
                            {"batch_id": "batch-b-rel-0929", "quantity": 18000}
                        ],
                    )
                ],
            },
            actor=self.beta,
        )
        self.assertEqual(rep_on_time["is_late"], 0)

    def test_correction_creates_new_version_and_keeps_history(self) -> None:
        v1 = self._report()
        v2 = self._report(source_ts="2026-09-30T06:00:00Z", note="更正放行数量")
        self.assertEqual(v1["version_no"], 1)
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["is_correction"], 1)
        self.assertEqual(v2["supersedes_id"], v1["id"])
        # 当前版本指针指向 v2，历史版本仍可取回。
        fetched_v1 = self.platform.reporting.get_report(v1["id"], actor=self.alpha)
        self.assertEqual(fetched_v1["version_no"], 1)

    def test_history_rows_are_immutable(self) -> None:
        v1 = self._report()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.writer().execute(
                "UPDATE daily_reports SET note='x' WHERE id=?", (v1["id"],)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.writer().execute(
                "DELETE FROM daily_report_items WHERE report_id=?", (v1["id"],)
            )

    def test_enterprise_cannot_report_or_read_other_enterprise(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.platform.reporting.submit_report(
                {
                    "line_id": "line-beta-01",
                    "production_date": "2026-09-29",
                    "source_ts": "2026-09-29T23:30:00Z",
                    "items": [
                        _item(
                            18000,
                            batches=[
                                {"batch_id": "batch-b-rel-0929", "quantity": 18000}
                            ],
                        )
                    ],
                },
                actor=self.alpha,
            )
