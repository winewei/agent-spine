# agent-spine

本仓库的 harness CLI 是 **`npc`**（`~/.local/bin/npc`，Python + uv）。

## 布局速览

- `src/npc/` —— 确定性执行层（Python 包名就是 `npc`，entry `npc.cli:main`；**不是** `agent_spine`）
- `plugins/agent-spine/` —— 智能层 plugin：commands（`/spine-run` `/spine-spec` `/spine-analyze`）+ agents（`spine-coder`、`spine-spec-writer`）+ hooks
- `docs/cli.md` —— npc 完整契约；`docs/principles.md` —— 4 条不变量
- 运行轨迹全部落 `~/task_log/<PROJ_KEY>/`，工程内零侵入

## 测试

```bash
uv run pytest -q                 # 全量（1200+ 测试）
uv run pytest tests/test_pipeline.py -v
uv run pytest --cov=npc          # 覆盖率（包名是 npc）
```

改 `src/npc/` 任何行为，对应 `tests/test_*.py` 必须同步补测试。

## 用 npc，不要自己写脚本

任何涉及 spine 生命周期、telemetry、state、run 索引、cost、status 的操作，**先跑 `npc --help` 和 `npc <cmd> --help`** 看有没有现成子命令，再决定是否需要新写代码。**不要为了做一次性检查就写临时 py/sh 脚本**——npc 已经封装了这一层。

常用入口：

- `npc init / resume / status` —— run 生命周期与当前进度
- `npc implement / review / fix / archive` —— SDD 阶段记录
- `npc verify` —— 质量门 + **路由不变量**（不是通用业务校验，见下）
- `npc telemetry` —— 跨 run 指标流与聚合
- `npc cost` —— 按后端拆 token 成本
- `npc doctor` —— 环境体检
- `npc agent` —— sub-agent prompt 渲染
- `npc auto-decide` —— `--auto` 模式主 session 决策器

完整契约见 `docs/cli.md`。

## npc 的边界（重要）

npc **只放跨项目通用的原子操作**：生命周期钩子、telemetry、state 读写、路由不变量。

**不放**具体项目的业务校验（SEO / 定价 / 目录规则等）——那些属于**各自项目仓库的 `scripts/check-*.ts` + `npm run check:*`** 家族。往 `npc verify` 里塞业务校验会污染 harness 职责边界。

## harness / 契约改动走 spec

harness 和 npc 契约的任何改动，即使很小，也**走 openspec + 充分测试**，不要直接编辑源码。见 `openspec/` 目录。
