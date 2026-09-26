---
name: new-plan-changes-v4
description: 并行推进 OpenSpec changes 的 spine-run 兼容入口。每个 change 在独立 worktree 完成 implement/review/fix，验证后发布到主 session 启动分支；启动时建立全角色进展 monitor。当用户说“并行推进 changes”“new-plan-changes-v4”“v4 跑 openspec changes”时使用。
metadata:
  category: OpenSpec
  tags: [openspec, parallel, worktree, alias]
---

# new-plan-changes-v4 → spine-run（1.10.0）

读取同目录 `references/spine-run.md`（若安装器已提供）；否则运行 `npc playbook show spine-run` 读取完整 playbook，然后执行。Claude Code 也可调用已同步的 `/spine-run`。只维护这一份执行协议，不再使用旧版波次屏障和共享工作区内环。

执行前检查 `npc --version` 至少为 **1.10.0**，且 `npc monitor register --help` 支持 `--kind` / `--done`、`npc change run --help` 支持新协议。若 CLI 低于 1.10.0，明确指出版本不匹配；不能用旧 CLI 默默执行新 skill。更新 CLI 后重新安装配套 playbook。

关键契约：

- 每个 change 的 implement/review/fix 在同一个独立 worktree 中完成；主 session 能读源码和诊断证据，自主调整实现与计划。整合目标是初始化时记录的分支。
- 按真实依赖补位，共享文件是整合风险；不把展示波次变成全连接依赖。全阶段共用真实并发预算。
- `npc init` 后、首个分析/执行 agent 前启动唯一 monitor。登记全部角色（远程作业与长测试登记为 `--kind job`，结果文件用 `--done`），证据停滞的 agent 每 10 分钟询问、持续产出的每 30 分钟询问，15 分钟无推进时诊断；终态信号由主 session 核验后 finish/cancel；询问、回复、干预均记录回执。用可唤醒主 session 的通知机制或至多 60 秒有界等待，不把后台日志误认为自动控制。
- 保留同一 worktree、提交和检查点，恢复时重新连接原执行体；不得丢弃已完成修复重跑。

参数与 spine-run 相同：空输入表示全部 in-progress changes；`--max-parallel` 默认 4；`--serial-waves` 映射 `--serial`；`--auto`、`--fresh`、`--no-architect`、`--webhook` 保留。
