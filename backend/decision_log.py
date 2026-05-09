"""推送决策 trace 落盘（L12）。

每次 notify_policy.submit / notify_filter.should_notify 做出"推 / 不推"判断时，
往 `data/push_decisions.jsonl` 追加一条记录，dashboard 可以直接看到原因。

设计要点：
- **不动 events.jsonl**：避免污染既有事件流，单独环形 log
- **轻量**：单行 JSON，按 session_id 截最近 N 条
- **per-session trim**：每条记录写入时在内存维护 sid → deque(max=50)，
  累积满 TRIM_THRESHOLD 行后整体重写一次（避免每次都全量重写）
- **崩溃可见**：进程重启时按文件 tail 重建 sid 索引，最多 50 * 活跃 session 数

读路径：
- `recent_for(sid, limit)` 给 list_sessions 注入字段用（默认 5 条）
- `all_for(sid)` 给 /api/session/{sid}/decisions endpoint 用（最多 50 条）
"""
from __future__ import annotations
import fcntl
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from threading import RLock
from typing import Any

from .config import DATA_DIR

log = logging.getLogger("claude-notify.decision_log")

DECISIONS_PATH = DATA_DIR / "push_decisions.jsonl"
PER_SESSION_MAX = 50
TRIM_THRESHOLD = 1000  # 文件累计行数 ≥ 此值时整体重写（保留每个 sid 最近 50 条）

_lock = RLock()


def append(
    session_id: str,
    event_type: str,
    decision: str,
    reason: str,
    policy: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """写一条决策记录。

    decision: "push" | "drop" | "scheduled" | "merged"
    reason:   notify_filter / notify_policy 返回的 reason 字符串
    policy:   "immediate" / "silence:12" / "off" 等（来自 notify_policy 配置）
    extra:    其它要落盘的字段（如 sensitivity / threshold_chars）
    """
    if not session_id:
        return
    rec = {
        "ts": round(time.time(), 3),
        "session_id": session_id,
        "event": event_type or "",
        "decision": decision,
        "reason": reason or "",
    }
    if policy:
        rec["policy"] = policy
    if extra:
        # 防 extra 把核心字段覆盖
        for k, v in extra.items():
            if k not in rec:
                rec[k] = v
    line = json.dumps(rec, ensure_ascii=False)

    DECISIONS_PATH.parent.mkdir(exist_ok=True)
    with _lock:
        try:
            with open(DECISIONS_PATH, "a", encoding="utf-8") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    f.write(line + "\n")
                    f.flush()
                except Exception:
                    pass
                finally:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
        except Exception:
            log.exception("decision_log append failed")
            return
        # 累计触发 trim
        try:
            line_count = _approx_line_count()
            if line_count >= TRIM_THRESHOLD:
                _trim_locked()
        except Exception:
            log.exception("decision_log trim failed")


def _approx_line_count() -> int:
    """便宜的行数估算 —— 用文件大小 / 平均行长（120 bytes）。

    精确计数需要全文件读，trim 阈值容忍误差，估算即可。
    """
    try:
        size = DECISIONS_PATH.stat().st_size
        return size // 120
    except Exception:
        return 0


def _read_all_records() -> list[dict[str, Any]]:
    if not DECISIONS_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(DECISIONS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        out.append(rec)
                except Exception:
                    continue
    except Exception:
        log.exception("decision_log read failed")
    return out


def _trim_locked() -> None:
    """锁内调用：保留每个 sid 最近 PER_SESSION_MAX 条，整体重写。"""
    records = _read_all_records()
    if not records:
        return
    # 按 sid 分组，保留每组最后 PER_SESSION_MAX 条；其它丢弃
    by_sid: dict[str, deque] = {}
    for rec in records:
        sid = rec.get("session_id") or ""
        if not sid:
            continue
        dq = by_sid.get(sid)
        if dq is None:
            dq = deque(maxlen=PER_SESSION_MAX)
            by_sid[sid] = dq
        dq.append(rec)
    # 拼回扁平 list（保留时间序，按 ts 升序）
    flat: list[dict[str, Any]] = []
    for dq in by_sid.values():
        flat.extend(dq)
    flat.sort(key=lambda r: r.get("ts") or 0)
    tmp = DECISIONS_PATH.with_suffix(".jsonl.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in flat:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, DECISIONS_PATH)
        log.info("decision_log trimmed: %d sessions, %d records kept",
                 len(by_sid), len(flat))
    except Exception:
        log.exception("decision_log trim write failed")
        try:
            tmp.unlink()
        except Exception:
            pass


def recent_for(session_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """读某 sid 最近 N 条决策（按 ts 倒序，最新在前）。

    供 event_store.list_sessions 注入 `recent_decisions` 字段用。
    """
    if not session_id:
        return []
    sid_buf: deque = deque(maxlen=max(1, int(limit)))
    if not DECISIONS_PATH.exists():
        return []
    try:
        with open(DECISIONS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("session_id") == session_id:
                    sid_buf.append(rec)
    except Exception:
        return []
    out = list(sid_buf)
    out.reverse()  # 最新在前
    return out


def all_for(session_id: str, limit: int = PER_SESSION_MAX) -> list[dict[str, Any]]:
    """读某 sid 所有决策（最多 PER_SESSION_MAX 条），按 ts 倒序。"""
    if not session_id:
        return []
    cap = max(1, min(int(limit), PER_SESSION_MAX))
    sid_buf: deque = deque(maxlen=cap)
    if not DECISIONS_PATH.exists():
        return []
    try:
        with open(DECISIONS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("session_id") == session_id:
                    sid_buf.append(rec)
    except Exception:
        return []
    out = list(sid_buf)
    out.reverse()
    return out


def reset_for_test() -> None:
    """测试用：清空 push_decisions.jsonl。生产代码不要调。"""
    with _lock:
        if DECISIONS_PATH.exists():
            try:
                DECISIONS_PATH.unlink()
            except Exception:
                pass
