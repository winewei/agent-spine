---
name: new-plan-changes-v4
description: 已并入 spine-run。保留本名仅为兼容旧调用；当用户说"new-plan-changes-v4""v4 跑 openspec changes""并行推进 changes"时，直接按 spine-run playbook 执行。
category: OpenSpec
tags: [openspec, deprecated, alias]
---

# new-plan-changes-v4（已并入 spine-run）

本 playbook 的全部内容（DAG 抽取与架构师裁定、worktree 并行 implementer、`npc integrate` 整合、后台 `npc change run` 内环、`--serial` 回退）已合并进 `spine-run`，并增加了"自由目标 → 拆解 changes"入口。两者不再并行维护。

**执行方式**：读取并按 `spine-run` playbook 执行（Claude Code 为 `/spine-run`，其它宿主 `npc playbook show spine-run`）。参数一一对应：

| 旧写法 | 新写法 |
|---|---|
| `/new-plan-changes-v4` | `/spine-run`（空输入 = 全部 in-progress changes） |
| `/new-plan-changes-v4 --auto --max-parallel 4` | `/spine-run --auto --max-parallel 4` |
| `--serial-waves` | `--serial`（旧名仍接受） |
| `--fresh` / `--no-architect` / `--webhook` | 同名不变 |
