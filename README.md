# 版画捐赠分批入藏、暂存与回执核对

共同权利份额以万分比计，估值金额以分计。交接记录的外箱号不作为藏品编号。custody 记库房保管事实，ownership 记捐赠协议中的移转范围。 本地登记均为合成数据。

## 资料端

`records/` 下的 `agreements`、`stockroom`、`valuation` 来自不同业务经办方。文件里按该来源的登记顺序列出原始观察。字段 `id` 在本来源内唯一。报告收到的时刻与业务实际发生的时刻分别保留，查询不据此代替任何业务确认。

需要 Python 3.11.2，使用标准库。服务监听回环地址。

```sh
python3 -m unittest -v
python3 desk_source.py --port 8765
```

- `GET /sources` 返回可查询的来源名和各来源的记录数量。
- `GET /sources/来源名?offset=0&limit=20` 查询该来源的一页登记。读取下一页时用上一次返回的 `next_offset`，其为 null 时到达末尾。
- 不存在的来源返回 404。页长支持 1 至 50，非法页长或偏移返回 400。
- `--records` 可指定另一组登记。每个 JSON 文件使用记录数组，文件名是来源名。

## 捐赠确认后台

资料端的三份观察不可变。征集办公室、库房、财务在同一套后台分别办理确认，所有动作追加到
JSONL 事件账（`--events`，默认 `back_office_events.jsonl`），业务视图全部由
`domain.Ledger` 从资料包加事件账派生，重启后重放恢复。

```sh
# 直接读本地 records/
python3 back_office.py --port 8766
# 或经 HTTP 从资料端分页拉取
python3 desk_source.py --port 8765
python3 back_office.py --port 8766 --upstream http://127.0.0.1:8765
```

### 领域边界（由领域层强制，不依赖调用方自觉）

- **暂存不入藏**：`custody` 协议只能出代存交接凭据，件仍可归还，永远不能确认入藏或开捐赠回执。
- **实物身份**：外箱标签不是藏品编号；同标签多件以 `mark` 区分，`/conflicts` 给出
  `duplicate_label` 与 `merged_identity` 告警，防止两件同号印样并成一件。
- **整套接收**：`all_or_none` 协议必须共有人签齐（含后补签）、每件查验在库且无未裁决冲突，
  缺一不可，且不得拆开入藏；非整套协议可逐件办理，未查清部分保留原承诺边界。
- **同号多头主张**：同一编号出现在多份协议时，按权益种类、共有人、实物同一性生成冲突，
  裁决前阻断相关件的入藏与凭据；裁决结果记录在事件账中。
- **估值适用**：`group_only` 估值只能对完整组套整套开回执，拆开确认直接拒绝，不得原价平分；
  有独立估值的件可单独出凭据；有效回执占用估值，不可重复使用。
- **回执更正**：资料端历史回执经审计标记违规（未入藏先开、组套拆半、金额不符）；
  更正登记把原回执置为 superseded 并记录差额（负数）和替代回执，捐赠人对账可查。
- **归还**：在库的代存/暂放件可办理归还；已入藏成为馆方实物的件不走库房归还路径。

### 只读视图（GET，支持 `?as_of_day=N` 做截至某日的回溯）

| 路径 | 内容 |
| --- | --- |
| `/pieces`、`/pieces/{piece}` | 占有时间线、协议主张与签署、冲突、代存凭据、入藏记录、适用估值、并号告警 |
| `/agreements`、`/agreements/{id}` | 签署状态、逐件占有状态、阻断原因、是否可入藏 |
| `/conflicts` | 多头主张冲突及裁决结论、外箱并号告警 |
| `/receipts` | 历史回执与新回执的状态、审计发现、更正记录与差额 |
| `/donors`、`/donors/{id}` | 捐赠人对账：待查、馆方接收、代存在库、已退回、裁决给他方的件、有效回执合计与更正差额 |

### 三方动作（POST，请求体均含业务日 `day`）

| 角色 | 路径 | 动作 |
| --- | --- | --- |
| 征集办公室 | `/office/conflicts/resolve` | 裁决同号冲突（piece、agreement、note） |
| 征集办公室 | `/office/accessions` | 确认入藏（agreement，可带 pieces；整套协议必须全件） |
| 库房 | `/stockroom/handoffs` | 已查清件交后续经办（未查清件拒绝） |
| 库房 | `/stockroom/custody-receipts` | 代存交接凭据（不产生馆藏身份，件仍可归还） |
| 库房 | `/stockroom/returns` | 归还在库代存/暂放件（已入藏件拒绝） |
| 财务 | `/finance/receipts` | 按入藏事实与适用估值开捐赠回执（可指定 valuation） |
| 财务 | `/finance/receipts/correct` | 更正回执（receipt、reason、可选 replacement），登记差额 |

业务规则拒绝返回 409 并带 `code`（如 `accession_blocked`、`group_only_split`、
`conflict_open`、`already_accessioned`）；请求格式错误返回 400；未知资源返回 404。
所有动作具备幂等保护，重复办理会被拒绝而不是重复入账。

## 测试

```sh
python3 -m unittest -v
```

- `test_desk_source.py`：资料端取页合同与错误码。
- `test_domain.py`：占有时间线、整套入藏条件、冲突裁决、代存与归还、估值与回执更正、
  捐赠人对账边界、事件重放。
- `test_back_office.py`：HTTP 合同、三方完整流程、事件账持久化与重建、经资料端 HTTP 联调。
