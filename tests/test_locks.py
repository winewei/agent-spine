"""locks 模块：state 锁 / main 锁的抢占、等待、释放与持有者记录。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from npc import locks as _locks
from npc import state as _state


def test_try_acquire_conflicts_and_releases(tmp_path: Path):
    lock = tmp_path / ".main.lock"
    a = _locks.try_acquire(lock, owner="first")
    assert a is not None
    holder = _locks.read_holder(lock)
    assert holder["owner"] == "first" and holder["pid"] == os.getpid()

    # 同一文件的第二个描述符抢不到
    assert _locks.try_acquire(lock, owner="second") is None
    _locks.release(a)
    b = _locks.try_acquire(lock, owner="second")
    assert b is not None
    assert _locks.read_holder(lock)["owner"] == "second"
    _locks.release(b)
    _locks.release(b)  # 幂等


def test_acquire_times_out_with_holder_info(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_locks, "_POLL_SEC", 0.01)
    lock = tmp_path / ".main.lock"
    a = _locks.try_acquire(lock, owner="change run seq=3")
    try:
        with pytest.raises(_locks.LockBusy) as ei:
            _locks.acquire(lock, owner="integrate", wait_sec=0.05)
        assert ei.value.holder["owner"] == "change run seq=3"
        assert "change run seq=3" in str(ei.value)
    finally:
        _locks.release(a)


def test_held_context_manager_waits_then_gets_lock(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_locks, "_POLL_SEC", 0.01)
    lock = tmp_path / "x.lock"
    with _locks.held(lock, owner="one", wait_sec=1.0):
        assert _locks.try_acquire(lock, owner="probe") is None
    fh = _locks.try_acquire(lock, owner="probe")
    assert fh is not None
    _locks.release(fh)


def test_read_holder_tolerates_garbage(tmp_path: Path):
    assert _locks.read_holder(tmp_path / "missing") is None
    p = tmp_path / "bad.lock"
    p.write_text("not json", encoding="utf-8")
    assert _locks.read_holder(p) is None
    p.write_text("[1]", encoding="utf-8")
    assert _locks.read_holder(p) is None


def test_lock_conflicts_across_processes(tmp_path: Path):
    """另一个进程持锁时本进程 try_acquire 必须失败（flock 是内核级）。"""
    lock = tmp_path / ".main.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; from npc import locks as L; from pathlib import Path\n"
                f"fh=L.try_acquire(Path({str(lock)!r}), owner='child'); print('ok' if fh else 'no', flush=True)\n"
                "sys.stdin.readline(); L.release(fh)"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ok"
        assert _locks.try_acquire(lock, owner="parent") is None
        assert _locks.read_holder(lock)["owner"] == "child"
    finally:
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=10)
    fh = _locks.try_acquire(lock, owner="parent")
    assert fh is not None
    _locks.release(fh)


def test_update_state_uses_lock_and_pid_tmp(tmp_path: Path, monkeypatch):
    state_json = tmp_path / "s.json"
    state_md = tmp_path / "s.md"
    state_json.write_text(json.dumps({"progress": [], "goal": "g"}), encoding="utf-8")

    seen_locks: list[Path] = []
    real_held = _locks.held

    def spy_held(path, **kw):
        seen_locks.append(path)
        return real_held(path, **kw)

    monkeypatch.setattr(_state._locks, "held", spy_held)
    monkeypatch.setattr(_state, "render_state_md", lambda s: "md")
    out = _state.update_state(state_json, state_md, lambda s: s.__setitem__("goal", "h"))
    assert out["goal"] == "h"
    assert seen_locks == [_locks.state_lock_path(state_json)]
    assert json.loads(state_json.read_text())["goal"] == "h"
    # 无残留 tmp
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_tmp_name_is_pid_unique(tmp_path: Path, monkeypatch):
    target = tmp_path / "s.json"
    names: list[str] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        names.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(_state.os, "replace", spy_replace)
    _state._atomic_write_text(target, "x")
    assert names == [f"s.json.{os.getpid()}.tmp"]
    assert target.read_text() == "x"
