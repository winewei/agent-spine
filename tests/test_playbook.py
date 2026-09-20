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
    assert len(result["installed"]) == len(EXPECTED_NAMES) - 1
    assert "new-plan-changes-v4" not in {i["name"] for i in result["installed"]}
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
        if i["name"] == "spine-run":
            assert Path(i["path"]) == tmp_path / ".codex/skills/spine-run/SKILL.md"
            assert Path(i["path"]).read_text() == playbook.read_text(playbook.get("spine-run"))
        else:
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


def test_spine_single_node_creates_runtime_dag(tmp_path):
    import os
    import re
    import subprocess
    import shutil
    if not shutil.which("jq"):
        pytest.skip("jq is required by the playbook")
    text = playbook.read_text(playbook.get("spine-run"))
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    block = next(b for b in blocks if b.startswith('jq -n --arg cid'))
    subprocess.run(["bash", "-c", block], check=True,
                   env={**os.environ, "CID": "single", "RUN_DIR": str(tmp_path)})
    from npc.waves import ready
    dag = json.loads((tmp_path / "v3-dag-extract.json").read_text())
    assert ready(dag)["ready"] == ["single"]


def test_spine_explicit_ordering_preserves_independent_work(tmp_path):
    import os
    import re
    import shutil
    import subprocess
    if not shutil.which("jq"):
        pytest.skip("jq is required by the playbook")
    text = playbook.read_text(playbook.get("spine-run"))
    block = next(b for b in re.findall(r"```bash\n(.*?)```", text, re.S)
                 if b.startswith('DAG=') and 'ordering_edges' in b)
    path = tmp_path / "v3-dag-extract.json"
    path.write_text(json.dumps({"nodes": ["a", "b", "c"], "edges": [["a", "c"]], "files": {}}))
    subprocess.run(["bash", "-c", block], check=True, env={**os.environ,
                   "RUN_DIR": str(tmp_path), "FINAL_WAVES": '[["a"],["b","c"]]'})
    from npc.waves import ready
    dag = json.loads(path.read_text())
    assert ready(dag)["ready"] == ["a", "b"]
    assert ready({**dag, "done": ["a"]})["ready"] == ["b", "c"]


def test_spine_ready_excludes_failed_terminal_changes(tmp_path):
    import os
    import re
    import shutil
    import subprocess
    if not shutil.which("jq"):
        pytest.skip("jq is required by the playbook")
    text = playbook.read_text(playbook.get("spine-run"))
    block = next(b for b in re.findall(r"```bash\n(.*?)```", text, re.S) if 'READY=$(jq' in b)
    # Execute the exact documented jq input builder, then use the production scheduler.
    builder = block.split('READY=$(', 1)[1].split('| npc plan ready', 1)[0].lstrip()
    path = tmp_path / "v3-dag-extract.json"
    path.write_text(json.dumps({"nodes": ["a", "b"], "edges": [], "files": {}}))
    result = subprocess.run(["bash", "-c", builder], check=True, capture_output=True, text=True,
                            env={**os.environ, "DAG": str(path), "DONE": "[]", "ACTIVE": "[]",
                                 "PENDING": "[]", "FINISHED": '["a"]', "SLOTS": "2"})
    from npc.waves import ready
    assert ready(json.loads(result.stdout))["ready"] == ["b"]


def test_spine_inner_loop_keeps_warning_out_of_json(tmp_path):
    import os
    import re
    import subprocess
    text = playbook.read_text(playbook.get("spine-run"))
    block = next(b for b in re.findall(r"```bash\n(.*?)```", text, re.S)
                 if b.startswith('npc change run --seq'))
    fake_npc = "npc() { echo warning >&2; echo '{\"ok\":true}'; }\n"
    subprocess.run(["bash", "-c", fake_npc + block], check=True,
                   env={**os.environ, "RUN_DIR": str(tmp_path), "SEQ": "1", "AUTO": ""})
    assert json.loads((tmp_path / "change-run-1.json").read_text()) == {"ok": True}
    assert (tmp_path / "change-run-1.stderr.log").read_text().strip() == "warning"


@pytest.mark.parametrize("resuming", [False, True])
def test_spine_init_binds_paths_and_original_start(tmp_path, resuming):
    import os
    import re
    import shutil
    import subprocess
    if not shutil.which("jq"):
        pytest.skip("jq is required by the playbook")
    run_dir = tmp_path / "run with spaces"
    run_dir.mkdir()
    state = tmp_path / "state.json"
    if resuming:
        state.write_text(json.dumps({"started_at": "2026-01-02T03:04:05Z"}))
    payload = {"run_dir": str(run_dir), "run_ts": "run-one", "state_json": str(state)}
    text = playbook.read_text(playbook.get("spine-run"))
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    init = next(b for b in blocks if b.startswith("INIT="))
    dag = next(b for b in blocks if b.startswith("jq -n --arg cid"))
    script = 'npc() { printf "%s\\n" "$INIT_PAYLOAD"; }\n' + init + dag + '\nprintf "%s" "$RUN_T0"'
    result = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True,
                            env={**os.environ, "INIT_PAYLOAD": json.dumps(payload), "CID": "one"})
    assert json.loads((run_dir / "v3-dag-extract.json").read_text())["nodes"] == ["one"]
    assert result.stdout == "2026-01-02T03:04:05Z" if resuming else result.stdout.endswith("Z")


def test_spine_checkpoint_retains_concurrent_jobs(tmp_path):
    import os
    import re
    import shutil
    import subprocess
    if not shutil.which("jq"):
        pytest.skip("jq is required by the playbook")
    text = playbook.read_text(playbook.get("spine-run"))
    block = next(b for b in re.findall(r"```bash\n(.*?)```", text, re.S)
                 if b.startswith('jq -n --arg run_ts'))
    jobs = {"b": {"handle": "agent-42"}, "c": {"result": "result with spaces.txt"}}
    subprocess.run(["bash", "-c", block], check=True, env={**os.environ,
                   "RUN_DIR": str(tmp_path), "RUN_TS": "original", "DONE": '["a"]',
                   "FINISHED": '["d"]', "ACTIVE": '["b"]', "PENDING": '["c"]',
                   "INNER": '["a"]', "JOBS": json.dumps(jobs)})
    saved = json.loads((tmp_path / "scheduler.json").read_text())
    assert saved == {"run_ts": "original", "done": ["a"], "finished": ["d"],
                     "active": ["b"], "pending": ["c"], "inner": ["a"], "jobs": jobs}
