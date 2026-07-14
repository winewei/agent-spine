# npc handoff：sub agent 派发-收单的文件信箱协议

- 日期：2026-07-03
- 状态：提案（owner 已口头同意方向）
- 关联：与 [2026-07-02-npc-watch.md](2026-07-02-npc-watch.md)（统一观测后台任务）合流评审
- 起因：kube-service-controller「Manager→构造注入 Service」六批次迁移实战复盘

## 问题（实测数据）

主 session 用 Agent 工具派发 teammate 批量执行重构，一轮 6 批下来暴露四类浪费：

1. **空闲通知刷屏**：双层结构（编排 teammate → 内部编码 agent）导致 teammate 每次等待都发 idle 通知，
   一轮累计 15+ 条，每条都是主 session 一个完整回合，绝大多数无信息量。
2. **长文本双向灌注**：6 批指令 prompt（各 500–700 tokens）+ 6 份验收报告（各 800–1500 tokens）
   原文进出主 session 上下文，约 1 万 tokens 本可下沉到磁盘。
3. **零留痕**：迁移清单、红线核查证据（行号对比、grep 结果）只活在对话上下文里，compact 即蒸发；
   commit 留了代码没留验证证据，跨 session 无法审计。
4. **无谓往返**：主 session 催问"开工没有"多次；teammate 误读 git log（把主 session 的收口 commit
   当成自己 sub agent 的违规提交）又耗一轮裁决。

## 方案

### A. 立即生效的编排约定（零开发成本）

- **单层派发优先**：主 session 直接派编码 agent，去掉中间编排 teammate。勘察与独立验收本来就在
  主 session（这是信任锚点，不外包），中间层增值有限而 idle 噪声是它的固有产物。
- 若确需双层（超大批次内部再拆），硬约定 teammate 只在「完成 / 阻塞」两个时点发消息。

### B. 文件信箱协议（下沉进 npc）

新增子命令组 `npc handoff`，task-id 为自由字符串，**不绑 OpenSpec change-id**：

```
npc handoff render --task-id batch-a [--template refactor]   # 指令模板写盘，stdout 返回薄引导语
npc handoff record --task-id batch-a --report <path>         # 校验 report.json 并装订状态
npc handoff status [--task-id X]                             # 列出各 handoff 的阶段/结果
```

目录结构（复用 `~/task_log/<PROJ_KEY>/`）：

```
~/task_log/<PROJ_KEY>/handoffs/<task-id>/
├── instruction.md    # 主 session Write，写盘不回读
├── report.md         # agent 写，人读留痕（清单/红线证据/测试输出）
└── report.json       # agent 写，机读收单（jq 取字段验收）
```

`report.json` 最小 schema：

```json
{
  "task_id": "batch-a",
  "verdict": "done | blocked",
  "tests": "1123 passed",
  "files_changed": ["app/services/config.py", "..."],
  "callsites": 27,
  "redlines": [{"item": "sync_service_logic commit 位置", "status": "untouched", "evidence": "L1180 未入 diff"}],
  "residue": 0,
  "blocked_reason": null
}
```

协议流：

1. 主 session 写 instruction.md（一次撰写成本，不回读）；spawn prompt 3 行薄引导：
   「读 <path>/instruction.md → 执行 → 写 report.md + report.json → 只回一行 done/blocked + 路径」。
2. agent 收单消息恒为一行；报告全文永不进主 session 上下文。
3. 主 session 验收 = `jq` 读 report.json 关键字段 + **独立复跑测试/grep**（验收不省，省的只是转述）。
4. 留痕即审计：handoffs/ 跨 session 可恢复，commit hash 可反查对应 report。

### C. 与 npc-watch 合流

watch 负责「跑着的任务可观测」（tail 事件流、超时告警），handoff 负责「派发与收单的契约」。
二者共享 task_log 目录与 task-id 命名空间，一并实现可共用状态装订代码。

> **注（2026-07-03）**：观测面已先行落地——`npc task start/update/heartbeat/finish`（`src/npc/task.py`，
> `<run_dir>/tasks/<task-id>.json` + `.events.jsonl`，task-id 为自由字符串不绑 change-id）与 `npc watch`
> 已在 commit `9624470` 实现。本提案实施时**复用 `npc task` 的 task-id 命名空间与状态装订代码**，
> 只需补"派发-收单信箱"（`handoff render/record/status` + instruction.md / report.json），勿重复造观测层。

### D. 记录权限归属：agent 产出 vs 主 session 记录

原则：**agent 只产出（return / 写盘工作证据），是否、以及如何写入"可被未来引用的记录"，由主 session 决定并亲自执行**。

- 调研/分析类任务（例：读一份 teammate session transcript 提炼 npc 改进点）：sub agent 以纯文本回复交付发现，不落盘写入 `docs/` 或 memory；主 session 收到回复后自行判断价值、组织措辞，再写入 `docs/optimization-proposals/` 或 memory 系统。
- 代码实现类任务（B 节 handoff 协议）：agent 仍写 `report.md`/`report.json` 作为工作证据（可复核的原始留痕），但这类文件是"工作台留痕"而非"结论记录"——主 session 验收后若要沉淀为决策/规范，仍需自己写入 docs 或更新 `design.md`，不能把 `report.json` 直接当规范文本引用。
- 原因：agent 缺乏"什么信息值得长期保留、以什么措辞/结构呈现"的跨 session 判断力；把这个决策权收在主 session，才能保证记录质量与一致性，也避免 sub agent 产出未经审视就成为事实来源。

## 不做的事

- 不改主 session 独立验收职责（测试/grep/红线核查仍主 session 亲跑）。
- 不把 handoff 绑进 OpenSpec 状态机；OpenSpec 流程继续走既有 implement/fix record。
- 不做跨 agent 的自动重试编排（保持 npc 安全闸哲学：blocked 就停下等人）。

## 预期收益

以本轮 6 批为基准：主 session 上下文节省约 1 万 tokens（指令+报告下沉）+ 15 回合噪声归零；
新增跨 session 审计链（此前为 0）。
