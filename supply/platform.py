"""平台门面：整合服务、身份与种子装载。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .db import Database
from .errors import PermissionDenied
from .services.allocation import AllocationService
from .services.fulfillment import FulfillmentService
from .services.production import ProductionService
from .services.reporting import ReportingService
from .services.requests import RequestService
from .services.views import ViewService


class SupplyPlatform:
    def __init__(self, db: Database):
        self.db = db
        self.production = ProductionService(db)
        self.reporting = ReportingService(db)
        self.requests = RequestService(db)
        self.allocations = AllocationService(db)
        self.fulfillment = FulfillmentService(db)
        self.views = ViewService(db)

    # -- 身份 -------------------------------------------------------------

    def actor(self, user_id: str) -> dict:
        row = self.db.writer().execute(
            "SELECT id, enterprise_id, role, name FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise PermissionDenied(f"未知用户：{user_id}")
        return dict(row)

    # -- 种子装载 ---------------------------------------------------------

    def load_seed(self, path: str | Path = "fixtures/seed.json") -> dict:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise ValueError("领域样例必须包含 records 数组")
        loaded: dict[str, int] = {}
        with self.db.begin_immediate() as conn:
            for rec in data["records"]:
                kind = rec["kind"]
                loaded[kind] = loaded.get(kind, 0) + 1
                self._insert_seed_record(conn, kind, rec)
        return {"loaded": loaded}

    @staticmethod
    def _insert_seed_record(conn: sqlite3.Connection, kind: str, rec: dict) -> None:
        fields = {k: v for k, v in rec.items() if k != "kind"}
        if kind == "medicine":
            conn.execute(
                "INSERT INTO medicines (id, name, priority, daily_demand) "
                "VALUES (?,?,?,?)",
                (rec["id"], rec.get("name", rec["id"]),
                 rec.get("priority", "重点"), int(rec.get("daily_demand", 0))),
            )
        elif kind == "enterprise":
            conn.execute(
                "INSERT INTO enterprises (id, name) VALUES (?,?)",
                (rec["id"], rec.get("name", rec["id"])),
            )
        elif kind == "user":
            conn.execute(
                "INSERT INTO users (id, enterprise_id, role, name) VALUES (?,?,?,?)",
                (rec["id"], rec.get("enterprise_id"), rec["role"],
                 rec.get("name", rec["id"])),
            )
        elif kind == "region":
            conn.execute(
                "INSERT INTO regions (id, name) VALUES (?,?)",
                (rec["id"], rec.get("name", rec["id"])),
            )
        elif kind == "material":
            conn.execute(
                "INSERT INTO materials (id, name, unit) VALUES (?,?,?)",
                (rec["id"], rec.get("name", rec["id"]), rec.get("unit", "kg")),
            )
        elif kind == "production_line":
            conn.execute(
                "INSERT INTO production_lines (id, enterprise_id, name, daily_capacity) "
                "VALUES (?,?,?,?)",
                (rec["id"], rec["enterprise_id"], rec.get("name", rec["id"]),
                 int(rec.get("daily_capacity", 0))),
            )
        elif kind == "license":
            conn.execute(
                "INSERT OR IGNORE INTO line_licenses (line_id, medicine_id) "
                "VALUES (?,?)",
                (rec["line_id"], rec["medicine_id"]),
            )
        elif kind == "changeover":
            conn.execute(
                "INSERT INTO line_changeovers (line_id, from_medicine, to_medicine, hours) "
                "VALUES (?,?,?,?)",
                (rec["line_id"], rec.get("from_medicine"), rec["to_medicine"],
                 float(rec["hours"])),
            )
        elif kind == "downtime":
            conn.execute(
                "INSERT INTO downtime_blocks (id, line_id, start_ts, end_ts, reason) "
                "VALUES (?,?,?,?,?)",
                (rec["id"], rec["line_id"], rec["start_ts"], rec["end_ts"],
                 rec.get("reason", "设备停机")),
            )
        elif kind == "transit":
            conn.execute(
                "INSERT INTO transit_times (enterprise_id, region_id, hours) "
                "VALUES (?,?,?)",
                (rec["enterprise_id"], rec["region_id"], float(rec["hours"])),
            )
        elif kind == "material_requirement":
            conn.execute(
                "INSERT INTO material_requirements (medicine_id, material_id, per_unit) "
                "VALUES (?,?,?)",
                (rec["medicine_id"], rec["material_id"], float(rec["per_unit"])),
            )
        elif kind == "material_supply":
            conn.execute(
                """
                INSERT INTO material_supply (id, enterprise_id, material_id,
                                             available_ts, quantity, reporter)
                VALUES (?,?,?,?,?,?)
                """,
                (rec["id"], rec["enterprise_id"], rec["material_id"],
                 rec["available_ts"], int(rec["quantity"]), rec["reporter"]),
            )
        elif kind == "batch":
            conn.execute(
                """
                INSERT INTO batches (id, line_id, enterprise_id, medicine_id, start_ts,
                                     produced_ts, qc_due_ts, quantity, qc_status,
                                     qc_decided_ts, qc_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (rec["id"], rec["line_id"], rec["enterprise_id"], rec["medicine_id"],
                 rec["start_ts"], rec["produced_ts"], rec.get("qc_due_ts"),
                 int(rec["quantity"]), rec.get("qc_status", "pending"),
                 rec.get("qc_decided_ts"), rec.get("qc_reason")),
            )
            if rec.get("qc_status") in ("released", "rejected"):
                conn.execute(
                    """
                    INSERT INTO qc_events (batch_id, ts, kind, new_due_ts, reporter_id, note)
                    VALUES (?,?,?, NULL, ?, ?)
                    """,
                    (
                        rec["id"],
                        rec.get("qc_decided_ts", rec["produced_ts"]),
                        "release" if rec["qc_status"] == "released" else "reject",
                        rec.get("reporter", "seed"),
                        rec.get("qc_reason", ""),
                    ),
                )
        else:
            raise ValueError(f"种子记录类型未知：{kind}")
