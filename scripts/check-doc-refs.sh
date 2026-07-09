#!/usr/bin/env bash
# 文档防漂移检查：核心文档里的事实性引用必须与仓库现实一致。
# 本次 2026-07-09 审计抓到的 11 处过时事实中，9 处属于本脚本可拦截的类型。
# 用法：bash scripts/check-doc-refs.sh   （exit 0 = 通过；CI / pre-commit 均可挂）
set -u
cd "$(dirname "$0")/.."
fail=0
err() { echo "FAIL: $*" >&2; fail=1; }

DOCS="CLAUDE.md README.md INSTALL.md docs/usage.md plugins/agent-spine/README.md"

# 1. CLAUDE.md 引用的关键路径必须存在
for p in src/npc docs/cli.md docs/principles.md plugins/agent-spine openspec; do
  [ -e "$p" ] || err "CLAUDE.md 引用路径不存在: $p"
done

# 2. 文档中出现的「npc <子命令>」必须出现在 npc --help
help_out=$(npc --help 2>/dev/null)
if [ -z "$help_out" ]; then
  err "npc CLI 不可用（~/.local/bin/npc）"
else
  # shellcheck disable=SC2086
  for sub in $(grep -hoE '`?npc ([a-z][a-z-]+)`?' $DOCS docs/cli.md 2>/dev/null \
               | sed -E 's/`//g; s/^npc //' | sort -u); do
    echo "$help_out" | grep -qE "^    ${sub}( |$)" \
      || err "文档提到的 npc 子命令不在 npc --help 中: npc $sub"
  done
fi

# 3. 文档中的 subagent_type="X" 必须有对应 plugins/agent-spine/agents/X.md
# shellcheck disable=SC2086
for a in $(grep -hoE 'subagent_type="[a-z-]+"' $DOCS docs/cli.md 2>/dev/null \
           | sed -E 's/subagent_type="([a-z-]+)"/\1/' | sort -u); do
  [ -f "plugins/agent-spine/agents/${a}.md" ] \
    || err "文档引用了不存在的 agent: subagent_type=\"$a\"（plugins/agent-spine/agents/ 下无此文件）"
done

# 4. 文档中的 /spine-* 与 /opsx:* 命令必须有实体文件
# shellcheck disable=SC2086
for c in $(grep -hoE '(^|[[:space:]`（(])/spine-[a-z]+' $DOCS plugins/agent-spine/commands/*.md 2>/dev/null \
           | grep -oE '/spine-[a-z]+' | sort -u); do
  [ -f "plugins/agent-spine/commands/${c#/}.md" ] || err "文档引用了不存在的命令: $c"
done
# shellcheck disable=SC2086
for c in $(grep -hoE '/opsx:[a-z]+' $DOCS plugins/agent-spine/commands/*.md .claude/commands/opsx/*.md 2>/dev/null | sort -u); do
  [ -f ".claude/commands/opsx/${c#/opsx:}.md" ] || err "文档引用了不存在的命令: $c"
done

# 5. plugin.json 声明的 agents/commands 与实际文件一致
python3 - <<'EOF' || fail=1
import json, os, re, sys
d = json.load(open("plugins/agent-spine/.claude-plugin/plugin.json"))
desc = d.get("description", "")
ok = True
m = re.search(r"agents:\s*([\w, -]+)；?", desc)
if m:
    for a in [x.strip() for x in m.group(1).split(",")]:
        if not os.path.isfile(f"plugins/agent-spine/agents/{a}.md"):
            print(f"FAIL: plugin.json 声明的 agent 无文件: {a}", file=sys.stderr); ok = False
sys.exit(0 if ok else 1)
EOF

if [ "$fail" -ne 0 ]; then
  echo "check-doc-refs: 有文档事实与仓库现实不一致（见上）" >&2
  exit 1
fi
echo "check-doc-refs: OK"
