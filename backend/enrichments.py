"""LLM 摘要的持久化缓存（避免 backend 重启后丢失，避免 LLM 反复调）。

存储：data/enrichments.jsonl，每行一条记录：
  {
    "kind": "topic" | "event",
    "key":  "<cache key>",
    "value": "<summary text>",
    "ts":   "<iso>"
  }

cache key 设计：
- topic: f"topic:{transcript_path}:{mtime_ns}"
- event: f"event:{session_id}:{event_ts}:{event_type}"

读取：启动时 load_all() 一次性进内存 dict；之后每次 get() 用 dict 查。
写入：set() 同步追加 jsonl + 更新内存 dict。
"""
from __future__ import annotations
import fcntl
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock

from .config import DATA_DIR

ENRICH_PATH: Path = DATA_DIR / "enrichments.jsonl"
SHANGHAI_TZ = timezone(timedelta(hours=8))
log = logging.getLogger("claude-notify.enrichments")

_cache: dict[str, str] = {}
_loaded = False
_lock = RLock()


def _now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _ensure_loaded():
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        if not ENRICH_PATH.exists():
            _loaded = True
            return
        try:
            with open(ENRICH_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        k, v = rec.get("key"), rec.get("value")
                        if k and isinstance(v, str):
                            _cache[k] = v
                    except Exception:
                        continue
        except Exception:
            log.exception("load enrichments failed")
        _loaded = True


def get(key: str) -> str | None:
    _ensure_loaded()
    return _cache.get(key)


def set(key: str, value: str, kind: str = "topic") -> None:
    if not key or value is None:
        return
    _ensure_loaded()
    with _lock:
        _cache[key] = value
        try:
            ENRICH_PATH.parent.mkdir(exist_ok=True)
            line = json.dumps(
                {"kind": kind, "key": key, "value": value, "ts": _now_iso()},
                ensure_ascii=False,
            )
            with open(ENRICH_PATH, "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line + "\n")
                    f.flush()
                finally:
                    try: fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except Exception: pass
        except Exception:
            log.exception("write enrichments failed")


def topic_key(transcript_path: str, mtime_ns: int) -> str:
    return f"topic:{transcript_path}:{mtime_ns}"


def event_key(session_id: str, event_ts: str, event_type: str) -> str:
    return f"event:{session_id}:{event_ts}:{event_type}"
