"""SQLite 持久化层。

设计要点：
- 所有写事务使用 ``BEGIN IMMEDIATE``，并发提交在数据库层串行化，
  第二个事务一定能看到第一个已提交的锁定，杜绝同一批次被承诺两次。
- 决定类记录（承诺、事件、上报版本、批次状态流水）只追加，不更新、不删除。
- 幂等键与业务写入在同一事务提交，崩溃恢复后重放不会产生第二把锁。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 身份与角色 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enterprises (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS regions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('REGULATOR','ENTERPRISE','REGION')),
    token TEXT NOT NULL UNIQUE,
    enterprise_id TEXT REFERENCES enterprises(id),
    region_id TEXT REFERENCES regions(id)
);

-- 主数据 -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS medicines (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT '重点',
    unit TEXT NOT NULL DEFAULT '支'
);
CREATE TABLE IF NOT EXISTS production_lines (
    id TEXT PRIMARY KEY,
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    name TEXT NOT NULL,
    daily_capacity REAL NOT NULL CHECK (daily_capacity > 0),
    changeover_hours REAL NOT NULL DEFAULT 0 CHECK (changeover_hours >= 0),
    qc_lead_hours REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS line_licenses (
    line_id TEXT NOT NULL REFERENCES production_lines(id),
    medicine_id TEXT NOT NULL REFERENCES medicines(id),
    PRIMARY KEY (line_id, medicine_id)
);
CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'kg'
);
CREATE TABLE IF NOT EXISTS bom (
    medicine_id TEXT NOT NULL REFERENCES medicines(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    qty_per_unit REAL NOT NULL CHECK (qty_per_unit > 0),
    PRIMARY KEY (medicine_id, material_id)
);
CREATE TABLE IF NOT EXISTS lanes (
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    region_id TEXT NOT NULL REFERENCES regions(id),
    transport_hours REAL NOT NULL CHECK (transport_hours >= 0),
    PRIMARY KEY (enterprise_id, region_id)
);
CREATE TABLE IF NOT EXISTS line_downtimes (
    id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL REFERENCES production_lines(id),
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    CHECK (end_ts > start_ts)
);

-- 企业日报（版本化，只追加） ----------------------------------------------
CREATE TABLE IF NOT EXISTS report_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    report_date TEXT NOT NULL,          -- 经营日 YYYY-MM-DD
    source_ts TEXT NOT NULL,            -- 来源时间（数据发生时刻）
    submitted_ts TEXT NOT NULL,         -- 系统接收时刻
    reporter TEXT NOT NULL,             -- 责任人
    kind TEXT NOT NULL CHECK (kind IN ('DAILY','CORRECTION')),
    supersedes_id INTEGER REFERENCES report_versions(id),
    late INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS report_line_summaries (
    version_id INTEGER NOT NULL REFERENCES report_versions(id),
    line_id TEXT NOT NULL REFERENCES production_lines(id),
    theoretical_capacity REAL NOT NULL, -- 理论产能
    awaiting_qc_qty REAL NOT NULL,      -- 等待检验成品（合计）
    wip_qty REAL NOT NULL,              -- 在制品（合计）
    PRIMARY KEY (version_id, line_id)
);
CREATE TABLE IF NOT EXISTS report_material_stock (
    version_id INTEGER NOT NULL REFERENCES report_versions(id),
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    on_hand_qty REAL NOT NULL,
    PRIMARY KEY (version_id, material_id)
);
-- 物料当前库存（可变当前态；每次上报以新版本为准覆盖，历史见 report_material_stock）
CREATE TABLE IF NOT EXISTS material_stock_current (
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    on_hand_qty REAL NOT NULL,
    version_id INTEGER NOT NULL REFERENCES report_versions(id),
    PRIMARY KEY (enterprise_id, material_id)
);

-- 批次台账（当前态）+ 状态流水（只追加） ------------------------------------
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,                -- 企业批号，全局真实批次
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    line_id TEXT NOT NULL REFERENCES production_lines(id),
    medicine_id TEXT NOT NULL REFERENCES medicines(id),
    source TEXT NOT NULL DEFAULT 'REPORT'
        CHECK (source IN ('REPORT','SYSTEM_PLAN')),
    status TEXT NOT NULL CHECK (status IN
        ('PLANNED','IN_PROGRESS','AWAIT_QC','RELEASED','REJECTED','CANCELLED')),
    planned_qty REAL NOT NULL CHECK (planned_qty >= 0),
    planned_start TEXT,
    planned_finish TEXT,
    expected_release_ts TEXT,
    qty_released REAL NOT NULL DEFAULT 0,
    material_short INTEGER NOT NULL DEFAULT 0,
    created_version_id INTEGER REFERENCES report_versions(id),
    updated_version_id INTEGER
);
CREATE TABLE IF NOT EXISTS batch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    event_type TEXT NOT NULL,           -- CREATED/STATUS/QC_RELEASED/QC_REJECTED/QC_DELAY/CORRECTION
    from_status TEXT,
    to_status TEXT,
    qty REAL,
    event_ts TEXT NOT NULL,             -- 来源时间
    recorded_ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    version_id INTEGER,
    payload TEXT
);

-- 区域需求 ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS demands (
    id TEXT PRIMARY KEY,
    region_id TEXT NOT NULL REFERENCES regions(id),
    medicine_id TEXT NOT NULL REFERENCES medicines(id),
    qty REAL NOT NULL CHECK (qty > 0),
    urgency TEXT NOT NULL CHECK (urgency IN ('URGENT','NORMAL')),
    coverage_days REAL NOT NULL DEFAULT 0,
    needed_by_ts TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','PARTIAL','FULFILLED','CANCELLED'))
);

-- 建议（不锁定任何产能；确认时重新校验） ------------------------------------
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    demand_id TEXT NOT NULL REFERENCES demands(id),
    computed_ts TEXT NOT NULL,
    score REAL NOT NULL,
    rationale TEXT NOT NULL,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS suggestion_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_id INTEGER NOT NULL REFERENCES suggestions(id),
    seq INTEGER NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('STOCK','QC_PENDING','WIP','PRODUCTION')),
    enterprise_id TEXT NOT NULL,
    batch_id TEXT,                      -- PRODUCTION 在确认时回填
    line_id TEXT,
    qty REAL NOT NULL CHECK (qty > 0),
    deliverable_ts TEXT NOT NULL,
    detail TEXT NOT NULL                -- JSON 快照
);

-- 承诺（不可变决定；状态字段只追加式推进） ----------------------------------
CREATE TABLE IF NOT EXISTS commitments (
    id TEXT PRIMARY KEY,
    demand_id TEXT NOT NULL REFERENCES demands(id),
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    medicine_id TEXT NOT NULL REFERENCES medicines(id),
    batch_id TEXT NOT NULL REFERENCES batches(id),
    qty_committed REAL NOT NULL CHECK (qty_committed > 0),
    qty_fulfilled REAL NOT NULL DEFAULT 0 CHECK (qty_fulfilled >= 0),
    qty_released REAL NOT NULL DEFAULT 0 CHECK (qty_released >= 0),
    deliverable_ts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE','FULFILLED','RELEASED','PARTIALLY_RELEASED','PARTIALLY_FULFILLED')),
    source_suggestion_id INTEGER REFERENCES suggestions(id),
    constraint_snapshot TEXT NOT NULL,  -- JSON：许可/换线/停机/物料/运输依据
    approved_by TEXT NOT NULL,
    approved_ts TEXT NOT NULL,
    approval_note TEXT,
    CHECK (qty_fulfilled + qty_released <= qty_committed)
);
-- 批次占用：每行是承诺对某批次真实数量的锁定
CREATE TABLE IF NOT EXISTS batch_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id TEXT NOT NULL REFERENCES commitments(id),
    batch_id TEXT NOT NULL REFERENCES batches(id),
    qty REAL NOT NULL CHECK (qty > 0)
);
CREATE TABLE IF NOT EXISTS commitment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id TEXT NOT NULL REFERENCES commitments(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('LOCKED','FULFILLED','RELEASED')),
    qty REAL NOT NULL CHECK (qty > 0),
    reason TEXT,
    actor TEXT NOT NULL,
    event_ts TEXT NOT NULL,
    payload TEXT
);

-- 新批次的物料预留（缺料批次不预留、不可承诺） -------------------------------
CREATE TABLE IF NOT EXISTS material_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    qty REAL NOT NULL CHECK (qty >= 0),
    commitment_id TEXT,
    UNIQUE (batch_id, material_id)
);

-- 幂等与审计 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS idempotency (
    idem_key TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_batches_ent_med ON batches(enterprise_id, medicine_id, status);
CREATE INDEX IF NOT EXISTS idx_locks_batch ON batch_locks(batch_id);
CREATE INDEX IF NOT EXISTS idx_commit_demand ON commitments(demand_id);
CREATE INDEX IF NOT EXISTS idx_downtime_line ON line_downtimes(line_id);
CREATE INDEX IF NOT EXISTS idx_batches_line_time ON batches(line_id, planned_finish);
"""


class Store:
    """线程安全的 SQLite 封装。每个事务使用独立连接。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        # :memory: 需共享缓存，否则每个连接看到的都是空库；
        # _keeper 持有长连接保证共享内存在 Store 生命周期内不被回收。
        self._keeper = None
        connect_path = self.path
        if self.path == ":memory:":
            connect_path = (
                "file:supply_guard_mem_%d?mode=memory&cache=shared" % id(self))
        first = sqlite3.connect(connect_path, timeout=15, isolation_level=None,
                                uri=connect_path.startswith("file:"))
        try:
            first.execute("PRAGMA journal_mode=WAL")
            first.execute("PRAGMA synchronous=FULL")
            first.execute("PRAGMA foreign_keys=ON")
            first.execute("PRAGMA busy_timeout=15000")
            first.executescript(SCHEMA)
            first.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            if self.path == ":memory:":
                self._keeper = first
        finally:
            if self._keeper is None:
                first.close()

    def _connect(self) -> sqlite3.Connection:
        connect_path = self.path
        if self.path == ":memory:":
            connect_path = (
                "file:supply_guard_mem_%d?mode=memory&cache=shared" % id(self))
        conn = sqlite3.connect(connect_path, timeout=15, isolation_level=None,
                               uri=connect_path.startswith("file:"))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE 串行写事务；异常回滚。

        文件库 WAL 下，抢锁失败表现为 SQLITE_BUSY，由 busy_timeout 等待串行；
        ROLLBACK 仅在事务确实开启时执行。
        """
        conn = self._connect()
        began = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            began = True
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if began:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def log_event(
    conn: sqlite3.Connection,
    *,
    ts: str,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: Any = None,
) -> None:
    conn.execute(
        "INSERT INTO events(ts, actor, action, entity_type, entity_id, payload)"
        " VALUES(?,?,?,?,?,?)",
        (ts, actor, action, entity_type, str(entity_id), dumps(payload) if payload is not None else None),
    )
