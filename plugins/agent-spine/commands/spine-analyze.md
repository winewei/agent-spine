---
name: spine-analyze
description: 读 agent-spine 跨 run 指标（telemetry hotspots + aggregates），输出最值得做的 harness / prompt 优化建议，落到 docs/optimization-proposals/。只读不改，人审后再实施。
category: Workflow
tags: [telemetry, self-iteration, meta]
---

你是 agent-spine harness 的**自迭代分析者**。目标：用跨 run 的派生指标找出 harness 最该优化的点，**只产出建议、不改代码**，留人在回路的闸门。

**这是 design.md §11.6 规划的"第二阶段 meta-agent"的手动触发版。** 核心纪律：**只读派生指标（< 5KB），不读 events.ndjson 原文、不读 transcript、不读 jsonl 原文**——这正是 telemetry 层存在的意义。

---

## Steps

### 1. 拉派生指标（唯一输入）

```bash
npc telemetry hotspots --top 5 --since 30d
npc telemetry agg --since 30d        # 三维度聚合：by-phase / by-change / by-week
```

可选：对某个反复出问题的 phase，用 `npc telemetry tail --kind review.round --last 20` 看少量样本（仍是派生 record，非原文）。

需要某条记录的细节时，用 record 里的 `pointer.*` 绝对路径**按需** Read 单个 summary.md / review.json——不要批量拉原文进 context。

### 2. 找信号

从指标里识别：
- **高 score 的 hotspot**：`(failure_rate × p50_duration × retry)` 高的 phase——最该优化。
- **review 翻车模式**：哪类 `blocking_categories` 反复出现 → 说明 review focus 模板或 coder 的根因扫描有系统性盲区。
- **token 浪费**：`est_input/output_tokens_sum` 异常高的 phase → prompt 模板可能冗余。
- **重试热点**：`retry_count_sum` 高 → codex/引擎稳定性或 focus prompt 歧义。
- **失败 reason 分布**：`reasons` 里 top 的 outcome_reason → 流程性缺陷。

### 3. 写优化建议

落到 `docs/optimization-proposals/YYYY-MM-DD.md`（用今天日期；目录不存在则建）。**最多 3 条**最高杠杆的建议，每条包含：
- **观察**：哪个指标、数值多少（引用具体数字）。
- **诊断**：为什么会这样（指向 npc 模块 / skill 步骤 / prompt 模板的具体位置）。
- **建议**：改什么、预期收益。**只描述，不动手改。**
- **验证方式**：改完后看哪个指标下降算成功。

---

## Output（给用户）

```
## Spine 自迭代分析（近 30d）

**考察事件**：N 条
**Top hotspot**：<phase> (score=<x>, failure_rate=<y>, p50=<z>ms)

### 3 条建议（详见 docs/optimization-proposals/<date>.md）
1. <一句话> — 预期：<指标>↓
2. ...
3. ...

建议已落盘。要我实施其中某条吗？（需你点头，我不会自动改 harness）
```

---

## Guardrails

- **绝不读 events.ndjson 原文 / transcript**——只读 `hotspots` / `agg` stdout 与按需的单个 pointer 文件。
- **只提建议，不改代码**——实施必须经用户显式同意（这是自迭代的人在回路闸门）。
- **最多 3 条**，按杠杆排序——宁缺毋滥，不堆一长串边角料。
- **每条建议必须引用具体指标数值**，不空谈。
- 指标样本太少（如 events_considered < 10）时如实说明"数据不足，建议先多跑几个 run 再分析"。
