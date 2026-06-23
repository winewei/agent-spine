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
