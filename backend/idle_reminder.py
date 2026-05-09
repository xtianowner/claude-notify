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

状态持久化策略：
- in-memory dict + lock；重启 backend 全部重置（用户预期 reminder 是临时状态，重启后从 0 开始可接受）
- 不写文件（避免 IO；与 decision_log 不同，decision_log 有用户回看价值）

容量保护：单实例最多保留 1024 个 sid 的计数（LRU 淘汰），防内存泄漏。
"""
from __future__ import annotations
from collections import OrderedDict
from threading import RLock

_MAX_ENTRIES = 1024

_lock = RLock()
_counts: "OrderedDict[str, int]" = OrderedDict()


def get_count(sid: str) -> int:
    if not sid:
        return 0
    with _lock:
        return _counts.get(sid, 0)


def mark_sent(sid: str) -> int:
    """该 sid 又发了一次 idle reminder，返回新的累计次数。"""
    if not sid:
        return 0
    with _lock:
        cur = _counts.get(sid, 0) + 1
        _counts[sid] = cur
        _counts.move_to_end(sid)
        # 容量保护
        while len(_counts) > _MAX_ENTRIES:
            _counts.popitem(last=False)
        return cur


def reset(sid: str) -> None:
    """新 Stop push 来 → 该 sid 进入新一回合，计数清零。"""
    if not sid:
        return
    with _lock:
        _counts.pop(sid, None)


def snapshot() -> dict[str, int]:
    """诊断用：返回当前内存里所有 sid 的 reminder 计数副本。"""
    with _lock:
        return dict(_counts)
