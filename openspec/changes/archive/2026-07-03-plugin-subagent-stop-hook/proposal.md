## Why

审计 B2/B12 的平台侧加固 + docs/research/2026-07-02-claude-code-platform-capabilities.md 第一节：SubagentStop hook 是「阻止 subagent 自声明完成」的核心平台机制，而 agent-spine plugin 目前没有任何 hooks（plugins/agent-spine/ 无 hooks/ 目录）。现状下 spine-coder 的最后消息（RESULT 行）不经任何校验直接回灌主 session：RESULT 行缺失/格式劣化、或谎报 `commit=<sha>`（sha 不存在）都要等到 `npc record` 甚至更晚才暴露（且按 B2 现状可能被静默吞掉）。在 subagent 出口就地设硬闸，符合不变量 2（结构化契约是唯一真相，且在最早的交接点强制）。

## What Changes

- `plugins/agent-spine/hooks/hooks.json` 新增 SubagentStop hook（`type=command`，命令走 `${CLAUDE_PLUGIN_ROOT}/hooks/verify-subagent-result.sh`，设 timeout）。
- 新增 bash 脚本 `hooks/verify-subagent-result.sh`：从 stdin JSON 取 subagent 最后消息，仅对 spine-coder 生效——(1) 校验末尾含合法 `RESULT:` 行（implement/fix/失败三套 schema 之一的 key 集合）；(2) 若 `commit=<sha>` 且 sha ≠ `-`，在 cwd 下 `git cat-file -e <sha>^{commit}` 验证真实存在。不合规 → exit 2 硬阻断回灌，stderr 给出缺陷说明。
- hook stdout 保持纯 JSON（或无输出），诊断信息只走 stderr——遵平台坑位说明。
- 非 spine-coder 的 subagent 直接放行（exit 0），hook 自身出错（如非 git 目录）放行不误伤。
- 补测试：脚本级用例（合法 RESULT 放行、缺 RESULT 阻断、假 sha 阻断、非 spine-coder 放行）。

## Capabilities

### New Capabilities

- `subagent-result-gate`: SubagentStop hook 对 spine-coder RESULT 行与 commit sha 真实性的出口硬闸。

### Modified Capabilities

## Impact

- `plugins/agent-spine/hooks/hooks.json`（新增）
- `plugins/agent-spine/hooks/verify-subagent-result.sh`（新增）
- `plugins/agent-spine/.claude-plugin/plugin.json`（如需声明 hooks 路径）
- `tests/`（hook 脚本行为用例）
