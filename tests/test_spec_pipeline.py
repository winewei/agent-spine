"""spec_pipeline 模块测试（change: spine-spec-writer）。

覆盖范围（对应 tasks.md 2.x-10.x）：

1. 质量门顺序：openspec validate → [spec_review] gate_cmd → LLM 引擎（2.x）。
2. 评审结果解析与轮次化落盘（3.x）。
3. 固定轮次上限的 fix 循环，拒绝 stale 检测（4.x）。
4. spec_write/spec_fix 的 RESULT 契约（5.x，pipeline.RESULT_REQUIRED_KEYS 已扩）。
5. 不变量 1 的渲染层防护：write 轮不泄漏 rubric；fix 轮只读上一轮已签发 findings（6.x）。
6. spec_review.round telemetry（7.x）。
7. 越界修改 / 意外 commit 的确定性拦截（8.2.x）。
8. v1 恒 in-session + 路由真相源唯一（8.3.x）。
9. 非目标守护 + 端到端（9.x / 10.x）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from npc import config as _config
from npc import pipeline as _pipeline
from npc import schema as _schema
from npc import spec_pipeline as _sp
from npc import telemetry as _telemetry


REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PIPELINE_SRC = REPO_ROOT / "src" / "npc" / "spec_pipeline.py"
TELEMETRY_SCHEMA_V1_PATH = REPO_ROOT / "src" / "npc" / "telemetry_schema_v1.json"


# ============================================================
# Helpers
# ============================================================


def _with_repo(p, repo_root: Path):
    return type(p)(**{**p.__dict__, "repo_root": repo_root})


_PI_SECTION_BODIES = {
    "## Analogs": "- `src/npc/spec_pipeline.py::spec_write_run` 处理 routing→marker→render→return 的形态\n",
    "## Assumptions": "- 既有 routing 真相源单一，不新增第二套白名单\n",
    "## Open Questions": "",
}


def _write_pattern_interrogation(
    repo_root: Path, change_id: str, *, sections=("## Analogs", "## Assumptions", "## Open Questions"),
    open_questions_bullets: int = 0,
) -> Path:
    """写一份 pattern-interrogation.md 到 change 目录。

    ``sections``：要包含的必需 H2 标题子集（默认三个齐全）；省略某个即模拟结构缺陷。
    ``open_questions_bullets``：``## Open Questions`` 段落下的顶层 bullet 条数。
    """
    change_dir = repo_root / "openspec" / "changes" / change_id
    change_dir.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    for h in sections:
        body = _PI_SECTION_BODIES.get(h, "")
        if h == "## Open Questions" and open_questions_bullets:
            body = "".join(f"- open question {i}\n" for i in range(open_questions_bullets))
        parts.append(f"{h}\n\n{body}")
    path = change_dir / "pattern-interrogation.md"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _make_change_dir(repo_root: Path, change_id: str, *, valid: bool = True, interrogated: bool = True) -> Path:
    change_dir = repo_root / "openspec" / "changes" / change_id
    change_dir.mkdir(parents=True, exist_ok=True)
    if interrogated:
        _write_pattern_interrogation(repo_root, change_id)
    (change_dir / "proposal.md").write_text(
        "## Why\n\nx\n\n## What Changes\n\n- y\n\n## Non-Goals\n\n- z\n", encoding="utf-8"
    )
    if valid:
        (change_dir / "tasks.md").write_text("## 1. x\n\n- [ ] 1.1 do it\n", encoding="utf-8")
        specs_dir = change_dir / "specs" / "demo"
        specs_dir.mkdir(parents=True, exist_ok=True)
        (specs_dir / "spec.md").write_text(
            "## ADDED Requirements\n\n"
            "### Requirement: demo\nThe system MUST do X.\n\n"
            "#### Scenario: ok\n- WHEN x\n- THEN y\n",
            encoding="utf-8",
        )
    else:
        (change_dir / "tasks.md").write_text("## 1. x\n\n- [ ] 1.1 do it\n", encoding="utf-8")
        specs_dir = change_dir / "specs" / "demo"
        specs_dir.mkdir(parents=True, exist_ok=True)
        (specs_dir / "spec.md").write_text(
            "## ADDED Requirements\n\n### Requirement: demo\nno modal verb here.\n\n"
            "#### Scenario: ok\n- WHEN x\n- THEN y\n",
            encoding="utf-8",
        )
    return change_dir



def _write_gate_cmd_config(fake_repo: Path, argv: list[str]) -> Path:
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir(exist_ok=True)
    argv_toml = ", ".join(f'"{a}"' for a in argv)
    (npc_dir / "config.toml").write_text(
        f"[spec_review]\ngate_cmd = [{argv_toml}]\n", encoding="utf-8"
    )
    return npc_dir / "config.toml"

def _fake_validate_runner(returncode: int = 0, stderr: str = ""):
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    return runner


def _fake_gate_runner(stdout: str, returncode: int = 0):
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    runner.calls = calls
    return runner


def _stub_engine_writes(review_payload: dict, rc: int = 0):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        if rc == 0:
            kwargs["review_out"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["review_out"].write_text(json.dumps(review_payload), encoding="utf-8")
            kwargs["events_out"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["events_out"].write_text("fake\n", encoding="utf-8")
        return rc

    fake.calls = calls
    return fake


# ============================================================
# 1. SPEC_REVIEW_SCHEMA / parse_spec_review（tasks 1.x, 3.x）
# ============================================================


def test_parse_spec_review_blocking_counts_severity_only():
    review = {
        "verdict": "changes-requested",
        "findings": [
            {"id": "F1", "severity": "critical", "category": "ambiguity", "title": "t",
             "file": "-", "line_range": "-", "detail": "d", "recommendation": "r"},
            {"id": "F2", "severity": "high", "category": "untestable", "title": "t",
             "file": "-", "line_range": "-", "detail": "d", "recommendation": "r"},
            {"id": "F3", "severity": "medium", "category": "scope-creep", "title": "t",
             "file": "-", "line_range": "-", "detail": "d", "recommendation": "r"},
        ],
    }
    metrics = _sp.parse_spec_review(review)
    assert metrics["blocking"] == 2
    assert metrics["advisory"] == 1


def test_parse_spec_review_blocking_categories_dedup():
    review = {
        "verdict": "changes-requested",
        "findings": [
            {"id": "F1", "severity": "high", "category": "ambiguity", "title": "t",
             "file": "-", "line_range": "-", "detail": "d", "recommendation": "r"},
            {"id": "F2", "severity": "critical", "category": "ambiguity", "title": "t",
             "file": "-", "line_range": "-", "detail": "d", "recommendation": "r"},
            {"id": "F3", "severity": "high", "category": "untestable", "title": "t",
             "file": "-", "line_range": "-", "detail": "d", "recommendation": "r"},
        ],
    }
    metrics = _sp.parse_spec_review(review)
    assert set(metrics["blocking_categories"]) == {"ambiguity", "untestable"}


# ============================================================
# 2. 质量门顺序（tasks 2.1–2.7）
# ============================================================


def test_spec_review_run_openspec_validate_fails_no_llm_call(env_setup, fake_repo, monkeypatch):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)

    engine_calls: list[dict] = []
    monkeypatch.setattr(_sp, "_spec_engine_exec", lambda **kw: (engine_calls.append(kw), 0)[1])
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _sp.spec_review_run(
        p, "add-foo", 0,
        validate_runner=_fake_validate_runner(returncode=1, stderr="Requirement missing SHALL"),
        gate_runner=_fake_gate_runner(json.dumps({"ok": True, "rule_hits": {}})),
    )
    assert result["ok"] is False
    assert result["gate_failed"] == "openspec_validate"
    assert engine_calls == []
    assert not (_sp._spec_base(p, "add-foo") / "round-0.spec-review.json").exists()


def test_spec_review_run_gate_cmd_ok_false_no_llm_call(env_setup, fake_repo, monkeypatch):
    _make_change_dir(fake_repo, "add-foo")
    config_path = _write_gate_cmd_config(fake_repo, ["stub-gate"])
    p = _with_repo(env_setup, fake_repo)

    engine_calls: list[dict] = []
    monkeypatch.setattr(_sp, "_spec_engine_exec", lambda **kw: (engine_calls.append(kw), 0)[1])
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    stub_gate = _fake_gate_runner(json.dumps({"ok": False, "rule_hits": {}}))
    result = _sp.spec_review_run(
        p, "add-foo", 0,
        config_path=config_path,
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=stub_gate,
    )
    assert result["ok"] is False
    assert result["gate_failed"] == "gate_cmd"
    assert engine_calls == []


def test_spec_review_run_gate_cmd_argv_and_shell_false(env_setup, fake_repo, monkeypatch):
    _make_change_dir(fake_repo, "add-foo")
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text(
        '[spec_review]\ngate_cmd = ["uv", "run", "scripts/check_spec.py"]\n', encoding="utf-8"
    )
    p = _with_repo(env_setup, fake_repo)

    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")
    monkeypatch.setattr(
        _sp, "_spec_engine_exec",
        lambda **kw: (
            kw["review_out"].write_text(json.dumps({"verdict": "approve", "findings": []})),
            0,
        )[1],
    )

    captured_argv: list[str] = []

    def spy_runner(cmd, **kwargs):
        captured_argv.extend(cmd)
        assert kwargs.get("shell") is False
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "rule_hits": {}}), stderr="")

    _sp.spec_review_run(
        p, "my-change", 0,
        config_path=npc_dir / "config.toml",
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=spy_runner,
    )
    assert captured_argv == ["uv", "run", "scripts/check_spec.py", "--change", "my-change"]


def test_spec_review_run_gate_cmd_unconfigured_skips_and_calls_llm(env_setup, fake_repo, monkeypatch):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    engine_calls: list[dict] = []

    def fake_engine(**kw):
        engine_calls.append(kw)
        kw["review_out"].parent.mkdir(parents=True, exist_ok=True)
        kw["review_out"].write_text(json.dumps({"verdict": "approve", "findings": []}))
        kw["events_out"].parent.mkdir(parents=True, exist_ok=True)
        kw["events_out"].write_text("x")
        return 0

    monkeypatch.setattr(_sp, "_spec_engine_exec", fake_engine)

    result = _sp.spec_review_run(
        p, "add-foo", 0,
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=subprocess.run,  # 不会被调用（gate_cmd 未配置）
    )
    assert result["ok"] is True
    assert result["gate_skipped"] is True
    assert result["gate_failed"] is None
    assert len(engine_calls) == 1


def test_spec_review_run_gate_cmd_invalid_json_is_gate_failure(env_setup, fake_repo, monkeypatch):
    _make_change_dir(fake_repo, "add-foo")
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text('[spec_review]\ngate_cmd = ["true"]\n', encoding="utf-8")
    p = _with_repo(env_setup, fake_repo)

    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")
    engine_calls: list[dict] = []
    monkeypatch.setattr(_sp, "_spec_engine_exec", lambda **kw: (engine_calls.append(kw), 0)[1])

    def bad_json_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="not-json", stderr="")

    result = _sp.spec_review_run(
        p, "add-foo", 0,
        config_path=npc_dir / "config.toml",
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=bad_json_runner,
    )
    assert result["ok"] is False
    assert result["gate_failed"] == "gate_cmd"
    assert "gate_output_invalid" in result["gate_error"]
    assert engine_calls == []


def test_spec_review_run_gate_cmd_warning_only_continues_to_llm(env_setup, fake_repo, monkeypatch):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    payload = {"verdict": "approve", "findings": []}
    fake_engine = _stub_engine_writes(payload)
    monkeypatch.setattr(_sp, "_spec_engine_exec", fake_engine)

    gate_runner = _fake_gate_runner(json.dumps({"ok": True, "rule_hits": {"vague_adverb": 0}}))
    result = _sp.spec_review_run(
        p, "add-foo", 0,
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=gate_runner,
    )
    assert result["ok"] is True
    assert len(fake_engine.calls) == 1
    review_path = _sp._spec_base(p, "add-foo") / "round-0.spec-review.json"
    assert review_path.is_file()


def test_spec_review_run_source_has_no_rule_name_literals():
    """边界测试：spec_pipeline.py 不含任何规则名字符串或延迟措辞/含糊副词词表常量。"""
    src = SPEC_PIPELINE_SRC.read_text(encoding="utf-8")
    for banned in (
        "deferred_decision_outside_open_questions",
        "vague_adverb",
        "scenario_missing_when_then",
        "proposal_missing_non_goals",
    ):
        assert banned not in src


# ============================================================
# 3. 评审结果轮次化落盘（tasks 3.3）
# ============================================================


def test_spec_review_run_writes_round_n_without_overwriting_round_0(env_setup, fake_repo, monkeypatch):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    base = _sp._spec_base(p, "add-foo")
    base.mkdir(parents=True, exist_ok=True)
    (base / "round-0.spec-review.json").write_text(json.dumps({"verdict": "approve", "findings": []}))

    monkeypatch.setattr(_sp, "_spec_engine_exec", _stub_engine_writes({"verdict": "approve", "findings": []}))

    result = _sp.spec_review_run(
        p, "add-foo", 1,
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=subprocess.run,
    )
    assert result["ok"] is True
    assert result["pointer"]["spec_review_json"].endswith("round-1.spec-review.json")
    assert (base / "round-0.spec-review.json").is_file()
    assert (base / "round-1.spec-review.json").is_file()


def test_spec_review_run_exposes_configured_max_rounds_default(env_setup, fake_repo, monkeypatch):
    """review round 3 fix (F3)：`/spine-spec` 的 fix 循环上限判定必须能从
    `npc spec review run` 的 stdout 直接读到 `[spec_review].max_rounds`——
    `npc verify routing` 从不 emit 该字段，不能作为真相源。默认配置（无
    `.npc/config.toml`）下应回落到 config.py 的默认值 3。
    """
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")
    monkeypatch.setattr(_sp, "_spec_engine_exec", _stub_engine_writes({"verdict": "approve", "findings": []}))

    result = _sp.spec_review_run(
        p, "add-foo", 0,
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=subprocess.run,
    )
    assert result["ok"] is True
    assert result["max_rounds"] == 3


def test_spec_review_run_exposes_configured_max_rounds_custom(env_setup, fake_repo, monkeypatch):
    """同上，但 `.npc/config.toml` 显式配置了非默认 `max_rounds`——stdout 必须
    原样透传该值，不得被硬编码默认值覆盖（回归 F3 具体给出的 `max_rounds = 0`
    「只审不修」场景）。
    """
    _make_change_dir(fake_repo, "add-foo")
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text("[spec_review]\nmax_rounds = 0\n", encoding="utf-8")
    p = _with_repo(env_setup, fake_repo)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")
    monkeypatch.setattr(_sp, "_spec_engine_exec", _stub_engine_writes({"verdict": "approve", "findings": []}))

    result = _sp.spec_review_run(
        p, "add-foo", 0,
        config_path=npc_dir / "config.toml",
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=subprocess.run,
    )
    assert result["ok"] is True
    assert result["max_rounds"] == 0


def test_spine_spec_command_reads_max_rounds_from_review_stdout_not_verify_routing():
    """command-contract 回归：`plugins/agent-spine/commands/spine-spec.md` 的
    fix 循环上限判定必须从 `$REVIEW`（`npc spec review run` 的 stdout）取
    `max_rounds`，禁止再出现 `npc verify routing` 派生 `MAX_ROUNDS` 的写法——
    后者只 emit 路由不变量字段，从不含 `[spec_review].max_rounds`（F3 根因）。
    """
    spine_spec_md = (
        Path(__file__).resolve().parent.parent
        / "plugins" / "agent-spine" / "commands" / "spine-spec.md"
    )
    src = spine_spec_md.read_text(encoding="utf-8")
    assert "MAX_ROUNDS=$(printf '%s' \"$REVIEW\" | jq -r '.max_rounds" in src
    # 允许散文/注释里提及 `npc verify routing`（解释为什么不用它）；
    # 禁止的是真正把它当 MAX_ROUNDS 数据源的调用写法。
    assert "MAX_ROUNDS=$(npc verify routing" not in src


def test_spine_spec_command_checks_ok_before_treating_review_as_clean():
    """command-contract 回归（round 4 F1）：`spine-spec.md` 的 review+fix 循环
    必须先判定 `.ok`，非门失败的 `ok:false`（``dependency_missing`` /
    ``<engine>-exec-failed`` / ``invalid_spec_review_schema`` 等，此时
    `gate_failed` 为空、`blocking` 键缺失）绝不能被 `jq '.blocking // 0'`
    的 `// 0` 缺省值悄悄判成 `BLOCKING=0` 进入 clean 分支。
    """
    spine_spec_md = (
        Path(__file__).resolve().parent.parent
        / "plugins" / "agent-spine" / "commands" / "spine-spec.md"
    )
    src = spine_spec_md.read_text(encoding="utf-8")

    assert "OK=$(printf '%s' \"$REVIEW\" | jq -r '.ok')" in src
    assert 'if [ "$OK" != "true" ]' in src

    # 顺序断言：`.ok` 判定必须先于 `.blocking` 求值（同一轮 while 循环体内）。
    while_body_start = src.index("while true; do")
    ok_check_pos = src.index('if [ "$OK" != "true" ]', while_body_start)
    blocking_read_pos = src.index(
        "BLOCKING=$(printf '%s' \"$REVIEW\" | jq -r '.blocking", while_body_start
    )
    assert ok_check_pos < blocking_read_pos, (
        "`.ok` 判定必须先于 `.blocking` 求值，否则非门失败的 ok:false 会被 "
        "`// 0` 缺省值悄悄当成 clean（F1 根因复发）"
    )


def test_spine_spec_command_inits_worktree_before_scaffolding_change():
    """command-contract 回归（round 4 F2）：`spine-spec.md` 必须先 `npc init`
    拿到 `worktree_root` 并 `cd` 进去，之后才能判断 change-id 是否存在 /
    跑 `npc plan new-change` 建脚手架——否则自由目标分支会在原 checkout 建
    脚手架，而后续 `npc spec ...` 与 `spine-spec-writer` 实际执行的 worktree
    里看不到这些文件。
    """
    spine_spec_md = (
        Path(__file__).resolve().parent.parent
        / "plugins" / "agent-spine" / "commands" / "spine-spec.md"
    )
    src = spine_spec_md.read_text(encoding="utf-8")

    init_pos = src.index("INIT=$(npc init)")
    cd_worktree_pos = src.index('cd "$WORKTREE_ROOT"')
    new_change_pos = src.index("npc plan new-change")

    assert init_pos < cd_worktree_pos < new_change_pos, (
        "`npc init` → cd 进 worktree_root → `npc plan new-change` 建脚手架 "
        "三者必须严格按此顺序出现，否则脚手架会建在错误的 repo root（F2 根因复发）"
    )


# ============================================================
# 4. 固定轮次上限的 fix 循环（tasks 4.1–4.4）
# ============================================================


def test_fix_loop_terminates_clean_when_no_blocking():
    calls = {"review": 0, "fix": 0}

    def review_fn(round_n):
        calls["review"] += 1
        return {"blocking": 0}

    def fix_fn(round_n):
        calls["fix"] += 1

    result = _sp.run_spec_fix_loop(review_fn, fix_fn, max_rounds=3)
    assert result["status"] == "clean"
    assert calls["fix"] == 0
    assert calls["review"] == 1


def test_fix_loop_needs_user_decision_at_max_rounds():
    calls = {"review": 0, "fix": 0}

    def review_fn(round_n):
        calls["review"] += 1
        return {"blocking": 2}

    def fix_fn(round_n):
        calls["fix"] += 1

    result = _sp.run_spec_fix_loop(review_fn, fix_fn, max_rounds=3)
    assert calls["fix"] == 3
    assert calls["review"] == 4
    assert result["status"] == "needs-user-decision"


def test_fix_loop_max_rounds_zero_means_review_only():
    calls = {"fix": 0}

    def review_fn(round_n):
        return {"blocking": 5}

    def fix_fn(round_n):
        calls["fix"] += 1

    result = _sp.run_spec_fix_loop(review_fn, fix_fn, max_rounds=0)
    assert calls["fix"] == 0
    assert result["status"] == "needs-user-decision"


def test_fix_loop_bouncing_blocking_not_mistaken_for_stale():
    sequence = [2, 4, 1, 3]

    def review_fn(round_n):
        return {"blocking": sequence[round_n]}

    def fix_fn(round_n):
        pass

    result = _sp.run_spec_fix_loop(review_fn, fix_fn, max_rounds=3)
    assert result["status"] == "needs-user-decision"
    assert result["fix_calls"] == 3


def test_fix_loop_source_has_no_stale_detection_reference():
    src = SPEC_PIPELINE_SRC.read_text(encoding="utf-8")
    assert "rounds_since_strict_decrease" not in src
    assert "from .trend import" not in src
    assert "import trend" not in src


def test_fix_loop_review_failure_without_ok_is_not_mistaken_for_clean():
    """round 5 F1：非门失败（如 dependency_missing）没有 `.blocking` 键——
    循环绝不能把它读成 `.get("blocking", 0) == 0` 而当作 clean。"""
    calls = {"review": 0, "fix": 0}

    def review_fn(round_n):
        calls["review"] += 1
        return {"ok": False, "error": "dependency_missing"}

    def fix_fn(round_n):
        calls["fix"] += 1

    result = _sp.run_spec_fix_loop(review_fn, fix_fn, max_rounds=3)
    assert result["status"] == "review-failed"
    assert result["status"] != "clean"
    assert result["error"] == "dependency_missing"
    assert calls["fix"] == 0
    assert calls["review"] == 1


def test_fix_loop_review_failure_stops_immediately_not_retried():
    """评审未真正跑完时不应重试掩盖——立即终止，不推进到下一轮 fix。"""
    calls = {"review": 0, "fix": 0}

    def review_fn(round_n):
        calls["review"] += 1
        return {"ok": False, "error": "claude-exec-failed"}

    def fix_fn(round_n):
        calls["fix"] += 1

    result = _sp.run_spec_fix_loop(review_fn, fix_fn, max_rounds=3)
    assert result["status"] == "review-failed"
    assert result["rounds"] == 1
    assert calls["fix"] == 0


def test_fix_loop_gate_failed_review_result_also_not_clean():
    """`gate_failed` 非空的确定性门失败同样没有 `.blocking` 键，同一分支处理。"""

    def review_fn(round_n):
        return {
            "ok": False,
            "change": "add-foo",
            "round": round_n,
            "gate_failed": "openspec_validate",
            "gate_skipped": False,
            "detail": "strict validation error",
        }

    def fix_fn(round_n):
        pass

    result = _sp.run_spec_fix_loop(review_fn, fix_fn, max_rounds=3)
    assert result["status"] == "review-failed"
    assert result["gate_failed"] == "openspec_validate"


# ============================================================
# 5. RESULT 契约（tasks 5.1–5.4）
# ============================================================


def test_result_required_keys_spec_write_and_fix_exact():
    assert _pipeline.RESULT_REQUIRED_KEYS["spec_write"] == frozenset(
        {"change", "artifacts", "validate", "summary"}
    )
    assert _pipeline.RESULT_REQUIRED_KEYS["spec_fix"] == frozenset(
        {"change", "fixed", "validate", "summary"}
    )


def test_result_required_keys_implement_and_fix_unchanged():
    assert _pipeline.RESULT_REQUIRED_KEYS["implement"] == frozenset(
        {"commit", "tasks", "tests", "summary"}
    )
    assert _pipeline.RESULT_REQUIRED_KEYS["fix"] == frozenset(
        {"commit", "fixed", "tests", "summary", "categories_scanned", "regressions_added"}
    )


def test_spec_write_record_missing_validate_key_rejected(env_setup, fake_repo):
    p = _with_repo(env_setup, fake_repo)
    line = "RESULT: change=add-foo artifacts=proposal.md summary=/tmp/s.md"
    result = _sp.spec_write_record(p, "add-foo", line)
    assert result["ok"] is False
    assert result["error"] == "result-missing-keys"
    assert "validate" in result["missing_keys"]


def test_spec_write_record_full_result_accepted(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    _sp.spec_write_run(p, "add-foo")  # 建 base + pre_head marker
    line = "RESULT: change=add-foo artifacts=proposal.md,tasks.md validate=pass summary=/tmp/s.md"
    result = _sp.spec_write_record(p, "add-foo", line)
    assert result["ok"] is True


# ============================================================
# 6. 不变量 1 的渲染层防护（tasks 6.1–6.5，本 change 最关键的一组）
# ============================================================


def test_spec_write_prompt_does_not_leak_rubric(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo")
    text = Path(result["prompt_file"]).read_text(encoding="utf-8")
    assert "scope-creep" not in text
    assert "implementation-leak" not in text
    assert "spec-review.json" not in text


def test_spec_write_prompt_contains_user_goal_when_provided(env_setup, fake_repo):
    """round-2 F1: 一句话自由目标必须原文透传进 spec-write.prompt.md，不能只有 CHANGE_ID。"""
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    goal = "给认证模块加限流，防止暴力破解"
    result = _sp.spec_write_run(p, "add-foo", goal=goal)
    text = Path(result["prompt_file"]).read_text(encoding="utf-8")
    assert goal in text
    # 目标段落与不变量 1 正交：仍不得出现 review rubric/category 枚举或 findings 原文。
    assert "scope-creep" not in text
    assert "implementation-leak" not in text
    assert "spec-review.json" not in text


def test_spec_write_prompt_omits_goal_section_when_absent(env_setup, fake_repo):
    """已存在 change-id 补全/修复分支：不传 goal 时不得渲染目标段落或伪造目标文本。"""
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo")
    text = Path(result["prompt_file"]).read_text(encoding="utf-8")
    assert "用户原始目标" not in text


def test_render_spec_writer_goal_section_placed_before_required_inputs():
    """目标段落是撰写侧的语义锚点，必须在"必读输入"之前出现，且原文一字不改。"""
    from npc import templates as _templates

    goal = "把首页加载时间降到 1 秒以内 (P95)"
    text = _templates.render_spec_writer(
        change_id="perf-home", base="/tmp/base", repo_root="/tmp/repo", goal=goal
    )
    assert goal in text
    assert text.index(goal) < text.index("必读输入")


def test_cli_spec_write_run_threads_goal_flag(env_setup, make_args, fake_repo, capsys):
    """CLI 层：`npc spec write run --goal` 必须真的把 goal 传到 spec_write_run/render 层。"""
    _make_change_dir(fake_repo, "add-foo")
    _with_repo(env_setup, fake_repo)
    args = make_args(change_id="add-foo", goal="一句话自由目标 GOAL_MARKER_XYZ", config=None)
    _sp.cli_spec_write_run(args)
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is True
    prompt_text = Path(out["prompt_file"]).read_text(encoding="utf-8")
    assert "GOAL_MARKER_XYZ" in prompt_text


def test_spec_fix_prompt_contains_prev_round_finding_detail(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    base = _sp._spec_base(p, "add-foo")
    base.mkdir(parents=True, exist_ok=True)
    (base / "round-0.spec-review.json").write_text(
        json.dumps({
            "verdict": "changes-requested",
            "findings": [{
                "id": "F1", "severity": "high", "category": "ambiguity",
                "title": "t", "file": "specs/demo/spec.md", "line_range": "5",
                "detail": "UNIQUE_DETAIL_TEXT_ROUND0", "recommendation": "r",
            }],
        })
    )
    result = _sp.spec_fix_run(p, "add-foo", 1)
    assert result["ok"] is True
    text = Path(result["prompt_file"]).read_text(encoding="utf-8")
    assert "UNIQUE_DETAIL_TEXT_ROUND0" in text


def test_spec_fix_prompt_does_not_contain_current_round_finding(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    base = _sp._spec_base(p, "add-foo")
    base.mkdir(parents=True, exist_ok=True)
    (base / "round-0.spec-review.json").write_text(
        json.dumps({
            "verdict": "changes-requested",
            "findings": [{
                "id": "F1", "severity": "high", "category": "ambiguity",
                "title": "t", "file": "-", "line_range": "-",
                "detail": "ROUND0_ONLY_DETAIL", "recommendation": "r",
            }],
        })
    )
    (base / "round-1.spec-review.json").write_text(
        json.dumps({
            "verdict": "changes-requested",
            "findings": [{
                "id": "F1", "severity": "high", "category": "contradiction",
                "title": "t", "file": "-", "line_range": "-",
                "detail": "ROUND1_ONLY_DETAIL", "recommendation": "r",
            }],
        })
    )
    result = _sp.spec_fix_run(p, "add-foo", 1)
    text = Path(result["prompt_file"]).read_text(encoding="utf-8")
    assert "ROUND0_ONLY_DETAIL" in text
    assert "ROUND1_ONLY_DETAIL" not in text


def test_spec_fix_run_missing_prev_review_rejected(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_fix_run(p, "add-foo", 1)
    assert result["ok"] is False
    assert result["error"] == "prev_spec_review_missing"


def test_implement_prompt_does_not_leak_spec_review_content(env_setup, fake_repo, make_args, capsys, monkeypatch):
    """跨链负向：round-0.spec-review.json 存在 → npc implement run 渲染的 prompt 不含其内容。"""
    from npc import state as _state
    from npc import agent as _agent

    _state.init_run(make_args(plan_order=json.dumps(["add-foo"])))
    capsys.readouterr()
    _state.add_change(make_args(seq=1, change_id="add-foo", base=None))
    capsys.readouterr()

    p = env_setup
    base = _sp._spec_base(p, "add-foo")
    base.mkdir(parents=True, exist_ok=True)
    (base / "round-0.spec-review.json").write_text(
        json.dumps({
            "verdict": "changes-requested",
            "findings": [{
                "id": "F1", "severity": "high", "category": "untestable",
                "title": "t", "file": "-", "line_range": "-",
                "detail": "SPEC_REVIEW_ONLY_DETAIL", "recommendation": "r",
            }],
        })
    )

    _agent.prompt_render(make_args(phase="implement", change_id="add-foo", seq=None, round_n=None, output=None))
    out = base.parent / "001-add-foo" / "implement.prompt.md"
    text = out.read_text(encoding="utf-8")
    assert "untestable" not in text
    assert "SPEC_REVIEW_ONLY_DETAIL" not in text


# ============================================================
# 7. telemetry（tasks 7.1–7.3, 7.2b）
# ============================================================


def test_emit_spec_review_round_produces_all_contract_fields(isolate_telemetry, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )
    _telemetry.emit_spec_review_round(
        proj_key="demo", run_ts="2026-01-01-0000", change_seq=None, change_id="add-foo",
        round_n=0, base="/tmp/base", ok=True, engine="codex", verdict="approve",
        blocking_count=0, blocking_categories=[], duration_ms=10, retry_count=0,
        outcome_reason=None, gate_failed=None, gate_skipped=False, gate_rule_hits={},
        state_json=None, run_events=None,
    )
    assert len(captured) == 1
    assert captured[0]["kind"] == "spec_review.round"
    missing = _telemetry.EMIT_FIELD_CONTRACT["spec_review.round"] - set(captured[0].keys())
    assert not missing


def test_emit_spec_review_round_record_validates_against_packaged_schema(isolate_telemetry, monkeypatch):
    """回归测试 F2：真实 emit_spec_review_round 产出的 record 必须能通过
    ``telemetry_schema_v1.json``（生产环境实际拷贝给消费者的那份 schema 文件）
    校验，而不仅是 EMIT_FIELD_CONTRACT 的字段名集合断言。覆盖 approve 判定 +
    gate_failed/gate_skipped/gate_rule_hits 三个 spec 侧专有字段 + kind 枚举。
    """
    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )
    _telemetry.emit_spec_review_round(
        proj_key="demo", run_ts="2026-01-01-0000", change_seq=None, change_id="add-foo",
        round_n=0, base="/tmp/base", ok=True, engine="codex", verdict="approve",
        blocking_count=0, blocking_categories=[], duration_ms=10, retry_count=0,
        outcome_reason=None, gate_failed=None, gate_skipped=False, gate_rule_hits={},
        state_json="/tmp/state.json", run_events="/tmp/run.events.jsonl",
    )
    assert len(captured) == 1
    record = dict(captured[0])
    record.setdefault("schema_version", 1)
    record.setdefault("ts", "2026-01-01T00:00:00+08:00")

    schema = json.loads(TELEMETRY_SCHEMA_V1_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(record, schema)  # 不抛即通过


def test_emit_spec_review_round_changes_requested_record_validates_against_packaged_schema(
    isolate_telemetry, monkeypatch
):
    """回归测试 F2：changes-requested 判定 + gate_cmd 门失败场景同样要能通过 schema。"""
    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )
    _telemetry.emit_spec_review_round(
        proj_key="demo", run_ts="2026-01-01-0000", change_seq=None, change_id="add-foo",
        round_n=1, base="/tmp/base", ok=False, engine=None, verdict=None,
        blocking_count=None, blocking_categories=None, duration_ms=5, retry_count=0,
        outcome_reason="gate_cmd_failed", gate_failed="gate_cmd", gate_skipped=False,
        gate_rule_hits={"rule-a": 2}, state_json="/tmp/state.json", run_events="/tmp/run.events.jsonl",
    )
    assert len(captured) == 1
    record = dict(captured[0])
    record.setdefault("schema_version", 1)
    record.setdefault("ts", "2026-01-01T00:00:00+08:00")

    schema = json.loads(TELEMETRY_SCHEMA_V1_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(record, schema)  # 不抛即通过


def test_gate_failed_event_has_null_verdict_not_changes_requested(env_setup, fake_repo, monkeypatch, isolate_telemetry):
    _make_change_dir(fake_repo, "add-foo")
    config_path = _write_gate_cmd_config(fake_repo, ["stub-gate"])
    p = _with_repo(env_setup, fake_repo)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )

    result = _sp.spec_review_run(
        p, "add-foo", 0,
        config_path=config_path,
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=_fake_gate_runner(json.dumps({"ok": False, "rule_hits": {}})),
    )
    assert result["gate_failed"] == "gate_cmd"
    assert len(captured) == 1
    assert captured[0]["gate_failed"] == "gate_cmd"
    assert captured[0]["verdict"] is None


def test_review_round_contract_unchanged_no_gate_failed(env_setup):
    assert "gate_failed" not in _telemetry.EMIT_FIELD_CONTRACT["review.round"]
    assert "blocking_categories" in _telemetry.EMIT_FIELD_CONTRACT["review.round"]
    assert "spec_attribution_counts" in _telemetry.EMIT_FIELD_CONTRACT["review.round"]


def test_gate_rule_hits_passthrough(env_setup, fake_repo, monkeypatch, isolate_telemetry):
    _make_change_dir(fake_repo, "add-foo")
    config_path = _write_gate_cmd_config(fake_repo, ["stub-gate"])
    p = _with_repo(env_setup, fake_repo)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")
    monkeypatch.setattr(_sp, "_spec_engine_exec", _stub_engine_writes({"verdict": "approve", "findings": []}))

    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )

    hits = {"foo_rule": 2, "bar_rule": 0}
    result = _sp.spec_review_run(
        p, "add-foo", 0,
        config_path=config_path,
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=_fake_gate_runner(json.dumps({"ok": True, "rule_hits": hits})),
    )
    assert result["ok"] is True
    assert captured[0]["gate_rule_hits"] == hits


# ============================================================
# 8. 越界修改 / 意外 commit 的确定性拦截（tasks 8.2.x）
# ============================================================


def test_spec_write_record_rejects_out_of_scope_changes(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    _sp.spec_write_run(p, "add-foo")

    subprocess.run(["git", "add", "-A"], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "wip"], cwd=fake_repo, check=True)

    (fake_repo / "src").mkdir(exist_ok=True)
    (fake_repo / "src" / "npc").mkdir(exist_ok=True)
    (fake_repo / "src" / "npc" / "cli.py").write_text("# tampered\n")

    line = "RESULT: change=add-foo artifacts=proposal.md validate=pass summary=/tmp/s.md"
    result = _sp.spec_write_record(p, "add-foo", line)
    assert result["ok"] is False
    assert result["error"] == "out_of_scope_changes"
    assert any("src/npc/cli.py" in pth for pth in result["paths"])


def test_spec_write_record_accepts_change_dir_only_edits(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    _sp.spec_write_run(p, "add-foo")

    (fake_repo / "openspec" / "changes" / "add-foo" / "design.md").write_text("# design\n")

    line = "RESULT: change=add-foo artifacts=design.md validate=pass summary=/tmp/s.md"
    result = _sp.spec_write_record(p, "add-foo", line)
    assert result["ok"] is True


def test_spec_write_record_rejects_unexpected_commit(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    _sp.spec_write_run(p, "add-foo")

    subprocess.run(["git", "add", "-A"], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "unexpected"], cwd=fake_repo, check=True)

    line = "RESULT: change=add-foo artifacts=proposal.md validate=pass summary=/tmp/s.md"
    result = _sp.spec_write_record(p, "add-foo", line)
    assert result["ok"] is False
    assert result["error"] == "unexpected_commit"


# ============================================================
# 9. v1 恒 in-session + 路由真相源唯一（tasks 8.3.x）
# ============================================================


def test_spec_write_run_always_deferred(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo")
    assert result["ok"] is True
    assert result["deferred"] is True
    assert "spawn_prompt" in result
    assert "prompt_file" in result


def test_spec_write_run_rejects_mimo_backend(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text('[spec_writer]\nbackend = "mimo"\n', encoding="utf-8")
    p = _with_repo(env_setup, fake_repo)

    result = _sp.spec_write_run(p, "add-foo", config_path=npc_dir / "config.toml")
    assert result["ok"] is False
    assert result["error"] == "spec_routing_violation"
    rules = {v["rule"] for v in result["violations"]}
    assert "spec_mimo_in_session" in rules
    assert not (_sp._spec_base(p, "add-foo") / "spec-write.prompt.md").exists()


def test_spec_write_run_rejects_non_orthogonal_writer_review(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text(
        '[spec_writer]\nbackend = "codex"\n\n[spec_review]\nengine = "codex"\n', encoding="utf-8"
    )
    p = _with_repo(env_setup, fake_repo)

    engine_calls: list[dict] = []
    result = _sp.spec_write_run(p, "add-foo", config_path=npc_dir / "config.toml")
    assert result["ok"] is False
    rules = {v["rule"] for v in result["violations"]}
    assert "spec_gen_not_orthogonal" in rules
    assert engine_calls == []


def test_spec_review_run_rejects_engine_name_cli_override_same_source(
    env_setup, fake_repo, monkeypatch
):
    """回归测试 F1：spec_writer.backend=codex，spec_review.engine=claude 通过配置层
    路由校验，但 CLI ``--engine codex`` 覆盖后 spec_review 实际执行身份变为 codex，
    与 spec_writer 同源（both_codex）→ 守卫必须用实际执行 engine 校验，拒绝执行，
    LLM 引擎零调用。
    """
    _make_change_dir(fake_repo, "add-foo")
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text(
        '[spec_writer]\nbackend = "codex"\n\n[spec_review]\nengine = "claude"\n',
        encoding="utf-8",
    )
    p = _with_repo(env_setup, fake_repo)

    engine_calls: list[dict] = []
    monkeypatch.setattr(_sp, "_spec_engine_exec", lambda **kw: (engine_calls.append(kw), 0)[1])
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _sp.spec_review_run(
        p, "add-foo", 0,
        engine_name="codex",
        config_path=npc_dir / "config.toml",
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=_fake_gate_runner(json.dumps({"ok": True, "rule_hits": {}})),
    )
    assert result["ok"] is False
    assert result["error"] == "spec_routing_violation"
    rules = {v["rule"] for v in result["violations"]}
    assert "spec_gen_not_orthogonal" in rules
    assert engine_calls == []


def test_spec_review_run_allows_engine_name_cli_override_different_source(
    env_setup, fake_repo, monkeypatch
):
    """回归测试 F1 正向场景：spec_writer.backend=claude，spec_review.engine=codex，
    CLI ``--engine claude`` 覆盖后 spec_review 变为 claude，但与 spec_writer 的
    bin/model 不同源 → 合法覆盖不应被误拒，LLM 引擎正常被调用。
    """
    _make_change_dir(fake_repo, "add-foo")
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text(
        '[spec_writer]\nbackend = "claude"\nbin = "claude"\nmodel = "claude-sonnet-4-5"\n\n'
        '[spec_review]\nengine = "codex"\nclaude_bin = "claude"\nclaude_model = "claude-opus-4-8"\n',
        encoding="utf-8",
    )
    p = _with_repo(env_setup, fake_repo)

    engine_calls: list[dict] = []

    def fake_engine(**kw):
        engine_calls.append(kw)
        kw["review_out"].parent.mkdir(parents=True, exist_ok=True)
        kw["review_out"].write_text(json.dumps({"verdict": "approve", "findings": []}))
        kw["events_out"].parent.mkdir(parents=True, exist_ok=True)
        kw["events_out"].write_text("x")
        return 0

    monkeypatch.setattr(_sp, "_spec_engine_exec", fake_engine)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    result = _sp.spec_review_run(
        p, "add-foo", 0,
        engine_name="claude",
        config_path=npc_dir / "config.toml",
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=_fake_gate_runner(json.dumps({"ok": True, "rule_hits": {}})),
    )
    assert result.get("error") != "spec_routing_violation"
    assert len(engine_calls) == 1


def test_spec_pipeline_source_has_no_independent_backend_whitelist():
    src = SPEC_PIPELINE_SRC.read_text(encoding="utf-8")
    assert "SUPPORTED_SPEC_" not in src
    assert "spec_writer_backend_unsupported" not in src


def test_agent_timeout_budget_by_change_positive(env_setup, make_args):
    from npc import agent as _agent

    result_ns = make_args(seq=None, change_id="add-foo", phase="spec_write", base=None, mult=None, max_sec=None)
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _agent.timeout_budget(result_ns)
    out = json.loads(buf.getvalue())
    assert out["ok"] is True
    assert out["timeout_sec"] > 0


# ============================================================
# 10. 非目标守护（tasks 9.1–9.3）
# ============================================================


def test_spine_run_command_not_modified_to_spawn_spec_writer():
    text = (REPO_ROOT / "plugins" / "agent-spine" / "commands" / "spine-run.md").read_text(encoding="utf-8")
    assert "spine-spec-writer" not in text


def test_auto_decide_valid_triggers_unchanged():
    from npc import auto_decide as _auto_decide

    assert not any(t.startswith("spec-") for t in _auto_decide.VALID_TRIGGERS)


def test_no_spec_attributable_blocking_rate_gate_in_spec_pipeline():
    src = SPEC_PIPELINE_SRC.read_text(encoding="utf-8")
    assert "spec_attributable_blocking_rate" not in src


# ============================================================
# 11. 端到端（tasks 10.1 / 10.1b）
# ============================================================


def test_e2e_gate_cmd_stub_failure_stops_before_llm(env_setup, fake_repo, monkeypatch, tmp_path):
    _make_change_dir(fake_repo, "add-foo")
    stub = tmp_path / "stub_gate.py"
    stub.write_text(
        "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'ok': False, 'rule_hits': {}}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text(
        f'[spec_review]\ngate_cmd = ["python3", "{stub}"]\n', encoding="utf-8"
    )
    p = _with_repo(env_setup, fake_repo)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    engine_calls: list[dict] = []
    monkeypatch.setattr(_sp, "_spec_engine_exec", lambda **kw: (engine_calls.append(kw), 0)[1])

    result = _sp.spec_review_run(
        p, "add-foo", 0,
        config_path=npc_dir / "config.toml",
        validate_runner=_fake_validate_runner(returncode=0),
    )
    assert result["ok"] is False
    assert result["gate_failed"] == "gate_cmd"
    assert engine_calls == []


@pytest.mark.skipif(
    __import__("shutil").which("openspec") is None or __import__("shutil").which("uv") is None,
    reason="需要真实 openspec/uv 二进制",
)
def test_e2e_real_check_spec_rule_hits_passthrough_and_continues_to_llm(
    env_setup, fake_repo, monkeypatch
):
    archived = (
        REPO_ROOT / "openspec" / "changes" / "archive" / "2026-07-03-parallel-dag-scheduling"
    )
    if not archived.is_dir():
        pytest.skip("archived fixture 不存在")

    change_dir = fake_repo / "openspec" / "changes" / "e2e-real-gate"
    change_dir.mkdir(parents=True, exist_ok=True)
    import shutil as _shutil

    for name in ("proposal.md", "tasks.md", "design.md"):
        src = archived / name
        if src.is_file():
            _shutil.copy(src, change_dir / name)
    specs_src = archived / "specs"
    if specs_src.is_dir():
        _shutil.copytree(specs_src, change_dir / "specs")

    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    check_spec_script = REPO_ROOT / "scripts" / "check_spec.py"
    (npc_dir / "config.toml").write_text(
        f'[spec_review]\ngate_cmd = ["uv", "run", "--project", "{REPO_ROOT}", "python3", "{check_spec_script}"]\n',
        encoding="utf-8",
    )
    p = _with_repo(env_setup, fake_repo)

    engine_calls: list[dict] = []
    monkeypatch.setattr(_sp, "_spec_engine_exec", _stub_engine_writes({"verdict": "approve", "findings": []}))

    result = _sp.spec_review_run(
        p, "e2e-real-gate", 0,
        config_path=npc_dir / "config.toml",
    )
    assert result["ok"] is True
    assert result["gate_failed"] is None
    assert result["gate_rule_hits"]["deferred_decision_outside_open_questions"] == 2


def test_e2e_spec_write_to_clean_review_emits_telemetry(env_setup, fake_repo, monkeypatch, isolate_telemetry):
    """端到端（干净 change，mock 引擎）：write → write record → review run → status=clean，且 telemetry 被 emit。"""
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    monkeypatch.setattr(_sp, "_find_openspec_bin", lambda override=None: "/fake/openspec")

    write_result = _sp.spec_write_run(p, "add-foo")
    assert write_result["ok"] is True

    (fake_repo / "openspec" / "changes" / "add-foo" / "design.md").write_text("# design\n")
    record_line = "RESULT: change=add-foo artifacts=design.md validate=pass summary=/tmp/s.md"
    record_result = _sp.spec_write_record(p, "add-foo", record_line)
    assert record_result["ok"] is True

    captured: list[dict] = []
    monkeypatch.setattr(
        _telemetry, "emit_event", lambda record, **kw: (captured.append(record), True)[1]
    )
    monkeypatch.setattr(_sp, "_spec_engine_exec", _stub_engine_writes({"verdict": "approve", "findings": []}))

    review_result = _sp.spec_review_run(
        p, "add-foo", 0,
        validate_runner=_fake_validate_runner(returncode=0),
        gate_runner=subprocess.run,
    )
    assert review_result["ok"] is True
    assert review_result["blocking"] == 0

    loop = _sp.run_spec_fix_loop(
        lambda round_n: review_result if round_n == 0 else {"blocking": 0}, lambda round_n: None, max_rounds=3
    )
    assert loop["status"] == "clean"
    assert any(rec["kind"] == "spec_review.round" for rec in captured)


# ============================================================
# 12. 跨链负向：spec write run 不泄漏 code review 内容（tasks 6.4b / 6.5）
# ============================================================


def test_spec_write_prompt_does_not_leak_code_review_spec_attribution(env_setup, fake_repo):
    """round-0.review.json 的 finding 含 spec_attribution 时，spec write run 的 prompt 不泄漏。"""
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)

    # 放一份 code review round.json 在别处（implement 流水线的 base，不是 spec 的 base）
    code_review_base = p.run_dir / "001-add-foo"
    code_review_base.mkdir(parents=True, exist_ok=True)
    (code_review_base / "round-0.review.json").write_text(
        json.dumps({
            "verdict": "changes-requested",
            "findings": [{
                "id": "F1", "severity": "high", "category": "validation", "title": "t",
                "file": "-", "line_range": "-", "detail": "CODE_REVIEW_ONLY_DETAIL",
                "recommendation": "r", "in_scope": True, "spec_attribution": "spec-ambiguous",
            }],
        })
    )

    result = _sp.spec_write_run(p, "add-foo")
    text = Path(result["prompt_file"]).read_text(encoding="utf-8")
    assert "spec_attribution" not in text
    assert "spec_attributable_blocking_rate" not in text
    for val in ("spec-silent", "spec-ambiguous", "spec-contradicted", "impl-deviation"):
        assert val not in text
    assert "CODE_REVIEW_ONLY_DETAIL" not in text


def test_spec_write_prompt_does_not_leak_code_review_findings(env_setup, fake_repo):
    """code round-N.review.json 存在 → spec write run 的 prompt 不含其 findings 原文。"""
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)

    code_review_base = p.run_dir / "001-add-foo"
    code_review_base.mkdir(parents=True, exist_ok=True)
    (code_review_base / "round-0.review.json").write_text(
        json.dumps({
            "verdict": "changes-requested",
            "findings": [{
                "id": "F1", "severity": "critical", "category": "security", "title": "leak-title",
                "file": "-", "line_range": "-", "detail": "ANOTHER_CODE_FINDING_DETAIL",
                "recommendation": "r", "in_scope": True, "spec_attribution": "impl-deviation",
            }],
        })
    )

    result = _sp.spec_write_run(p, "add-foo")
    text = Path(result["prompt_file"]).read_text(encoding="utf-8")
    assert "ANOTHER_CODE_FINDING_DETAIL" not in text


# ============================================================
# 13. subagent 契约内容（tasks 8.5）
# ============================================================


def test_spine_spec_writer_agent_md_contains_result_schema_and_read_instruction():
    text = (
        REPO_ROOT / "plugins" / "agent-spine" / "agents" / "spine-spec-writer.md"
    ).read_text(encoding="utf-8")
    assert "change=" in text and "artifacts=" in text and "validate=" in text
    assert "fixed=" in text
    assert "Read" in text
    assert "git commit" in text


# ============================================================
# 14. change: spec-writer-pattern-interrogation
# ============================================================

from npc import templates as _templates  # noqa: E402

SPEC_REVIEW_CATEGORY_ENUM = (
    "ambiguity", "missing-scenario", "implementation-leak",
    "untestable", "deferred-decision", "contradiction", "scope-creep",
)


# ----- 1. RESULT 契约扩展 -----


def test_result_required_keys_spec_interrogate_exact():
    assert _pipeline.RESULT_REQUIRED_KEYS["spec_interrogate"] == frozenset(
        {"change", "artifacts", "summary"}
    )


def test_result_required_keys_spec_write_and_fix_still_intact():
    assert _pipeline.RESULT_REQUIRED_KEYS["spec_write"] == frozenset(
        {"change", "artifacts", "validate", "summary"}
    )
    assert _pipeline.RESULT_REQUIRED_KEYS["spec_fix"] == frozenset(
        {"change", "fixed", "validate", "summary"}
    )


def test_interrogate_record_missing_summary_key_rejected(env_setup, fake_repo):
    _write_pattern_interrogation(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    line = "RESULT: change=add-foo artifacts=openspec/changes/add-foo/pattern-interrogation.md"
    result = _sp.spec_interrogate_record(p, "add-foo", line)
    assert result["ok"] is False
    assert result["error"] == "result-missing-keys"
    assert "summary" in result["missing_keys"]


# ----- 2. npc spec interrogate run -----


def _valid_interrogate_result(change_id="add-foo"):
    return (
        f"RESULT: change={change_id} "
        f"artifacts=openspec/changes/{change_id}/pattern-interrogation.md summary=/tmp/s.md"
    )


def test_interrogate_run_always_deferred_with_prompt_file(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_run(p, "add-foo")
    assert result["ok"] is True
    assert result["deferred"] is True
    assert "spawn_prompt" in result
    assert result["prompt_file"].endswith("pattern-interrogation.prompt.md")


def test_interrogate_run_rejects_mimo_backend_no_prompt(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text('[spec_writer]\nbackend = "mimo"\n', encoding="utf-8")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_run(p, "add-foo", config_path=npc_dir / "config.toml")
    assert result["ok"] is False
    assert result["error"] == "spec_routing_violation"
    rules = {v["rule"] for v in result["violations"]}
    assert "spec_mimo_in_session" in rules
    assert not (_sp._spec_base(p, "add-foo") / "pattern-interrogation.prompt.md").exists()


def test_interrogate_run_goal_passthrough(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_run(p, "add-foo", goal="给认证模块加限流")
    text = Path(result["prompt_file"]).read_text(encoding="utf-8")
    assert "给认证模块加限流" in text


def test_interrogate_prompt_does_not_leak_rubric(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_run(p, "add-foo")
    text = Path(result["prompt_file"]).read_text(encoding="utf-8")
    assert "scope-creep" not in text
    assert "implementation-leak" not in text
    for cat in SPEC_REVIEW_CATEGORY_ENUM:
        assert cat not in text
    assert "spec-review.json" not in text


def test_interrogate_prompt_requires_three_sections():
    text = _templates.render_spec_interrogator(
        change_id="add-foo", base="/tmp/b", repo_root="/tmp/r"
    )
    assert "## Analogs" in text
    assert "## Assumptions" in text
    assert "## Open Questions" in text
    assert "pattern-interrogation.md" in text


def test_interrogate_run_timeout_budget_phase(env_setup, fake_repo, make_args, capsys):
    from npc import agent as _agent

    _make_change_dir(fake_repo, "add-foo")
    _with_repo(env_setup, fake_repo)
    _agent.timeout_budget(
        make_args(seq=None, change_id="add-foo", phase="spec_interrogate",
                  base=None, mult=None, max_sec=None)
    )
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is True
    assert isinstance(out["timeout_sec"], int) and out["timeout_sec"] > 0


# ----- 3. npc spec interrogate record + open_questions 计数 -----


def test_interrogate_record_counts_open_questions(env_setup, fake_repo):
    _write_pattern_interrogation(fake_repo, "add-foo", open_questions_bullets=3)
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_record(p, "add-foo", _valid_interrogate_result())
    assert result["ok"] is True
    assert result["open_questions"] == 3


def test_interrogate_record_missing_file(env_setup, fake_repo):
    (fake_repo / "openspec" / "changes" / "add-foo").mkdir(parents=True)
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_record(p, "add-foo", _valid_interrogate_result())
    assert result["ok"] is False
    assert result["error"] == "pattern_interrogation_missing"


def test_interrogate_record_missing_open_questions_section(env_setup, fake_repo):
    _write_pattern_interrogation(fake_repo, "add-foo", sections=("## Analogs", "## Assumptions"))
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_record(p, "add-foo", _valid_interrogate_result())
    assert result["ok"] is False
    assert result["error"] == "pattern_interrogation_missing_section"
    assert "## Open Questions" in result["missing_sections"]


def test_interrogate_record_empty_open_questions_is_zero(env_setup, fake_repo):
    _write_pattern_interrogation(fake_repo, "add-foo", open_questions_bullets=0)
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_record(p, "add-foo", _valid_interrogate_result())
    assert result["ok"] is True
    assert result["open_questions"] == 0


def test_interrogate_record_rejects_out_of_scope(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    _sp.spec_interrogate_run(p, "add-foo")  # 建 base + pre_head marker
    subprocess.run(["git", "add", "-A"], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "wip"], cwd=fake_repo, check=True)
    (fake_repo / "src").mkdir(exist_ok=True)
    (fake_repo / "src" / "npc").mkdir(exist_ok=True)
    (fake_repo / "src" / "npc" / "templates.py").write_text("# tampered\n")
    result = _sp.spec_interrogate_record(p, "add-foo", _valid_interrogate_result())
    assert result["ok"] is False
    assert result["error"] == "out_of_scope_changes"


def test_interrogate_record_rejects_unexpected_commit(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    _sp.spec_interrogate_run(p, "add-foo")
    subprocess.run(["git", "add", "-A"], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "unexpected"], cwd=fake_repo, check=True)
    result = _sp.spec_interrogate_record(p, "add-foo", _valid_interrogate_result())
    assert result["ok"] is False
    assert result["error"] == "unexpected_commit"


# ----- 4. npc spec write run 的硬前置门 -----


def test_write_run_rejected_when_interrogation_missing(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo", interrogated=False)
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo")
    assert result["ok"] is False
    assert result["error"] == "pattern_interrogation_missing"
    assert not (_sp._spec_base(p, "add-foo") / "spec-write.prompt.md").exists()


def test_write_run_ok_when_three_sections_present(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo")
    assert result["ok"] is True
    assert result["deferred"] is True
    assert "spawn_prompt" in result
    assert "prompt_file" in result


def test_write_run_rejected_when_missing_assumptions(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo", interrogated=False)
    _write_pattern_interrogation(fake_repo, "add-foo", sections=("## Analogs", "## Open Questions"))
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo")
    assert result["ok"] is False
    assert result["error"] == "pattern_interrogation_missing_section"
    assert "## Assumptions" in result["missing_sections"]
    assert not (_sp._spec_base(p, "add-foo") / "spec-write.prompt.md").exists()


@pytest.mark.parametrize(
    "missing,present",
    [
        ("## Analogs", ("## Assumptions", "## Open Questions")),
        ("## Open Questions", ("## Analogs", "## Assumptions")),
    ],
)
def test_write_run_rejected_each_missing_section(env_setup, fake_repo, missing, present):
    _make_change_dir(fake_repo, "add-foo", interrogated=False)
    _write_pattern_interrogation(fake_repo, "add-foo", sections=present)
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo")
    assert result["ok"] is False
    assert result["error"] == "pattern_interrogation_missing_section"
    assert missing in result["missing_sections"]


def test_write_run_gate_applies_to_branch_b_no_goal(env_setup, fake_repo):
    # 分支 B：proposal 已存在但 pattern-interrogation.md 不存在
    change_dir = fake_repo / "openspec" / "changes" / "add-foo"
    change_dir.mkdir(parents=True)
    (change_dir / "proposal.md").write_text("## Why\n\nx\n", encoding="utf-8")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo")
    assert result["ok"] is False
    assert result["error"] == "pattern_interrogation_missing"


def test_routing_violation_precedes_interrogation_gate(env_setup, fake_repo):
    # mimo + 缺 pattern-interrogation.md：错误标识恒为 spec_routing_violation
    change_dir = fake_repo / "openspec" / "changes" / "add-foo"
    change_dir.mkdir(parents=True)
    npc_dir = fake_repo / ".npc"
    npc_dir.mkdir()
    (npc_dir / "config.toml").write_text('[spec_writer]\nbackend = "mimo"\n', encoding="utf-8")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo", config_path=npc_dir / "config.toml")
    assert result["ok"] is False
    assert result["error"] == "spec_routing_violation"


# ----- 5. record 与 write gate 判定一致（同一判据、无分歧）-----


@pytest.mark.parametrize(
    "present,expected_missing",
    [
        (("## Assumptions", "## Open Questions"), "## Analogs"),
        (("## Analogs", "## Open Questions"), "## Assumptions"),
        (("## Analogs", "## Assumptions"), "## Open Questions"),
    ],
)
def test_record_and_write_gate_agree_on_missing_sections(env_setup, fake_repo, present, expected_missing):
    _write_pattern_interrogation(fake_repo, "add-foo", sections=present)
    p = _with_repo(env_setup, fake_repo)
    rec = _sp.spec_interrogate_record(p, "add-foo", _valid_interrogate_result())
    wr = _sp.spec_write_run(p, "add-foo")
    assert rec["ok"] is False and wr["ok"] is False
    assert rec["error"] == "pattern_interrogation_missing_section"
    assert wr["error"] == "pattern_interrogation_missing_section"
    assert set(rec["missing_sections"]) == set(wr["missing_sections"])
    assert expected_missing in rec["missing_sections"]


# ----- 6. render_spec_writer 扩写 -----


def _write_prompt_text(env_setup, fake_repo):
    _make_change_dir(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_write_run(p, "add-foo")
    return Path(result["prompt_file"]).read_text(encoding="utf-8")


def test_write_prompt_lists_pattern_interrogation_input(env_setup, fake_repo):
    text = _write_prompt_text(env_setup, fake_repo)
    assert "pattern-interrogation.md" in text


def test_write_prompt_mechanical_criterion_not_semantic(env_setup, fake_repo):
    text = _write_prompt_text(env_setup, fake_repo)
    assert "## User Decisions (Interactive)" in text
    assert "已被回应" not in text
    assert "resolved" not in text


def test_write_prompt_user_decisions_branch_instruction(env_setup, fake_repo):
    text = _write_prompt_text(env_setup, fake_repo)
    assert "把 `## Open Questions` + `## User Decisions (Interactive)` 段原样写入 design.md 的 `## Pattern Mapping` 段" in text


def test_write_prompt_no_user_decisions_branch_instruction(env_setup, fake_repo):
    text = _write_prompt_text(env_setup, fake_repo)
    assert "把 `## Open Questions` + `## Assumptions` 段原样写入 design.md 的 `## Pattern Mapping` 与 `## Assumptions` 段" in text


def test_write_prompt_touchpoint_search_command_instruction(env_setup, fake_repo):
    text = _write_prompt_text(env_setup, fake_repo)
    assert "确定性搜索命令" in text
    assert "grep" in text and "rg" in text and "git grep" in text
    assert "tasks.md" in text


def test_write_prompt_still_no_rubric_after_extension(env_setup, fake_repo):
    text = _write_prompt_text(env_setup, fake_repo)
    assert "scope-creep" not in text
    assert "implementation-leak" not in text
    for cat in SPEC_REVIEW_CATEGORY_ENUM:
        assert cat not in text


# ----- 6b. npc spec interrogate decide -----


def test_decide_missing_file(env_setup, fake_repo):
    (fake_repo / "openspec" / "changes" / "add-foo").mkdir(parents=True)
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_decide(p, "add-foo", "Q1: 用户选择方案 A")
    assert result["ok"] is False
    assert result["error"] == "pattern_interrogation_missing"


def test_decide_appends_verbatim(env_setup, fake_repo):
    path = _write_pattern_interrogation(fake_repo, "add-foo", open_questions_bullets=1)
    p = _with_repo(env_setup, fake_repo)
    result = _sp.spec_interrogate_decide(p, "add-foo", "Q1: 用户选择方案 A")
    assert result["ok"] is True
    text = path.read_text(encoding="utf-8")
    assert "## User Decisions (Interactive)" in text
    assert "Q1: 用户选择方案 A" in text


def test_decide_second_call_rejected_no_overwrite(env_setup, fake_repo):
    path = _write_pattern_interrogation(fake_repo, "add-foo", open_questions_bullets=1)
    p = _with_repo(env_setup, fake_repo)
    _sp.spec_interrogate_decide(p, "add-foo", "first decision")
    before = path.read_bytes()
    result = _sp.spec_interrogate_decide(p, "add-foo", "second decision")
    assert result["ok"] is False
    assert result["error"] == "decisions_already_recorded"
    assert path.read_bytes() == before


def test_decide_does_not_parse_content(env_setup, fake_repo):
    path = _write_pattern_interrogation(fake_repo, "add-foo")
    p = _with_repo(env_setup, fake_repo)
    raw = "任意原文\n- 含 markdown\n### 甚至标题\n**加粗**"
    _sp.spec_interrogate_decide(p, "add-foo", raw)
    text = path.read_text(encoding="utf-8")
    assert raw in text


# ----- 9. 非目标守护 -----


def test_existing_phase_keys_unchanged():
    assert _pipeline.RESULT_REQUIRED_KEYS["implement"] == frozenset(
        {"commit", "tasks", "tests", "summary"}
    )
    assert _pipeline.RESULT_REQUIRED_KEYS["fix"] == frozenset(
        {"commit", "fixed", "tests", "summary", "categories_scanned", "regressions_added"}
    )


def test_spec_fix_run_source_unchanged_signature():
    import inspect

    src = inspect.getsource(_sp.spec_fix_run)
    assert "prev_spec_review_missing" in src  # 既有行为未被改动


# ----- 10. 端到端 -----


def test_e2e_interrogate_then_write(env_setup, fake_repo):
    # 全新 change：interrogate 未完成时 write 被拒；完成后成功
    change_dir = fake_repo / "openspec" / "changes" / "add-foo"
    change_dir.mkdir(parents=True)
    p = _with_repo(env_setup, fake_repo)
    assert _sp.spec_write_run(p, "add-foo")["error"] == "pattern_interrogation_missing"
    _write_pattern_interrogation(fake_repo, "add-foo", open_questions_bullets=2)
    rec = _sp.spec_interrogate_record(p, "add-foo", _valid_interrogate_result())
    assert rec["ok"] is True and rec["open_questions"] == 2
    assert _sp.spec_write_run(p, "add-foo")["ok"] is True


# ----- CLI 层 -----


def test_cli_interrogate_run_record_decide(env_setup, make_args, fake_repo, capsys):
    _make_change_dir(fake_repo, "add-foo", interrogated=False)
    _write_pattern_interrogation(fake_repo, "add-foo", open_questions_bullets=2)
    _with_repo(env_setup, fake_repo)
    _sp.cli_spec_interrogate_run(make_args(change_id="add-foo", goal=None, config=None))
    run_out = json.loads(capsys.readouterr().out.strip())
    assert run_out["ok"] is True
    _sp.cli_spec_interrogate_record(make_args(change_id="add-foo", result=_valid_interrogate_result()))
    rec_out = json.loads(capsys.readouterr().out.strip())
    assert rec_out["ok"] is True and rec_out["open_questions"] == 2
    _sp.cli_spec_interrogate_decide(make_args(change_id="add-foo", decisions_md="裁决原文"))
    dec_out = json.loads(capsys.readouterr().out.strip())
    assert dec_out["ok"] is True


# ----- 命令文档 / subagent 契约 -----


def test_spine_spec_command_auto_flag_and_interrogate_step():
    text = (
        REPO_ROOT / "plugins" / "agent-spine" / "commands" / "spine-spec.md"
    ).read_text(encoding="utf-8")
    assert "--auto" in text
    assert "模式盘问" in text
    # interrogate step 在 spec write 之前
    assert text.index("npc spec interrogate run") < text.index("npc spec write run")
    assert "AskUserQuestion" in text
    assert "npc spec interrogate decide" in text


def test_spine_spec_command_auto_forbids_askuserquestion():
    text = (
        REPO_ROOT / "plugins" / "agent-spine" / "commands" / "spine-spec.md"
    ).read_text(encoding="utf-8")
    assert "绝不调用 `AskUserQuestion`" in text


def test_spine_run_step_2b_untouched():
    text = (
        REPO_ROOT / "plugins" / "agent-spine" / "commands" / "spine-run.md"
    ).read_text(encoding="utf-8")
    assert "spine-spec-writer" not in text


def test_spine_spec_writer_agent_lists_interrogate_phase():
    text = (
        REPO_ROOT / "plugins" / "agent-spine" / "agents" / "spine-spec-writer.md"
    ).read_text(encoding="utf-8")
    assert "spec_interrogate" in text
    assert "## Analogs" in text and "## Assumptions" in text and "## Open Questions" in text
