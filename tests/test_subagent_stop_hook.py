"""Tests for plugins/agent-spine/hooks/verify-subagent-result.sh

Each test drives the shell script via subprocess, feeding a crafted JSON
payload over stdin and asserting the expected exit code and stderr content.

Test matrix (from spec):
  3.1  合法 implement RESULT + 真实 sha → exit 0
  3.2  缺 RESULT 行 / key 缺失 → exit 2
  3.3  sha 不存在于 git → exit 2；commit=- → 放行
  3.4  非 spine-coder subagent → exit 0
  3.5  pytest 全绿（本文件）
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# Absolute path to the hook script
REPO_ROOT = Path(__file__).parent.parent
HOOK_SCRIPT = REPO_ROOT / "plugins" / "agent-spine" / "hooks" / "verify-subagent-result.sh"


def _run_hook(payload: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run the hook script with a JSON payload via stdin."""
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def _make_result_line(
    commit: str = "abc1234",
    tasks: str = "5",
    tests: str = "pass",
    summary: str = "/tmp/summary.md",
    notes: str = "-",
) -> str:
    return (
        f"RESULT: commit={commit} tasks={tasks} tests={tests}"
        f" summary={summary} notes={notes}"
    )


def _spine_payload(
    last_message: str,
    cwd: str = "/tmp",
    agent_type: str = "spine-coder",
) -> dict:
    return {
        "agent_type": agent_type,
        "last_assistant_message": last_message,
        "cwd": cwd,
        "session_id": "test-session",
    }


# ── 3.1 合法 implement RESULT + 真实 sha → exit 0 ────────────────────────────

def test_valid_result_with_real_sha(fake_repo: Path) -> None:
    """3.1: Valid implement RESULT line with a real commit sha exits 0."""
    # Get the HEAD sha from the fake repo fixture
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()

    result_line = _make_result_line(commit=sha)
    message = f"Some output\n{result_line}"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0, got {proc.returncode}\nstderr: {proc.stderr}"


def test_valid_fix_result(fake_repo: Path) -> None:
    """3.1: Valid fix RESULT line (fix schema) exits 0."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()

    result_line = (
        f"RESULT: commit={sha} fixed=3 tests=pass"
        f" summary=/tmp/s.md categories_scanned=validation regressions_added=test_a notes=-"
    )
    message = f"Fix work done.\n{result_line}"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0\nstderr: {proc.stderr}"


def test_failure_schema_commit_dash_passes(fake_repo: Path) -> None:
    """3.3: commit=- (failure schema) is released even without real sha."""
    result_line = "RESULT: commit=- tasks=2 tests=fail summary=- notes=build error occurred"
    message = f"Implementation failed.\n{result_line}"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0 for commit=-\nstderr: {proc.stderr}"


# ── 3.2 缺 RESULT 行 / key 缺失 → exit 2 ────────────────────────────────────

def test_missing_result_line(fake_repo: Path) -> None:
    """3.2: No RESULT: line in last message → exit 2."""
    message = "I finished implementing everything. Summary is at /tmp/summary.md"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2\nstderr: {proc.stderr}"
    assert "RESULT" in proc.stderr or "result" in proc.stderr.lower()


def test_result_line_missing_commit_key(fake_repo: Path) -> None:
    """3.2: RESULT line missing 'commit=' key → exit 2."""
    result_line = "RESULT: tasks=5 tests=pass summary=/tmp/s.md notes=-"
    message = f"Done.\n{result_line}"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2\nstderr: {proc.stderr}"


def test_result_line_missing_tests_key(fake_repo: Path) -> None:
    """3.2: RESULT line missing 'tests=' key → exit 2."""
    result_line = "RESULT: commit=abc123 tasks=5 summary=/tmp/s.md notes=-"
    message = f"Done.\n{result_line}"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2\nstderr: {proc.stderr}"


def test_result_line_missing_notes_key(fake_repo: Path) -> None:
    """3.2: RESULT line missing 'notes=' key → exit 2."""
    result_line = "RESULT: commit=abc123 tasks=5 tests=pass summary=/tmp/s.md"
    message = f"Done.\n{result_line}"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2\nstderr: {proc.stderr}"


def test_empty_last_message(fake_repo: Path) -> None:
    """3.2: Empty last_assistant_message → exit 2 (spine-coder must emit RESULT)."""
    payload = _spine_payload("", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2\nstderr: {proc.stderr}"


# ── 3.3 sha 不存在 → exit 2 ──────────────────────────────────────────────────

def test_fake_sha_blocked(fake_repo: Path) -> None:
    """3.3: commit=abc1234 but sha does not exist in git → exit 2."""
    fake_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    result_line = _make_result_line(commit=fake_sha)
    message = f"Implementation complete.\n{result_line}"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2 for fake sha\nstderr: {proc.stderr}"
    assert fake_sha in proc.stderr or "commit" in proc.stderr.lower()


def test_commit_dash_no_git_check(fake_repo: Path) -> None:
    """3.3: commit=- skips git verification → exit 0."""
    result_line = _make_result_line(commit="-", tests="fail")
    message = f"Failed.\n{result_line}"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0 for commit=-\nstderr: {proc.stderr}"


# ── 3.4 非 spine-coder subagent → exit 0 ────────────────────────────────────

def test_non_spine_coder_released() -> None:
    """3.4: agent_type != spine-coder → hook releases immediately (exit 0)."""
    payload = {
        "agent_type": "some-other-agent",
        "last_assistant_message": "I did something without RESULT line",
        "cwd": "/tmp",
        "session_id": "test-session",
    }
    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0 for non-spine-coder\nstderr: {proc.stderr}"


def test_unknown_agent_without_result_released() -> None:
    """3.4: Unknown agent_type with no RESULT pattern → released (exit 0)."""
    payload = {
        "agent_type": "",
        "last_assistant_message": "Just finished some work.",
        "cwd": "/tmp",
        "session_id": "test-session",
    }
    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0 for unknown agent\nstderr: {proc.stderr}"


def test_no_agent_type_field_no_result_released() -> None:
    """3.4: Missing agent_type field and no RESULT: in message → released."""
    payload = {
        "last_assistant_message": "Regular work output without any RESULT prefix.",
        "cwd": "/tmp",
        "session_id": "test-session",
    }
    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0\nstderr: {proc.stderr}"


# ── Robustness: self-error / non-git-dir → exit 0 ────────────────────────────

def test_non_git_dir_releases(tmp_path: Path) -> None:
    """Spec: hook self-error (non-git dir) → exit 0, no misfire."""
    # tmp_path is not a git repo
    sha = "abc1234abc1234abc1234abc1234abc1234abc1234"
    result_line = _make_result_line(commit=sha)
    message = f"Done.\n{result_line}"
    payload = _spine_payload(message, cwd=str(tmp_path))

    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0 for non-git dir\nstderr: {proc.stderr}"


def test_stdout_is_empty_on_success(fake_repo: Path) -> None:
    """Hook stdout must stay clean (empty or pure JSON) — never pollute parser."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()
    result_line = _make_result_line(commit=sha)
    message = f"Done.\n{result_line}"
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 0
    # stdout must be empty or valid JSON
    out = proc.stdout.strip()
    if out:
        json.loads(out)  # raises if not valid JSON


def test_stdout_clean_on_failure(fake_repo: Path) -> None:
    """On exit 2, stdout must remain empty/JSON; diagnostics only on stderr."""
    message = "No result line here at all."
    payload = _spine_payload(message, cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2
    out = proc.stdout.strip()
    if out:
        json.loads(out)
    # Diagnostics must go to stderr
    assert proc.stderr.strip() != ""


# ── Schema variant validation: implement schema ───────────────────────────────

def test_implement_missing_summary_blocked(fake_repo: Path) -> None:
    """Regression F1: implement RESULT missing summary= → exit 2."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()
    # omit summary=
    result_line = f"RESULT: commit={sha} tasks=3 tests=pass notes=-"
    payload = _spine_payload(f"Done.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2 (missing summary=)\nstderr: {proc.stderr}"
    assert "summary=" in proc.stderr


def test_implement_missing_tasks_blocked(fake_repo: Path) -> None:
    """Regression F1: implement RESULT missing tasks= → exit 2."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()
    # omit tasks=
    result_line = f"RESULT: commit={sha} tests=pass summary=/tmp/s.md notes=-"
    payload = _spine_payload(f"Done.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2 (missing tasks=)\nstderr: {proc.stderr}"
    assert "tasks=" in proc.stderr


def test_implement_tests_fail_blocked(fake_repo: Path) -> None:
    """Regression F1: implement RESULT with tests=fail is an invalid combination → exit 2."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()
    # implement schema requires tests=pass; tests=fail without fixed= is invalid
    result_line = f"RESULT: commit={sha} tasks=3 tests=fail summary=/tmp/s.md notes=oops"
    payload = _spine_payload(f"Done.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2 (implement with tests=fail)\nstderr: {proc.stderr}"


# ── Schema variant validation: fix schema ─────────────────────────────────────

def test_fix_missing_summary_blocked(fake_repo: Path) -> None:
    """Regression F1: fix RESULT missing summary= → exit 2."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()
    # omit summary=
    result_line = (
        f"RESULT: commit={sha} fixed=2 tests=pass"
        f" categories_scanned=validation regressions_added=- notes=-"
    )
    payload = _spine_payload(f"Fix done.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2 (fix missing summary=)\nstderr: {proc.stderr}"
    assert "summary=" in proc.stderr


def test_fix_missing_fixed_blocked(fake_repo: Path) -> None:
    """Regression F1: fix RESULT missing fixed= → exit 2."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()
    # omit fixed= — this could be misclassified as implement and still fail
    result_line = (
        f"RESULT: commit={sha} tests=pass summary=/tmp/s.md"
        f" categories_scanned=validation regressions_added=- notes=-"
    )
    # We inject categories_scanned= to force fix schema detection via fixed=
    # but without fixed= it falls to implement schema, which is also invalid (missing tasks=)
    payload = _spine_payload(f"Fix done.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2 (fix missing fixed=)\nstderr: {proc.stderr}"


def test_fix_missing_categories_scanned_blocked(fake_repo: Path) -> None:
    """Regression F1: fix RESULT missing categories_scanned= → exit 2."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()
    # omit categories_scanned=
    result_line = (
        f"RESULT: commit={sha} fixed=2 tests=pass"
        f" summary=/tmp/s.md regressions_added=- notes=-"
    )
    payload = _spine_payload(f"Fix done.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2 (fix missing categories_scanned=)\nstderr: {proc.stderr}"
    assert "categories_scanned=" in proc.stderr


def test_fix_missing_regressions_added_blocked(fake_repo: Path) -> None:
    """Regression F1: fix RESULT missing regressions_added= → exit 2."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()
    # omit regressions_added=
    result_line = (
        f"RESULT: commit={sha} fixed=2 tests=pass"
        f" summary=/tmp/s.md categories_scanned=validation notes=-"
    )
    payload = _spine_payload(f"Fix done.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2 (fix missing regressions_added=)\nstderr: {proc.stderr}"
    assert "regressions_added=" in proc.stderr


def test_fix_valid_full_schema_passes(fake_repo: Path) -> None:
    """Regression F1: fix RESULT with all required keys passes."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fake_repo), text=True
    ).strip()
    result_line = (
        f"RESULT: commit={sha} fixed=3 tests=pass summary=/tmp/s.md"
        f" categories_scanned=validation,concurrency regressions_added=test_a,test_b notes=-"
    )
    payload = _spine_payload(f"Fix done.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0 (valid fix schema)\nstderr: {proc.stderr}"


# ── Schema variant validation: failure schema ─────────────────────────────────

def test_failure_schema_missing_summary_blocked(fake_repo: Path) -> None:
    """Regression F1: failure RESULT missing summary= → exit 2.

    Previously `commit=- tests=fail notes=oops` would exit 0 despite missing
    tasks= and summary=. This test confirms the blocking is now enforced.
    """
    # The exact malformed case called out in the finding
    result_line = "RESULT: commit=- tests=fail notes=oops"
    payload = _spine_payload(f"Failed.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, (
        f"Expected exit 2 (failure schema missing tasks= and summary=)\nstderr: {proc.stderr}"
    )
    assert "tasks=" in proc.stderr or "summary=" in proc.stderr


def test_failure_schema_missing_tasks_blocked(fake_repo: Path) -> None:
    """Regression F1: failure RESULT missing tasks= → exit 2."""
    result_line = "RESULT: commit=- tests=fail summary=/tmp/s.md notes=build failed"
    payload = _spine_payload(f"Failed.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 2, f"Expected exit 2 (failure missing tasks=)\nstderr: {proc.stderr}"
    assert "tasks=" in proc.stderr


def test_failure_schema_full_passes(fake_repo: Path) -> None:
    """Regression F1: properly formed failure RESULT passes."""
    result_line = "RESULT: commit=- tasks=2 tests=fail summary=/tmp/s.md notes=build error"
    payload = _spine_payload(f"Failed.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0 (valid failure schema)\nstderr: {proc.stderr}"


def test_failure_schema_summary_dash_passes(fake_repo: Path) -> None:
    """Regression F1: failure RESULT with summary=- is valid (summary may not exist)."""
    result_line = "RESULT: commit=- tasks=0 tests=fail summary=- notes=crashed before commit"
    payload = _spine_payload(f"Failed.\n{result_line}", cwd=str(fake_repo))

    proc = _run_hook(payload)
    assert proc.returncode == 0, f"Expected exit 0 (failure with summary=-)\nstderr: {proc.stderr}"
