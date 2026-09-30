"""兼容入口：领域样例读取迁移到 supply_guard.seed。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supply_guard.seed import load_seed as _load_seed


def load_seed(path: str | Path = "fixtures/seed.json") -> dict[str, Any]:
    """读取项目维护的领域样例并检查顶层结构。"""
    return _load_seed(path)
