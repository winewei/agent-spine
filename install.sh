#!/usr/bin/env bash
# spine agent 一键安装：npc CLI（内置 src/npc）+ harness plugin。
# 幂等：可重复运行。从仓库根目录执行：`bash install.sh`
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓ %s\033[0m\n' "$*"; }
warn(){ printf '  \033[33m⚠ %s\033[0m\n' "$*"; }
die() { printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# 0. 前置工具
say "检查前置工具"
command -v git >/dev/null || die "缺 git"
command -v uv  >/dev/null || die "缺 uv —— 装：curl -LsSf https://astral.sh/uv/install.sh | sh"
ok "git / uv 就位"
HAVE_CLAUDE=0
if command -v claude >/dev/null; then HAVE_CLAUDE=1; ok "claude CLI 就位"; else
  warn "无 claude CLI —— 跳过 plugin 自动安装（稍后在 Claude Code 内 /plugin 安装）"
fi

# 1. 校验内置 npc
say "校验内置 npc（src/npc）"
[ -f pyproject.toml ] && [ -d src/npc ] || die "src/npc 未就绪（仓库不完整？）"
ok "src/npc 就位"

# 2. 装 npc CLI
say "安装 npc CLI（内置 src/npc，从仓库根安装）"
uv tool install --force --from . npc
NPC_VER="$(npc --version 2>/dev/null || true)"
[ -n "$NPC_VER" ] || die "npc 安装失败（npc --version 无输出）"
ok "$NPC_VER"

# 3. 装 harness plugin（有 claude CLI 才做）
if [ "$HAVE_CLAUDE" = 1 ]; then
  say "安装 harness plugin（agent-spine）"
  claude plugin marketplace add "$REPO_ROOT" >/dev/null 2>&1 \
    || claude plugin marketplace update agent-spine >/dev/null 2>&1 || true
  if claude plugin install agent-spine@agent-spine --scope user >/dev/null 2>&1; then
    ok "plugin 已安装"
  elif claude plugin update agent-spine@agent-spine >/dev/null 2>&1; then
    ok "plugin 已更新"
  else
    warn "plugin 安装未确认 —— 可手动：/plugin marketplace add $REPO_ROOT 然后 /plugin install agent-spine@agent-spine"
  fi
  warn "plugin 需【重启 Claude Code】才加载 /spine-run、/spine-analyze、spine-coder"
fi

# 4. 环境体检（缺 openspec/codex 等只警告，不阻断安装）
say "环境体检（npc doctor）"
DOC_TMP="$(mktemp)"
npc doctor > "$DOC_TMP" 2>/dev/null || true   # doctor 缺 required 会 exit 3，不阻断
python3 -c "
import json
d=json.load(open('$DOC_TMP'))
m={'ok':'✓','warn':'⚠','missing':'✗'}
for c in d.get('checks',[]):
    req=' (必需)' if c.get('required') else ''
    print('  '+m.get(c.get('status'),'?')+' '+str(c.get('name'))+req+': '+str(c.get('detail','')))
" 2>/dev/null || warn "doctor 输出解析失败（不影响安装）"
rm -f "$DOC_TMP"

say "完成 ✅"
echo "  下一步："
echo "  1) 重启 Claude Code 加载 plugin"
echo "  2) 在 git + openspec 工程内：/spine-run <目标或 change名>  [--auto]"
echo "  3) 成本路由（可选，默认 claude）：工程 .npc/config.toml 写 [coder.phase] fix=\"mimo\""
echo "  详见 docs/usage.md 与 INSTALL.md"
