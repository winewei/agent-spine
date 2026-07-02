"""notify.py 测试：payload 形状 / URL 解析 / 永不非零退出。"""

from __future__ import annotations

import argparse
import json
import urllib.error

import pytest

from npc import notify as _notify


# ============================================================
# 纯函数
# ============================================================


def test_payload_raw_merges_kv():
    p = _notify.build_payload("wave-done", {"wave": "2", "parallel": "3"}, "", "raw")
    assert p["event"] == "wave-done"
    assert p["wave"] == "2"
    assert "wave-done" in p["text"]


def test_payload_slack_shape():
    p = _notify.build_payload("x", {}, "hello", "slack")
    assert p == {"text": "hello"}


def test_payload_feishu_shape():
    p = _notify.build_payload("x", {}, "hi", "feishu")
    assert p == {"msg_type": "text", "content": {"text": "hi"}}


def test_parse_kv_skips_malformed():
    assert _notify.parse_kv(["a=1", "bad", "b=x=y"]) == {"a": "1", "b": "x=y"}


# ============================================================
# run（CLI handler）
# ============================================================


def _args(**over) -> argparse.Namespace:
    base = dict(event="test", url="", format="raw", kv=[], text="", timeout=5.0)
    base.update(over)
    return argparse.Namespace(**base)


def test_run_no_url_silent_noop(monkeypatch, capsys):
    monkeypatch.delenv("NPC_WEBHOOK", raising=False)
    monkeypatch.delenv("NPC_V3_WEBHOOK", raising=False)
    _notify.run(_args())  # 不抛 SystemExit 即 exit 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "event": "test", "url_set": False, "delivered": False}


def test_run_env_fallback_v3_compat(monkeypatch, capsys):
    monkeypatch.delenv("NPC_WEBHOOK", raising=False)
    monkeypatch.setenv("NPC_V3_WEBHOOK", "http://hook.local/x")
    sent = {}

    def fake_post(url, payload, timeout, opener=None):
        sent["url"] = url
        return True

    monkeypatch.setattr(_notify, "post", fake_post)
    _notify.run(_args(kv=["cid=add-foo"]))
    out = json.loads(capsys.readouterr().out)
    assert out["url_set"] is True and out["delivered"] is True
    assert sent["url"] == "http://hook.local/x"


def test_run_webhook_failure_still_exit_0(monkeypatch, capsys):
    def boom(req, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    _notify.run(_args(url="http://dead.local/hook"))  # 不抛 SystemExit
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["delivered"] is False


def test_post_2xx_true_4xx_false(monkeypatch):
    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    assert _notify.post("http://x", {}, 1.0, opener=lambda req, timeout: Resp()) is True
    Resp.status = 404
    assert _notify.post("http://x", {}, 1.0, opener=lambda req, timeout: Resp()) is False
