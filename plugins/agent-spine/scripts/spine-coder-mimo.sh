#!/usr/bin/env bash
# spine-coder-mimo.sh — 成本感知路由：把 spine 的 coder（高 token 消耗的 bulk 工作）
# 路由到 MiMo（Anthropic 兼容后端），经 headless `claude -p` 执行，省 Claude 订阅额度。
#
# 用法：
#   spine-coder-mimo.sh implement <change-id>
#   spine-coder-mimo.sh fix <change-id> <round>
#
# 行为：
#   1. npc 渲染该阶段完整 prompt 到 disk，并生成 ~150 token 引导语
#   2. source 仓库外的 ~/.config/npc/mimo.env（含 MiMo base_url + token + model=mimo-v2.5-pro）
#   3. 在 repo 根用 `claude -p ... --model mimo-v2.5-pro --permission-mode bypassPermissions` 跑 coder
#   4. 从 stdout 抽最后一行 RESULT 打印出来（供主 session 喂给 npc implement/fix record）
#
# 退出码：0=产出了 RESULT 行；3=mimo.env 缺失（主 session 应回退到 Claude 上的 spine-coder subagent）
#
# 设计纪律（见 docs/principles.md 不变量 #1 生成⊥验证）：
#   本启动器只跑 coder（生成）。review（验证闸门）必须仍走 codex/Claude，
#   绝不路由到与 coder 同源的 MiMo —— 否则就是“自己评自己”。
set -euo pipefail

PHASE="${1:?usage: spine-coder-mimo.sh <implement|fix> <change-id> [round]}"
CID="${2:?missing change-id}"
ROUND="${3:-}"
MIMO_ENV="${SPINE_MIMO_ENV:-$HOME/.config/npc/mimo.env}"
MODEL="${SPINE_CODER_MODEL:-mimo-v2.5-pro}"

if [ ! -f "$MIMO_ENV" ]; then
  echo "RESULT: commit=- tasks=0 tests=fail summary=- notes=mimo.env 缺失($MIMO_ENV)，主 session 请回退 Claude spine-coder" >&2
  exit 3
fi

# 1. 渲染 prompt + 取引导语（确定性，由 npc 完成）
if [ "$PHASE" = "fix" ]; then
  : "${ROUND:?fix 阶段需要 round}"
  npc agent prompt render --phase fix --change-id "$CID" --round "$ROUND" >/dev/null
  SPAWN=$(npc agent spawn-prompt --phase fix --change-id "$CID" --round "$ROUND")
else
  npc agent prompt render --phase implement --change-id "$CID" >/dev/null
  SPAWN=$(npc agent spawn-prompt --phase implement --change-id "$CID")
fi
PROMPT=$(printf '%s' "$SPAWN" | jq -r '.prompt')
REPO=$(git rev-parse --show-toplevel)

# 2+3. 在 repo 根、MiMo 后端、headless 跑 coder（token 只活在子 shell，绝不回显）
OUT=$(
  cd "$REPO"
  # shellcheck disable=SC1090
  source "$MIMO_ENV"
  claude -p "$PROMPT" --model "$MODEL" --permission-mode bypassPermissions < /dev/null 2>/dev/null
)

# 4. 抽最后一行 RESULT
RESULT_LINE=$(printf '%s\n' "$OUT" | grep -E '^RESULT:' | tail -1 || true)
if [ -z "$RESULT_LINE" ]; then
  echo "RESULT: commit=- tasks=0 tests=fail summary=- notes=MiMo coder 未产出 RESULT 行（claude -p 可能异常）"
  exit 0
fi
printf '%s\n' "$RESULT_LINE"
