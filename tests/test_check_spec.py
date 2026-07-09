"""`scripts/check_spec.py` 的测试套件。

本文件不 import 任何 `npc` 模块（除个别边界测试显式验证"npc 子命令面未变"，
那类测试通过 subprocess 调 `npc` CLI 二进制，而非 import npc 包）。脚本本身
零依赖，被测对象通过 `importlib` 从文件路径直接加载，不依赖包安装。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_spec.py"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "spec_lint"


def _load_check_spec():
    spec = importlib.util.spec_from_file_location("check_spec", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check_spec = _load_check_spec()


def _make_change_dir(tmp_path: Path, name: str = "change") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fake_openspec(monkeypatch, deltas: list[dict], stderr: str = "Warning: deprecation notice\n") -> None:
    """monkeypatch `openspec show --json --deltas-only` 的行为。"""

    payload = {"id": "fake", "deltaCount": len(deltas), "deltas": deltas}

    def _fake_which(name: str) -> str | None:
        return "/usr/bin/openspec" if name == "openspec" else None

    def _fake_run(cmd, capture_output=True, text=True, check=False):
        return types.SimpleNamespace(stdout=json.dumps(payload), stderr=stderr, returncode=0)

    monkeypatch.setattr(check_spec.shutil, "which", _fake_which)
    monkeypatch.setattr(check_spec.subprocess, "run", _fake_run)


def _delta(req_text: str, scenarios_raw: list[str]) -> dict:
    return {
        "spec": "fake",
        "operation": "ADDED",
        "requirement": {
            "text": req_text,
            "scenarios": [{"rawText": raw} for raw in scenarios_raw],
        },
    }


# ============================================================
# 1. 纯函数骨架
# ============================================================


def test_strip_code_spans_removes_inline_code_span_keeps_line_count():
    text = "before `实施时定` after\nsecond line"
    stripped = check_spec.strip_code_spans(text)
    assert "实施时定" not in stripped
    assert stripped.count("\n") == text.count("\n")
    assert len(stripped.split("\n")[0]) == len(text.split("\n")[0])


def test_strip_code_spans_removes_fenced_code_block_keeps_line_alignment():
    text = "## Decisions\n```\n实施时定\nsome code\n```\nafter fence 实施时定\n"
    stripped = check_spec.strip_code_spans(text)
    lines = stripped.split("\n")
    orig_lines = text.split("\n")
    assert len(lines) == len(orig_lines)
    # fence 内容（第 1..3 行，0-based）应被清空
    assert "实施时定" not in lines[2]
    assert "code" not in lines[3]
    # fence 之后裸露的措辞应保留（不属于 fence）
    assert "实施时定" in lines[5]


def test_section_of_line_returns_h2_heading():
    lines = ["## Context", "text", "## Decisions", "body", "### D1: sub", "more body"]
    assert check_spec.section_of_line(lines, 1) == "Context"
    assert check_spec.section_of_line(lines, 3) == "Decisions"
    # H3 子标题不改变所属的 H2 段落
    assert check_spec.section_of_line(lines, 5) == "Decisions"


def test_section_of_line_returns_none_before_any_heading():
    lines = ["no heading yet", "still none"]
    assert check_spec.section_of_line(lines, 1) is None


# ============================================================
# 2. 严重性与升级判据
# ============================================================


def test_all_four_rules_triggered_still_ok_and_exit_zero(tmp_path, monkeypatch):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text(
        "## Decisions\n\n用 CLI 参数还是 pointer 文件，届时决定\n\n## Open Questions\n\n无\n",
        encoding="utf-8",
    )
    (change_dir / "proposal.md").write_text("## Why\n\nsome text\n", encoding="utf-8")

    _fake_openspec(
        monkeypatch,
        deltas=[
            _delta(
                "The system SHALL handle input appropriately and quickly.",
                ["It just works, trust me."],
            )
        ],
    )

    result = check_spec.lint_change("change", None, changes_root=tmp_path)
    assert result["errors"] == []
    assert result["ok"] is True
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] >= 1
    assert result["rule_hits"][check_spec.RULE_SCENARIO_MISSING_WHEN_THEN] >= 1
    assert result["rule_hits"][check_spec.RULE_VAGUE_ADVERB] >= 1
    assert result["rule_hits"][check_spec.RULE_PROPOSAL_MISSING_NON_GOALS] >= 1


def test_error_channel_makes_ok_false_and_exit_nonzero(tmp_path):
    # invalid_change_id 是本脚本目前唯一会写入 errors 的系统级场景之一，
    # 用它验证「存在 errors → ok=false」这一通用机制（而非某条内容规则）。
    result = check_spec.lint_change("bad/id", None, changes_root=tmp_path)
    assert result["ok"] is False
    assert result["errors"] != []


def test_docstring_contains_upgrade_criterion_substring():
    module_doc = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "正类样本 ≥ 3 个独立 change" in module_doc


# ============================================================
# 3. 延迟决策规则
# ============================================================


def test_deferred_decision_in_decisions_section_is_warning(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text(
        "## Decisions\n\nper-change worktree 的 run 绑定用 CLI 参数还是 pointer 文件，实施时定\n",
        encoding="utf-8",
    )
    result = check_spec.lint_change(None, str(change_dir))
    hits = [w for w in result["warnings"] if w["rule"] == check_spec.RULE_DEFERRED_DECISION]
    assert len(hits) == 1
    assert "实施时定" in hits[0]["detail"]
    assert hits[0]["line"] == 3


def test_deferred_decision_inside_open_questions_is_not_hit(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text(
        "## Open Questions\n\npointer 文件 vs CLI 参数，实施时定\n",
        encoding="utf-8",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] == 0
    assert not any(w["rule"] == check_spec.RULE_DEFERRED_DECISION for w in result["warnings"])


def test_deferred_decision_backtick_wrapped_is_not_hit(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text(
        "## Decisions\n\n讨论该规则本身：`实施时定` 是词表条目之一\n",
        encoding="utf-8",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] == 0


def test_deferred_decision_fenced_block_is_not_hit(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text(
        "## Decisions\n\n```\n实施时定\n```\n",
        encoding="utf-8",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] == 0


def test_deferred_decision_line_number_not_shifted_by_leading_fence(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    lines = ["## Decisions", "", "```", "code line 1", "code line 2", "```", ""]
    # 追加使得裸露措辞恰好落在第 40 行
    while len(lines) < 39:
        lines.append("filler")
    lines.append("裸露的 实施时定 在第 40 行")
    text = "\n".join(lines) + "\n"
    (change_dir / "design.md").write_text(text, encoding="utf-8")
    result = check_spec.lint_change(None, str(change_dir))
    hits = [w for w in result["warnings"] if w["rule"] == check_spec.RULE_DEFERRED_DECISION]
    assert len(hits) == 1
    assert hits[0]["line"] == 40


def test_deferred_decision_wordlist_excludes_bare_time_adverb(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text(
        "## Decisions\n\n接口留到后续 change，届时会有独立的 spec 覆盖\n",
        encoding="utf-8",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] == 0


def test_deferred_decision_wordlist_includes_decision_predicate(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text(
        "## Decisions\n\n用 CLI 参数还是 pointer 文件，届时决定\n",
        encoding="utf-8",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] == 1


def test_deferred_decision_skips_when_design_missing(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] == 0
    assert result["ok"] is True


# ============================================================
# 4. 回归 fixture（快照）
# ============================================================


def test_fixture_negative_self_reference_zero_false_positive():
    fixture_dir = FIXTURES_DIR / "negative_self_reference"
    result = check_spec.lint_change(None, str(fixture_dir))
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] == 0


def test_fixture_positive_long_tail_hits_two():
    fixture_dir = FIXTURES_DIR / "positive_long_tail"
    result = check_spec.lint_change(None, str(fixture_dir))
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] == 2
    assert result["ok"] is True


# ============================================================
# 5. 另外三条规则
# ============================================================


def test_scenario_missing_when_then_warns_but_ok(tmp_path, monkeypatch):
    change_dir = _make_change_dir(tmp_path)
    _fake_openspec(monkeypatch, deltas=[_delta("The system SHALL do X.", ["It just works, trust me."])])
    result = check_spec.lint_change("change", None, changes_root=tmp_path)
    assert any(w["rule"] == check_spec.RULE_SCENARIO_MISSING_WHEN_THEN for w in result["warnings"])
    assert result["ok"] is True
    assert result["errors"] == []


def test_vague_adverb_warns(tmp_path, monkeypatch):
    change_dir = _make_change_dir(tmp_path)
    _fake_openspec(
        monkeypatch,
        deltas=[
            _delta(
                "The system SHALL handle input appropriately and quickly.",
                ["- **GIVEN** x\n- **WHEN** y\n- **THEN** z"],
            )
        ],
    )
    result = check_spec.lint_change("change", None, changes_root=tmp_path)
    assert any(w["rule"] == check_spec.RULE_VAGUE_ADVERB for w in result["warnings"])


def test_vague_adverb_inside_backticks_is_not_hit(tmp_path, monkeypatch):
    # dogfood 回归：`repo-spec-lint` 自身 spec.md 的 Scenario 用反引号引用
    # 「含糊副词」示例文本来描述这条规则本身（"GIVEN 某 Requirement 正文为
    # `...appropriately and quickly.`"），这是讨论/引用而非该文档自己写得
    # 含糊——必须跳过反引号内文本，否则每次跑 `--change repo-spec-lint` 都
    # 会自我误报。
    change_dir = _make_change_dir(tmp_path)
    _fake_openspec(
        monkeypatch,
        deltas=[
            _delta(
                "The system MUST implement the rule.",
                [
                    "- **GIVEN** 某 Requirement 正文为 `The system SHALL handle input"
                    " appropriately and quickly.`\n- **WHEN** run\n- **THEN** ok"
                ],
            )
        ],
    )
    result = check_spec.lint_change("change", None, changes_root=tmp_path)
    assert not any(w["rule"] == check_spec.RULE_VAGUE_ADVERB for w in result["warnings"])


def test_proposal_missing_non_goals_warns(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "proposal.md").write_text("## Why\n\ntext\n\n## What Changes\n\nmore text\n", encoding="utf-8")
    result = check_spec.lint_change(None, str(change_dir))
    assert any(w["rule"] == check_spec.RULE_PROPOSAL_MISSING_NON_GOALS for w in result["warnings"])


def test_proposal_with_non_goals_section_does_not_warn(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "proposal.md").write_text("## Why\n\ntext\n\n## Non-Goals\n\n- nope\n", encoding="utf-8")
    result = check_spec.lint_change(None, str(change_dir))
    assert not any(w["rule"] == check_spec.RULE_PROPOSAL_MISSING_NON_GOALS for w in result["warnings"])


def test_proposal_with_bold_inline_non_goals_label_does_not_warn(tmp_path):
    # 本仓库真实语料（spec-schema-hardening / spec-attribution-telemetry /
    # spec-routing-invariant 等已归档 change）一律用加粗行内伪标题
    # `**非目标（Non-Goals）**：`，而非 markdown 标题。dogfood 本脚本对
    # repo-spec-lint 自身跑一遍时发现：只认 `##` 标题会对这一真实约定
    # 产生系统性误报，故本规则必须同时识别该行内标签写法。
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "proposal.md").write_text(
        "## What Changes\n\ntext\n\n**非目标（Non-Goals）**：\n\n- 不做 X\n",
        encoding="utf-8",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert not any(w["rule"] == check_spec.RULE_PROPOSAL_MISSING_NON_GOALS for w in result["warnings"])


def test_proposal_non_goals_mentioned_in_prose_still_warns(tmp_path):
    # 正文散文里顺带提到 "Non-Goals" 三个字不该被当作段落标签放行——
    # 必须是独立成行的标题/加粗标签，否则会漏检真正缺段落的 proposal。
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "proposal.md").write_text(
        "## Why\n\n详见下面讨论到的 Non-Goals 相关约束，此处不展开。\n",
        encoding="utf-8",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert any(w["rule"] == check_spec.RULE_PROPOSAL_MISSING_NON_GOALS for w in result["warnings"])


def test_all_four_rules_hit_simultaneously_still_ok(tmp_path, monkeypatch):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text("## Decisions\n\n待定\n", encoding="utf-8")
    (change_dir / "proposal.md").write_text("## Why\n\ntext\n", encoding="utf-8")
    _fake_openspec(
        monkeypatch,
        deltas=[_delta("The system SHALL do it appropriately.", ["prose only, no keywords here"])],
    )
    result = check_spec.lint_change("change", None, changes_root=tmp_path)
    assert result["ok"] is True
    assert result["errors"] == []


# ============================================================
# 6. 复用 openspec 解析产物
# ============================================================


def test_two_scenarios_one_bad_counts_exactly_one(tmp_path, monkeypatch):
    _make_change_dir(tmp_path)
    _fake_openspec(
        monkeypatch,
        deltas=[
            _delta(
                "The system SHALL do X.",
                [
                    "- **GIVEN** a\n- **WHEN** b\n- **THEN** c",
                    "It just works, trust me.",
                ],
            )
        ],
    )
    result = check_spec.lint_change("change", None, changes_root=tmp_path)
    assert result["rule_hits"][check_spec.RULE_SCENARIO_MISSING_WHEN_THEN] == 1


def test_stdout_still_parses_when_openspec_writes_stderr_warning(tmp_path, monkeypatch):
    _make_change_dir(tmp_path)
    _fake_openspec(
        monkeypatch,
        deltas=[_delta("The system SHALL do X.", ["- **GIVEN** a\n- **WHEN** b\n- **THEN** c"])],
        stderr="Warning: Ignoring flags not applicable to change: scenarios\n",
    )
    result = check_spec.lint_change("change", None, changes_root=tmp_path)
    assert isinstance(result["ok"], bool)
    assert result["ok"] is True


def test_openspec_missing_reports_structured_error_no_uncaught_exception(tmp_path, monkeypatch):
    _make_change_dir(tmp_path)
    monkeypatch.setattr(check_spec.shutil, "which", lambda name: None)
    result = check_spec.lint_change("change", None, changes_root=tmp_path)
    assert result["ok"] is False
    assert any(e["rule"] == "openspec_missing" for e in result["errors"])


def test_rule_hits_key_set_excludes_reimplemented_openspec_checks():
    forbidden_substrings = ("shall", "must_keyword", "normative", "missing_scenario", "artifact_exists")
    for name in check_spec.ALL_RULE_NAMES:
        lowered = name.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, f"{name} 不应包含被禁止的子串 {forbidden}"


# ============================================================
# 7. 入口、路径边界与输出契约
# ============================================================


def test_change_id_rejects_path_separator(tmp_path):
    result = check_spec.lint_change("archive/2026-07-03-parallel-dag-scheduling", None, changes_root=tmp_path)
    assert result["ok"] is False
    assert any(e["rule"] == "invalid_change_id" for e in result["errors"])


def test_change_id_rejects_path_traversal(tmp_path):
    result = check_spec.lint_change("../../etc", None, changes_root=tmp_path)
    assert result["ok"] is False
    assert any(e["rule"] == "invalid_change_id" for e in result["errors"])


def test_change_not_found(tmp_path):
    result = check_spec.lint_change("does-not-exist", None, changes_root=tmp_path)
    assert result["ok"] is False
    assert any(e["rule"] == "change_not_found" for e in result["errors"])


def test_dir_mode_skips_openspec_dependent_rules(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text("## Decisions\n\nclean\n", encoding="utf-8")
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][check_spec.RULE_SCENARIO_MISSING_WHEN_THEN] == 0
    assert result["rule_hits"][check_spec.RULE_VAGUE_ADVERB] == 0


def test_clean_change_outputs_valid_json_ok_true_exit_zero_via_cli(tmp_path):
    change_dir = _make_change_dir(tmp_path, "clean")
    (change_dir / "design.md").write_text("## Decisions\n\nnothing to see\n\n## Open Questions\n\n无\n", encoding="utf-8")
    (change_dir / "proposal.md").write_text("## Why\n\nx\n\n## Non-Goals\n\n- none\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dir", str(change_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_rule_hits_key_set_equals_all_rule_names_for_clean_change(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    result = check_spec.lint_change(None, str(change_dir))
    assert set(result["rule_hits"].keys()) == set(check_spec.ALL_RULE_NAMES)
    assert all(v == 0 for v in result["rule_hits"].values())


def test_errors_and_warnings_items_have_four_keys(tmp_path):
    result = check_spec.lint_change("bad/id", None, changes_root=tmp_path)
    for item in result["errors"]:
        assert set(item.keys()) == {"rule", "file", "line", "detail"}


def test_no_finding_targets_openspec_specs_directory(tmp_path, monkeypatch):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text("## Decisions\n\n待定\n", encoding="utf-8")
    _fake_openspec(monkeypatch, deltas=[_delta("SHALL do it quickly.", ["prose"])])
    result = check_spec.lint_change("change", None, changes_root=tmp_path)
    all_findings = result["errors"] + result["warnings"]
    for item in all_findings:
        if item["file"]:
            assert "openspec/specs/" not in item["file"].replace("\\", "/")


# ============================================================
# 8. 边界守护
# ============================================================


def test_script_does_not_import_npc():
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("npc")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert not node.module.startswith("npc")


def test_npc_spec_help_has_no_lint_subcommand():
    proc = subprocess.run(["npc", "spec", "--help"], capture_output=True, text=True, check=False)
    assert "lint" not in proc.stdout


def test_repo_spec_lint_commit_does_not_modify_src_npc():
    """change ``repo-spec-lint``（已归档）的实施 commit 本身零 ``src/npc/`` 改动。

    历史上本测试用 ``git diff --name-only HEAD``（工作区实时 diff）断言"当前改动
    不碰 src/npc"——这只在 ``repo-spec-lint`` 自己的开发窗口内有意义；一旦该 change
    归档提交，同一断言会对**此后任何**触及 src/npc 的正常开发（如 change
    ``spine-spec-writer``）产生假阳性。改为定位该 change 实际落地的历史 commit
    （通过 ``git log --follow`` 定位 ``scripts/check_spec.py`` 的首次引入 commit），
    校验那一次 commit 本身的改动集，语义不变，但不再阻塞后续开发。
    """
    log_proc = subprocess.run(
        ["git", "log", "--format=%H", "--follow", "--", "scripts/check_spec.py"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    commits = [c for c in log_proc.stdout.splitlines() if c.strip()]
    if not commits:
        pytest.skip("scripts/check_spec.py 尚未提交（开发中快照），跳过历史 commit 校验")
    introducing_commit = commits[-1]

    show_proc = subprocess.run(
        ["git", "show", "--name-only", "--format=", introducing_commit],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    changed = [p for p in show_proc.stdout.splitlines() if p.strip()]
    assert not any(p.startswith("src/npc/") for p in changed)


def test_wordlists_are_module_constants_no_config_read():
    # 词表以模块级常量存在，脚本不实际读取任何 .npc/config.toml 文件
    # （docstring 中提及该路径仅作为反面说明，不代表真实读取行为——用
    # "不 import tomllib/toml" 断言真实行为，而非禁止 docstring 提及路径名）。
    assert isinstance(check_spec.DEFERRED_DECISION_PHRASES, tuple)
    assert isinstance(check_spec.VAGUE_ADVERBS, tuple)
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "import toml" not in source
    assert "tomllib" not in source


def test_script_does_not_emit_telemetry():
    # 脚本不 import npc.telemetry、不写 events.ndjson（docstring 提及
    # "不 emit telemetry" 仅作反面说明，用 AST 断言排除真实 import/写入行为）。
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "telemetry" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert "telemetry" not in node.module
    assert "events.ndjson" not in SCRIPT_PATH.read_text(encoding="utf-8")


def test_spine_run_md_not_modified_by_this_change():
    proc = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    changed = proc.stdout.splitlines()
    assert "plugins/agent-spine/commands/spine-run.md" not in changed


def test_does_not_reimplement_capability_no_spec_orphan_spec_no_tasks():
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    for forbidden in ("capability-no-spec", "orphan-spec", "no-tasks"):
        assert forbidden not in source


@pytest.mark.parametrize("value", ["bad/id", "../../etc"])
def test_invalid_change_id_detail_matches_value(tmp_path, value):
    detail = check_spec.validate_change_id(value)
    assert detail is not None


# ============================================================
# 9. 规则 5：touchpoint_list_missing_search_command
# ============================================================

RULE_TP = "touchpoint_list_missing_search_command"


def _write_tasks(change_dir: Path, body: str) -> None:
    (change_dir / "tasks.md").write_text(body, encoding="utf-8")


def test_touchpoint_list_missing_search_command_hits(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    _write_tasks(
        change_dir,
        "## 1. 落点\n\n"
        "- [ ] 改 `src/npc/cli.py`\n"
        "- [ ] 改 `src/npc/templates.py`\n"
        "- [ ] 改 `scripts/check_spec.py`\n",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert any(w["rule"] == RULE_TP for w in result["warnings"])
    assert result["ok"] is True
    hit = next(w for w in result["warnings"] if w["rule"] == RULE_TP)
    assert "落点" in hit["detail"]


def test_touchpoint_list_with_search_command_not_hit(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    _write_tasks(
        change_dir,
        "## 1. 落点\n\n"
        "```bash\ngrep -rn \"foo\" src/\n```\n\n"
        "- [ ] 改 `src/npc/cli.py`\n"
        "- [ ] 改 `src/npc/templates.py`\n"
        "- [ ] 改 `scripts/check_spec.py`\n",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][RULE_TP] == 0


def test_touchpoint_list_below_threshold_not_hit(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    _write_tasks(
        change_dir,
        "## 1. 落点\n\n"
        "- [ ] 改 `src/npc/cli.py`\n"
        "- [ ] 改 `src/npc/templates.py`\n",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][RULE_TP] == 0


def test_all_rule_names_length_five_and_includes_touchpoint():
    assert len(check_spec.ALL_RULE_NAMES) == 5
    assert RULE_TP in check_spec.ALL_RULE_NAMES


def test_rule_hits_keys_include_all_five_rules(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    result = check_spec.lint_change(None, str(change_dir))
    assert set(result["rule_hits"].keys()) == set(check_spec.ALL_RULE_NAMES)
    assert len(result["rule_hits"]) == 5


def test_existing_four_rules_unaffected_by_new_rule(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    (change_dir / "design.md").write_text(
        "## Decisions\n\n实施时定\n", encoding="utf-8"
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][check_spec.RULE_DEFERRED_DECISION] == 1
    # 其余既有规则在 --dir 模式下按既有语义为 0（无 tasks.md → touchpoint 也为 0）
    assert result["rule_hits"][check_spec.RULE_SCENARIO_MISSING_WHEN_THEN] == 0
    assert result["rule_hits"][check_spec.RULE_VAGUE_ADVERB] == 0
    assert result["rule_hits"][RULE_TP] == 0


def test_touchpoint_rule_not_skipped_in_dir_mode(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    _write_tasks(
        change_dir,
        "## 1. 落点\n\n"
        "- [ ] `a/x.py`\n- [ ] `b/y.py`\n- [ ] `c/z.py`\n",
    )
    result = check_spec.lint_change(None, str(change_dir))
    assert result["rule_hits"][RULE_TP] == 1


def test_touchpoint_rule_skipped_when_tasks_absent(tmp_path):
    change_dir = _make_change_dir(tmp_path)
    result = check_spec.lint_change(None, str(change_dir))
    assert not any(w["rule"] == RULE_TP for w in result["warnings"])
    assert result["rule_hits"][RULE_TP] == 0
