"""spec_analyze.py 测试。

纯函数（markdown 解析 + 覆盖启发式 + analyze_change）用 tmp_path 造各种 change
目录布局直接断言；handler run 用 monkeypatch 注入 repo_root + tmp change 目录验
emit/退出码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import spec_analyze as _sa


# ============================================================
# 布局工厂：在 <repo>/openspec/changes/<id>/ 造 change 目录
# ============================================================


def _make_change(
    repo: Path,
    change_id: str,
    *,
    proposal: str | None = None,
    specs: dict[str, str] | None = None,
    tasks: str | None = None,
) -> Path:
    """造一个 change 目录。specs: {capability: spec.md 内容}。"""
    change_dir = repo / "openspec" / "changes" / change_id
    change_dir.mkdir(parents=True)
    if proposal is not None:
        (change_dir / "proposal.md").write_text(proposal, encoding="utf-8")
    if tasks is not None:
        (change_dir / "tasks.md").write_text(tasks, encoding="utf-8")
    if specs:
        for cap, spec_md in specs.items():
            cap_dir = change_dir / "specs" / cap
            cap_dir.mkdir(parents=True)
            (cap_dir / "spec.md").write_text(spec_md, encoding="utf-8")
    return change_dir


def _proposal(*caps: str) -> str:
    items = "\n".join(f"- `{c}`: 描述 {c}" for c in caps)
    return f"# Proposal\n\n## Capabilities\n\n### New Capabilities\n\n{items}\n"


def _spec(*reqs: str) -> str:
    body = "\n".join(
        f"### Requirement: {r}\n\n#### Scenario: s\n- WHEN x\n- THEN y\n" for r in reqs
    )
    return f"# Spec\n\n{body}"


def _tasks_open(*lines: str) -> str:
    body = "\n".join(f"- [ ] {ln}" for ln in lines)
    return f"## 1. 组名\n\n{body}\n"


def _tasks_done(*lines: str) -> str:
    body = "\n".join(f"- [x] {ln}" for ln in lines)
    return f"## 1. 组名\n\n{body}\n"


# ============================================================
# 纯函数：parse_new_capabilities
# ============================================================


def test_parse_new_capabilities_basic():
    text = _proposal("alpha-cap", "beta-cap")
    assert _sa.parse_new_capabilities(text) == ["alpha-cap", "beta-cap"]


def test_parse_new_capabilities_stops_at_next_heading():
    text = (
        "## Capabilities\n\n### New Capabilities\n\n"
        "- `keep-me`: yes\n\n"
        "### Modified Capabilities\n\n"
        "- `not-this`: no\n"
    )
    assert _sa.parse_new_capabilities(text) == ["keep-me"]


def test_parse_new_capabilities_dedup():
    text = (
        "### New Capabilities\n\n- `dup`: a\n- `dup`: b\n- `other`: c\n"
    )
    assert _sa.parse_new_capabilities(text) == ["dup", "other"]


def test_parse_new_capabilities_none_when_absent():
    assert _sa.parse_new_capabilities("# Proposal\n\nno caps section\n") == []


def test_parse_new_capabilities_extended_heading_not_matched():
    # `### New Capabilities Extended` 不是 New Capabilities 段，不应激活收集
    text = (
        "## Capabilities\n\n### New Capabilities Extended\n\n"
        "- `should-not-collect`: nope\n"
    )
    assert _sa.parse_new_capabilities(text) == []


def test_parse_new_capabilities_case_insensitive_heading():
    # 大小写标题 `### NEW CAPABILITIES` 同样激活
    text = "## Capabilities\n\n### NEW CAPABILITIES\n\n- `cap-x`: yes\n"
    assert _sa.parse_new_capabilities(text) == ["cap-x"]


def test_parse_new_capabilities_star_bullet():
    # `* \`name\`` 星号 bullet 同样收集
    text = "### New Capabilities\n\n* `star-cap`: via asterisk\n"
    assert _sa.parse_new_capabilities(text) == ["star-cap"]


# ============================================================
# 纯函数：parse_requirements
# ============================================================


def test_parse_requirements_basic():
    text = _spec("First Thing", "Second Thing")
    assert _sa.parse_requirements(text) == ["First Thing", "Second Thing"]


def test_parse_requirements_empty():
    assert _sa.parse_requirements("# Spec\n\nno requirements\n") == []


# ============================================================
# 纯函数：parse_tasks
# ============================================================


def test_parse_tasks_open_and_done():
    text = "## 1. G\n- [ ] 1.1 do a\n- [x] 1.2 did b\n"
    tasks = _sa.parse_tasks(text)
    assert len(tasks) == 2
    assert tasks[0] == {"done": False, "text": "1.1 do a"}
    assert tasks[1] == {"done": True, "text": "1.2 did b"}


def test_parse_tasks_uppercase_x():
    tasks = _sa.parse_tasks("- [X] done\n")
    assert tasks == [{"done": True, "text": "done"}]


def test_parse_tasks_ignores_non_checkbox():
    text = "## heading\nsome prose\n- bullet without box\n- [ ] real task\n"
    assert _sa.parse_tasks(text) == [{"done": False, "text": "real task"}]


def test_parse_tasks_empty():
    assert _sa.parse_tasks("## only heading\n\nprose\n") == []


def test_parse_tasks_star_bullet():
    # `* [ ]` 星号 bullet 的任务项也认
    tasks = _sa.parse_tasks("* [ ] star open\n* [x] star done\n")
    assert tasks == [
        {"done": False, "text": "star open"},
        {"done": True, "text": "star done"},
    ]


def test_parse_tasks_empty_text():
    # 复选框后无文本：text 为空字符串，仍是合法任务项
    assert _sa.parse_tasks("- [ ] \n") == [{"done": False, "text": ""}]


# ============================================================
# 纯函数：capability_mentioned_in_tasks（覆盖启发式）
# ============================================================


def test_capability_mentioned_true():
    assert _sa.capability_mentioned_in_tasks(
        "spec-analyze", "- [ ] implement spec analyze gate\n"
    )


def test_capability_mentioned_false():
    assert not _sa.capability_mentioned_in_tasks(
        "spec-analyze", "- [ ] unrelated plumbing work\n"
    )


def test_capability_mentioned_token_match():
    # 任一非平凡 token 命中即算提到
    assert _sa.capability_mentioned_in_tasks(
        "drift-gate", "- [ ] add a gate for tasks\n"
    )


def test_capability_mentioned_short_name_word_boundary():
    # 短名 "no"（无 ≥3 token，走回退分支）不应子串命中 "not"
    assert not _sa.capability_mentioned_in_tasks(
        "no", "- [ ] do not break the build\n"
    )
    # 但作为独立词时应命中
    assert _sa.capability_mentioned_in_tasks(
        "no", "- [ ] wire up no op handler\n"
    )


# ============================================================
# analyze_change：综合布局
# ============================================================


def test_analyze_healthy_no_findings(tmp_path):
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "add-feature",
        proposal=_proposal("feature-cap"),
        specs={"feature-cap": _spec("Do The Feature Cap Thing")},
        tasks=_tasks_open("1.1 implement feature-cap", "1.2 test feature-cap"),
    )
    res = _sa.analyze_change(cd)
    assert res["change"] == "add-feature"
    assert res["capabilities"] == ["feature-cap"]
    assert res["requirements_count"] == 1
    assert res["tasks_count"] == 2
    assert res["findings"] == []


def test_analyze_no_tasks_missing(tmp_path):
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("cap"),
        specs={"cap": _spec("R about cap")},
        # tasks 缺失
    )
    res = _sa.analyze_change(cd)
    kinds = {f["kind"] for f in res["findings"]}
    assert "no-tasks" in kinds
    nt = next(f for f in res["findings"] if f["kind"] == "no-tasks")
    assert nt["severity"] == "high"


def test_analyze_no_tasks_empty_file(tmp_path):
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("cap"),
        specs={"cap": _spec("R about cap")},
        tasks="## 1. group\n\njust prose, no checkboxes\n",
    )
    res = _sa.analyze_change(cd)
    assert res["tasks_count"] == 0
    assert any(f["kind"] == "no-tasks" for f in res["findings"])


def test_analyze_no_tasks_no_maybe_uncovered_noise(tmp_path):
    # tasks 缺失时只报 no-tasks，不叠加误导性的 requirement-maybe-uncovered
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("cap"),
        specs={"cap": _spec("Some Requirement")},
        # tasks 缺失
    )
    res = _sa.analyze_change(cd)
    kinds = {f["kind"] for f in res["findings"]}
    assert "no-tasks" in kinds
    assert "requirement-maybe-uncovered" not in kinds


def test_analyze_empty_tasks_no_maybe_uncovered_noise(tmp_path):
    # tasks 文件存在但无任务项：同样不叠加 requirement-maybe-uncovered
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("cap"),
        specs={"cap": _spec("Some Requirement")},
        tasks="## 1. group\n\njust prose, no checkboxes\n",
    )
    res = _sa.analyze_change(cd)
    kinds = {f["kind"] for f in res["findings"]}
    assert "no-tasks" in kinds
    assert "requirement-maybe-uncovered" not in kinds


def test_analyze_html_comment_not_parsed_as_task(tmp_path):
    # HTML 注释里的假任务条目不应被当作真任务解析
    repo = tmp_path / "repo"
    tasks = (
        "## 1. group\n\n"
        "<!-- - [ ] fake commented task -->\n"
        "- [ ] 1.1 real task\n"
    )
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("cap"),
        specs={"cap": _spec("R cap")},
        tasks=tasks,
    )
    res = _sa.analyze_change(cd)
    assert res["tasks_count"] == 1


def test_analyze_capability_no_spec(tmp_path):
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("declared-cap"),
        specs={},  # 没有对应 spec
        tasks=_tasks_open("1.1 implement declared-cap"),
    )
    res = _sa.analyze_change(cd)
    kinds = {f["kind"] for f in res["findings"]}
    assert "capability-no-spec" in kinds
    f = next(x for x in res["findings"] if x["kind"] == "capability-no-spec")
    assert f["severity"] == "high"
    assert "declared-cap" in f["detail"]


def test_analyze_orphan_spec(tmp_path):
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("declared-cap"),
        specs={
            "declared-cap": _spec("R declared cap"),
            "orphan-cap": _spec("R orphan cap"),
        },
        tasks=_tasks_open("1.1 implement declared-cap", "1.2 do orphan-cap"),
    )
    res = _sa.analyze_change(cd)
    orphans = [f for f in res["findings"] if f["kind"] == "orphan-spec"]
    assert len(orphans) == 1
    assert "orphan-cap" in orphans[0]["detail"]
    assert orphans[0]["severity"] == "high"
    # 并集包含 orphan
    assert "orphan-cap" in res["capabilities"]


def test_analyze_requirement_maybe_uncovered(tmp_path):
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("lonely-cap"),
        specs={"lonely-cap": _spec("Important Requirement")},
        # tasks 完全不提 lonely-cap
        tasks=_tasks_open("1.1 wire up unrelated plumbing"),
    )
    res = _sa.analyze_change(cd)
    unc = [f for f in res["findings"] if f["kind"] == "requirement-maybe-uncovered"]
    assert len(unc) == 1
    assert unc[0]["severity"] == "medium"
    assert "lonely-cap" in unc[0]["detail"]
    assert "可能误报" in unc[0]["detail"]


def test_analyze_requirement_covered_no_finding(tmp_path):
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("covered-cap"),
        specs={"covered-cap": _spec("Req One")},
        tasks=_tasks_open("1.1 implement covered-cap end to end"),
    )
    res = _sa.analyze_change(cd)
    assert not any(
        f["kind"] == "requirement-maybe-uncovered" for f in res["findings"]
    )


def test_analyze_tasks_all_done(tmp_path):
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("done-cap"),
        specs={"done-cap": _spec("R done cap")},
        tasks=_tasks_done("1.1 implement done-cap", "1.2 test done-cap"),
    )
    res = _sa.analyze_change(cd)
    done = [f for f in res["findings"] if f["kind"] == "tasks-all-done"]
    assert len(done) == 1
    assert done[0]["severity"] == "low"


def test_analyze_partial_done_no_all_done_finding(tmp_path):
    repo = tmp_path / "repo"
    cd = _make_change(
        repo,
        "c",
        proposal=_proposal("cap"),
        specs={"cap": _spec("R cap")},
        tasks="## 1. g\n- [x] 1.1 implement cap\n- [ ] 1.2 test cap\n",
    )
    res = _sa.analyze_change(cd)
    assert not any(f["kind"] == "tasks-all-done" for f in res["findings"])


# ============================================================
# handler：run（emit + 退出码）
# ============================================================


def test_run_missing_change_exits_2(make_args, capsys):
    with pytest.raises(SystemExit) as ei:
        _sa.run(make_args())  # 无 change
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "usage"


def test_run_change_not_found_exits_3(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_sa, "_resolve_repo_root", lambda args: repo)
    with pytest.raises(SystemExit) as ei:
        _sa.run(make_args(change="ghost-change"))
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "change_not_found"


def test_run_healthy_exit_0(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _make_change(
        repo,
        "good",
        proposal=_proposal("good-cap"),
        specs={"good-cap": _spec("Good Cap Req")},
        tasks=_tasks_open("1.1 implement good-cap"),
    )
    monkeypatch.setattr(_sa, "_resolve_repo_root", lambda args: repo)
    _sa.run(make_args(change="good"))  # 不抛 → exit 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["change"] == "good"
    assert out["findings"] == []
    assert out["capabilities"] == ["good-cap"]
    assert out["requirements_count"] == 1
    assert out["tasks_count"] == 1


def test_run_drift_exit_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    _make_change(
        repo,
        "drifty",
        proposal=_proposal("missing-spec-cap"),
        specs={},  # capability-no-spec (high)
        tasks=_tasks_open("1.1 do work"),
    )
    monkeypatch.setattr(_sa, "_resolve_repo_root", lambda args: repo)
    with pytest.raises(SystemExit) as ei:
        _sa.run(make_args(change="drifty"))
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert any(f["kind"] == "capability-no-spec" for f in out["findings"])


def test_run_low_severity_only_exit_0(tmp_path, make_args, capsys, monkeypatch):
    # 只有 tasks-all-done (low) → 不算 drift → exit 0
    repo = tmp_path / "repo"
    _make_change(
        repo,
        "alldone",
        proposal=_proposal("ad-cap"),
        specs={"ad-cap": _spec("AD Cap Req")},
        tasks=_tasks_done("1.1 implement ad-cap"),
    )
    monkeypatch.setattr(_sa, "_resolve_repo_root", lambda args: repo)
    _sa.run(make_args(change="alldone"))  # 不抛
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert any(f["kind"] == "tasks-all-done" for f in out["findings"])


def test_run_change_id_alias(tmp_path, make_args, capsys, monkeypatch):
    # 契约：args.change-id 同义
    repo = tmp_path / "repo"
    _make_change(
        repo,
        "via-alias",
        proposal=_proposal("a-cap"),
        specs={"a-cap": _spec("A Cap Req")},
        tasks=_tasks_open("1.1 implement a-cap"),
    )
    monkeypatch.setattr(_sa, "_resolve_repo_root", lambda args: repo)
    args = make_args()
    setattr(args, "change-id", "via-alias")
    _sa.run(args)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["change"] == "via-alias"


def test_run_repo_locate_failure_exit_3(make_args, capsys, monkeypatch):
    from npc import paths as _paths

    def _boom_detect(start=None):
        raise _paths.PathsError("not a git repo")

    def _boom_load(args):
        raise _paths.PathsError("no run")

    monkeypatch.setattr(_paths, "detect_repo_root", _boom_detect)
    monkeypatch.setattr(_paths, "load_paths", _boom_load)
    with pytest.raises(SystemExit) as ei:
        _sa.run(make_args(change="whatever"))
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "env_missing"
