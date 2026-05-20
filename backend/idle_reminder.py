"""L22: per-session idle reminder 计数器。

设计目的：限制 Claude Code idle prompt（"waiting for your input" /
"needs your attention"）在一个回合内最多被推送多少次。

用户期望：
1. 第 1 次：任务完成（Stop push）—— 这条不由本模块管，由 notify_filter._stop_decision 控制
2. 第 2 次：5 分钟后用户没反应，推一次提醒
3. 第 3 次：10 分钟后用户还没反应，最后一次提醒
4. 之后：再来 idle prompt 也不推（即使用户离开 1 小时）

下一次新 Stop push 来时（= 用户又触发了新任务） → 计数重置 → 重新走 3 次循环。

接口：
- get_count(sid) → 已发了多少次 reminder（不含初始 Stop push）
- mark_sent(sid) → reminder 计数 +1
- reset(sid) → 计数清零（在 Stop push 成功时调用）

状态持久化策略（R50/F2）：
- 落盘 data/idle_reminder.json，schema: {"<sid>": {"count": int, "last_at": iso8601}}
- mark_sent / reset 后同步落盘（tmp + os.replace 原子写）
- 模块首次访问时 lazy load（与 hidden_sessions.py 模式一致）
- 重启 backend 时计数从盘恢复 —— 防止"重启后用户被当作新一回合再推 reminder #1"

并发：threading.RLock；与 dispatcher（async）跨线程安全（mark_sent/reset 可能从
任意 event loop 线程调用，且 hidden_sessions 也是同模式）。

容量保护：单实例最多保留 _MAX_ENTRIES（512）个 sid 的计数（LRU 淘汰），防内存/磁盘爆炸。
"""
from __future__ import annotations
import json
import os
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock

from .config import DATA_DIR

_MAX_ENTRIES = 512  # R50/F2：从 1024 降到 512，落盘场景下控制文件体积
_REMINDER_PATH: Path = DATA_DIR / "idle_reminder.json"
_SHANGHAI_TZ = timezone(timedelta(hours=8))

_lock = RLock()
# entry: {"count": int, "last_at": iso8601}
_counts: "OrderedDict[str, dict] | None" = None  # None = 尚未 lazy load


def _now_iso() -> str:
    return datetime.now(_SHANGHAI_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _load_unlocked() -> "OrderedDict[str, dict]":
    """从盘加载到 _counts。损坏 / 不存在 → 空 OrderedDict。"""
    if not _REMINDER_PATH.exists():
        return OrderedDict()
    try:
        raw = json.loads(_REMINDER_PATH.read_text("utf-8"))
    except Exception:
        return OrderedDict()
    if not isinstance(raw, dict):
        return OrderedDict()
    out: "OrderedDict[str, dict]" = OrderedDict()
    for sid, entry in raw.items():
        if not isinstance(sid, str) or not sid:
            continue
        # 容错：旧格式 {sid: int} 也接住
        if isinstance(entry, int):
            out[sid] = {"count": entry, "last_at": ""}
            continue
        if not isinstance(entry, dict):
            continue
        try:
            cnt = int(entry.get("count") or 0)
        except Exception:
            cnt = 0
        if cnt <= 0:
            continue
        out[sid] = {
            "count": cnt,
            "last_at": str(entry.get("last_at") or ""),
        }
    return out


def _ensure_loaded_unlocked() -> "OrderedDict[str, dict]":
    global _counts
    if _counts is None:
        _counts = _load_unlocked()
    return _counts


def _persist_unlocked() -> None:
    """tmp + os.replace 原子写。调用方必须已持 _lock。"""
    global _counts
    if _counts is None:
        return
    data = {sid: dict(entry) for sid, entry in _counts.items()}
    try:
        _REMINDER_PATH.parent.mkdir(exist_ok=True)
        tmp = _REMINDER_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, _REMINDER_PATH)
    except Exception:
        # 落盘失败不抛 —— 内存计数仍然有效，下一次 mark_sent/reset 会重试
        pass


def get_count(sid: str) -> int:
    if not sid:
        return 0
    with _lock:
        c = _ensure_loaded_unlocked()
        entry = c.get(sid)
        if not entry:
            return 0
        try:
            return int(entry.get("count") or 0)
        except Exception:
            return 0


def mark_sent(sid: str) -> int:
    """该 sid 又发了一次 idle reminder，返回新的累计次数。"""
    if not sid:
        return 0
    with _lock:
        c = _ensure_loaded_unlocked()
        entry = c.get(sid) or {"count": 0, "last_at": ""}
        try:
            cur = int(entry.get("count") or 0) + 1
        except Exception:
            cur = 1
        entry = {"count": cur, "last_at": _now_iso()}
        c[sid] = entry
        c.move_to_end(sid)
        # 容量保护（LRU 淘汰最老）
        while len(c) > _MAX_ENTRIES:
            c.popitem(last=False)
        _persist_unlocked()
        return cur


def reset(sid: str) -> None:
    """新 Stop push 来 → 该 sid 进入新一回合，计数清零。"""
    if not sid:
        return
    with _lock:
        c = _ensure_loaded_unlocked()
        if sid not in c:
            return
        c.pop(sid, None)
        _persist_unlocked()


def snapshot() -> dict[str, int]:
    """诊断用：返回当前所有 sid 的 reminder 计数副本（仅 count，向后兼容）。"""
    with _lock:
        c = _ensure_loaded_unlocked()
        out: dict[str, int] = {}
        for sid, entry in c.items():
            try:
                out[sid] = int(entry.get("count") or 0)
            except Exception:
                out[sid] = 0
        return out


def _reload_for_tests() -> None:
    """测试钩子：强制下一次访问 lazy load 重新读盘。"""
    global _counts
    with _lock:
        _counts = None
