"""agent 模块测试：prompt render + spawn-prompt（v1.0.0）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import agent as _agent
from npc import state as _state


# ============================================================
# Helpers
# ============================================================


def _bootstrap(env_setup, make_args, capsys, *change_ids: str) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids))))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def _read_emit(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _write_review(base: Path, round_n: int, findings: list[dict], verdict: str = "changes-requested") -> Path:
    rv = base / f"round-{round_n}.review.json"
    rv.write_text(
        json.dumps({"verdict": verdict, "findings": findings}, ensure_ascii=False),
        encoding="utf-8",
    )
    return rv


# ============================================================
# resolve helpers
# ============================================================


def test_resolve_seq_by_change_id(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo", "add-bar", "add-baz")
    state = _state.read_state(env_setup.state_json)
    assert _agent._resolve_seq(state, "add-bar", None) == 2
    assert _agent._resolve_seq(state, "add-baz", 3) == 3


def test_resolve_seq_unknown_change_id(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    state = _state.read_state(env_setup.state_json)
    with pytest.raises(ValueError, match="不在 STATE_JSON.progress"):
        _agent._resolve_seq(state, "add-nope", None)


def test_resolve_seq_mismatched_explicit(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo", "add-bar")
    state = _state.read_state(env_setup.state_json)
    with pytest.raises(ValueError, match="不一致"):
        _agent._resolve_seq(state, "add-foo", 2)


# ============================================================
# prompt render — implement
# ============================================================


def test_prompt_render_implement_writes_file(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    out = _read_emit(capsys)
    assert out["ok"] is True
    assert out["phase"] == "implement"
    assert out["seq"] == 1
    assert out["template_version"]
    assert out["bytes"] > 0

    p = Path(out["output"])
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "add-foo" in text
    assert "RESULT:" in text
    assert p.name == "implement.prompt.md"


def test_prompt_render_implement_rejects_round(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.prompt_render(
            make_args(
                phase="implement",
                change_id="add-foo",
                seq=None,
                round_n=2,
                output=None,
                review_json=None,
                implement_commit=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "round_not_allowed"


def test_prompt_render_explicit_output_path(env_setup, make_args, capsys, tmp_path):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    out_path = tmp_path / "custom.md"
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=str(out_path),
            review_json=None,
            implement_commit=None,
        )
    )
    payload = _read_emit(capsys)
    assert Path(payload["output"]) == out_path
    assert out_path.exists()


# ============================================================
# prompt render — fix
# ============================================================


def test_prompt_render_fix_requires_round(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.prompt_render(
            make_args(
                phase="fix",
                change_id="add-foo",
                seq=None,
                round_n=None,
                output=None,
                review_json=None,
                implement_commit=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "missing_round"


def test_prompt_render_fix_requires_implement_commit(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    # 不在 state 里写 implement_commit、也不传 --implement-commit
    with pytest.raises(SystemExit):
        _agent.prompt_render(
            make_args(
                phase="fix",
                change_id="add-foo",
                seq=None,
                round_n=1,
                output=None,
                review_json=None,
                implement_commit=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "missing_implement_commit"


def test_prompt_render_fix_missing_review(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.prompt_render(
            make_args(
                phase="fix",
                change_id="add-foo",
                seq=None,
                round_n=1,
                output=None,
                review_json=None,
                implement_commit="abc1234",
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "review_not_found"


def test_prompt_render_fix_full_flow(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    # 找到 base
    state = _state.read_state(env_setup.state_json)
    base = Path(state["progress"][0]["base"])

    # 写 round-0.review.json（fix-r1 需要）
    findings = [
        {
            "id": "F1",
            "severity": "high",
            "category": "validation",
            "title": "Missing input check",
            "file": "src/foo.py",
            "line_range": "42-58",
            "detail": "no validation on x",
            "recommendation": "add type check",
            "in_scope": True,
        },
        {
            "id": "F2",
            "severity": "medium",
            "category": "style",
            "title": "trailing whitespace",
            "file": "src/bar.py",
            "line_range": "1-1",
            "detail": "ws",
            "recommendation": "remove",
            "in_scope": True,
        },
    ]
    _write_review(base, 0, findings)

    # 把 categories_seen / blocking_trend / implement_commit 注入 state
    def _mut(s: dict) -> None:
        s["progress"][0]["implement_commit"] = "deadbeef"
        s["progress"][0]["categories_seen"] = ["validation", "style"]
        s["progress"][0]["blocking_trend"] = [3]

    _state.update_state(env_setup.state_json, env_setup.state_md, _mut)

    _agent.prompt_render(
        make_args(
            phase="fix",
            change_id="add-foo",
            seq=None,
            round_n=1,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    out = _read_emit(capsys)
    assert out["ok"] is True
    assert out["phase"] == "fix"
    assert out["round"] == 1
    assert out["blocking_count"] == 1  # 仅 F1（critical/high + in_scope）
    assert out["implement_commit"] == "deadbeef"

    text = Path(out["output"]).read_text(encoding="utf-8")
    # 精确断言：blocking findings 段（## Review Findings 与 ## 修复历史 之间）
    # 应该只含 in_scope blocking 的 F1，不含 advisory 的 F2
    findings_start = text.index("## Review Findings")
    findings_end = text.index("## 修复历史")
    findings_section = text[findings_start:findings_end]
    assert "F1" in findings_section
    assert "F2" not in findings_section  # advisory 不进入 Fixer prompt 的 findings 段
    assert "Missing input check" in findings_section
    # 其它运行时变量
    assert "deadbeef" in text
    assert "FIX_ROUND=1" in text
    assert "validation, style" in text
    assert "fix(add-foo): review round 1" in text


def test_prompt_render_fix_explicit_implement_commit_overrides_state(
    env_setup, make_args, capsys
):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    state = _state.read_state(env_setup.state_json)
    base = Path(state["progress"][0]["base"])
    _write_review(base, 0, [])

    _agent.prompt_render(
        make_args(
            phase="fix",
            change_id="add-foo",
            seq=None,
            round_n=1,
            output=None,
            review_json=None,
            implement_commit="override-hash",
        )
    )
    out = _read_emit(capsys)
    assert out["implement_commit"] == "override-hash"


# ============================================================
# spawn-prompt
# ============================================================


def test_spawn_prompt_basic_implement(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")

    # 先 render
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    rendered = _read_emit(capsys)

    # 然后 spawn-prompt
    _agent.spawn_prompt(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            prompt_file=None,
            extension=None,
            extension_inline=None,
        )
    )
    out = _read_emit(capsys)

    assert out["ok"] is True
    assert out["prompt_file"] == rendered["output"]
    assert "prompt" in out
    assert rendered["output"] in out["prompt"]
    assert out["has_extension"] is False
    # 引导语应该极短（不携带模板本体）。
    # 实测 ~600 bytes（含 prompt 文件绝对路径，路径越长引导语越长）；
    # 给一个宽松上限，确保不会回退到内联完整模板的程度。
    assert out["bytes"] < 1000


def test_spawn_prompt_missing_prompt_file(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.spawn_prompt(
            make_args(
                phase="implement",
                change_id="add-foo",
                seq=None,
                round_n=None,
                prompt_file=None,
                extension=None,
                extension_inline=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "prompt_file_not_found"


def test_spawn_prompt_with_inline_extension(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    capsys.readouterr()

    _agent.spawn_prompt(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            prompt_file=None,
            extension=None,
            extension_inline="- 跑 make race-test",
        )
    )
    out = _read_emit(capsys)
    assert out["has_extension"] is True
    assert "make race-test" in out["prompt"]


def test_spawn_prompt_extension_file(env_setup, make_args, capsys, tmp_path):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    capsys.readouterr()

    ext_file = tmp_path / "ext.md"
    ext_file.write_text("- 额外指令 A\n- 额外指令 B")

    _agent.spawn_prompt(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            prompt_file=None,
            extension=str(ext_file),
            extension_inline=None,
        )
    )
    out = _read_emit(capsys)
    assert "额外指令 A" in out["prompt"]
    assert "额外指令 B" in out["prompt"]


def test_spawn_prompt_extension_conflict(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    _agent.prompt_render(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            output=None,
            review_json=None,
            implement_commit=None,
        )
    )
    capsys.readouterr()

    with pytest.raises(SystemExit):
        _agent.spawn_prompt(
            make_args(
                phase="implement",
                change_id="add-foo",
                seq=None,
                round_n=None,
                prompt_file=None,
                extension="/some/path",
                extension_inline="inline text",
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "conflicting_args"


def test_spawn_prompt_fix_requires_round(env_setup, make_args, capsys):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    with pytest.raises(SystemExit):
        _agent.spawn_prompt(
            make_args(
                phase="fix",
                change_id="add-foo",
                seq=None,
                round_n=None,
                prompt_file=None,
                extension=None,
                extension_inline=None,
            )
        )
    err = _read_emit(capsys)
    assert err["error"] == "missing_round"


def test_spawn_prompt_custom_prompt_file(env_setup, make_args, capsys, tmp_path):
    _bootstrap(env_setup, make_args, capsys, "add-foo")
    custom = tmp_path / "my-prompt.md"
    custom.write_text("hi")

    _agent.spawn_prompt(
        make_args(
            phase="implement",
            change_id="add-foo",
            seq=None,
            round_n=None,
            prompt_file=str(custom),
            extension=None,
            extension_inline=None,
        )
    )
    out = _read_emit(capsys)
    assert Path(out["prompt_file"]) == custom.resolve()
