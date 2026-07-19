# 配置指南：review 引擎 / coder 多模型 / 宿主

npc 的配置为 TOML 文件，按优先级分层加载：

1. `--config <path>`（CLI 显式传入；只读该文件，不参与合并）
2. `<repo_root>/.npc/config.toml`（项目级；可入 git，只放路由不放凭据）
3. `~/.config/npc/config.toml`（用户全局；provider 定义与凭据指针的家）
4. `~/task_log/config.toml`（兼容 task_log 布局）

2-4 层做**分层深合并**（v1.6+）：table 递归合并、标量/数组整体覆盖，低优先级打底、高优先级覆盖。典型分工：全局定义 `[providers.*]`，项目只写 `[coder]` 路由。

---

## Review 引擎配置

`npc review run` 默认用 `codex`，也可切到 `claude`（即 `claude -p` 非交互模式）。

完整 schema：

```toml
[review]
engine = "codex"           # codex（默认）| claude

[review.codex]
bin = "codex"              # 可省略；默认 PATH 查找

[review.claude]
bin = "claude"             # 可省略；默认 PATH 查找
model = "claude-opus-4-7"  # 可省略；省略则用 claude 默认 model
extra_args = ["--permission-mode", "default"]   # 可省略
```

### 用自定义 claude 包装（路由到 qwen / deepseek 等后端）

常见做法是用 shell alias 把 `claude` 指到不同后端，例如 `~/.zshrc` 里：

```zsh
alias claude-qwen='claude --settings ~/.claude/qwen-settings.json'
```

**alias 不能直接填进 `bin`**——npc 用 `subprocess` 起子进程，不经过 shell，看不到 `.zshrc` 里的 alias。正确做法是把 alias 拆成 `bin` + `extra_args`：

```toml
[review]
engine = "claude"

[review.claude]
bin = "claude"
extra_args = ["--settings", "/Users/you/.claude/qwen-settings.json"]
```

四个易错点：

- **`bin` 填真实可执行文件**（`claude`，PATH 上有；或写绝对路径），**不是 alias 名**。
- **alias 后面的 flag 全挪进 `extra_args`**；npc 会拼成 `claude -p --output-format text <extra_args...>`，顺序无所谓。
- **路径必须用绝对路径，`~` 不会展开**——`extra_args` 原样进 argv、不经过 shell，写 `~/...` 会被当字面量找不到文件。
- 后端模型由 settings 文件决定时，**别再设 `model`**，否则会多传一个冲突的 `--model`。

> 若 alias 里含内联环境变量（如 `FOO=bar claude ...`），`extra_args` 表达不了，需做一个真实 wrapper 脚本（如 `~/.local/bin/claude-qwen`，内部 `export` 后 `exec claude "$@"`），再把 `bin` 指向它的绝对路径。

---

## Coder 多模型配置（每工程独立选型）

v1.6+ 的 coder（implement / fix 执行体）通过 **provider 注册表**路由：全局定义一次模型与凭据，每个工程只声明"用哪个"。内置三个 provider 无需声明：`claude`（默认，走订阅）、`mimo`、`codex`。接入 kimi / qwen / deepseek 等 Anthropic 兼容端点按下面四步照做。

**第 1 步：为每个模型建一个 env 凭据文件**（全局，勿入任何 git 仓库）：

```bash
mkdir -p ~/.config/npc
cat > ~/.config/npc/kimi.env <<'EOF'
export ANTHROPIC_BASE_URL=https://api.moonshot.cn/anthropic
export ANTHROPIC_AUTH_TOKEN=sk-你的密钥
EOF
chmod 600 ~/.config/npc/kimi.env
```

`BASE_URL` 填厂商的 **Anthropic 兼容端点**（以各厂商官方文档为准；kimi 为 `https://api.moonshot.cn/anthropic`，deepseek 为 `https://api.deepseek.com/anthropic`）。支持 `export K=V` 与裸 `K=V` 两种写法，`#` 注释行会被忽略。

**第 2 步：在全局 `~/.config/npc/config.toml` 注册 provider**：

```toml
[providers.kimi]
runner = "claude-cli"                  # claude-cli（默认，可省略）| codex-cli
env_file = "~/.config/npc/kimi.env"
model = "kimi-k3"

[providers.deepseek]
env_file = "~/.config/npc/deepseek.env"
model = "deepseek-chat"

[providers.gpt-codex]
runner = "codex-cli"                   # 走 codex exec，无需 env_file（用 codex 自身登录态）
model = "gpt-5.4-codex"
```

**第 3 步：在目标工程写 `.npc/config.toml`，只做路由**（可入 git，无凭据）：

```toml
[coder]
backend = "kimi"          # 本工程 coder 默认走 kimi

[coder.phase]             # 可选：按阶段细分
implement = "kimi"
fix = "deepseek"
```

review 不用配——恒走 premium 引擎（codex / claude），这是刻意设计：第三方廉价层只许执行，不许给自己盖章（`npc verify routing` 会强制拦截 review 路由到任何带 env_file 的 provider）。

**第 4 步：验证**：

```bash
npc doctor | jq '.checks[] | select(.name=="providers")'   # 期望 status=="ok"，detail 列出在用 provider
npc verify routing                                          # 期望 {"ok":true,...}，exit 0
```

临时切换某一次执行的后端：`npc implement run --seq 1 --backend deepseek`（`--backend` 接受任意已注册 provider 名）。

---

## 宿主配置（`[host]`，v1.7）

npc 默认自动探测宿主：`CLAUDECODE` env 存在 → `claude`（完整能力），否则 `generic`（session 识别退化为 by-cwd hook、`--auto` 不写授权文件）。需要显式指定或为非 Claude 宿主补 session 目录时：

```toml
[host]
name = "kimi"                              # claude | generic | 任意自定义名
session_dir = ".kimi/sessions/{proj_key}"  # 可选：相对 home 的 session 目录模板
```

---

## 常见报错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 加载配置报 `未知 coder backend：'kimi'` (exit 2) | 路由引用了没注册的 provider | 检查全局 config.toml 的 `[providers.kimi]` 是否存在、拼写是否一致 |
| `env_file 缺失`（exit 4）| 第 1 步的 env 文件路径不对或没建 | `npc doctor` 的 providers 检查会给出期望路径 |
| `env_file 读取失败...权限`（exit 3）| 文件权限过紧且属主不对 | 确认文件属主为当前用户且 `chmod 600` |
| coder 跑完但走的还是 Claude 订阅 | env 文件里 `ANTHROPIC_BASE_URL` 拼错 / 未生效 | 检查 env 文件 key 名；provider 路由只在显式配置后启用 |
