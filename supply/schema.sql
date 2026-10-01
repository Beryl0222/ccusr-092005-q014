-- 重点药品生产监测与调配：事实底账
--
-- 设计原则：
--  1. 理论产能 / 等待检验成品 / 缺料在制品 只出现在版本化日报快照中，永远不参与承诺。
--  2. 可承诺量只有一个来源：检验状态=released 的真实批次。
--  3. 所有数量变动都是只追加（append-only）事件；已履行数量与旧决定不可改写。
--  4. 硬约束（许可范围、换线清洁、缺料、停机）在数据层排除 + 触发器兜底，
--     任何应用路径都无法突破。
--  5. 触发器 + 幂等键 + 单一写连接（BEGIN IMMEDIATE）共同保证故障恢复后不重复锁定。

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 组织与身份
-- ---------------------------------------------------------------------------

CREATE TABLE enterprises (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE users (
    id             TEXT PRIMARY KEY,
    enterprise_id  TEXT REFERENCES enterprises(id),  -- 监管人员为 NULL
    role           TEXT NOT NULL CHECK (role IN ('enterprise', 'regulator')),
    name           TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 静态底账：药品、生产线、许可、物料、停机/换线块、运输时限
-- ---------------------------------------------------------------------------

CREATE TABLE medicines (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    priority      TEXT NOT NULL DEFAULT '重点',
    daily_demand  INTEGER NOT NULL CHECK (daily_demand >= 0)
);

CREATE TABLE production_lines (
    id              TEXT PRIMARY KEY,
    enterprise_id   TEXT NOT NULL REFERENCES enterprises(id),
    name            TEXT NOT NULL,
    daily_capacity  INTEGER NOT NULL CHECK (daily_capacity >= 0),  -- 理论日产能（仅监测）
    active          INTEGER NOT NULL DEFAULT 1
);

-- 许可范围：生产线允许生产哪些药品。不在表中 = 不被许可 = 硬约束。
CREATE TABLE line_licenses (
    line_id     TEXT NOT NULL REFERENCES production_lines(id),
    medicine_id TEXT NOT NULL REFERENCES medicines(id),
    PRIMARY KEY (line_id, medicine_id)
);

CREATE TABLE materials (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    unit  TEXT NOT NULL DEFAULT 'kg'
);

-- 生产线换线清洁所需时长（小时）。同品换线为 0。
CREATE TABLE line_changeovers (
    line_id       TEXT NOT NULL REFERENCES production_lines(id),
    from_medicine TEXT REFERENCES medicines(id),   -- NULL 表示任意/冷启动
    to_medicine   TEXT NOT NULL REFERENCES medicines(id),
    hours         REAL NOT NULL CHECK (hours >= 0),
    PRIMARY KEY (line_id, from_medicine, to_medicine)
);

-- 不可用时间窗：设备停机、计划性维护。换线清洁窗口由应用按
-- (上一批次完工时间, +changeover_hours] 推导，二者都属于不可突破约束。
CREATE TABLE downtime_blocks (
    id          TEXT PRIMARY KEY,
    line_id     TEXT NOT NULL REFERENCES production_lines(id),
    start_ts    TEXT NOT NULL,   -- ISO8601，含时区或 Z
    end_ts      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    CHECK (end_ts > start_ts)
);

CREATE TABLE regions (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

-- 区域-企业运输时限（小时）。无记录 = 不可达，不产生调配建议。
CREATE TABLE transit_times (
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    region_id     TEXT NOT NULL REFERENCES regions(id),
    hours         REAL NOT NULL CHECK (hours >= 0),
    PRIMARY KEY (enterprise_id, region_id)
);

-- ---------------------------------------------------------------------------
-- 真实批次：可承诺量的唯一来源
-- ---------------------------------------------------------------------------

CREATE TABLE batches (
    id              TEXT PRIMARY KEY,
    line_id         TEXT NOT NULL REFERENCES production_lines(id),
    enterprise_id   TEXT NOT NULL REFERENCES enterprises(id),
    medicine_id     TEXT NOT NULL REFERENCES medicines(id),
    start_ts        TEXT NOT NULL,               -- 开工时间（换线/停机校验窗口起点）
    produced_ts     TEXT NOT NULL,               -- 下线时间
    qc_due_ts       TEXT,                        -- 检验预计放行时间（延期时只追加事件）
    quantity        INTEGER NOT NULL CHECK (quantity >= 0),  -- 批次总数量
    qc_status       TEXT NOT NULL DEFAULT 'pending'
                    CHECK (qc_status IN ('pending','released','rejected')),
    qc_decided_ts   TEXT,
    qc_reason       TEXT,
    CHECK (produced_ts >= start_ts),
    UNIQUE (id, enterprise_id)
);

-- 检验生命周期事件：放行 / 不合格 / 延期（只追加，批次状态是其折叠结果）。
CREATE TABLE qc_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         TEXT NOT NULL REFERENCES batches(id),
    ts               TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('release','reject','postpone')),
    new_due_ts       TEXT,                        -- postpone 时的新预计放行时间
    reporter_id      TEXT NOT NULL REFERENCES users(id),
    note             TEXT NOT NULL DEFAULT ''
);

-- 原料库存/到货（按企业+物料+时间点）。availability 计算时物料不足 => 缺料硬约束。
CREATE TABLE material_supply (
    id            TEXT PRIMARY KEY,
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    material_id   TEXT NOT NULL REFERENCES materials(id),
    available_ts  TEXT NOT NULL,                 -- 该数量自何时起可用
    quantity      INTEGER NOT NULL CHECK (quantity >= 0),
    reporter      TEXT NOT NULL REFERENCES users(id)
);

-- 生产单位产品对物料的消耗（用于缺料判定）
CREATE TABLE material_requirements (
    medicine_id  TEXT NOT NULL REFERENCES medicines(id),
    material_id  TEXT NOT NULL REFERENCES materials(id),
    per_unit     REAL NOT NULL CHECK (per_unit >= 0),
    PRIMARY KEY (medicine_id, material_id)
);

-- ---------------------------------------------------------------------------
-- 版本化企业日报
--   同一 report_key 多次上报 => 新版本；旧版本快照永不修改。
-- ---------------------------------------------------------------------------

CREATE TABLE daily_reports (
    id                TEXT PRIMARY KEY,
    report_key        TEXT NOT NULL,             -- 业务幂等键：企业+生产日期+生产线
    enterprise_id     TEXT NOT NULL REFERENCES enterprises(id),
    version_no        INTEGER NOT NULL,
    source_ts         TEXT NOT NULL,             -- 资料来源时间（企业填报口径）
    submitted_ts      TEXT NOT NULL,             -- 系统接收时间
    due_ts            TEXT NOT NULL,             -- 应报时限
    reporter_id       TEXT NOT NULL REFERENCES users(id),
    line_id           TEXT NOT NULL REFERENCES production_lines(id),
    is_late           INTEGER NOT NULL DEFAULT 0,   -- submitted_ts 晚于 due_ts
    is_correction     INTEGER NOT NULL DEFAULT 0,
    supersedes_id     TEXT REFERENCES daily_reports(id),
    note              TEXT NOT NULL DEFAULT '',
    UNIQUE (report_key, version_no)
);

-- 日报行项目：三类“看起来有货但不能承诺”的数量与真正可承诺量严格分列。
CREATE TABLE daily_report_items (
    id                  TEXT PRIMARY KEY,
    report_id           TEXT NOT NULL REFERENCES daily_reports(id),
    medicine_id         TEXT NOT NULL REFERENCES medicines(id),
    theoretical_capacity INTEGER NOT NULL CHECK (theoretical_capacity >= 0),  -- 理论产能
    pending_qc          INTEGER NOT NULL CHECK (pending_qc >= 0),             -- 等待检验成品
    wip_material_short  INTEGER NOT NULL CHECK (wip_material_short >= 0),      -- 缺料在制品
    -- 真正可承诺量必须等于条目列出的已放行批次可承诺量之和（应用校验，
    -- 且必须能在 batches 中逐批溯源），不得等于前三列之和。
    deliverable         INTEGER NOT NULL CHECK (deliverable >= 0)
);

-- 可承诺量逐批溯源：日报的 deliverable 必须由真实已放行批次支撑。
CREATE TABLE report_deliverable_batches (
    item_id           TEXT NOT NULL REFERENCES daily_report_items(id),
    batch_id          TEXT NOT NULL REFERENCES batches(id),
    enterprise_id     TEXT NOT NULL REFERENCES enterprises(id),
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (item_id, batch_id)
);

-- 每个报告键只保留一个“当前版本”指针；旧版本保留可查。
CREATE TABLE report_current_versions (
    report_key    TEXT PRIMARY KEY,
    report_id     TEXT NOT NULL UNIQUE REFERENCES daily_reports(id),
    version_no    INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- 区域保供申请
-- ---------------------------------------------------------------------------

CREATE TABLE supply_requests (
    id              TEXT PRIMARY KEY,
    region_id       TEXT NOT NULL REFERENCES regions(id),
    medicine_id     TEXT NOT NULL REFERENCES medicines(id),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    needed_by_ts    TEXT NOT NULL,               -- 运输时限截止（到货不得晚于此）
    urgency         TEXT NOT NULL CHECK (urgency IN ('critical','urgent','normal')),
    stock_days      REAL NOT NULL CHECK (stock_days >= 0),  -- 现有覆盖天数
    created_ts      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','planning','locked','partial','closed')),
    client_token    TEXT NOT NULL UNIQUE          -- 申请幂等键
);

-- ---------------------------------------------------------------------------
-- 调配建议（计算结果，不锁任何产能）
-- ---------------------------------------------------------------------------

CREATE TABLE allocations (
    id               TEXT PRIMARY KEY,
    request_id       TEXT NOT NULL REFERENCES supply_requests(id),
    batch_id         TEXT NOT NULL REFERENCES batches(id),
    enterprise_id    TEXT NOT NULL REFERENCES enterprises(id),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    available_ts     TEXT NOT NULL,               -- 可发运时间（已放行）
    transit_hours    REAL NOT NULL,
    arrives_ts       TEXT NOT NULL,
    score            REAL NOT NULL,               -- 建议排序分
    rationale        TEXT NOT NULL DEFAULT '',    -- 约束与计算依据（人类可读）
    created_ts       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed','confirmed','rejected','expired'))
);

-- ---------------------------------------------------------------------------
-- 监管决定：确认后才锁定产能与去向
-- ---------------------------------------------------------------------------

CREATE TABLE decisions (
    id              TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL REFERENCES supply_requests(id),
    regulator_id    TEXT NOT NULL REFERENCES users(id),
    decided_ts      TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    -- 同一申请的同一决定只能落库一次（重试/崩溃恢复的防重入口）
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE TABLE commitments (
    id              TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL REFERENCES decisions(id),
    allocation_id   TEXT NOT NULL UNIQUE REFERENCES allocations(id),  -- 一个建议最多锁一次
    request_id      TEXT NOT NULL REFERENCES supply_requests(id),
    batch_id        TEXT NOT NULL REFERENCES batches(id),
    enterprise_id   TEXT NOT NULL REFERENCES enterprises(id),
    region_id       TEXT NOT NULL REFERENCES regions(id),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    locked_ts       TEXT NOT NULL,
    -- 运行态由事件汇总维护，不由应用直接改写：
    fulfilled_qty   INTEGER NOT NULL DEFAULT 0,
    released_qty    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','released','completed','rejected_batch')),
    CHECK (released_qty >= 0),
    CHECK (fulfilled_qty + released_qty <= quantity)
);

-- 只追加事件：履行 / 释放。已写入的行永远不能 UPDATE/DELETE。
CREATE TABLE commitment_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id   TEXT NOT NULL REFERENCES commitments(id),
    ts              TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('fulfill','release')),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    reason          TEXT NOT NULL,                -- shipment / transport_cancel / qc_reject
    ref             TEXT NOT NULL DEFAULT '',     -- 运单号等外部凭证
    reporter_id     TEXT NOT NULL REFERENCES users(id),
    idempotency_key TEXT NOT NULL UNIQUE,         -- 事件级防重（崩溃重放不重复入账）
    CHECK (reason IN ('shipment','transport_cancel','qc_reject'))
);

-- 检验不合格时按批次作废承诺的记录（批次级释放审计）
CREATE TABLE batch_rejections (
    batch_id     TEXT PRIMARY KEY REFERENCES batches(id),
    rejected_ts  TEXT NOT NULL,
    reporter_id  TEXT NOT NULL REFERENCES users(id),
    reason       TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- 触发器：把不可突破的规则钉在数据层
-- ---------------------------------------------------------------------------

-- 批次维度的锁/履约/释放总量视图（INSERT 承诺或事件时逐行重算）
DROP TRIGGER IF EXISTS trg_commitment_batch_overlock_insert;
DROP TRIGGER IF EXISTS trg_commitment_batch_overlock_update;
DROP TRIGGER IF EXISTS trg_event_overfulfill_insert;
DROP TRIGGER IF EXISTS trg_event_immutable_update;
DROP TRIGGER IF EXISTS trg_event_immutable_delete;
DROP TRIGGER IF EXISTS trg_commitment_immutable_update;
DROP TRIGGER IF EXISTS trg_commitment_immutable_delete;
DROP TRIGGER IF EXISTS trg_commitment_balance_update;
DROP TRIGGER IF EXISTS trg_released_batch_no_new_lock;
DROP TRIGGER IF EXISTS trg_pending_batch_no_lock;
DROP TRIGGER IF EXISTS trg_commitment_request_overlock_insert;
DROP TRIGGER IF EXISTS trg_commitment_request_overlock_update;

-- 规则 A：同一批次的有效承诺占用永远不得超过批次数量。
-- 已释放（released_qty / 整批退回）的份额回流，可被再次分配。
CREATE TRIGGER trg_commitment_batch_overlock_insert
AFTER INSERT ON commitments
WHEN (
    SELECT COALESCE(SUM(c.quantity - c.released_qty),0)
    FROM commitments c
    WHERE c.batch_id = NEW.batch_id
      AND c.status IN ('active','completed','released')
) > (SELECT quantity FROM batches WHERE id = NEW.batch_id)
BEGIN
    SELECT RAISE(ABORT, '批次有效承诺总量超过批次数量：同一产能被重复锁定');
END;

-- 释放导致 released_qty/状态变化时同样重算（纵深防御）。
CREATE TRIGGER trg_commitment_batch_overlock_update
AFTER UPDATE OF quantity, status, batch_id, released_qty ON commitments
WHEN (
    SELECT COALESCE(SUM(c.quantity - c.released_qty),0)
    FROM commitments c
    WHERE c.batch_id = NEW.batch_id
      AND c.status IN ('active','completed','released')
) > (SELECT quantity FROM batches WHERE id = NEW.batch_id)
BEGIN
    SELECT RAISE(ABORT, '批次有效承诺总量超过批次数量');
END;

-- 规则 B：只追加事件累计（履行+释放）不得超过承诺数量；
-- 且事件不允许修改/删除——旧决定和已履行数量不能被改写。
CREATE TRIGGER trg_event_overfulfill_insert
AFTER INSERT ON commitment_events
WHEN (
    SELECT COALESCE(SUM(CASE WHEN kind='fulfill' THEN quantity ELSE 0 END),0)
         + COALESCE(SUM(CASE WHEN kind='release' THEN quantity ELSE 0 END),0)
    FROM commitment_events WHERE commitment_id = NEW.commitment_id
) > (SELECT quantity FROM commitments WHERE id = NEW.commitment_id)
BEGIN
    SELECT RAISE(ABORT, '履行与释放累计超过承诺数量');
END;

CREATE TRIGGER trg_event_immutable_update
BEFORE UPDATE ON commitment_events
BEGIN
    SELECT RAISE(ABORT, '承诺事件只追加，禁止修改');
END;

CREATE TRIGGER trg_event_immutable_delete
BEFORE DELETE ON commitment_events
BEGIN
    SELECT RAISE(ABORT, '承诺事件只追加，禁止删除');
END;

CREATE TRIGGER trg_commitment_immutable_delete
BEFORE DELETE ON commitments
BEGIN
    SELECT RAISE(ABORT, '已生效的承诺不可删除');
END;

-- 承诺数量本身锁定后不可变；履行/释放只能经只追加事件汇总到余额列。
CREATE TRIGGER trg_commitment_immutable_update
BEFORE UPDATE OF quantity, batch_id, decision_id, request_id ON commitments
BEGIN
    SELECT RAISE(ABORT, '承诺的数量、批次与批准依据锁定后不可变');
END;

-- 余额约束：履行与释放是互斥的两种去向，合计不得超过承诺数量。
CREATE TRIGGER trg_commitment_balance_update
AFTER UPDATE OF fulfilled_qty, released_qty ON commitments
WHEN NEW.fulfilled_qty + NEW.released_qty > NEW.quantity
  OR NEW.fulfilled_qty < 0 OR NEW.released_qty < 0
BEGIN
    SELECT RAISE(ABORT, '履行与释放合计超过承诺数量');
END;

-- 规则 C：只有 released 批次才能被锁定。
CREATE TRIGGER trg_pending_batch_no_lock
AFTER INSERT ON commitments
WHEN (SELECT qc_status FROM batches WHERE id = NEW.batch_id) != 'released'
BEGIN
    SELECT RAISE(ABORT, '只能锁定检验放行(released)的真实批次');
END;

-- 规则 D：同一申请的净锁定总量不得超过申请数量（释放份额已扣除）。
CREATE TRIGGER trg_commitment_request_overlock_insert
AFTER INSERT ON commitments
WHEN (
    SELECT COALESCE(SUM(c.quantity - c.released_qty),0)
    FROM commitments c
    WHERE c.request_id = NEW.request_id
      AND c.status IN ('active','completed','released')
) > (SELECT quantity FROM supply_requests WHERE id = NEW.request_id)
BEGIN
    SELECT RAISE(ABORT, '同一申请累计锁定超过申请数量');
END;

CREATE TRIGGER trg_commitment_request_overlock_update
AFTER UPDATE OF quantity, released_qty, status, request_id ON commitments
WHEN (
    SELECT COALESCE(SUM(c.quantity - c.released_qty),0)
    FROM commitments c
    WHERE c.request_id = NEW.request_id
      AND c.status IN ('active','completed','released')
) > (SELECT quantity FROM supply_requests WHERE id = NEW.request_id)
BEGIN
    SELECT RAISE(ABORT, '同一申请累计锁定超过申请数量');
END;

-- 已被整批判不合格的批次不得再产生承诺。
CREATE TRIGGER trg_released_batch_no_new_lock
AFTER INSERT ON commitments
WHEN EXISTS (SELECT 1 FROM batch_rejections WHERE batch_id = NEW.batch_id)
BEGIN
    SELECT RAISE(ABORT, '该批次已判不合格，不得锁定');
END;

-- 有用索引
CREATE INDEX idx_batches_med_status ON batches(medicine_id, qc_status);
CREATE INDEX idx_commitments_batch ON commitments(batch_id, status);
CREATE INDEX idx_events_commitment ON commitment_events(commitment_id);
CREATE INDEX idx_alloc_request ON allocations(request_id, status);
CREATE INDEX idx_reports_ent ON daily_reports(enterprise_id, version_no);

-- 规则 D：决定、检验事件、日报历史版本均为只追加事实，禁止修改/删除。
DROP TRIGGER IF EXISTS trg_decision_immutable_update;
DROP TRIGGER IF EXISTS trg_decision_immutable_delete;
DROP TRIGGER IF EXISTS trg_qc_event_immutable_update;
DROP TRIGGER IF EXISTS trg_qc_event_immutable_delete;
DROP TRIGGER IF EXISTS trg_report_immutable_update;
DROP TRIGGER IF EXISTS trg_report_immutable_delete;
DROP TRIGGER IF EXISTS trg_report_item_immutable_update;
DROP TRIGGER IF EXISTS trg_report_item_immutable_delete;

CREATE TRIGGER trg_decision_immutable_update BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, '监管决定一经作出不可修改');
END;
CREATE TRIGGER trg_decision_immutable_delete BEFORE DELETE ON decisions
BEGIN
    SELECT RAISE(ABORT, '监管决定不可删除');
END;
CREATE TRIGGER IF NOT EXISTS trg_qc_event_immutable_update BEFORE UPDATE ON qc_events
BEGIN
    SELECT RAISE(ABORT, '检验事件只追加，禁止修改');
END;
CREATE TRIGGER IF NOT EXISTS trg_qc_event_immutable_delete BEFORE DELETE ON qc_events
BEGIN
    SELECT RAISE(ABORT, '检验事件只追加，禁止删除');
END;
CREATE TRIGGER trg_report_immutable_update BEFORE UPDATE ON daily_reports
BEGIN
    SELECT RAISE(ABORT, '日报历史版本不可修改，更正请提交新版本');
END;
CREATE TRIGGER trg_report_immutable_delete BEFORE DELETE ON daily_reports
BEGIN
    SELECT RAISE(ABORT, '日报历史版本不可删除');
END;
CREATE TRIGGER trg_report_item_immutable_update BEFORE UPDATE ON daily_report_items
BEGIN
    SELECT RAISE(ABORT, '日报历史版本不可修改');
END;
CREATE TRIGGER trg_report_item_immutable_delete BEFORE DELETE ON daily_report_items
BEGIN
    SELECT RAISE(ABORT, '日报历史版本不可删除');
END;
