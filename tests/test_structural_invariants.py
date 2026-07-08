"""结构不变量测试（change: structural-invariant-checks）。

确定性检查，跑在既有 `uv run pytest` 内，MUST NOT 进 `npc verify`（守 CLAUDE.md 边界）：

- R1 telemetry emit 两层字段契约：EMIT_FIELD_CONTRACT（emit 输出） +
  PHASE_EXIT_EXTRA_CONTRACT（_do_phase_exit 调用点 handoff），拦截"字段在调用点被丢"
  这一最高频复发缺陷（如 tests_verified 算出却只透传 engine 给 telemetry）。
- R2 record RESULT 必需键：RESULT_REQUIRED_KEYS 单一事实源 + 解析器缺键即失败并指明缺失键。
- R3 hook fixture 静态回归：hooks.json 的 SubagentStop matcher 与真实触发路径。

所有检查均为确定性（monkeypatch 捕获真实输出 / 静态常量比对 / AST），无 LLM、无运行时随机性。
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from npc import config as _config
from npc import pipeline as _pipeline
from npc import state as _state
from npc import telemetry as _telemetry
from npc import verify as _verify

REPO_ROOT = Path(__file__).resolve().parent.parent
TELEMETRY_SRC = REPO_ROOT / "src" / "npc" / "telemetry.py"
PIPELINE_SRC = REPO_ROOT / "src" / "npc" / "pipeline.py"
HOOKS_JSON_PATH = REPO_ROOT / "plugins" / "agent-spine" / "hooks" / "hooks.json"
HOOK_SCRIPT_PATH = REPO_ROOT / "plugins" / "agent-spine" / "hooks" / "verify-subagent-result.sh"


def _bootstrap_run(make_args, capsys, *change_ids: str) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids))))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def _make_commit(fake_repo: Path, fname: str, msg: str) -> str:
    (fake_repo / fname).write_text("x")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=fake_repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=fake_repo, capture_output=True, text=True
    ).stdout.strip()


# ============================================================
# R1a — EMIT_FIELD_CONTRACT：emit 输出契约
# ============================================================


def test_emit_field_contract_registers_known_kinds():
    for kind in ("phase.exit", "review.round", "archive.done", "agent.spawn"):
        assert kind in _telemetry.EMIT_FIELD_CONTRACT
        assert _telemetry.EMIT_FIELD_CONTRACT[kind], f"{kind} 契约字段集合不应为空"


def _emit_kind_literals_in_source() -> dict[str, str]:
    """AST 扫描 telemetry.py：{kind 字面量: 所在 emit_* 函数名}。

    只识别 `"kind": "<literal>"` 形式的 dict 字面量项，足以覆盖当前所有 emit_* 实现方式。
    """
    tree = ast.parse(TELEMETRY_SRC.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("emit_"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    for k, v in zip(sub.keys, sub.values):
                        if (
                            isinstance(k, ast.Constant)
                            and k.value == "kind"
                            and isinstance(v, ast.Constant)
                            and isinstance(v.value, str)
                        ):
                            found[v.value] = node.name
    return found


def test_every_emit_kind_is_registered_in_contract():
    """Scenario：新增 emit_* kind 未登记进 EMIT_FIELD_CONTRACT → fail。"""
    found = _emit_kind_literals_in_source()
    assert found, "AST 未找到任何 emit_* kind 字面量，扫描逻辑可能已失效"
    missing = {kind: fn for kind, fn in found.items() if kind not in _telemetry.EMIT_FIELD_CONTRACT}
    assert not missing, f"以下 emit kind 未登记进 EMIT_FIELD_CONTRACT：{missing}"


def test_emit_phase_exit_produces_all_contract_fields(isolate_telemetry, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )
    _telemetry.emit_phase_exit(
        proj_key="demo", run_ts="2026-01-01-0000", change_seq=1, change_id="add-foo",
        phase="implement", status="done", duration_ms=10,
    )
    assert len(captured) == 1
    missing = _telemetry.EMIT_FIELD_CONTRACT["phase.exit"] - set(captured[0].keys())
    assert not missing, f"phase.exit 事件缺少契约字段：{missing}"


def test_emit_review_round_produces_all_contract_fields(isolate_telemetry, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )
    _telemetry.emit_review_round(
        proj_key="demo", run_ts="2026-01-01-0000", change_seq=1, change_id="add-foo",
        round_n=0, base="/tmp/base", ok=True, engine="codex", verdict="approve",
        blocking_count=0, blocking_categories=[], duration_ms=10, retry_count=0,
        outcome_reason=None, state_json=None, run_events=None,
    )
    assert len(captured) == 1
    missing = _telemetry.EMIT_FIELD_CONTRACT["review.round"] - set(captured[0].keys())
    assert not missing, f"review.round 事件缺少契约字段：{missing}"


def test_emit_archive_done_produces_all_contract_fields(isolate_telemetry, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )
    _telemetry.emit_archive_done(
        proj_key="demo", run_ts="2026-01-01-0000", change_seq=1, change_id="add-foo",
        archive_commit="abc123", total_rounds=2, duration_ms=10,
        state_json=None, run_events=None, base=None,
    )
    assert len(captured) == 1
    missing = _telemetry.EMIT_FIELD_CONTRACT["archive.done"] - set(captured[0].keys())
    assert not missing, f"archive.done 事件缺少契约字段：{missing}"


def test_emit_agent_spawn_produces_all_contract_fields(isolate_telemetry, monkeypatch, tmp_path: Path):
    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )
    prompt_file = tmp_path / "implement.prompt.md"
    prompt_file.write_text("hello")
    _telemetry.emit_agent_spawn(
        proj_key="demo", run_ts="2026-01-01-0000", change_seq=1, change_id="add-foo",
        phase="implement", round_n=None, prompt_file=prompt_file, state_json=None,
    )
    assert len(captured) == 1
    missing = _telemetry.EMIT_FIELD_CONTRACT["agent.spawn"] - set(captured[0].keys())
    assert not missing, f"agent.spawn 事件缺少契约字段：{missing}"


# ============================================================
# R1b — PHASE_EXIT_EXTRA_CONTRACT：调用点 handoff 契约（核心 scenario）
# ============================================================


def test_record_implement_forwards_tests_verified_to_telemetry_on_success(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """核心 scenario：record_implement 算出 tests_verified，_do_phase_exit 必须把它透传给 telemetry
    emit，而不是像修复前那样只透传 engine（导致 tests_verified 在调用点被静默丢弃）。"""
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    commit = _make_commit(fake_repo, "wire.txt", "wire: test")
    summary = p.run_dir / "001-add-foo" / "implement.summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("# s\n")

    monkeypatch.setattr(
        _pipeline, "load_config",
        lambda repo_root, **kw: _config.Config(verify=_config.VerifyConfig(rerun_tests=True)),
    )
    monkeypatch.setattr(
        _verify, "run_tests_result",
        lambda repo_root, cfg, runner=None: {
            "no_command": False, "passed": True, "cmd": "pytest", "tail": "1 passed",
        },
    )

    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )

    result_line = f"RESULT: commit={commit} tasks=2 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)
    assert result["ok"] is True
    assert result["tests_verified"] is True

    phase_exit_events = [ev for ev in captured if ev.get("kind") == "phase.exit"]
    assert len(phase_exit_events) == 1
    ev = phase_exit_events[0]
    assert "tests_verified" in ev, (
        "PHASE_EXIT_EXTRA_CONTRACT['implement'] 要求 tests_verified 被 _do_phase_exit 透传给 "
        f"emit，但捕获事件缺失该字段：{ev}"
    )
    assert ev["tests_verified"] is True


def test_record_implement_forwards_tests_verified_false_on_rerun_failure(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    """rerun-tests-failed 失败路径同样必须透传 tests_verified=False（历史缺陷两条路径都丢）。"""
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    commit = _make_commit(fake_repo, "wire2.txt", "wire: test2")
    summary = p.run_dir / "001-add-foo" / "implement.summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("# s\n")

    monkeypatch.setattr(
        _pipeline, "load_config",
        lambda repo_root, **kw: _config.Config(verify=_config.VerifyConfig(rerun_tests=True)),
    )
    monkeypatch.setattr(
        _verify, "run_tests_result",
        lambda repo_root, cfg, runner=None: {
            "no_command": False, "passed": False, "cmd": "pytest", "tail": "1 failed",
        },
    )

    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )

    result_line = f"RESULT: commit={commit} tasks=2 tests=pass summary={summary} notes=ok"
    result = _pipeline.record_implement(p_with_repo, 1, result_line)
    assert result["ok"] is False
    assert result["tests_verified"] is False

    phase_exit_events = [ev for ev in captured if ev.get("kind") == "phase.exit"]
    assert len(phase_exit_events) == 1
    assert phase_exit_events[0].get("tests_verified") is False


def test_record_fix_forwards_tests_verified_to_telemetry(
    env_setup, make_args, capsys, fake_repo: Path, monkeypatch
):
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    commit = _make_commit(fake_repo, "fixwire.txt", "fix: wire")
    base = p.run_dir / "001-add-foo"
    base.mkdir(parents=True, exist_ok=True)
    summary = base / "round-1.fix.summary.md"
    summary.write_text("# fix\n")

    monkeypatch.setattr(
        _pipeline, "load_config",
        lambda repo_root, **kw: _config.Config(verify=_config.VerifyConfig(rerun_tests=True)),
    )
    monkeypatch.setattr(
        _verify, "run_tests_result",
        lambda repo_root, cfg, runner=None: {
            "no_command": False, "passed": True, "cmd": "pytest", "tail": "ok",
        },
    )

    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )

    result_line = (
        f"RESULT: commit={commit} fixed=1 tests=pass summary={summary} "
        f"categories_scanned=validation regressions_added=- notes=-"
    )
    result = _pipeline.record_fix(p_with_repo, 1, 1, result_line)
    assert result["ok"] is True
    assert result["tests_verified"] is True

    phase_exit_events = [ev for ev in captured if ev.get("kind") == "phase.exit"]
    assert len(phase_exit_events) == 1
    assert phase_exit_events[0].get("tests_verified") is True


def _do_phase_exit_extra_keys(func_name: str) -> set[str]:
    """AST 扫描 pipeline.py：收集某函数体内所有 `_do_phase_exit(..., extra={...})` 字面量的 key。"""
    tree = ast.parse(PIPELINE_SRC.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "_do_phase_exit":
                    for kw in sub.keywords:
                        if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                            for k in kw.value.keys:
                                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                    keys.add(k.value)
    return keys


@pytest.mark.parametrize(
    "func_name,phase_family",
    [("record_implement", "implement"), ("record_fix", "fix")],
)
def test_extra_fields_are_all_registered_in_handoff_or_local_only(func_name, phase_family):
    """Scenario：调用点新增一个已算出字段却未登记进两层契约 → fail（防止绕过）。"""
    keys = _do_phase_exit_extra_keys(func_name)
    assert keys, f"AST 未在 {func_name} 中找到任何 _do_phase_exit(extra=...) 字面量"
    registered = (
        _pipeline.PHASE_EXIT_EXTRA_CONTRACT.get(phase_family, frozenset())
        | _pipeline.PHASE_EXIT_EXTRA_LOCAL_ONLY.get(phase_family, frozenset())
    )
    unregistered = keys - registered
    assert not unregistered, (
        f"{func_name} 新增了未登记的 extra 字段：{unregistered}；须显式加入 "
        "PHASE_EXIT_EXTRA_CONTRACT（需透传 telemetry）或 PHASE_EXIT_EXTRA_LOCAL_ONLY"
        "（明确仅落 state，不透传）"
    )


# ============================================================
# R2 — RESULT_REQUIRED_KEYS：单一事实源 + 解析器强制校验
# ============================================================


@pytest.mark.parametrize("missing_key", sorted(_pipeline.RESULT_REQUIRED_KEYS["implement"]))
def test_record_implement_rejects_result_line_missing_required_key(
    env_setup, make_args, capsys, fake_repo: Path, missing_key
):
    """Scenario：implement RESULT 行缺 RESULT_REQUIRED_KEYS['implement'] 中某一键 → ok:false 且指明缺失键。"""
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    commit = _make_commit(fake_repo, "reqkey.txt", "req: key")
    summary = p.run_dir / "001-add-foo" / "implement.summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("# s\n")

    fields = {"commit": commit, "tasks": "3", "tests": "pass", "summary": str(summary)}
    del fields[missing_key]
    result_line = "RESULT: " + " ".join(f"{k}={v}" for k, v in fields.items()) + " notes=ok"

    result = _pipeline.record_implement(p_with_repo, 1, result_line, require_summary=False)
    assert result["ok"] is False
    assert result["error"] == "result-missing-keys"
    assert missing_key in result["missing_keys"]

    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "failed"


@pytest.mark.parametrize("missing_key", sorted(_pipeline.RESULT_REQUIRED_KEYS["fix"]))
def test_record_fix_rejects_result_line_missing_required_key(
    env_setup, make_args, capsys, fake_repo: Path, missing_key
):
    """Scenario：fix RESULT 行缺 RESULT_REQUIRED_KEYS['fix'] 中某一键 → ok:false 且指明缺失键。

    implement 与 fix 各自的必需键集合取自同一事实源常量的不同 phase 条目。
    """
    _bootstrap_run(make_args, capsys, "add-foo")
    p = env_setup
    p_with_repo = type(p)(**{**p.__dict__, "repo_root": fake_repo})
    commit = _make_commit(fake_repo, "reqkeyfix.txt", "req: key fix")
    base = p.run_dir / "001-add-foo"
    base.mkdir(parents=True, exist_ok=True)
    summary = base / "round-1.fix.summary.md"
    summary.write_text("# fix\n")

    fields = {
        "commit": commit, "fixed": "1", "tests": "pass", "summary": str(summary),
        "categories_scanned": "validation", "regressions_added": "-",
    }
    del fields[missing_key]
    result_line = "RESULT: " + " ".join(f"{k}={v}" for k, v in fields.items()) + " notes=ok"

    result = _pipeline.record_fix(p_with_repo, 1, 1, result_line, require_summary=False)
    assert result["ok"] is False
    assert result["error"] == "result-missing-keys"
    assert missing_key in result["missing_keys"]

    s = json.loads(p.state_json.read_text())
    assert s["progress"][0]["status"] == "needs-user-decision"


def test_result_required_keys_implement_and_fix_are_distinct_entries():
    """两个 phase 各自的必需键集合来自同一常量的不同条目，且互不影响。"""
    assert "implement" in _pipeline.RESULT_REQUIRED_KEYS
    assert "fix" in _pipeline.RESULT_REQUIRED_KEYS
    assert _pipeline.RESULT_REQUIRED_KEYS["implement"] != _pipeline.RESULT_REQUIRED_KEYS["fix"]


# ============================================================
# R3 — hook fixture 静态回归（收窄，非通用 matcher 语义引擎）
# ============================================================


def test_hooks_json_subagent_stop_matcher_is_spine_coder():
    """Scenario：hooks.json 的 SubagentStop matcher 被改成非 spine-coder 值 → fail。"""
    data = json.loads(HOOKS_JSON_PATH.read_text(encoding="utf-8"))
    matchers = [entry["matcher"] for entry in data["hooks"]["SubagentStop"]]
    assert matchers == ["spine-coder"], (
        f"hooks.json 的 SubagentStop matcher 期望恰好 ['spine-coder']，实际：{matchers}"
    )


def test_hooks_json_subagent_stop_binds_verify_script():
    data = json.loads(HOOKS_JSON_PATH.read_text(encoding="utf-8"))
    entry = data["hooks"]["SubagentStop"][0]
    command = entry["hooks"][0]["command"]
    assert "verify-subagent-result.sh" in command, (
        f"SubagentStop matcher 绑定的 command 未指向 verify-subagent-result.sh：{command}"
    )


def test_realistic_subagent_stop_payload_triggers_verification_path():
    """Scenario：realistic SubagentStop payload 证明 hook 触发校验路径（非 matcher 绑错字段导致
    永不匹配 / 直接放行）。agent_type=spine-coder 且缺 RESULT 行 → 校验路径必须真正跑起来并拒绝。"""
    payload = {
        "agent_type": "spine-coder",
        "last_assistant_message": "done, but forgot the RESULT line",
        "cwd": "/tmp",
        "session_id": "structural-invariant-test",
    }
    proc = subprocess.run(
        ["bash", str(HOOK_SCRIPT_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, (
        f"realistic spine-coder payload 未触发校验路径（exit={proc.returncode}），"
        "matcher 可能绑定了错误字段，导致 hook 永不生效"
    )
    assert "RESULT" in proc.stderr


def test_realistic_subagent_stop_payload_non_spine_coder_releases():
    """对照组：非 spine-coder agent_type 必须被放行（证明 matcher 语义确实是按 agent_type 区分，
    而不是对所有 agent 都无差别拦截）。"""
    payload = {
        "agent_type": "some-other-agent",
        "last_assistant_message": "no RESULT line at all",
        "cwd": "/tmp",
        "session_id": "structural-invariant-test",
    }
    proc = subprocess.run(
        ["bash", str(HOOK_SCRIPT_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0

