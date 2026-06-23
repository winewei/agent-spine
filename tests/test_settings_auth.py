"""settings_auth 测试：auto 授权的合并纯函数 + 落盘行为。"""

from __future__ import annotations

import json

import pytest

from npc import settings_auth as sa


# ============================================================
# merge_auto_permissions 纯函数
# ============================================================


def test_merge_empty_sets_mode_and_allow():
    new, summary = sa.merge_auto_permissions({})
    assert new["permissions"]["defaultMode"] == "acceptEdits"
    assert summary["defaultMode_set"] is True
    # 全部 harness 白名单都被加入
    assert set(sa.HARNESS_BASH_ALLOW).issubset(set(new["permissions"]["allow"]))
    assert summary["added_allow"] == list(sa.HARNESS_BASH_ALLOW)


def test_merge_preserves_existing_deny_and_other_keys():
    existing = {
        "permissions": {"deny": ["Read(.env)", "Read(.secrets)"], "allow": ["Bash(ls *)"]},
        "enabledPlugins": {"foo": True},
    }
    new, _ = sa.merge_auto_permissions(existing)
    # deny 原样保留
    assert new["permissions"]["deny"] == ["Read(.env)", "Read(.secrets)"]
    # 其它顶层键保留
    assert new["enabledPlugins"] == {"foo": True}
    # 既有 allow 项保留 + harness 项追加
    assert "Bash(ls *)" in new["permissions"]["allow"]
    assert "Bash(npc *)" in new["permissions"]["allow"]


def test_merge_idempotent():
    new1, s1 = sa.merge_auto_permissions({})
    new2, s2 = sa.merge_auto_permissions(new1)
    # 第二次不再改 mode、不再加 allow
    assert s2["defaultMode_set"] is False
    assert s2["added_allow"] == []
    assert new2["permissions"]["allow"] == new1["permissions"]["allow"]


def test_merge_does_not_mutate_input():
    existing = {"permissions": {"allow": ["Bash(ls *)"]}}
    sa.merge_auto_permissions(existing)
    # 原对象不被改
    assert existing == {"permissions": {"allow": ["Bash(ls *)"]}}


def test_merge_respects_existing_acceptedits_mode():
    existing = {"permissions": {"defaultMode": "acceptEdits"}}
    _, summary = sa.merge_auto_permissions(existing)
    assert summary["defaultMode_set"] is False


# ============================================================
# grant_auto_permissions 落盘
# ============================================================


def test_grant_creates_settings(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    res = sa.grant_auto_permissions(repo)
    assert res["ok"] is True
    assert res["created"] is True
    sp = repo / ".claude" / "settings.json"
    assert sp.is_file()
    data = json.loads(sp.read_text())
    assert data["permissions"]["defaultMode"] == "acceptEdits"
    assert "Bash(npc *)" in data["permissions"]["allow"]


def test_grant_merges_existing_preserves_deny(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    sp = repo / ".claude" / "settings.json"
    sp.write_text(json.dumps({"permissions": {"deny": ["Read(.env)"]}, "hooks": {"x": 1}}))
    res = sa.grant_auto_permissions(repo)
    assert res["ok"] is True
    assert res["created"] is False
    data = json.loads(sp.read_text())
    assert data["permissions"]["deny"] == ["Read(.env)"]
    assert data["hooks"] == {"x": 1}
    assert data["permissions"]["defaultMode"] == "acceptEdits"


def test_grant_idempotent_on_disk(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sa.grant_auto_permissions(repo)
    res2 = sa.grant_auto_permissions(repo)
    assert res2["ok"] is True
    assert res2["defaultMode_set"] is False
    assert res2["added_allow"] == []


def test_grant_skips_unparseable_without_clobber(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    sp = repo / ".claude" / "settings.json"
    sp.write_text("{ this is not json")
    res = sa.grant_auto_permissions(repo)
    assert res["ok"] is False
    assert res["skipped"] == "unparseable"
    # 原文件未被覆盖
    assert sp.read_text() == "{ this is not json"


def test_grant_skips_non_object(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    sp = repo / ".claude" / "settings.json"
    sp.write_text("[1, 2, 3]")
    res = sa.grant_auto_permissions(repo)
    assert res["ok"] is False
    assert res["skipped"] == "not-an-object"
