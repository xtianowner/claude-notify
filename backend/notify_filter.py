"""推送过滤 should_notify —— v3 spec §4。

原则：
- 纯计算，不调 LLM，不读 transcript（cache 已有的 summary 字段可用）
- 只有「需要用户介入」的事件才返回 True
- TimeoutSuspect / TestNotify → 永远 True
- Notification → 跨事件 dedupe（教训 L05）：Stop 已推过 N 分钟内 + 内容是空话 → 吞
- SubagentStop / SessionStart / SessionEnd / SessionDead / Heartbeat / PreToolUse / PostToolUse → 永远 False
- Stop → 复合判别（last_milestone 长度 / turn_summary 长度 + 黑词表 / Notification 间距）
"""
from __future__ import annotations
import time
from typing import Any

# v3 spec §4.4 默认黑词表（短纯应答，不构成"任务回合结束"）
DEFAULT_BLACKLIST = (
    "ok", "yes", "no", "好", "好的", "已完成", "完成", "收到",
    "嗯", "是", "不", "对", "行", "可以", "thanks", "thank you", "ack",
)

DEFAULT_FILLER_PHRASES = (
    "waiting for your input",
    "press to continue",
    "press enter",
)

DEFAULT_FILTER_CFG = {
    "stop_min_milestone_chars": 12,
    "stop_min_summary_chars": 15,
    "stop_min_gap_after_notification_min": 5,
    "notif_suppress_after_stop_min": 3,
    "notif_filler_phrases": list(DEFAULT_FILLER_PHRASES),
    "blacklist_words": list(DEFAULT_BLACKLIST),
}


_ALWAYS_TRUE = {"TimeoutSuspect", "TestNotify"}
_ALWAYS_FALSE = {
    "SubagentStop", "SessionStart", "SessionEnd", "SessionDead",
    "Heartbeat", "PreToolUse", "PostToolUse",
}


def should_notify(
    evt: dict[str, Any],
    summary: dict[str, Any] | None,
    cfg: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """返回 (是否推送, 拒推原因 / 推送原因)。"""
    ev_type = (evt.get("event") or "").strip()
    if ev_type in _ALWAYS_TRUE:
        return True, f"always_true:{ev_type}"
    if ev_type in _ALWAYS_FALSE:
        return False, f"always_false:{ev_type}"
    if ev_type == "Notification":
        return _notification_decision(evt, summary or {}, cfg or {})
    if ev_type == "Stop":
        return _stop_decision(evt, summary or {}, cfg or {})
    # 未知事件类型保守 → False（与 notify_policy.submit 的 fallback 一致）
    return False, f"unknown_event:{ev_type}"


def _notification_decision(
    evt: dict[str, Any],
    summary: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    """L05 跨事件 dedupe：Claude Code 在主回合 Stop 后 ~60s 还会派一条
    Notification('waiting for your input') 提醒。对用户而言这是同一交互回合
    的两面（已完成 → 等输入），不该推两次。

    规则：last_stop_pushed_unix 距今 < N 分钟 且 message 是空话 → 吞。
    `last_stop_pushed_unix` 由 NotifyDispatcher 在 Stop 推送成功后注入到 summary。
    """
    fcfg = cfg.get("notify_filter") or {}
    suppress_min = float(fcfg.get("notif_suppress_after_stop_min", 3))
    filler_phrases = [p.strip().lower() for p in
                      (fcfg.get("notif_filler_phrases") or DEFAULT_FILLER_PHRASES) if p]

    last_stop_unix = float(summary.get("last_stop_pushed_unix") or 0)
    if last_stop_unix > 0 and suppress_min > 0:
        gap_min = (time.time() - last_stop_unix) / 60.0
        if gap_min < suppress_min:
            msg = (evt.get("message") or "").strip().lower()
            is_filler = (not msg) or any(p in msg for p in filler_phrases)
            if is_filler:
                return False, f"notif_dedup_after_stop({gap_min:.1f}min)"
    return True, "notification_kept"


def _stop_decision(
    evt: dict[str, Any],
    summary: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    fcfg = (cfg.get("notify_filter") or {})
    min_milestone = int(fcfg.get("stop_min_milestone_chars", 12))
    min_summary = int(fcfg.get("stop_min_summary_chars", 15))
    min_gap_min = float(fcfg.get("stop_min_gap_after_notification_min", 5))
    blacklist = {w.strip().lower() for w in (fcfg.get("blacklist_words") or DEFAULT_BLACKLIST) if w}

    last_milestone = (summary.get("last_milestone") or "").strip()
    turn_summary = (summary.get("turn_summary") or "").strip()
    last_assistant = (
        evt.get("last_assistant_message")
        or summary.get("last_assistant_message")
        or ""
    ).strip()

    # 条件 1：LLM 已提取出有内容的里程碑
    if len(last_milestone) >= min_milestone:
        return True, f"stop_milestone_len={len(last_milestone)}"

    # 条件 2：turn_summary 够长 + 不命中黑词表
    if len(turn_summary) >= min_summary and not _is_blacklisted(last_assistant, blacklist):
        return True, f"stop_summary_len={len(turn_summary)}"

    # 条件 3：距离上一次 Notification > M 分钟（兜底，cache miss 时用）
    last_notif_unix = float(summary.get("last_notification_unix") or 0)
    if last_notif_unix > 0:
        gap_min = (time.time() - last_notif_unix) / 60.0
        if gap_min > min_gap_min:
            return True, f"stop_gap_after_notif={gap_min:.1f}min"

    return False, "stop_low_signal"


def _is_blacklisted(text: str, blacklist: set[str]) -> bool:
    """判定 last_assistant_message 是否是「短纯应答」黑词。

    规则：strip + lower 后完全等于黑词，或长度 ≤ 4 字且不含标点/空格。
    """
    if not text:
        return True  # 空回复也算无信息
    norm = text.strip().lower()
    if norm in blacklist:
        return True
    # 短回复但含标点/空格 → 可能是有内容的短句，不算黑
    if len(text.strip()) <= 4:
        if not any(c.isspace() or c in "，。！？.!?,;；:：" for c in text.strip()):
            return True
    return False
