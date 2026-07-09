"""focus 模块测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import focus as _focus


def test_extract_section_hit():
    md = """# Top

普通内容

## 评审重点

- 点 A
- 点 B

## 其他

不相关
"""
    out = _focus._extract_section(md, ["评审重点"])
    assert out is not None
    assert "点 A" in out
    assert "其他" not in out


def test_extract_section_case_insensitive_english():
    md = """## Review Context

context body line

## Next
"""
    out = _focus._extract_section(md, ["review context"])
    assert out is not None
    assert "context body line" in out


def test_extract_section_no_match():
    md = "# Foo\n\nbar baz\n"
    assert _focus._extract_section(md, ["评审重点"]) is None


def test_load_project_context_default(fake_repo: Path):
    ctx, src = _focus.load_project_context(fake_repo)
    assert src == "default"
    assert "默认" in ctx


def test_load_project_context_from_openspec(fake_repo: Path):
    (fake_repo / "openspec").mkdir()
    (fake_repo / "openspec" / "project.md").write_text(
        "# Project\n\n## 评审重点\n\n这是项目特有的约束\n"
    )
    ctx, src = _focus.load_project_context(fake_repo)
    assert src == "openspec/project.md"
    assert "项目特有" in ctx


def test_load_project_context_both(fake_repo: Path):
    (fake_repo / "openspec").mkdir()
    (fake_repo / "openspec" / "project.md").write_text("# X\n\n## 威胁模型\n\nABCD\n")
    (fake_repo / "CLAUDE.md").write_text("# Y\n\n## 评审重点\n\nEFGH\n")
    ctx, src = _focus.load_project_context(fake_repo)
    assert src == "both"
    assert "ABCD" in ctx and "EFGH" in ctx


def test_load_project_context_override(fake_repo: Path, tmp_path: Path):
    override = tmp_path / "ctx.md"
    override.write_text("OVERRIDE CONTENT")
    ctx, src = _focus.load_project_context(fake_repo, override)
    assert src == "override"
    assert ctx == "OVERRIDE CONTENT"


def test_render_round_0(env_setup, capsys, make_args, tmp_path):
    out = tmp_path / "focus.md"
    _focus.render(
        make_args(
            round_n=0,
            change_id="add-foo",
            implement_commit=None,
            output=str(out),
            project_context=None,
        )
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["project_context_source"] == "default"
    text = out.read_text()
    assert "add-foo" in text
    assert "Round 0" not in text  # Round 0 模板不带 "第 N 轮" 段
    assert "git --no-pager diff HEAD~1..HEAD" in text


def test_render_round_n_requires_implement_commit(env_setup, capsys, make_args, tmp_path):
    out = tmp_path / "f.md"
    with pytest.raises(SystemExit):
        _focus.render(
            make_args(
                round_n=2,
                change_id="add-foo",
                implement_commit=None,
                output=str(out),
                project_context=None,
            )
        )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "missing_implement_commit"


_SPEC_ATTRIBUTION_ENUM_VALUES = [
    "spec-silent",
    "spec-ambiguous",
    "spec-contradicted",
    "impl-deviation",
]


def test_round_0_and_round_n_both_include_spec_attribution_contract(
    env_setup, capsys, make_args, tmp_path
):
    """回归 fix round 1 F1：Round 0 与 Round N 的输出要求必须同源，都得包含
    spec_attribution 字段名与全部四个枚举值，避免 re-review 场景 reviewer 不知道
    要填该字段导致 telemetry 信号缺失。"""
    out0 = tmp_path / "round0.md"
    _focus.render(
        make_args(
            round_n=0,
            change_id="add-foo",
            implement_commit=None,
            output=str(out0),
            project_context=None,
        )
    )
    capsys.readouterr()
    text0 = out0.read_text()

    outn = tmp_path / "roundn.md"
    _focus.render(
        make_args(
            round_n=2,
            change_id="add-foo",
            implement_commit="abc1234",
            output=str(outn),
            project_context=None,
        )
    )
    capsys.readouterr()
    textn = outn.read_text()

    for text in (text0, textn):
        assert "spec_attribution" in text
        for value in _SPEC_ATTRIBUTION_ENUM_VALUES:
            assert value in text


# ============================================================
# 对抗式 round-0 模板（change review-r0-adversarial-pass）
# ============================================================

_ADVERSARIAL_FORBIDDEN = [
    "proposal.md",
    "tasks.md",
    "specs/",
    "design.md",
    "openspec/project.md",
    "CLAUDE.md",
]


def test_adversarial_template_no_spec_file_refs():
    """负向测试（task 2.5）：对抗式模板 MUST NOT 含任何 spec 文件引用字样。"""
    text = _focus._adversarial_round_0_template("add-foo")
    for forbidden in _ADVERSARIAL_FORBIDDEN:
        assert forbidden not in text, f"对抗式模板不应含 {forbidden!r}"


def test_adversarial_template_no_authority_disclaimer():
    """task 2.4：pass2 变体 MUST NOT 含"与 tasks.md / design.md 决策一致"免责条款。"""
    text = _focus._adversarial_round_0_template("add-foo")
    assert "决策一致" not in text
    assert "权威决策" not in text


def test_adversarial_template_has_diff_command_and_change_id():
    text = _focus._adversarial_round_0_template("add-foo")
    assert "add-foo" in text
    assert "git --no-pager diff HEAD~1..HEAD" in text
    # 不得用裸 git diff（clean worktree 为空）：所有 "git " + "diff" 都必须带 --no-pager
    assert "git diff" not in text  # 裸 git diff 不出现


def test_adversarial_template_has_four_focus_points():
    """task 2.2 / spec Scenario：四个审查重点关键词齐备。"""
    text = _focus._adversarial_round_0_template("add-foo")
    assert "资源释放" in text or "double-free" in text
    assert "边界" in text
    assert "急切求值" in text or "短路" in text
    assert "并发" in text or "生命周期" in text


def test_adversarial_template_fixed_attribution_and_in_scope_instructions():
    """task 2.3 / D5：固定指令 spec-silent + in_scope 默认 true。"""
    text = _focus._adversarial_round_0_template("add-foo")
    assert "spec-silent" in text
    assert "in_scope=true" in text


def test_adversarial_template_retains_schema_output_and_spec_attribution_semantics():
    """task 2.4：pass2 仍保留 JSON schema 输出要求与 spec_attribution 四值语义。"""
    text = _focus._adversarial_round_0_template("add-foo")
    assert "output-schema" in text
    for value in _SPEC_ATTRIBUTION_ENUM_VALUES:
        assert value in text


def test_output_requirements_block_disclaimer_toggle():
    """参数化单一来源：True 保留免责条款（现文案），False 去除且不含 tasks/design 字样。"""
    with_disclaimer = _focus._output_requirements_block(authority_disclaimer=True)
    without = _focus._output_requirements_block(authority_disclaimer=False)
    assert "与 tasks.md / design.md 决策一致的实现不作为 finding 报告" in with_disclaimer
    assert "tasks.md" not in without
    assert "design.md" not in without
    # 默认参数保持 pass1/round-N 现文案不变
    assert _focus._output_requirements_block() == with_disclaimer


def test_output_requirements_block_contains_stub_blocking_criteria():
    """task 3.1/3.2/3.3：输出要求块含 stub / 删测为 blocking 及自我辩护可疑信号判据。"""
    block = _focus._output_requirements_block()
    const = _focus.STUB_AND_TEST_TAMPERING_BLOCKING
    assert const in block
    # 三条判据要点齐备
    assert "stub" in const and "blocking" in const
    assert "占位实现" in const
    assert ("删除" in const or "注释" in const or "skip" in const) and "测试" in const
    assert "断言被弱化" in const or "断言范围被放宽" in const
    assert "多段注释自我辩护" in const


def test_round_0_and_round_n_share_stub_criteria_verbatim():
    """task 3.4 / Scenario 两轮判据同源不漂移：round 0 与 round N 渲染均逐字含同一判据常量。"""
    const = _focus.STUB_AND_TEST_TAMPERING_BLOCKING
    r0 = _focus._round_0_template("add-foo", "CTX")
    rn = _focus._round_n_template("add-foo", 2, "abc1234", "CTX")
    assert const in r0
    assert const in rn


def test_render_round_n_includes_history_hints(env_setup, capsys, make_args, tmp_path):
    out = tmp_path / "f.md"
    _focus.render(
        make_args(
            round_n=2,
            change_id="add-foo",
            implement_commit="abc1234",
            output=str(out),
            project_context=None,
        )
    )
    capsys.readouterr()
    text = out.read_text()
    assert "第 2 轮" in text
    assert "abc1234~1..HEAD" in text
    assert "Round 0 ~ Round 1 段落" in text
