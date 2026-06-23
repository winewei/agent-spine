"""templates 模块单元测试（v1.0.0）。"""

from __future__ import annotations

from npc import templates


def test_template_version_is_string():
    assert isinstance(templates.TEMPLATE_VERSION, str)
    assert templates.TEMPLATE_VERSION.count(".") >= 1


def test_render_implementer_substitutes_runtime_vars():
    text = templates.render_implementer(
        change_id="add-foo",
        base="/abs/base",
        repo_root="/abs/repo",
    )
    assert "add-foo" in text
    assert "/abs/base" in text
    assert "/abs/repo" in text
    assert "implement.summary.md" in text


def test_render_implementer_includes_dual_artifact_contract():
    text = templates.render_implementer("c", "/b", "/r")
    # 双产物契约：summary 文件路径 + RESULT 行
    assert "Write" in text
    assert "RESULT:" in text
    # summary path 必须出现在 RESULT 模板里
    assert "/b/implement.summary.md" in text


def test_render_implementer_forbids_archive_in_subagent():
    text = templates.render_implementer("c", "/b", "/r")
    assert "不要 archive" in text


def test_render_fixer_substitutes_all_runtime_vars():
    text = templates.render_fixer(
        change_id="add-foo",
        round_n=3,
        implement_commit="deadbeef",
        base="/abs/base",
        repo_root="/abs/repo",
        blocking_findings_md="## F1 — [high][validation] x\n",
        categories_seen=["validation", "edge-case"],
        blocking_trend=[5, 4, 4],
    )
    assert "add-foo" in text
    assert "deadbeef" in text
    assert "FIX_ROUND=3" in text
    assert "round-3.fix.summary.md" in text
    assert "F1" in text
    assert "validation, edge-case" in text
    assert "5 → 4 → 4" in text


def test_render_fixer_first_round_empty_history():
    text = templates.render_fixer(
        change_id="c",
        round_n=1,
        implement_commit="abc",
        base="/b",
        repo_root="/r",
        blocking_findings_md="（本轮无 in_scope blocking findings）\n",
        categories_seen=[],
        blocking_trend=[],
    )
    assert "首轮" in text  # categories_seen / blocking_trend 缺省占位


def test_render_fixer_includes_root_cause_sweep_rule():
    text = templates.render_fixer(
        "c", 1, "abc", "/b", "/r", "F1\n", ["validation"], [3]
    )
    assert "Root-cause" in text
    assert "Locations Scanned" in text


def test_render_fixer_includes_real_regression_rule():
    text = templates.render_fixer(
        "c", 1, "abc", "/b", "/r", "F1\n", ["concurrency"], [3]
    )
    assert "Real Regressions" in text
    assert "concurrency" in text
    assert "race-condition" in text


def test_render_fixer_commit_format_includes_change_id_and_round():
    text = templates.render_fixer("change-x", 7, "abc", "/b", "/r", "F1\n", [], [])
    assert 'fix(change-x): review round 7' in text


def test_render_spawn_prompt_implement_minimal():
    s = templates.render_spawn_prompt(
        phase="implement",
        change_id="add-foo",
        prompt_file="/abs/prompt.md",
    )
    assert "/abs/prompt.md" in s
    assert "add-foo" in s
    assert "RESULT" in s
    # 不能包含 §A 模板本体（这正是优化点：模板内容不能流过主 session）
    # 引导语可以"提及"双产物契约这个名词以提示 sub-agent，但不能展开成完整 schema：
    assert "Implement Summary —" not in s  # summary 文件 schema header
    assert "Key Decisions" not in s  # summary 段名
    assert "Issues Encountered" not in s  # summary 段名


def test_render_spawn_prompt_fix_includes_phrase():
    s = templates.render_spawn_prompt(
        phase="fix",
        change_id="add-foo",
        prompt_file="/abs/p.md",
    )
    assert "review findings" in s
    assert "add-foo" in s


def test_render_spawn_prompt_with_extension():
    s = templates.render_spawn_prompt(
        phase="implement",
        change_id="add-foo",
        prompt_file="/p.md",
        extension="- 跑 make race-test 而不是 go test\n- commit 前必须签名",
    )
    assert "追加约束" in s
    assert "make race-test" in s


def test_render_spawn_prompt_size_under_400_bytes_no_extension():
    """spawn-prompt 引导语应该极薄——这是 v1.0 设计目标之一。"""
    s = templates.render_spawn_prompt(
        phase="implement",
        change_id="add-foo",
        prompt_file="/some/abs/path/to/implement.prompt.md",
    )
    assert len(s.encode("utf-8")) < 400


def test_implementer_template_significantly_larger_than_spawn_prompt():
    """证明优化的实际价值：完整模板 vs 薄引导语。"""
    impl = templates.render_implementer("add-foo", "/b", "/r")
    spawn = templates.render_spawn_prompt(
        "implement", "add-foo", "/b/implement.prompt.md"
    )
    # 模板内容应该至少是引导语的 5 倍（实际是 10x+）
    assert len(impl) > 5 * len(spawn)
