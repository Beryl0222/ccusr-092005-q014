from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_seed(path: str | Path = "fixtures/seed.json") -> dict[str, Any]:
    """读取项目维护的领域样例，并检查顶层结构。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError("领域样例必须包含 records 数组")
    return data
