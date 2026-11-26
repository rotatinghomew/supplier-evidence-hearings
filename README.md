# 原料拒收申诉服务

面向柑橘原料采购中的多方拒收异议：厂方登记拒收，供货商对部分事实提出异议，经办人标明各方/事项/代表资格并交接证据，复核人按事项裁决，仓班只执行已生效且可独立执行的部分。

仅使用 CPython 3.11 标准库；业务状态可保存在 JSON 文件，也可在测试中使用内存实例。

## 1. 快速运行

```sh
# 核查输入交换格式并运行本服务测试
python3 -m unittest discover -s tests -v

# 从 inputs/ 与 evidence/ 载入合成样例到 var/state.json
python3 -m appeals_service seed --state var/state.json

# 启动 JSON HTTP 服务；首次可用 --seed 直接播种
python3 -m appeals_service serve --seed --state var/state.json --host 127.0.0.1 --port 8080

# 等价的零参数便利入口：默认 --seed 并使用 var/state.json、127.0.0.1:8080
python3 app.py

# 内存中跑通“裁决 → 实物回执 → 送达纠正 → 补救移送”演示
python3 -m appeals_service demo
```

服务无第三方依赖。所有写接口接收 JSON；身份通过 `X-Actor: 人员编号` 传递，也可在 JSON 中给 `actor`。时间字段均可传带偏移量的 ISO 8601 字符串，便于回放。

## 2. 合成样例中的关键结论

播种后的案件为 `CASE-ORDER-73`（采购约定 `ORDER-73`）。

- `DG19/Z1`：13:03 回报 `unknown`。**未知不等于确定未送**，不启动答辩期。
- `DG19/Z2`：同一 `bundle=DG19` 的后续回执，13:12 证明物理交付给 `LP-01`；但 `LP-01` 的代表权限在 13:10 到期，且上界不含端点，因此该次交付**物理存在但不构成有效送达**。
- `DG20/Z3`：14:34 有效送达给新任代表 `LP-02`，龙浦两个事项的材料答辩期均到 `2026-12-04T02:34:00+08:00`。
- `DG22/Z4`：14:35 有效送达给 `YY-09`，岩源答辩期到 `2026-12-04T02:35:00+08:00`。
- `G1#1` 是岩源原件，可证明 `co_loading` 与 `price`，只获准 `CO-YY` 取得；龙浦不能打开，因此看不到“780”价格。
- `G1#2` 是经确认的遮盖件，只保留 `co_loading`，原获准龙浦/岩源取得；`DW` 于 14:45 撤回龙浦的披露同意。
- `DG20` 在撤回前已经实际送达，所以送达回执和凭据继续保留；但在 `2026-12-04 09:00` 查询时，`G1#2` 对龙浦已 `openable_now=false`。
- 因此 `L1:co_loading` 对龙浦没有可用于裁决的材料，状态为 `returned_for_substitute`，不能作出维持拒收；同一 `co_loading` 事实对岩源仍可使用 `G1#1`。
- `L1:seal_check` 可使用已有效送达且未撤回的 `G2#1`，期限届满后可维持拒收并进入可独立执行清单。

## 3. 领域模型与业务规则

核心实现位于 `appeals_service/service.py`。

### 3.1 案件、事项与代表资格

- `create_case()`：厂方登记批次、原判、采购约定、拟处置和 `appeal_hours/files_due_hours`。
- `file_appeal()`：供货商只能在申诉期限内对所属批次的部分事实提出异议。
- `accept_case()`：经办人受理，按“subject + fact”形成可独立审查的事项，标明受影响方，并对争议批次建立保留 hold。
- `add_mandate()`：登记人员/组织/有效期/文件组。`valid_until` 按交换口径为**不含端点**。
- `configure_matter()`：受理后补充事项的受影响方、是否可独立审查/执行。

### 3.2 证据版本与披露

每个材料版本保存：

- 稳定 `id + rev`，同版本不可覆盖；
- `owner` 归属；
- 正文、SHA-256、来源文件；
- `facts`：该版本可证明的事实；
- `allows`：当前获准取得的组织；
- 遮盖件/摘录的确认或同意凭据。

关键方法：

- `upload_material()` / `confirm_material()`
- `withdraw_consent()`
- `get_material_content()`
- `usable_materials()`：逐方判断材料是否可作为该方的答辩/裁决依据。
- `verification_view()`：经办人/复核人核对实际正文、哈希、事实用途、披露撤回、有效送达和裁决引用。

不可用原因会明确区分为：未向该方披露、遮盖/摘录未经确认、披露同意已撤回、无有效送达。

### 3.3 送达、回执与期限

- `create_delivery()` / `send_delivery()`：准备和发出文件组。
- `ingest_receipt()`：按回执 `id` 保存原件，并通过 `bundle` 认出原交付；同一 bundle 的 `unknown → delivered/rejected` 全过程保留。
- `record_timeout_check()`：只记录查询/超时检查，绝不把 `unknown` 改写成“确定未送”。
- 只有 `state=delivered`、接收人在交付时点对案件和文件组有有效代表资格、且文件当时可披露，才形成 `service_event` 并起算期限。
- 期限按“组织 + 事项 + generation”控制：
  - 普通补件只给新增/新送达事项起算期限，无关旧期限继续；
  - `changed_matters` 中列出的、会改变答辩内容的新版本产生新 generation 和完整 `files_due_hours`；
  - 旧答案绑定旧 generation，仍可查询；晚到的旧版回执不会生成新版期限。
- `file_answer()`、`waive_period()`：答案和放弃余期均绑定当前期限 generation。

### 3.4 裁决、仓班执行和纠正

`make_decision()` 按事项作出：

- `uphold_reject`：维持拒收，必须引用经事实匹配、可向该方使用且有有效送达的具体版本；
- `restore_receipt`：恢复收货，可生成收货任务；
- `supplement_investigation`：要求补查。

作出决定前，该方当前事项期限必须是 `expired`、`answered` 或 `waived`，否则返回 `409` 和当前程序状态。

执行链路：

1. 复核决定中设置 `independently_executable=true` 的已生效部分才进入仓班清单；
2. `create_execution()` 创建转处/处置/恢复收货执行单；
3. `record_physical_receipt()` 记录独立的实物回执（编号前缀 `PR-`），与送达回执严格区分；
4. `warehouse_view()` 返回当前可执行拒收、恢复收货任务、已执行项、保留货物和实物回执。

送达范围纠正 `correct_service_scope()`：

- 无实物回执的未执行部分：置为 `blocked_pending_reconsideration`，撤出仓班待执行清单，等待新决定；
- 已有实物回执的已执行部分：保留决定、证据、送达、执行单和实物回执快照，生成 `REM-*` 补救移送给 `remedy_authority`；
- 已执行事项再次创建执行单会冲突，不能重新变成“待转处”。

## 4. 主要 HTTP 接口

所有案件接口前缀为 `/api/cases/{case_id}`。

| 方法 | 路径 | 身份 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | - | 健康检查 |
| GET | `/api/cases` | 任意登记人员 | 厂方/供货商按身份列出可见案件 |
| POST | `/api/cases` | `factory`/`clerk` | 厂方登记拒收 |
| GET | `/api/cases/{id}` | 相关方 | 供货商/厂方视图；经办/复核返回核对视图 |
| POST | `.../appeals` | 供货商代表 | 提出部分事实异议 |
| POST | `.../accept` | `clerk` | 受理并生成事项/保留 |
| POST | `.../mandates` | `clerk` | 登记代表资格 |
| POST | `.../matters/{mid}/configure` | `clerk` | 标明受影响方/可独立性 |
| POST/GET | `.../materials` | 有权人员 | 上传/列出材料版本 |
| GET | `.../materials/{mid}/versions/{rev}` | 相关方 | 打开当前仍可打开的正文 |
| POST | `.../materials/{mid}/versions/{rev}/confirm` | `clerk`/证据方 | 确认遮盖件或摘录 |
| POST | `.../withdrawals` | `clerk`/证据方 | 撤回某方披露同意 |
| POST/GET | `.../deliveries` | `clerk` | 创建/列出文件送达 |
| GET | `.../deliveries/{bundle}` | `clerk` | 查看 bundle 与全部回执 |
| POST | `.../deliveries/{bundle}/send` | `clerk`/`delivery` | 发出已准备文件 |
| POST | `.../deliveries/{bundle}/timeout` | `clerk` | 超时查询；不改成确定未送 |
| POST | `.../receipts` | `delivery`/`clerk` | 录入送达机构回执 |
| GET | `.../served` | 相关方 | 分别查询已送达与此刻可打开 |
| POST | `.../answers` | 供货商代表 | 答辩 |
| POST | `.../waivers` | 供货商代表 | 明确放弃剩余期限 |
| POST | `.../decisions` | `reviewer` | 按事项裁决 |
| GET | `.../verification` | `clerk`/`reviewer` | 核对实际证据与有效送达 |
| GET | `.../remands` | 相关方/经办 | 查询因无可答辩材料而退回的事项 |
| POST | `.../corrections` | `clerk`/`reviewer` | 复核后纠正送达范围 |
| POST | `.../executions` | `warehouse`/`factory` | 创建实物转处/收货执行单 |
| POST | `.../executions/{exec_id}/receipts` | `warehouse` | 登记实物回执 |
| GET | `/api/warehouse` | `warehouse` 等 | 仓班可执行清单 |
| GET | `/api/remedies` | `remedy_authority` | 已执行后的补救移送 |

### curl 示例

```sh
BASE=http://127.0.0.1:8080
CID=CASE-ORDER-73

# 龙浦新任代表查询自己的事项、期限、已送达和可打开材料
curl -sS -H 'X-Actor: LP-02' \
  "$BASE/api/cases/$CID?at=2026-12-04T09:00:00%2B08:00"

# 龙浦尝试打开岩源原件 G1#1：返回 403，但送达索引仍可查
curl -i -H 'X-Actor: LP-02' \
  "$BASE/api/cases/$CID/materials/G1/versions/1?at=2026-12-04T09:00:00%2B08:00"

# 复核人在期限未完成时裁决会得到 409
curl -sS -X POST "$BASE/api/cases/$CID/decisions" \
  -H 'Content-Type: application/json' -H 'X-Actor: REV-01' \
  -d '{
    "at":"2026-12-03T15:00:00+08:00",
    "items":[{
      "matter_id":"L1:seal_check",
      "outcome":"uphold_reject",
      "cited_materials":[["G2",1]],
      "independently_executable":true
    }]
  }'

# 期满后作出封签事项裁决
curl -sS -X POST "$BASE/api/cases/$CID/decisions" \
  -H 'Content-Type: application/json' -H 'X-Actor: REV-01' \
  -d '{
    "decision_id":"DEC-SEAL",
    "at":"2026-12-04T09:00:00+08:00",
    "items":[{
      "matter_id":"L1:seal_check",
      "outcome":"uphold_reject",
      "cited_materials":[["G2",1]],
      "independently_executable":true
    }]
  }'

# 仓班查询可执行裁定
curl -sS -H 'X-Actor: WH-01' "$BASE/api/warehouse"

# 经办人核对裁决引用的证据正文、哈希和有效送达
curl -sS -H 'X-Actor: CLERK-01' "$BASE/api/cases/$CID/verification"
```

## 5. 作为 Python 领域服务使用

```python
from appeals_service import AppealsService
from appeals_service.seed import seed_service

svc = seed_service(AppealsService("var/state.json"))
cid = "CASE-ORDER-73"

# 某方当前能看到什么、期限状态、哪些材料可用于答辩
view = svc.party_view(cid, "CO-LP", "2026-12-04T09:00:00+08:00",
                      actor=svc.actor("LP-02"))

# 仓班待执行清单
work = svc.warehouse_view("WH-01", at="2026-12-04T09:00:00+08:00")

# 经办核对
check = svc.verification_view(cid, "CLERK-01", at="2026-12-04T09:00:00+08:00")
```

## 6. 测试覆盖

`tests/test_exchange.py` 保留并核查既有交换材料；`tests/test_service.py` 与 `tests/test_api.py` 覆盖：

- unknown 回执、同 bundle 后续 delivered、代表资格边界；
- 已物理交付、有效送达、当前可打开三者分离；
- 价格保密、遮盖件、撤回同意和逐方证据排除；
- 补件期限不影响无关事项；
- 答辩内容变更的新版本重新给完整期限、旧答案/旧版本保留、晚到旧回执不重启期限；
- 未交付材料停止；
- 裁决程序门槛和引用证据校验；
- 实物回执、仓班清单、执行后纠正转补救、未执行部分阻止执行；
- HTTP 作用域和 JSON 错误。
