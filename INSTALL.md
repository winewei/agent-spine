# 安装 spine agent（面向 LLM 的逐步指南）

本指南给 **执行安装的 agent（如 Claude Code）** 用。一键脚本是 `install.sh`；下面是它的每一步 + 验证命令 + 失败处置，便于 agent 自检与排错。

> spine agent = `npc` CLI（确定性执行层，内置 `src/npc`）+ harness plugin（`/spine-run`、`/spine-analyze`、`spine-coder`）。

## 一键安装

```bash
bash install.sh
```

幂等，可重复跑。若要逐步执行 / 排错，按下面来。

---

## 逐步（agent 可逐条执行并核验）

### 0. 前置工具
```bash
command -v git && command -v uv && echo OK
```
- 缺 `uv` → 装：`curl -LsSf https://astral.sh/uv/install.sh | sh`
- `claude` CLI 可选（无则跳过 plugin 自动安装，改在 Claude Code 内手动 `/plugin`）。

### 1. 校验内置 npc
```bash
test -f pyproject.toml && test -d src/npc && echo "src/npc OK"
```
- npc 已内置在本仓库（`src/npc`），无需子模块。同一套 npc 亦作独立仓库 [cmzz/npc](https://github.com/cmzz/npc) 发布供其它项目复用。

### 2. 装 npc CLI（内置 src/npc）
```bash
uv tool install --force --from . npc
npc --version          # 期望：npc 1.3.0
```
验证：`npc --version` 有输出即成功。`--force` 必需（覆盖旧版）。

### 3. 装 harness plugin（需 `claude` CLI）
```bash
claude plugin marketplace add "$(pwd)"
claude plugin install agent-spine@agent-spine --scope user
claude plugin list | grep agent-spine
```
- **装完必须重启 Claude Code** 才加载 `/spine-run`、`/spine-analyze`、`spine-coder`。
- 已装过 → `claude plugin update agent-spine@agent-spine`。

### 4. 环境体检
```bash
npc doctor
```
逐项检查 git/openspec/codex/claude/jq/schema/portable-timeout 等。`required`（git）必须 ✓；`openspec`（archive + 目标拆解）、`codex`（默认 review 引擎）缺则按需装——不阻断安装，但 `/spine-run` 跑到对应阶段会需要。

---

## 验证整体可用

重启 Claude Code 后，在一个 **git + openspec** 工程内：
```text
/spine-run <一句话目标 或 已有 change 名>   [--auto]
```

## 成本路由（可选，默认 claude）

MiMo 默认**不启用**。要把某阶段卸到 MiMo（较慢但省 Claude 订阅），在工程根 `.npc/config.toml`：
```toml
[coder.phase]
fix = "mimo"          # 只把 fix 给 MiMo；implement 仍 claude
```
并准备仓库外密钥 `~/.config/npc/mimo.env`（chmod 600）。约束由 `npc verify routing` 在代码层强制（review 永不与 coder 同源、不含 mimo）。

## 卸载
```bash
uv tool uninstall npc
claude plugin uninstall agent-spine@agent-spine
```
