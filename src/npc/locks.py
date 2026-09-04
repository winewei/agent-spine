"""进程间互斥原语（stdlib fcntl.flock）。

两把锁，都以文件为载体、进程退出自动释放：

- **state 锁**（``<state_json>.lock``）：包住 ``update_state`` 的读→改→写，让主 session
  的 ``state add-change`` / ``phase rotate`` 与后台 ``change run`` 的装订不互相覆盖。
- **main 锁**（``<task_log_dir>/.main.lock``）：谁在 main worktree 上做提交类操作
  （``integrate`` 的 cherry-pick / 测试 / revert；``change run`` 的 fix / archive commit）
  谁持有，整个操作期间不放。``integrate`` 只 try-lock，拿不到即返回 ``main-busy``；
  ``change run`` 有界等待后失败。锁文件内容记录持有者（pid / owner / ts）供诊断。

非 POSIX 平台（无 fcntl）退化为无锁并 warn 一次；npc 的目标宿主均为 macOS / Linux。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import IO, Iterator

from . import _io

try:  # pragma: no cover - 平台差异
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

STATE_LOCK_WAIT_SEC = 30.0
MAIN_LOCK_WAIT_SEC = 120.0
_POLL_SEC = 0.1

_warned_no_fcntl = False


class LockBusy(RuntimeError):
    """在允许的等待时间内没拿到锁。``holder`` 为锁文件里的持有者信息（可能为 None）。"""

    def __init__(self, path: Path, holder: dict | None):
        self.path = path
        self.holder = holder
        who = f"{holder.get('owner')}(pid {holder.get('pid')})" if holder else "unknown"
        super().__init__(f"lock busy: {path} held by {who}")


def _warn_once() -> None:
    global _warned_no_fcntl
    if not _warned_no_fcntl:
        _warned_no_fcntl = True
        _io.warn("locks: 当前平台无 fcntl，互斥退化为无锁运行")


def read_holder(lock_path: Path) -> dict | None:
    """读锁文件里的持有者记录；文件不存在 / 内容非 JSON 返回 None。"""
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_holder(fh: IO[str], owner: str) -> None:
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({"pid": os.getpid(), "owner": owner, "ts": _io.now_iso()}))
    fh.flush()


def _try_flock(fh: IO[str]) -> bool:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, PermissionError):
        return False
    except OSError as e:  # pragma: no cover - EWOULDBLOCK 的其它包装
        if e.errno in (11, 35):
            return False
        raise


def try_acquire(lock_path: Path, *, owner: str) -> IO[str] | None:
    """非阻塞抢锁。成功返回已持锁的文件句柄（关闭即释放），失败返回 None。"""
    if fcntl is None:  # pragma: no cover
        _warn_once()
        return open(os.devnull, "w", encoding="utf-8")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    if _try_flock(fh):
        _write_holder(fh, owner)
        return fh
    fh.close()
    return None


def acquire(lock_path: Path, *, owner: str, wait_sec: float) -> IO[str]:
    """有界等待抢锁；超时抛 :class:`LockBusy`。"""
    deadline = time.monotonic() + max(0.0, wait_sec)
    while True:
        fh = try_acquire(lock_path, owner=owner)
        if fh is not None:
            return fh
        if time.monotonic() >= deadline:
            raise LockBusy(lock_path, read_holder(lock_path))
        time.sleep(_POLL_SEC)


def release(fh: IO[str] | None) -> None:
    """释放锁（关闭句柄即可；flock 随描述符关闭释放）。幂等。"""
    if fh is None:
        return
    with contextlib.suppress(OSError, ValueError):
        if fcntl is not None and fh.fileno() >= 0 and fh.name != os.devnull:
            fh.seek(0)
            fh.truncate()
            fh.flush()
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError, ValueError):
        fh.close()


@contextlib.contextmanager
def held(lock_path: Path, *, owner: str, wait_sec: float) -> Iterator[None]:
    """``with held(path, owner=..., wait_sec=...):`` 形式的有界等待锁。"""
    fh = acquire(lock_path, owner=owner, wait_sec=wait_sec)
    try:
        yield
    finally:
        release(fh)


def state_lock_path(state_json: Path) -> Path:
    return state_json.with_suffix(state_json.suffix + ".lock")


def main_lock_path(task_log_dir: Path) -> Path:
    return Path(task_log_dir) / ".main.lock"
