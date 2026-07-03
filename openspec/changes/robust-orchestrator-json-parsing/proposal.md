# robust-orchestrator-json-parsing

## Why

实测 bug（2026-07-03 使用中遇到）：编排者跑 `IMPL=$(npc implement run --seq 1); echo "$IMPL" | jq -r '.ok'` 时 jq 报 `parse error: Invalid string: control characters from U+0000 through U+001F must be escaped at line 3`。

**根因（已复现确诊）**：与 npc 无关。npc 的 `_io.emit` 用 `json.dumps(ensure_ascii=False)`，Python 编码器**总是**把 C0 控制字符（U+0000–U+001F）转义为 `\n`/`\t`/`\uXXXX`，输出是合规单行 JSON。问题在**消费端 shell 惯用法** `echo "$VAR" | jq`：**zsh 的 `echo` 默认解释反斜杠转义**，会把 JSON 字符串里的 `\n`（两字符：反斜杠+n）反转义成**真正的换行符**，破坏 JSON。因此只有输出含多行字符串字段（如 `spawn_prompt`、`.prompt`）的命令会触发——`implement run`/`fix run` 挂，而 `init-run`/`archive`/`finalize` 因无多行字段而幸存，掩盖了问题的系统性。

复现与验证（zsh）：

```
echo "$IMPL" | jq -r '.ok'        # ✗ parse error（echo 反转义 \n）
printf '%s' "$IMPL" | jq -r '.ok' # ✓ true
jq -r '.ok' <<< "$IMPL"           # ✓ true
```

该脆弱惯用法在编排契约里**遍布约 71 处**（`spine-run.md` 59、`hooks/verify-subagent-result.sh` 4、`docs/cli.md` 8），是随时会因新增多行字段而爆的定时炸弹。当前靠"存文件再用 python 取字段"绕过，是临时工作绕行而非修复。

## What Changes

- **统一 escape-safe 提取惯用法**：把编排契约里所有 `echo "$VAR" | jq …` 替换为 `printf '%s' "$VAR" | jq …`（`printf '%s'` 不解释转义、POSIX 可移植、与原写法视觉结构一致，diff 最小、易逐处复核）。覆盖 `plugins/agent-spine/commands/spine-run.md`、`plugins/agent-spine/hooks/verify-subagent-result.sh`、`docs/cli.md`。
- **加自动化守卫防回归**：新增测试 grep 上述契约文件，断言不再出现 `echo "$VAR" | jq` 脆弱模式——把"文档契约"纳入可测，防止未来新增 call site 重新引入炸弹。
- **npc 侧不改**：`emit` 输出已合规，根因在消费端，不动 npc 代码（避免为消费端 bug 改生产端）。

## Capabilities

### New Capabilities

- `json-safe-field-extraction`：编排契约从 npc 单行 JSON 提取字段 MUST 用 escape-safe 惯用法（`printf '%s'` / here-string），禁用 `echo "$VAR" | jq`；含防回归的自动化守卫。

### Modified Capabilities

<!-- 无已建立的 spec capability 需 delta；本 change 为新增契约 + 文档惯用法修正。 -->

## Impact

- `plugins/agent-spine/commands/spine-run.md`（~59 处 `echo "$X" | jq` → `printf '%s' "$X" | jq`）
- `plugins/agent-spine/hooks/verify-subagent-result.sh`（~4 处）
- `docs/cli.md`（~8 处，含 line 564 `.prompt` 提取的高危样例）
- 新增测试：`tests/`（跨 shell 复现+修复验证、契约文件脆弱模式守卫）
- 无 npc 源码改动
