# 安装 spine agent（面向 LLM 的逐步指南）

本指南给 **执行安装的 agent**（任意 agent CLI 宿主：Claude Code / Kimi CLI / Qwen Code / Codex / OpenCode / …）用。下面是一句话安装 + 每步验证 + 失败处置，便于 agent 自检与排错。

> spine agent = 安装后的 `npc` 命令（确定性执行层，代码在 `src/npc`；**唯一分发物**）+ 随包发行的宿主中立 playbooks（`spine-run` / `spine-analyze` / `spine-coder` / `new-plan-changes-v2/v3/v4`），经 `npc playbook install` 物化到宿主。v1.7 起不再有 Claude Code plugin。

## 一句话安装

在 agent-spine 仓库根执行（幂等，可重复跑）：

```bash
uv tool install --force --from . npc && npc playbook install --host claude
```

第二段按宿主替换：Claude Code 用 `--host claude`；Codex CLI 用 `--host codex`；其它宿主（kimi / qwen / opencode 等）用 `--dest <该宿主的自定义命令目录>`。装完 `npc doctor` 体检。若要逐步执行 / 排错，按下面来。

---

## 逐步（agent 可逐条执行并核验）

### 0. 前置工具
```bash
command -v git && command -v uv && echo OK
```
- 缺 `uv` → 装：`curl -LsSf https://astral.sh/uv/install.sh | sh`

### 1. 校验 npc 源码
```bash
test -f pyproject.toml && test -d src/npc && echo "src/npc OK"
```
- `npc` 命令由本仓库的 `src/npc` 安装得到，无需子模块；直接从本仓库根安装即可。

### 2. 装 npc 命令（从 src/npc）
```bash
uv tool install --force --from . npc
npc --version          # 期望：npc 1.7.0
```
验证：`npc --version` 有输出即成功。`--force` 必需（覆盖旧版）。

### 3. 物化 playbooks 到宿主（按宿主三选一）
```bash
npc playbook install --host claude       # Claude Code：~/.claude/{commands,skills,agents}/
npc playbook install --host codex        # Codex CLI：~/.codex/prompts/
npc playbook install --dest <DIR>        # 其它宿主（kimi / qwen / opencode 等）：平铺 md 到任意目录，按宿主机制挂载
```
- 验证：stdout JSON 的 `.installed` 非空、`.ok == true`；`npc playbook list` 可枚举全部。
- 幂等覆盖：升级 npc 后重跑同一条命令即同步 playbook 内容。
- Claude Code 装完**重启后生效**（`/spine-run` 等 slash command 与 skills）。
- 不想物化也行：任何宿主可直接 `npc playbook show spine-run` 把原文拉进 context 执行。

### 4. 环境体检
```bash
npc doctor
```
逐项检查 git/openspec/codex/claude/jq/schema/portable-timeout/host 等。`required`（git）必须通过；`openspec`（archive + 目标拆解）、`codex`（默认 review 引擎）缺则按需装——不阻断安装，但 `spine-run` 跑到对应阶段会需要。`host` 检查项显示探测到的宿主与 session 识别能力。

---

## 验证整体可用

在一个 **git + openspec** 工程内，让宿主执行 playbook（Claude Code 为 `/spine-run ...`；其它宿主先 `npc playbook show spine-run` 拉进 context 再给参数）：

```text
spine-run <一句话目标 或 已有 change 名>   [--auto]
new-plan-changes-v2                        # 批量推进所有活跃 openspec changes（串行）
```

## 非 Claude 宿主的可选配置

```toml
# <repo>/.npc/config.toml 或 ~/.config/npc/config.toml
[host]
name = "kimi"                              # 显式声明宿主（缺省自动探测：CLAUDECODE env → claude，否则 generic）
session_dir = ".kimi/sessions/{proj_key}"  # 可选：宿主有 per-project transcript 目录时补模板，升级 session 识别
```

generic 宿主下 `npc init --auto` 不写授权文件（那是 claude 专属的 `.claude/settings.json`），无人值守权限请按宿主自身机制放行。

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
# 已物化的 playbook 文件按安装目标手动删除（如 ~/.claude/commands/spine-*.md）
```
