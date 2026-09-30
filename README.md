# 重点药品生产监测与调配后端

面向重点药品保供调度的后端服务：企业按**来源时间、责任人**版本化上报生产线、批次、
物料、检验放行情况；系统将**理论产能、等待检验成品、缺料在制品、已放行库存**四类
来源严格分列，只有**真实批次**上的可承诺量（ATP）才能被锁定。区域需求按紧急程度、
现有覆盖天数、运输时限形成**建议**，**只有监管人员确认后**才锁定产能和去向；换线清洁、
许可范围、原料短缺、设备停机是不可突破的硬约束。运输取消或检验不合格时，未履行部分
释放并可重新分配，旧决定与已履行数量永久不可改写。

## 核心规则

| 主题 | 规则 |
| --- | --- |
| 四类来源不混加 | 已放行未锁定库存才可立即承诺；待检成品、缺料在制品、理论产能均标注 `committable=false` |
| 生产可承诺 | 理论产能必须排产落到**具体批次+时间槽**，通过许可/换线清洁/停机/原料BOM/检验/运输全部约束 |
| 建议≠锁定 | 建议只计算与留痕，可反复重算（旧建议置为 superseded）；锁定在监管确认时**重新校验全部约束** |
| 并发不双锁 | 所有写事务 `BEGIN IMMEDIATE` 串行提交，确认时重检批次余量与产线/物料占用 |
| 只追加 | 上报版本、批次事件、承诺事件（LOCKED/FULFILLED/RELEASED）只追加，不更新删除 |
| 已履行不可改写 | 履行数量永久保留；释放只作用于未履行余额，锁随释放归还可承诺池 |
| 检验联动 | 不合格释放全部未履行；延期导致加运输后赶不上截止的才释放，赶得上保留 |
| 故障恢复 | SQLite WAL + 同步持久化 + 原子提交；写接口支持 `Idempotency-Key`，崩溃后重放不产生第二把锁 |
| 数据隔离 | 企业仅见本企业商业资料；区域仅见本区域需求与可达通道；监管见跨企业态势 |

## 运行

```bash
pip install -r requirements.txt
python3 -m supply_guard.api --host 127.0.0.1 --port 8080 --db supply_guard.db
```

首次启动用 `fixtures/seed.json` 播种（企业/区域/账号/许可/运输通道/停机/初始日报）。

## 测试

```bash
python3 -m pytest tests/ -q
```

覆盖：两地区同小时并发抢同一批次（多线程，恰赢一把锁）、跨日换线清洁与设备停机排产、
原料/许可/时限硬约束、迟报与更正版本链、检验延期/不合格释放、运输取消后再分配、
进程重启后的幂等重放（不重复锁定）、角色隔离与 HTTP 全流程。

## 主要接口

鉴权：`Authorization: Bearer <token>`；写接口建议带 `Idempotency-Key`。

- `POST /api/reports` 企业日报/更正（来源时间 `source_ts`、责任人 `reporter`；超 24h 标记迟报）
- `GET  /api/reports?date=` 本企业某经营日版本链
- `POST /api/qc-events` 检验 `RELEASE/REJECT/DELAY`（按事件来源时间）
- `POST /api/demands` 区域需求（紧急度、覆盖天数、需求截止时间）
- `POST /api/demands/{id}/cancel` 运输取消（自动释放未履行承诺）
- `POST /api/suggestions` 计算建议（`{demand_id}`，不锁定）
- `POST /api/commitments/approve` 监管确认（整单 `suggestion_id` 或逐项 `items`）
- `POST /api/commitments/{id}/fulfill` 登记履行
- `POST /api/commitments/{id}/release` 释放未履行部分
- `GET  /api/commitments` 承诺台账（按角色隔离）
- `GET  /api/atp?medicine_id=&region_id=` 可承诺量四分类视图
- `GET  /api/board` 跨企业保供态势（仅监管）
- `GET  /api/batches/{id}` 批次全链路追溯（事件 + 占用它的承诺 + 约束快照 + 批准依据）

种子账号令牌：监管 `tok-regulator`，华东/华北企业 `tok-huadong`/`tok-huabei`，
华东/华中区域 `tok-east`/`tok-central`。

## 承诺的可追溯依据

每条承诺都带 `constraint_snapshot`：来源类型（STOCK/PRODUCTION）、真实批次、
产线产能/换线/检验周期、排产时间槽、停机窗口、运输时限、批准人与批准文号，
以及确认时该批次/物料的已占用量。任一保供承诺都可回溯到真实批次、硬约束与批准依据。

## 代码结构

```
supply_guard/
  store.py     SQLite schema、BEGIN IMMEDIATE 事务、幂等表、只追加事件
  timeutil.py  UTC 时间
  service.py   领域服务：版本上报、ATP、排产约束、建议评分、确认锁定、履行/释放
  seed.py      主数据与初始日报引导
  api.py       http.server JSON API 与 Bearer 鉴权
fixtures/seed.json  现有生产与需求资料
tests/         并发/跨日换线/检验/恢复/隔离测试
```
