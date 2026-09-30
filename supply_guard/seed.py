"""从 fixtures/seed.json 引导主数据与初始日报。

主数据幂等插入（INSERT OR IGNORE）；初始日报通过领域服务上报，
因此同样产生版本号、责任人与批次事件流水。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .service import Actor, Service
from .store import Store


def load_seed(path: str | Path = "fixtures/seed.json") -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("领域样例必须是 JSON 对象")
    for key in ("enterprises", "regions", "medicines", "production_lines"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"领域样例缺少 {key} 数组")
    return data


def seed_store(store: Store, seed: dict[str, Any], *, service: Service | None = None,
               force: bool = False) -> bool:
    """幂等播种：已播种过的库不会重复写入（尤其初始日报不会产生重复版本）。

    返回是否实际执行了播种。
    """
    svc = service or Service(store)
    with store.read() as conn:
        marker = conn.execute(
            "SELECT value FROM meta WHERE key='seeded_project'").fetchone()
    if marker is not None and not force:
        return False

    with store.transaction() as conn:
        for e in seed["enterprises"]:
            conn.execute("INSERT OR IGNORE INTO enterprises(id, name) VALUES(?,?)",
                         (e["id"], e["name"]))
        for r in seed["regions"]:
            conn.execute("INSERT OR IGNORE INTO regions(id, name) VALUES(?,?)",
                         (r["id"], r["name"]))
        for u in seed["users"]:
            conn.execute(
                "INSERT OR IGNORE INTO users(username, display_name, role, token,"
                " enterprise_id, region_id) VALUES(?,?,?,?,?,?)",
                (u["username"], u["display_name"], u["role"], u["token"],
                 u.get("enterprise_id"), u.get("region_id")))
        for m in seed["medicines"]:
            conn.execute(
                "INSERT OR IGNORE INTO medicines(id, name, priority, unit) VALUES(?,?,?,?)",
                (m["id"], m["name"], m.get("priority", "重点"), m.get("unit", "支")))
        for mt in seed.get("materials", []):
            conn.execute("INSERT OR IGNORE INTO materials(id, name, unit) VALUES(?,?,?)",
                         (mt["id"], mt["name"], mt.get("unit", "kg")))
        for b in seed.get("bom", []):
            conn.execute(
                "INSERT OR IGNORE INTO bom(medicine_id, material_id, qty_per_unit)"
                " VALUES(?,?,?)",
                (b["medicine_id"], b["material_id"], b["qty_per_unit"]))
        for line in seed["production_lines"]:
            conn.execute(
                "INSERT OR IGNORE INTO production_lines(id, enterprise_id, name,"
                " daily_capacity, changeover_hours, qc_lead_hours) VALUES(?,?,?,?,?,?)",
                (line["id"], line["enterprise_id"], line["name"],
                 line["daily_capacity"], line.get("changeover_hours", 0),
                 line.get("qc_lead_hours", 0)))
        for lic in seed.get("line_licenses", []):
            conn.execute(
                "INSERT OR IGNORE INTO line_licenses(line_id, medicine_id) VALUES(?,?)",
                (lic["line_id"], lic["medicine_id"]))
        for lane in seed.get("lanes", []):
            conn.execute(
                "INSERT OR IGNORE INTO lanes(enterprise_id, region_id, transport_hours)"
                " VALUES(?,?,?)",
                (lane["enterprise_id"], lane["region_id"], lane["transport_hours"]))
        for dt in seed.get("line_downtimes", []):
            conn.execute(
                "INSERT OR IGNORE INTO line_downtimes(id, line_id, start_ts, end_ts, reason)"
                " VALUES(?,?,?,?,?)",
                (dt["id"], dt["line_id"], dt["start_ts"], dt["end_ts"], dt["reason"]))
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('seeded_project', ?)",
            (seed.get("project", "supply_guard"),))

    # 初始日报走标准上报通道（生成版本、责任人和批次流水）
    for rep in seed.get("initial_reports", []):
        username = rep["enterprise_user"]
        with store.read() as conn:
            u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        actor = Actor(u["username"], u["role"], u["enterprise_id"], u["region_id"])
        svc.submit_report(actor, {
            "report_date": rep["report_date"],
            "source_ts": rep["source_ts"],
            "kind": "DAILY",
            "reporter": rep["reporter"],
            "lines": rep["lines"],
            "materials": rep["materials"],
            "batches": rep["batches"],
        }, received_ts=rep.get("received_ts", rep["source_ts"]))
    return True
