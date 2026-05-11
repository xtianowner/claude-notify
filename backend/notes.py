"""记事本（notes.md）后端模块（简化版）。

- 单文件全量读写（无 version 锁、无 append endpoint）
- 进程内 RLock + 跨进程 fcntl.flock（写时持锁）
- 原子写：tmp + os.replace
- 正文 normalize：strip 末尾空行 + 末尾补一个 '\n'
- 大小上限：256 KB（超限抛 NotesOversizeError）

参考：
- backend/event_store.py append_event 的 flock 用法
- backend/config.py save 的 tmp+replace 模式
"""
from __future__ import annotations

import fcntl
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock

NOTES_PATH = Path(__file__).resolve().parent.parent / "data" / "notes.md"
MAX_SIZE_BYTES = 256 * 1024  # 256 KB

SHANGHAI_TZ = timezone(timedelta(hours=8))

_lock = RLock()


class NotesOversizeError(Exception):
    """正文超过 MAX_SIZE_BYTES。"""


def _now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _mtime_iso(p: Path) -> str:
    try:
        ts = p.stat().st_mtime
        return datetime.fromtimestamp(ts, SHANGHAI_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    except Exception:
        return ""


def _normalize(content: str) -> str:
    """strip 末尾空行 + 末尾补一个 '\n'。空字符串保持空。"""
    if not content:
        return ""
    # 去掉末尾所有空白行 / 空白字符（保留正文中部的空行）
    stripped = content.rstrip()
    if not stripped:
        return ""
    return stripped + "\n"


def read() -> tuple[str, int, str]:
    """返回 (content, size_bytes, updated_at_iso)。

    文件不存在 → ("", 0, "")，不抛。
    """
    with _lock:
        if not NOTES_PATH.exists():
            return ("", 0, "")
        try:
            data = NOTES_PATH.read_bytes()
        except Exception:
            return ("", 0, "")
        try:
            content = data.decode("utf-8")
        except Exception:
            content = data.decode("utf-8", errors="replace")
        return (content, len(data), _mtime_iso(NOTES_PATH))


def write(content: str) -> tuple[int, str]:
    """全量写入。返回 (size_bytes, updated_at_iso)。

    超过 256KB → 抛 NotesOversizeError。
    使用 fcntl.flock(LOCK_EX) + tmp/replace 原子写。
    """
    if not isinstance(content, str):
        raise TypeError("content must be str")
    normalized = _normalize(content)
    encoded = normalized.encode("utf-8")
    if len(encoded) > MAX_SIZE_BYTES:
        raise NotesOversizeError(
            f"content size {len(encoded)} exceeds max {MAX_SIZE_BYTES}"
        )

    with _lock:
        NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = NOTES_PATH.with_suffix(".md.tmp")
        # 写 tmp 时持有 LOCK_EX，保证跨进程互斥
        # 注意：flock 加在 tmp fd 上无法防别的进程同时写 NOTES_PATH；
        # 因此用一个独立的 lock 文件 NOTES_PATH.lock 作为跨进程信号量。
        lock_path = NOTES_PATH.with_suffix(".md.lock")
        # 开 lock 文件（不存在则创建）
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                with open(tmp, "wb") as f:
                    f.write(encoded)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, NOTES_PATH)
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
        finally:
            try:
                os.close(lock_fd)
            except Exception:
                pass

        size = len(encoded)
        return (size, _mtime_iso(NOTES_PATH) or _now_iso())
