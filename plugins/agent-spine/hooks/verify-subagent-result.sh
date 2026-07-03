#!/usr/bin/env bash
# verify-subagent-result.sh — SubagentStop hook for agent-spine plugin
#
# Validates that spine-coder's last message contains a well-formed RESULT line
# and that any declared git commit sha actually exists in the repo.
#
# Exit codes:
#   0 — pass (valid RESULT, or not spine-coder, or self-error → safe release)
#   2 — hard block (malformed/missing RESULT or fake commit sha)
#
# stdin:  SubagentStop hook JSON payload
# stdout: empty or pure JSON (never pollutes parser)
# stderr: human-readable diagnostics on failure

set -euo pipefail

# ── 1. Read stdin ────────────────────────────────────────────────────────────
INPUT="$(cat)"

# ── 2. Identify agent (spine-coder only) ───────────────────────────────────
# Prefer agent_type field (set when spawned with subagent_type=spine-coder).
# Fall back to checking transcript path or last message pattern.
AGENT_TYPE=""
if command -v jq >/dev/null 2>&1; then
    AGENT_TYPE="$(echo "$INPUT" | jq -r '.agent_type // ""' 2>/dev/null || true)"
else
    # No jq: use python3 fallback
    AGENT_TYPE="$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('agent_type', ''))
except Exception:
    print('')
" 2>/dev/null || true)"
fi

# If agent_type is not explicitly set, try to detect by presence of RESULT: in
# last_assistant_message (only spine-coder is expected to emit it, but we also
# check for absence which means → probably not spine-coder → release).
if [ -z "$AGENT_TYPE" ] || [ "$AGENT_TYPE" = "null" ]; then
    # Try to read last_assistant_message to decide
    LAST_MSG=""
    if command -v jq >/dev/null 2>&1; then
        LAST_MSG="$(echo "$INPUT" | jq -r '.last_assistant_message // ""' 2>/dev/null || true)"
    else
        LAST_MSG="$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('last_assistant_message', ''))
except Exception:
    print('')
" 2>/dev/null || true)"
    fi
    # Only apply validation if the message looks like it came from spine-coder
    # (contains a RESULT: pattern). Unknown agents without RESULT → release.
    if ! echo "$LAST_MSG" | grep -qE '^RESULT:[[:space:]]'; then
        exit 0
    fi
    # Treat as spine-coder
    AGENT_TYPE="spine-coder"
fi

# Non-spine-coder agents: release immediately
if [ "$AGENT_TYPE" != "spine-coder" ]; then
    exit 0
fi

# ── 3. Extract last assistant message ───────────────────────────────────────
LAST_MSG=""
if command -v jq >/dev/null 2>&1; then
    LAST_MSG="$(echo "$INPUT" | jq -r '.last_assistant_message // ""' 2>/dev/null || true)"
else
    LAST_MSG="$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('last_assistant_message', ''))
except Exception:
    print('')
" 2>/dev/null || true)"
fi

# ── 4. Find RESULT line (must be the last non-empty line) ───────────────────
RESULT_LINE="$(echo "$LAST_MSG" | grep -E '^RESULT:[[:space:]]' | tail -1 || true)"

if [ -z "$RESULT_LINE" ]; then
    echo "ERROR: spine-coder 最后消息不含 RESULT: 行" >&2
    echo "Last message (tail):" >&2
    echo "$LAST_MSG" | tail -5 >&2
    exit 2
fi

# ── 5. Validate RESULT key set ──────────────────────────────────────────────
# Accepted schema variants:
#   implement success: commit= tasks= tests= summary= notes=
#   fix success:       commit= fixed= tests= summary= categories_scanned= regressions_added= notes=
#   failure:           commit=- tasks= tests=fail summary= notes=
#
# Minimum required keys for any valid RESULT line:
REQUIRED_KEYS="commit= tests= notes="

missing_keys=""
for key in commit= tests= notes=; do
    if ! echo "$RESULT_LINE" | grep -qE "(^|[[:space:]])${key}"; then
        missing_keys="${missing_keys} ${key}"
    fi
done

if [ -n "$missing_keys" ]; then
    echo "ERROR: RESULT 行缺少必需 key:${missing_keys}" >&2
    echo "RESULT line: $RESULT_LINE" >&2
    exit 2
fi

# ── 6. Validate commit sha if non-"-" ───────────────────────────────────────
# Extract commit= value
COMMIT_VAL="$(echo "$RESULT_LINE" | grep -oE 'commit=[^[:space:]]+' | head -1 | sed 's/commit=//' || true)"

if [ -z "$COMMIT_VAL" ]; then
    echo "ERROR: RESULT 行中 commit= 值为空" >&2
    exit 2
fi

if [ "$COMMIT_VAL" != "-" ]; then
    # Verify sha exists in current repo
    CWD_VAL=""
    if command -v jq >/dev/null 2>&1; then
        CWD_VAL="$(echo "$INPUT" | jq -r '.cwd // ""' 2>/dev/null || true)"
    else
        CWD_VAL="$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('cwd', ''))
except Exception:
    print('')
" 2>/dev/null || true)"
    fi

    # Determine working directory for git check
    GIT_DIR="${CWD_VAL:-$PWD}"

    # Check if the directory is a git repo; if not, release safely
    if ! git -C "$GIT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        # Not a git repo — cannot verify; release safely per spec
        exit 0
    fi

    # Verify the commit exists
    if ! git -C "$GIT_DIR" cat-file -e "${COMMIT_VAL}^{commit}" 2>/dev/null; then
        echo "ERROR: RESULT 行声明的 commit sha 在 git 对象库中不存在: ${COMMIT_VAL}" >&2
        echo "Working directory: ${GIT_DIR}" >&2
        exit 2
    fi
fi

# ── 7. All checks passed ─────────────────────────────────────────────────────
exit 0
