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
    AGENT_TYPE="$(printf '%s' "$INPUT" | jq -r '.agent_type // ""' 2>/dev/null || true)"
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
        LAST_MSG="$(printf '%s' "$INPUT" | jq -r '.last_assistant_message // ""' 2>/dev/null || true)"
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
    LAST_MSG="$(printf '%s' "$INPUT" | jq -r '.last_assistant_message // ""' 2>/dev/null || true)"
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

# ── 5. Validate RESULT key set (schema-variant aware) ───────────────────────
# Accepted schema variants:
#   implement success: commit=<sha> tasks=<n> tests=pass summary=<path> notes=<...>
#   fix success:       commit=<sha> fixed=<n> tests=pass summary=<path>
#                        categories_scanned=<csv> regressions_added=<csv|-> notes=<...>
#   failure (any):     commit=- tasks=<n> tests=fail summary=<path|-> notes=<...>
#
# Detection logic:
#   1. commit=- AND tests=fail  → failure schema
#   2. fixed= present           → fix success schema
#   3. otherwise                → implement success schema

_has_key() {
    echo "$RESULT_LINE" | grep -qE "(^|[[:space:]])${1}"
}

# Extract commit= and tests= values for schema detection
COMMIT_RAW="$(echo "$RESULT_LINE" | grep -oE 'commit=[^[:space:]]+' | head -1 | sed 's/commit=//' || true)"
TESTS_RAW="$(echo "$RESULT_LINE" | grep -oE 'tests=[^[:space:]]+' | head -1 | sed 's/tests=//' || true)"

# Determine which schema variant this RESULT claims to be
SCHEMA_VARIANT=""
if [ "$COMMIT_RAW" = "-" ] && [ "$TESTS_RAW" = "fail" ]; then
    SCHEMA_VARIANT="failure"
elif _has_key "fixed="; then
    SCHEMA_VARIANT="fix"
else
    SCHEMA_VARIANT="implement"
fi

# Define required keys per variant and validate
missing_keys=""
case "$SCHEMA_VARIANT" in
    implement)
        # implement success: commit= tasks= tests= summary= notes=
        for key in commit= tasks= tests= summary= notes=; do
            if ! _has_key "${key}"; then
                missing_keys="${missing_keys} ${key}"
            fi
        done
        # tests= must be "pass" for implement success
        if [ "$TESTS_RAW" != "pass" ] && [ -n "$TESTS_RAW" ]; then
            echo "ERROR: implement RESULT schema requires tests=pass, got tests=${TESTS_RAW}" >&2
            echo "RESULT line: $RESULT_LINE" >&2
            exit 2
        fi
        # commit=- is reserved for the failure schema; success must have a real hash
        if [ "$COMMIT_RAW" = "-" ]; then
            echo "ERROR: implement success RESULT requires a real commit hash, not commit=-" >&2
            echo "       Use commit=- only with tests=fail (failure schema)" >&2
            echo "RESULT line: $RESULT_LINE" >&2
            exit 2
        fi
        ;;
    fix)
        # fix success: commit= fixed= tests= summary= categories_scanned= regressions_added= notes=
        for key in commit= fixed= tests= summary= categories_scanned= regressions_added= notes=; do
            if ! _has_key "${key}"; then
                missing_keys="${missing_keys} ${key}"
            fi
        done
        # tests= must be "pass" for fix success
        if [ "$TESTS_RAW" != "pass" ] && [ -n "$TESTS_RAW" ]; then
            echo "ERROR: fix RESULT schema requires tests=pass, got tests=${TESTS_RAW}" >&2
            echo "RESULT line: $RESULT_LINE" >&2
            exit 2
        fi
        # commit=- is reserved for the failure schema; fix success must have a real hash
        if [ "$COMMIT_RAW" = "-" ]; then
            echo "ERROR: fix success RESULT requires a real commit hash, not commit=-" >&2
            echo "       Use commit=- only with tests=fail (failure schema)" >&2
            echo "RESULT line: $RESULT_LINE" >&2
            exit 2
        fi
        ;;
    failure)
        # failure: commit=- tasks= tests=fail summary= notes=
        for key in commit= tasks= tests= summary= notes=; do
            if ! _has_key "${key}"; then
                missing_keys="${missing_keys} ${key}"
            fi
        done
        # commit must be "-" in failure schema
        if [ "$COMMIT_RAW" != "-" ]; then
            echo "ERROR: failure RESULT schema requires commit=-, got commit=${COMMIT_RAW}" >&2
            echo "RESULT line: $RESULT_LINE" >&2
            exit 2
        fi
        ;;
esac

if [ -n "$missing_keys" ]; then
    echo "ERROR: RESULT 行 (schema=${SCHEMA_VARIANT}) 缺少必需 key:${missing_keys}" >&2
    echo "RESULT line: $RESULT_LINE" >&2
    exit 2
fi

# ── 6. Validate commit sha if non-"-" ───────────────────────────────────────
# COMMIT_RAW was extracted in step 5 for schema detection; reuse it here.
COMMIT_VAL="$COMMIT_RAW"

if [ -z "$COMMIT_VAL" ]; then
    echo "ERROR: RESULT 行中 commit= 值为空" >&2
    exit 2
fi

if [ "$COMMIT_VAL" != "-" ]; then
    # Verify sha exists in current repo
    CWD_VAL=""
    if command -v jq >/dev/null 2>&1; then
        CWD_VAL="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null || true)"
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
