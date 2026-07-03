# json-safe-field-extraction

## ADDED Requirements

### Requirement: 编排契约用 escape-safe 惯用法从 npc JSON 提取字段

编排契约（`spine-run.md` 及配套 hook / docs）从 npc 单行 JSON 提取字段时 MUST 使用不解释反斜杠转义的惯用法——`printf '%s' "$VAR" | jq …`（或等价的 here-string `jq … <<< "$VAR"`）。MUST NOT 使用 `echo "$VAR" | jq`，因为在解释转义的 shell（zsh 默认）下 `echo` 会把 JSON 字符串值里的 `\n` 等转义序列还原成裸控制字符，破坏 JSON。

#### Scenario: 含多行字段的输出被正确提取

- **WHEN** npc 输出含多行字符串字段（如 `spawn_prompt` 的换行被转义为 `\n`）的单行 JSON，编排者用 `printf '%s' "$VAR" | jq -r '.spawn_prompt'` 提取
- **THEN** 在 zsh 与 bash 下均取到完整正确的字段值，jq 无 parse error

#### Scenario: 禁用的 echo 惯用法在 zsh 下破坏（对照）

- **WHEN** 用 `echo "$VAR" | jq` 处理同一含多行字段的 JSON
- **THEN** zsh 下 jq 报 `control characters ... must be escaped`（证明该惯用法不可用）

### Requirement: 自动化守卫禁止脆弱 echo 惯用法回归

仓库 MUST 有一个自动化守卫（测试），对 `plugins/agent-spine/commands/spine-run.md`、`plugins/agent-spine/hooks/verify-subagent-result.sh`、`docs/cli.md` 断言脆弱模式 `echo "$VAR" | jq`（含空格变体）零出现。任何未来新增或改动引入该模式时守卫 MUST 失败。

#### Scenario: 契约文件无脆弱模式

- **WHEN** 守卫测试扫描三个契约文件
- **THEN** 匹配到 `echo "$VAR" | jq` 的数量为 0，测试通过

#### Scenario: 回归被拦截

- **WHEN** 某次改动在契约文件里新增一处 `echo "$X" | jq`
- **THEN** 守卫测试失败并指出文件与行

### Requirement: npc 输出侧保持不变

本能力 MUST NOT 修改 npc 的 JSON 序列化（`_io.emit` 等）。根因在消费端 shell 惯用法；npc 的 `json.dumps(ensure_ascii=False)` 已对全部 U+0000–U+001F 控制字符合规转义，输出为合法单行 JSON。

#### Scenario: npc emit 输出仍是合规单行 JSON

- **WHEN** 任一 npc 子命令经 `_io.emit` 输出
- **THEN** 输出为单行、所有 C0 控制字符已转义的合法 JSON，可被 `printf '%s' | jq` 与 `python json.loads` 正确解析
