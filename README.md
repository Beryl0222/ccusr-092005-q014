# 重点药品生产监测与调配后端

面向重点药品保供调度的事实底账与调配服务。解决核心问题：**企业日报把理论产能、
等待检验成品、缺料在制品混在一起，调度员直接相加会把同一条生产线向两个地区
承诺两次。** 系统把“可承诺量”严格锚定到**检验已放行的真实批次**，建议只计算、
不锁定；只有监管人员确认后才锁定产能与去向，且任何故障恢复都不会重复锁定。

## 设计原则

1. **三类数字分离，永不相加**：理论产能（`theoretical_capacity`）、等待检验成品
   （`pending_qc`）、缺料在制品（`wip_material_short`）只进版本化日报做监测；
   真正可承诺量（`deliverable`）必须逐批溯源到 `qc_status='released'` 的真实批次，
   且不得超过批次扣除已锁定后的余量。
2. **建议不锁产能，确认才锁定**：`/allocations/plan` 仅产出建议（含依据/评分），
   `/decisions/confirm` 在 `BEGIN IMMEDIATE` 事务内重算批次余量后落库。
3. **不可突破的约束**：许可范围、换线清洁（按绝对时间，跨日有效）、设备停机、
   原料短缺——在批次登记时强制拦截；锁定环节再由数据库触发器兜底。
4. **只追加（append-only）事实**：日报版本、检验事件、监管决定、履行/释放事件
   均不可 UPDATE/DELETE；旧决定和已履行数量不能被改写。
5. **取消/不合格只释放未履行部分**：运输取消或整批检验不合格时，未履行份额
   回流可重新分配；已履行部分保留。
6. **故障恢复不重复锁定**：申请 `client_token`、决定 `idempotency_key`、
   事件 `idempotency_key` 三重幂等键 + SQLite WAL 事务 + 批次占用触发器。
7. **分级可见**：企业账号仅见本企业批次/承诺/日报；监管账号可见跨企业态势，
   并可对任一承诺做批次—约束—批准依据的全链溯源。

## 技术栈

Python 3 标准库（SQLite + `http.server`），无运行期第三方依赖。
`requirements.txt` 中的 pytest 仅为可选测试工具，`python3 -m unittest` 即可运行。

## 目录

```
supply/
  schema.sql                 # 全部表与硬约束触发器
  db.py                      # 单一写连接 + BEGIN IMMEDIATE 事务
  constraints.py             # 许可/换线/停机/缺料校验
  timeutil.py                # ISO8601 时间
  errors.py                  # 领域异常 -> HTTP 状态码
  platform.py                # 服务门面 + 种子装载
  httpapi.py                 # JSON HTTP API
  services/
    production.py            # 批次登记、检验放行/延期/不合格（自动释放）
    reporting.py             # 版本化日报、迟报与更正
    requests.py              # 区域保供申请
    allocation.py            # 建议引擎 + 监管确认锁定
    fulfillment.py           # 只追加履行/释放台账
    views.py                 # 企业/监管视图与承诺溯源
fixtures/seed.json           # 两企业/两地区完整样例
tests/                       # 53 项测试（unittest）
```

## 启动

```bash
python3 -m supply --db supply.db --seed fixtures/seed.json --port 8080
# 可选 --reset 清库重建，--verbose 打印访问日志
```

所有业务接口需 `X-User-Id` 头（种子账号：`u-reg` 监管员、`u-alpha`/`u-beta` 企业）。
`GET /health` 免鉴权。

## API 摘要

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/batches` | 企业 | 登记批次（强制校验许可/换线/停机/缺料） |
| POST | `/batches/{id}/qc` | 企业/监管 | 检验 `release`/`reject`/`postpone`（只追加） |
| POST | `/material-supplies` | 企业 | 物料到货（来源时间+责任人，记录 ID 幂等） |
| POST | `/reports` | 企业 | 版本化日报；迟报打标，更正产生新版本 |
| GET | `/reports`、`/reports?id=` | 企业(本企业)/监管 | 历史版本可查 |
| POST | `/requests` | 监管 | 保供申请（紧急程度/覆盖天数/到货时限/`client_token`） |
| POST | `/allocations/plan` | 监管 | 计算建议，不锁定 |
| POST | `/decisions/confirm` | 监管 | 确认后锁定批次与去向（`idempotency_key`） |
| POST | `/commitments/{id}/events` | 企业 | 履行 `shipment` / 运输取消释放 `transport_cancel` |
| GET | `/commitments/{id}` | 本企业/监管 | 承诺与事件台账 |
| GET | `/commitments/{id}/trace` | 本企业/监管 | 批次+检验+许可+决定+建议依据全链溯源 |
| GET | `/dashboard/enterprise` | 企业 | 仅本企业商业资料 |
| GET | `/dashboard/regulator` | 监管 | 跨企业态势 |

### 日报示例（三类数字分列，可承诺量逐批溯源）

```json
{
  "line_id": "line-alpha-01",
  "production_date": "2026-09-29",
  "source_ts": "2026-09-29T23:30:00Z",
  "items": [{
    "medicine_id": "drug-emergency-a",
    "theoretical_capacity": 26000,
    "pending_qc": 10000,
    "wip_material_short": 4000,
    "deliverable": 5000,
    "batches": [{"batch_id": "batch-a-rel-0929", "quantity": 5000}]
  }]
}
```

`deliverable` 必须等于已放行批次溯源数量之和；待检批次、不合格批次、已被其他
地区锁定的份额都会被拒绝。

## 建议计算口径

* 需求排序：紧急程度（critical>urgent>normal）→ 现有覆盖天数升序 → 到货截止。
* 候选条件：已放行且未判不合格；企业-区域有运输时限记录；放行时间+运输时长
  不晚于到货截止。
* 同轮计划内更高优先级申请对批次的预留对后续申请可见，防止“同一批次承诺两次”；
* 行内 `rationale` 记录批次、放行时间、运输时长、预计到达与时限余量。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：两地区同小时并发申请的计划与确认（真实多线程抢锁，唯一赢家）、幂等重放、
文件库进程重开故障恢复、跨日换线清洁、设备停机、许可范围、原料短缺、检验延期
与不合格自动释放、运输取消回流再分配、日报版本化与历史不可变、分级可见性、
HTTP 端到端。
