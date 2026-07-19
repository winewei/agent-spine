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


# ============================================================
# v1.2 own_commits：并行 worktree 下按本 change 自身提交出 git show
# ============================================================


def _write_state_with_commits(p, change_id: str = "add-foo") -> None:
    state = {
        "progress": [
            {
                "seq": 1,
                "change_id": change_id,
                "implement_commit": "aaa1111",
                "phases": {
                    "fix-r1": {"commit": "bbb2222"},
                    "fix-r2": {"commit": "ccc3333"},
                },
            }
        ]
    }
    p.state_json.write_text(json.dumps(state), encoding="utf-8")


def test_own_commits_orders_implement_then_fix_rounds():
    entry = {
        "implement_commit": "aaa1111",
        "phases": {"fix-r2": {"commit": "ccc3333"}, "fix-r1": {"commit": "bbb2222"}},
    }
    assert _focus._own_commits(entry, 3) == ["aaa1111", "bbb2222", "ccc3333"]
    assert _focus._own_commits(entry, 1) == ["aaa1111"]
    assert _focus._own_commits(None, 3) == []


def test_render_round_0_uses_own_commit_show(env_setup, capsys, make_args, tmp_path):
    _write_state_with_commits(env_setup)
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
    capsys.readouterr()
    text = out.read_text()
    assert "git --no-pager show aaa1111" in text
    assert "HEAD~1..HEAD" not in text


def test_render_round_n_includes_current_fix_commit(env_setup, capsys, make_args, tmp_path):
    """review-rN 起跑时 fix-rN 已落地，git show 列表必须包含 fix-rN 的 commit。"""
    _write_state_with_commits(env_setup)
    out = tmp_path / "focus.md"
    _focus.render(
        make_args(
            round_n=2,
            change_id="add-foo",
            implement_commit="aaa1111",
            output=str(out),
            project_context=None,
        )
    )
    capsys.readouterr()
    text = out.read_text()
    assert "git --no-pager show aaa1111" in text
    assert "git --no-pager show bbb2222" in text
    assert "git --no-pager show ccc3333" in text
    assert "aaa1111~1..HEAD" not in text


# ============================================================
# v1.7 AGENTS.md fallback
# ============================================================


def test_load_project_context_agents_md_fallback(fake_repo: Path):
    (fake_repo / "AGENTS.md").write_text(
        "# 项目\n\n## 评审重点\n\n- 来自 AGENTS 的约束\n", encoding="utf-8"
    )
    text, label = _focus.load_project_context(fake_repo)
    assert label == "AGENTS.md"
    assert "来自 AGENTS 的约束" in text


def test_load_project_context_claude_md_wins_over_agents_md(fake_repo: Path):
    (fake_repo / "CLAUDE.md").write_text(
        "## 评审重点\n\n- 来自 CLAUDE 的约束\n", encoding="utf-8"
    )
    (fake_repo / "AGENTS.md").write_text(
        "## 评审重点\n\n- 来自 AGENTS 的约束\n", encoding="utf-8"
    )
    text, label = _focus.load_project_context(fake_repo)
    assert label == "CLAUDE.md"
    assert "来自 CLAUDE 的约束" in text
    assert "来自 AGENTS 的约束" not in text
