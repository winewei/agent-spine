"""playbook 模块单元测试（v1.7 去 plugin 化）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import playbook


EXPECTED_NAMES = {
    "spine-run",
    "spine-analyze",
    "new-plan-changes-v2",
    "new-plan-changes-v3",
    "new-plan-changes-v4",
    "spine-coder",
}


def test_list_contains_all_playbooks():
    items = playbook.list_playbooks()
    assert {i["name"] for i in items} == EXPECTED_NAMES
    for i in items:
        assert i["kind"] in ("command", "skill", "agent")
        assert i["bytes"] > 0


def test_get_unknown_raises():
    with pytest.raises(playbook.PlaybookError):
        playbook.get("no-such-playbook")


def test_read_text_returns_markdown():
    text = playbook.read_text(playbook.get("spine-run"))
    assert text.startswith("---\n")
    assert "宿主适配" in text  # 宿主中立契约块必须在


def test_install_dest_flat_layout(tmp_path: Path):
    result = playbook.install(None, host=None, dest=tmp_path / "out", home=tmp_path)
    assert result["ok"] is True
    assert len(result["installed"]) == len(EXPECTED_NAMES)
    assert result["skipped"] == []
    for item in result["installed"]:
        assert Path(item["path"]).is_file()
        assert Path(item["path"]).parent == tmp_path / "out"


def test_install_host_claude_layout(tmp_path: Path):
    result = playbook.install(None, host="claude", dest=None, home=tmp_path)
    paths = {i["name"]: Path(i["path"]) for i in result["installed"]}
    assert paths["spine-run"] == tmp_path / ".claude" / "commands" / "spine-run.md"
    assert (
        paths["new-plan-changes-v3"]
        == tmp_path / ".claude" / "skills" / "new-plan-changes-v3" / "SKILL.md"
    )
    assert paths["spine-coder"] == tmp_path / ".claude" / "agents" / "spine-coder.md"
    assert result["skipped"] == []
    for p in paths.values():
        assert p.is_file()


def test_install_host_codex_skips_agent(tmp_path: Path):
    result = playbook.install(None, host="codex", dest=None, home=tmp_path)
    names = {i["name"] for i in result["installed"]}
    assert "spine-coder" not in names
    assert result["skipped"] and result["skipped"][0]["name"] == "spine-coder"
    for i in result["installed"]:
        assert Path(i["path"]).parent == tmp_path / ".codex" / "prompts"


def test_install_subset_and_idempotent(tmp_path: Path):
    r1 = playbook.install(["spine-run"], host=None, dest=tmp_path / "d", home=tmp_path)
    assert [i["name"] for i in r1["installed"]] == ["spine-run"]
    assert r1["installed"][0]["replaced"] is False
    r2 = playbook.install(["spine-run"], host=None, dest=tmp_path / "d", home=tmp_path)
    assert r2["installed"][0]["replaced"] is True


def test_install_requires_exactly_one_target(tmp_path: Path):
    with pytest.raises(playbook.PlaybookError):
        playbook.install(None, host="claude", dest=tmp_path, home=tmp_path)
    with pytest.raises(playbook.PlaybookError):
        playbook.install(None, host=None, dest=None, home=tmp_path)


def test_install_unknown_host_raises(tmp_path: Path):
    with pytest.raises(playbook.PlaybookError):
        playbook.install(None, host="cursor", dest=None, home=tmp_path)


def test_cli_list_emits_json(capsys):
    import argparse

    playbook.cli_list(argparse.Namespace())
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["ok"] is True
    assert {i["name"] for i in data["playbooks"]} == EXPECTED_NAMES


def test_cli_show_raw_markdown(capsys):
    import argparse

    playbook.cli_show(argparse.Namespace(name="new-plan-changes-v4"))
    out = capsys.readouterr().out
    assert out.startswith("---\n")
    assert "new-plan-changes-v4" in out


def test_cli_show_unknown_exits_2(capsys):
    import argparse

    with pytest.raises(SystemExit) as ei:
        playbook.cli_show(argparse.Namespace(name="nope"))
    assert ei.value.code == 2
