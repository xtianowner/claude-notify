"""R40: data/hidden_sessions.json 持久化用户主动删除的 session_id 集合。

与 mark-dead（仅改 status）不同：hidden = 从 list_sessions 结果中完全跳过，
dashboard 看不到该 session（events.jsonl 内事件保留作为审计/debug）。

schema:
{
  "<session_id>": {"deleted_at": "iso8601", "name": "...", "source": "..."}
}
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock

from .config import DATA_DIR

HIDDEN_PATH: Path = DATA_DIR / "hidden_sessions.json"
SHANGHAI_TZ = timezone(timedelta(hours=8))
_lock = RLock()
_cache: dict[str, dict] | None = None


def _now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def load_all() -> dict[str, dict]:
    """加载全部 hidden 记录（带 cache 避免每次 list_sessions 都 IO）。"""
    global _cache
    if _cache is not None:
        return _cache
    if not HIDDEN_PATH.exists():
        _cache = {}
        return _cache
    try:
        _cache = json.loads(HIDDEN_PATH.read_text("utf-8"))
        if not isinstance(_cache, dict):
            _cache = {}
    except Exception:
        _cache = {}
    return _cache


def is_hidden(session_id: str) -> bool:
    return session_id in load_all()


def hidden_ids() -> set[str]:
    return set(load_all().keys())


def _persist_unlocked(data: dict[str, dict]) -> None:
    global _cache
    HIDDEN_PATH.parent.mkdir(exist_ok=True)
    tmp = HIDDEN_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(HIDDEN_PATH)
    _cache = data


def add(session_id: str, *, name: str = "", source: str = "") -> dict:
    """把 session_id 加入 hidden（用户主动删除）。"""
    with _lock:
        data = dict(load_all())
        entry = {
            "deleted_at": _now_iso(),
            "name": name or "",
            "source": source or "",
        }
        data[session_id] = entry
        _persist_unlocked(data)
        return entry


def remove(session_id: str) -> bool:
    """恢复被删除的 session（unhide）。"""
    with _lock:
        data = dict(load_all())
        if session_id not in data:
            return False
        del data[session_id]
        _persist_unlocked(data)
        return True


def clear_cache() -> None:
    """测试 / 手动改 hidden_sessions.json 后让 cache 失效。"""
    global _cache
    _cache = None
