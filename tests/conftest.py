import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from supply_guard.api import build_service
from supply_guard.service import Actor

NOW = "2026-09-30T12:00:00+00:00"


@pytest.fixture
def svc(tmp_path):
    # 文件库（WAL）：并发写由 busy_timeout 串行化，与生产/崩溃恢复场景一致
    return build_service(str(tmp_path / "test_guard.db"),
                         str(ROOT / "fixtures" / "seed.json"))


@pytest.fixture
def actors():
    return {
        "reg": Actor("regulator", "REGULATOR"),
        "hd": Actor("ent-huadong", "ENTERPRISE", enterprise_id="ent-huadong"),
        "hb": Actor("ent-huabei", "ENTERPRISE", enterprise_id="ent-huabei"),
        "east": Actor("region-east", "REGION", region_id="region-east"),
        "central": Actor("region-central", "REGION", region_id="region-central"),
    }


def make_demand(svc, actor, *, id, qty, urgency="URGENT", coverage=2.0,
                needed="2026-10-08T00:00:00+00:00", medicine="med-a"):
    return svc.create_demand(actor, {
        "id": id, "medicine_id": medicine, "qty": qty, "urgency": urgency,
        "coverage_days": coverage, "needed_by_ts": needed})
