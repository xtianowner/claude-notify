"""data/aliases.json 读写：用户给 session 起的别名 + 备注。

schema:
{
  "<session_id>": {"alias": "...", "note": "...", "updated_at": "iso8601"}
}
"""
from __future__ import annotations
import fcntl
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock

from .config import DATA_DIR

ALIASES_PATH: Path = DATA_DIR / "aliases.json"
SHANGHAI_TZ = timezone(timedelta(hours=8))
_lock = RLock()


def _now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def load_all() -> dict[str, dict]:
    if not ALIASES_PATH.exists():
        return {}
    try:
        return json.loads(ALIASES_PATH.read_text("utf-8"))
    except Exception:
        return {}


def get(session_id: str) -> dict:
    return load_all().get(session_id, {})


def set_alias(session_id: str, alias: str | None, note: str | None = None) -> dict:
    with _lock:
        ALIASES_PATH.parent.mkdir(exist_ok=True)
        all_ = load_all()
        if not alias and not note:
            all_.pop(session_id, None)
        else:
            entry = all_.get(session_id, {})
            if alias is not None:
                entry["alias"] = alias.strip()
            if note is not None:
                entry["note"] = note.strip()
            entry["updated_at"] = _now_iso()
            all_[session_id] = entry
        tmp = ALIASES_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(json.dumps(all_, ensure_ascii=False, indent=2))
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
        os.replace(tmp, ALIASES_PATH)
        return all_.get(session_id, {})
