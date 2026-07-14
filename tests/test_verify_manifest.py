"""verify manifest 测试：RESULT 行双格式解析 / manifest 文件核对 / CLI 退出码。"""

from __future__ import annotations

import argparse
import hashlib
import json

import pytest

from npc import verify as _verify


# ============================================================
# parse_result_verdict（纯函数）
# ============================================================


def test_npc_format_with_commit_is_code():
    r = _verify.parse_result_verdict(
        "RESULT: commit=abc123 tasks=3 tests=pass summary=/tmp/s.md notes=ok", "/m.json"
    )
    assert r["verdict"] == "code" and r["commit"] == "abc123" and r["manifest"] == "/m.json"


def test_npc_format_dash_commit_is_plan_only():
    r = _verify.parse_result_verdict("RESULT: commit=- tasks=0 tests=fail", None)
    assert r["verdict"] == "plan_only" and r["reason"] == "no_commit"


def test_json_format_plan_only_self_declared():
    r = _verify.parse_result_verdict('RESULT: {"status": "plan_only"}', None)
    assert r["verdict"] == "plan_only" and r["reason"] == "self_declared"


def test_json_format_error():
    r = _verify.parse_result_verdict('RESULT: {"status": "error", "error": "boom"}', None)
    assert r["verdict"] == "error" and r["reason"] == "boom"


def test_json_format_zero_files():
    r = _verify.parse_result_verdict('RESULT: {"files_written": 0}', None)
    assert r["verdict"] == "plan_only" and r["reason"] == "zero_files_written"


def test_json_format_manifest_field_used_when_no_arg():
    r = _verify.parse_result_verdict(
        'RESULT: {"files_written": 2, "manifest": "/from/json.json"}', None
    )
    assert r["verdict"] == "code" and r["manifest"] == "/from/json.json"


def test_no_result_line_is_plan_only():
    r = _verify.parse_result_verdict("I made a detailed plan instead.", None)
    assert r["verdict"] == "plan_only" and r["reason"] == "no_result_line"


# ============================================================
# check_manifest_files
# ============================================================


def _write_manifest(tmp_path, files_written):
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"cid": "add-foo", "files_written": files_written}))
    return m


def test_manifest_missing():
    out = _verify.check_manifest_files("/no/such/file.json")
    assert out["ok"] is False and out["reason"] == "manifest_missing"


def test_manifest_empty_files(tmp_path):
    m = _write_manifest(tmp_path, [])
    out = _verify.check_manifest_files(str(m))
    assert out["ok"] is False and out["reason"] == "manifest_empty_files"


def test_manifest_object_entries_with_sha(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("print(1)\n")
    sha = hashlib.sha256(f.read_bytes()).hexdigest()
    m = _write_manifest(tmp_path, [{"path": str(f), "sha256": sha}])
    out = _verify.check_manifest_files(str(m))
    assert out["ok"] is True and out["present"] == 1 and out["total"] == 1


def test_manifest_string_entries_accepted(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x")
    m = _write_manifest(tmp_path, [str(f)])
    out = _verify.check_manifest_files(str(m))
    assert out["ok"] is True and out["present"] == 1


def test_manifest_sha_mismatch(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x")
    m = _write_manifest(tmp_path, [{"path": str(f), "sha256": "0" * 64}])
    out = _verify.check_manifest_files(str(m))
    assert out["ok"] is False and out["reason"] == "sha_mismatch"


def test_manifest_file_missing(tmp_path):
    m = _write_manifest(tmp_path, [str(tmp_path / "ghost.py")])
    out = _verify.check_manifest_files(str(m))
    assert out["ok"] is False and out["reason"] == "files_missing"


def test_manifest_malformed_int_entry(tmp_path):
    # manifest 是不可信的 sub-agent 输出，非法条目须结构化失败而非 traceback
    m = _write_manifest(tmp_path, [123])
    out = _verify.check_manifest_files(str(m))
    assert out["ok"] is False and out["reason"] == "manifest_malformed_entry"


def test_manifest_malformed_dict_without_path(tmp_path):
    m = _write_manifest(tmp_path, [{"sha256": "0" * 64}])
    out = _verify.check_manifest_files(str(m))
    assert out["ok"] is False and out["reason"] == "manifest_malformed_entry"


# ============================================================
# run_manifest（CLI handler + 退出码）
# ============================================================


def _run(capsys, result: str, manifest: str | None):
    args = argparse.Namespace(result=result, manifest=manifest)
    _verify.run_manifest(args)
    return json.loads(capsys.readouterr().out)


def test_run_manifest_happy_path(tmp_path, capsys):
    f = tmp_path / "a.py"
    f.write_text("x")
    m = _write_manifest(tmp_path, [str(f)])
    out = _run(capsys, "RESULT: commit=abc tasks=1 tests=pass summary=- notes=-", str(m))
    assert out["ok"] is True and out["verdict"] == "code" and out["files"]["present"] == 1


def test_run_manifest_plan_only_exit_1(capsys):
    with pytest.raises(SystemExit) as ei:
        _run(capsys, "no result here", None)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "plan_only"


def test_run_manifest_code_but_manifest_missing_becomes_plan_only(capsys):
    with pytest.raises(SystemExit) as ei:
        _run(capsys, "RESULT: commit=abc tasks=1 tests=pass", "/no/such.json")
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "plan_only" and out["reason"] == "manifest_missing"


def test_run_manifest_malformed_entry_becomes_plan_only(tmp_path, capsys):
    m = _write_manifest(tmp_path, [123])
    with pytest.raises(SystemExit) as ei:
        _run(capsys, "RESULT: commit=abc tasks=1 tests=pass", str(m))
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "plan_only" and out["reason"] == "manifest_malformed_entry"


def test_run_manifest_sha_mismatch_stays_code_verdict(tmp_path, capsys):
    f = tmp_path / "a.py"
    f.write_text("x")
    m = _write_manifest(tmp_path, [{"path": str(f), "sha256": "0" * 64}])
    with pytest.raises(SystemExit) as ei:
        _run(capsys, "RESULT: commit=abc tasks=1 tests=pass", str(m))
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    # 代码确实写了（verdict=code），但与声明不符 → ok=false, reason=sha_mismatch
    assert out["verdict"] == "code" and out["reason"] == "sha_mismatch"
