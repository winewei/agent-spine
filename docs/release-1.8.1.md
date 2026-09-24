# 1.8.1：让 worktree 承载完整开发闭环

问题：1.7.1/1.8.0 只并行 implement，把 review/fix/archive 放在共享工作区全程持锁；待整合产物继续阻塞依赖和文件排他。1.8.0 又把展示波次转换为全连接依赖，制造额外等待。限制主 session 读取诊断证据，使 agent 很难主动解除这些阻塞。

1.8.1 的默认 spine-run 改为：

- 每个 change 在同一个 worktree 内 implement → review → fix → review。`npc change run --isolated` 不持目标工作区锁；`--handoff` 返回原生 Codex/Claude Code coder 所需的持久任务，保留执行体的工具能力和修复上下文。
- 整合目标为 `npc init` 时所在命名分支，记录完整 ref 与启动提交。续跑不重绑定；切换分支、改写目标历史时拒绝发布。内部 `.main.lock` 文件名为兼容旧执行器保留，并不表示 Git 分支名 main。
- `npc integrate --prepared` 在 change worktree 内合入目标分支变化，比较完整二进制补丁（包含文件前后内容与模式），并验证组合树。补丁变化或冲突必须由 agent 处理并重新审查；补丁不变可复用独立审查结果。组合验证不是跨 change 语义正确性的完整证明，DAG 的真实依赖、review 的调用者检查及项目集成测试仍然必要。
- 只有最终快进和 OpenSpec 归档持目标工作区锁。发布前再次核对目标 ref、HEAD 与干净状态；目标推进返回可重试状态，保留工作与验证回执。实现和 fix 的原提交链保留，不 squash/cherry-pick 单个末端提交遗漏修复。
- 准备回执绑定审查 HEAD、补丁摘要与测试结果；同一候选和锁竞争重试不重复测试。已完成阶段、已返回但尚未装订的 coder RESULT 可恢复；存在未装订提交、脏工作区或未知旧进程时保留现场，不丢弃后重做。
- `npc plan ready` 新增 `file_policy="isolated"`：共享文件只返回 `integration_risks`，真实依赖仍阻塞。默认 exclusive 保留旧接口兼容。playbook 不再从波次推导笛卡尔积依赖，只采纳有依据的 edges/ordering_edges。
- 主 agent 可按需读规格、源码、review 与日志、调整任务、直接处理小修复，随后独立验证。分析任务按复杂度分派，不固定重跑完整分析团队。并发预算覆盖 implement/review/fix，不能把同一容量重复计算。

## 使用

```bash
# 在主 session 的启动分支初始化；每个 change 先登记稳定 SEQ
npc init
npc state init-run --plan-order '["my-change"]'
npc state add-change 1 my-change

# 原生 agent 模式：返回 needs-coder（phase、round、worktree、prompt）
npc change run --seq 1 --isolated --handoff

# 在返回的 worktree 内编码/提交；保存 RESULT 和 manifest 后继续
npc change run --seq 1 --isolated --handoff --result-file /path/result.txt --manifest /path/manifest.json

# ready-to-integrate 后发布并归档
npc integrate --seq 1 --prepared
```

不用 `--handoff` 时，npc 按 `[coder]` 配置调用 headless 执行体。新 run 默认采用隔离 playbook；不加 `--isolated` 的老命令保留原执行语义，只供兼容已有 run。更新 CLI 后也要更新宿主安装的 playbook，不能继续使用 1.7.1 的旧 skill。

## 返回与恢复

| status | 调用方动作 |
|---|---|
| needs-coder | 派发或重新连接原生 coder；同一回执不能重复派单 |
| ready-to-integrate | 调 integrate --prepared |
| target-busy / target-moved | 保留发布队列，重试 integrate，不重写代码 |
| needs-review | 在原 worktree 审查集成增量（1.8.2 起；1.8.1 为最终补丁全量） |
| needs-resolution / tests-failed | 原 worktree 内诊断、合并/修复、提交，再审查集成增量；`reason`/`conflicts`/`files` 给出原因与涉及文件 |
| needs-recovery | 核验现有 commit/RESULT/工作区，补齐回执 |
| needs-decision | 主 agent 取证并裁定；不能用 force-archive 绕过独立审查 |
| archive-failed | 保留已发布证据，解决归档原因后重试 |
| archived | 终态，可解锁下游 |

`needs-coder`、`ready-to-integrate`、`archived` 返回 exit 0；needs-decision 返回 5；其余可重试业务状态返回 1。exit 1 不等于 change 已失败。

## 验证范围与边界

回归覆盖真实 Git worktree 并行、原生 agent 交接、目标分支身份、组合冲突、目标推进竞争、验证失败不污染目标、修复回执恢复、发布后中断与幂等重试。测试中的模型和 OpenSpec 归档使用可控替身，不代表已完成外部业务工程的真实模型耗时基准。

目前隔离路径采用严格测试结果：检测不到测试命令时明确返回 skipped；配置了命令则 exit 0 才通过，不继承旧 integrate 的 baseline-diff 放行。worktree 依赖安装由工程/agent 准备，不复制未跟踪文件或主工作区虚拟环境；provider 配置从启动工程分层读取，保留未跟踪的项目路由与全局 provider 定义。发布本身和 OpenSpec 归档仍是串行临界区；不宣称移除所有串行成本。

旧 run 没有记录启动分支时，不会从当前 checkout 推断它的历史身份。保留原产物与兼容流程，核实原目标后再迁移；不要为升级覆盖旧 state 或删除旧 worktree。

## 全角色进展监控（1.8.1）

`npc monitor tick` 初始化/恢复监控；`follow --interval 60` 单实例后台检查，默认询问周期 600 秒、无进展阈值 900 秒、回复期限 300 秒（可用 `--inquiry-seconds`、`--stalled-seconds`、`--grace-seconds` 调整）。主 session 在首个 subagent 前启动，并用宿主通知或有界等待处理事件。

所有角色显式 `register --id ID --role ROLE --handle HANDLE [--worktree PATH] [--artifact PATH ...]`。每个任务的专属产物内容和隔离 worktree 的 HEAD/diff 是工作证据，心跳/日志活动不算进展；无 Git 产出的分析任务使用专属 artifact。未跟踪文件需显式登记 artifact。文件变化不代表收敛，周期询问不会因此取消。

`CHECK_IN` 由宿主发送询问，再 `ack --action-id ID --decision sent`。回复后 `--decision progress --note 证据`；合理长任务 `--decision wait --wait-seconds 600 --note 原因`（1–900 秒）；到期未处理进入 `CONTROL_REQUIRED`，主 session 诊断并 `--decision intervene --note 已采取的动作`。相同 action id 幂等展示，不能每个 tick 重发询问；重复 sent 不延长回复期限。监控只维护自己的检查点，不直接发宿主消息、停止进程或修改 Git/业务 state。

核验退出/收单后 `finish --id ID --note 证据`；全部结束后 `stop`，后台 follow 随之退出。恢复保留未完成 action 与计时，缺少转录不等于任务完成。`npc watch` 仍提供只读活动快照，供核对漏登任务；只有真实通知能力或主 session 的有界等待才能驱动干预，单独运行 follow 不等于自治控制。

统一入口为 `spine-run`。默认安装不再包含 v4；包内旧名字仅保留兼容跳转。Codex 安装到 `~/.codex/skills/spine-run/SKILL.md`，Claude Code 保留 `/spine-run` 命令。
