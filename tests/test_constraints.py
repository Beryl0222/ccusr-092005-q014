"""不可突破的生产约束：许可、换线清洁（含跨日）、停机、原料短缺。"""
from __future__ import annotations

from supply.errors import ConstraintViolation, PermissionDenied

from support import PlatformTestCase


def _batch(batch_id, line_id, med, start, produced, qty):
    return {
        "id": batch_id,
        "line_id": line_id,
        "medicine_id": med,
        "start_ts": start,
        "produced_ts": produced,
        "quantity": qty,
    }


class HardConstraintTest(PlatformTestCase):
    def test_license_scope_blocks_unlicensed_line(self) -> None:
        # line-alpha-02 只有 drug-b 许可
        with self.assertRaises(ConstraintViolation):
            self.platform.production.register_batch(
                _batch(
                    "x-lic", "line-alpha-02", "drug-emergency-a",
                    "2026-10-02T00:00:00Z", "2026-10-02T08:00:00Z", 100,
                ),
                actor=self.alpha,
            )

    def test_same_product_needs_no_changeover(self) -> None:
        # line-alpha-01 最后批次 batch-a-pend-0930（急救针A）09-30 09:00 下线；
        # 同产品立即开工合法（缺料/停机另算），加少量数量验证放行。
        b = self.platform.production.register_batch(
            _batch(
                "x-same", "line-alpha-01", "drug-emergency-a",
                "2026-09-30T09:30:00Z", "2026-09-30T12:00:00Z", 100,
            ),
            actor=self.alpha,
        )
        self.assertEqual(b["qc_status"], "pending")

    def test_changeover_cleaning_blocks_early_start_same_day(self) -> None:
        # A -> B 需 4h 清洁，09:00 下线，11:00 开工被拦。
        with self.assertRaises(ConstraintViolation):
            self.platform.production.register_batch(
                _batch(
                    "x-co-early", "line-alpha-01", "drug-b",
                    "2026-09-30T11:00:00Z", "2026-09-30T20:00:00Z", 100,
                ),
                actor=self.alpha,
            )

    def test_changeover_cleaning_allows_start_after_ready(self) -> None:
        b = self.platform.production.register_batch(
            _batch(
                "x-co-ok", "line-alpha-01", "drug-b",
                "2026-09-30T13:00:00Z", "2026-09-30T22:00:00Z", 100,
            ),
            actor=self.alpha,
        )
        self.assertEqual(b["id"], "x-co-ok")

    def test_cross_midnight_changeover_is_measured_in_absolute_time(self) -> None:
        # 先在 alpha 线加一个 09-30 23:00 下线的 A 批次（同产品换线 0h），
        # 次日 01:00 想开工 B：A->B 需 4h，清洁 10-01 03:00 才完成，必须拦截。
        self.platform.production.register_batch(
            _batch(
                "x-late-a", "line-alpha-01", "drug-emergency-a",
                "2026-09-30T20:00:00Z", "2026-09-30T23:00:00Z", 1000,
            ),
            actor=self.alpha,
        )
        with self.assertRaises(ConstraintViolation):
            self.platform.production.register_batch(
                _batch(
                    "x-co-cross", "line-alpha-01", "drug-b",
                    "2026-10-01T01:00:00Z", "2026-10-01T06:00:00Z", 100,
                ),
                actor=self.alpha,
            )
        # 清洁完成（03:00）后跨日开工合法
        b = self.platform.production.register_batch(
            _batch(
                "x-co-cross-ok", "line-alpha-01", "drug-b",
                "2026-10-01T03:30:00Z", "2026-10-02T02:00:00Z", 100,
            ),
            actor=self.alpha,
        )
        self.assertEqual(b["id"], "x-co-cross-ok")

    def test_downtime_block_overlap_rejected(self) -> None:
        with self.assertRaises(ConstraintViolation):
            self.platform.production.register_batch(
                _batch(
                    "x-dt", "line-beta-01", "drug-emergency-a",
                    "2026-10-01T02:00:00Z", "2026-10-01T08:00:00Z", 100,
                ),
                actor=self.beta,
            )
        # 检修结束后可开工（注意原料约束：10-01 10:00 后）
        b = self.platform.production.register_batch(
            _batch(
                "x-dt-ok", "line-beta-01", "drug-emergency-a",
                "2026-10-01T10:30:00Z", "2026-10-01T18:00:00Z", 100,
            ),
            actor=self.beta,
        )
        self.assertEqual(b["id"], "x-dt-ok")

    def test_material_shortage_blocks_batch(self) -> None:
        # beta 到货 30000 支西林瓶，已有 A 批次 18000+6000=24000 消耗，余 6000。
        with self.assertRaises(ConstraintViolation):
            self.platform.production.register_batch(
                _batch(
                    "x-mat", "line-beta-01", "drug-emergency-a",
                    "2026-10-01T12:00:00Z", "2026-10-01T20:00:00Z", 7000,
                ),
                actor=self.beta,
            )
        b = self.platform.production.register_batch(
            _batch(
                "x-mat-ok", "line-beta-01", "drug-emergency-a",
                "2026-10-01T12:00:00Z", "2026-10-01T20:00:00Z", 1000,
            ),
            actor=self.beta,
        )
        self.assertEqual(b["id"], "x-mat-ok")

    def test_cannot_register_on_other_enterprise_line(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.platform.production.register_batch(
                _batch(
                    "x-other", "line-beta-01", "drug-emergency-a",
                    "2026-10-02T00:00:00Z", "2026-10-02T06:00:00Z", 10,
                ),
                actor=self.alpha,
            )

    def test_material_arrival_report_lifts_shortage_constraint(self) -> None:
        # beta 现有余量 6000，7000 批次缺料被拦；上报 2000 到货后即可登记。
        with self.assertRaises(ConstraintViolation):
            self.platform.production.register_batch(
                _batch(
                    "x-mat-2", "line-beta-01", "drug-emergency-a",
                    "2026-10-01T12:00:00Z", "2026-10-01T20:00:00Z", 7000,
                ),
                actor=self.beta,
            )
        self.platform.production.report_material_supply(
            {"id": "supply-beta-vials-2", "material_id": "vial-10ml",
             "available_ts": "2026-10-01T11:00:00Z", "quantity": 2000},
            actor=self.beta,
        )
        b = self.platform.production.register_batch(
            _batch(
                "x-mat-2", "line-beta-01", "drug-emergency-a",
                "2026-10-01T12:00:00Z", "2026-10-01T20:00:00Z", 7000,
            ),
            actor=self.beta,
        )
        self.assertEqual(b["id"], "x-mat-2")

        # 同一记录 ID 重放不重复增加库存：可用余量只剩 1000，1001 仍缺料
        self.platform.production.report_material_supply(
            {"id": "supply-beta-vials-2", "material_id": "vial-10ml",
             "available_ts": "2026-10-01T11:00:00Z", "quantity": 2000},
            actor=self.beta,
        )
        with self.assertRaises(ConstraintViolation):
            self.platform.production.register_batch(
                _batch(
                    "x-mat-3", "line-beta-01", "drug-emergency-a",
                    "2026-10-01T21:00:00Z", "2026-10-02T03:00:00Z", 1001,
                ),
                actor=self.beta,
            )

    def test_regulator_cannot_report_material_supply(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.platform.production.report_material_supply(
                {"id": "x", "material_id": "vial-10ml",
                 "available_ts": "2026-10-01T11:00:00Z", "quantity": 1},
                actor=self.reg,
            )
