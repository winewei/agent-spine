# robust-orchestrator-json-parsing — Tasks

## 1. 契约惯用法替换

- [ ] 1.1 `plugins/agent-spine/commands/spine-run.md`：所有 `echo "$VAR" | jq …` → `printf '%s' "$VAR" | jq …`（含 `$(…)` 命令替换内、`[ "$(…)" ]` 测试内、`|| …` 多条件行内的全部形态）；面向用户的 `echo "[spine-run] …"` 提示行**不动**
- [ ] 1.2 `plugins/agent-spine/hooks/verify-subagent-result.sh`：同规则替换（~4 处）
- [ ] 1.3 `docs/cli.md`：同规则替换（~8 处，含 line 564 `.prompt` 提取样例）
- [ ] 1.4 人工复核每处：jq 表达式、后续管道、重定向均原样保留，仅 `echo`→`printf '%s'`；空格变体也覆盖

## 2. 充分测试

- [ ] 2.1 **根因复现 + 修复（跨 shell）**：构造含多行字符串字段（模拟 spawn_prompt 的 `\n`）的合规单行 JSON；断言 `printf '%s' "$V" | jq -r .ok` 在 **bash 与 zsh** 下均取到正确值；断言 `echo "$V" | jq` 在 zsh 下破坏（记录为对照，证明修法必要）
- [ ] 2.2 **契约守卫（防回归）**：测试 grep `spine-run.md` / `verify-subagent-result.sh` / `docs/cli.md`，断言脆弱模式 `echo "$VAR" | jq`（含空格变体）**零匹配**；未来新增 call site 用回 echo 即失败
- [ ] 2.3 **抽样正确性**：对替换后 `.ok` / `.spawn_prompt` / `.action` / `.prompt` 等关键提取点用构造 JSON 验证仍取到正确值，确认机械替换未误伤 jq 表达式
- [ ] 2.4 zsh 分支用 `shutil.which("zsh")` 守卫：无 zsh 时 skip 并标注；bash 分支与 grep 守卫恒跑
- [ ] 2.5 npc 现有全量测试回归通过（本 change 不动 npc 源码，应零影响）

## 3. 验证

- [ ] 3.1 手动在 zsh 里跑一次真实 `IMPL=$(npc implement run …); printf '%s' "$IMPL" | jq -r '.spawn_prompt'`，确认多行字段正确取出
- [ ] 3.2 确认 `verify-subagent-result.sh` hook 在 SubagentStop 下仍正确校验（替换不改逻辑）
