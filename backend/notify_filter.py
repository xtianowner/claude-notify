"""推送过滤 should_notify —— v3 spec §4。

原则：
- 纯计算，不调 LLM，不读 transcript（cache 已有的 summary 字段可用）
- 只有「需要用户介入」的事件才返回 True
- TimeoutSuspect / TestNotify → 永远 True
- Notification → 跨事件 dedupe（教训 L05）：Stop 已推过 N 分钟内 + 内容是空话 → 吞
- SubagentStop / SessionStart / SessionEnd / SessionDead / Heartbeat / PreToolUse / PostToolUse → 永远 False
- Stop → 复合判别（教训 L11）：以下任一即放过
    1. milestone / summary 字数过阈值（fast/normal/strict 三档可调）
    2. 含锚词（"完成 / done / passed / 失败 / 报错 …"）→ 放过不计字数
    3. 距离同 sid 上一次推送 > grace_min → 长任务收尾即使一字也推
"""
from __future__ import annotations
import time
from datetime import datetime
from typing import Any

from . import decision_log, idle_reminder

# L15：quiet_hours 默认放行的事件白名单（即使时段命中也照推）。
# 用户视角：「真等输入 / 疑挂」是关键事件，再急也得叫醒；其它（Stop / SubagentStop / Heartbeat）夜里吞掉。
DEFAULT_QUIET_HOURS_PASSTHROUGH = ("Notification", "TimeoutSuspect", "TestNotify")

# v3 spec §4.4 默认黑词表（短纯应答，不构成"任务回合结束"）
# L11：黑词检测改为「整句严格相等」（norm in blacklist），不再做子串匹配，
# 否则"已完成端到端验证"会被"已完成"误吞。
DEFAULT_BLACKLIST = (
    "ok", "yes", "no", "好", "好的", "已完成", "完成", "收到",
    "嗯", "是", "不", "对", "行", "可以", "thanks", "thank you", "ack",
)

DEFAULT_FILLER_PHRASES = (
    "claude is waiting for your input",
    "claude code needs your attention",
    "press to continue",
    "press enter",
)

# Stop sensitivity 三档预设（L11）
# fast：用户希望"短回合也推"，字数阈值 4
# normal：默认，字数阈值 milestone=6 / summary=8（比旧版 12/15 显著放宽）
# strict：用户希望"只推有内容回合"，字数阈值 20
STOP_SENSITIVITY_PRESETS = {
    "fast":   {"stop_min_milestone_chars": 4,  "stop_min_summary_chars": 4},
    "normal": {"stop_min_milestone_chars": 6,  "stop_min_summary_chars": 8},
    "strict": {"stop_min_milestone_chars": 20, "stop_min_summary_chars": 20},
}

DEFAULT_FILTER_CFG = {
    "stop_min_milestone_chars": 6,
    "stop_min_summary_chars": 8,
    "stop_min_gap_after_notification_min": 5,
    "stop_short_summary_grace_min": 8,
    "stop_sensitivity": "normal",
    # L22：每个任务最多 3 次推送（任务完成 + 5min + 10min）
    "notif_idle_reminder_minutes": [5, 10],
    # 兼容字段（已废弃，仅当 notif_idle_reminder_minutes 被显式设为 [] 时才走 strict 模式）
    "notif_suppress_after_stop_min": 3,
    "notif_filler_phrases": list(DEFAULT_FILLER_PHRASES),
    "blacklist_words": list(DEFAULT_BLACKLIST),
    # 教训 L08：子 agent 内触发的 Notification 用户切到终端看不到提示（父 session
    # 已恢复或子 agent 自处理），形成"虚假打扰"。默认开启过滤。
    # 关掉：data/config.json 设 notify_filter.filter_sidechain_notifications=false
    "filter_sidechain_notifications": True,
}


_ALWAYS_TRUE = {"TimeoutSuspect", "TestNotify"}
_ALWAYS_FALSE = {
    "SubagentStop", "SessionStart", "SessionEnd", "SessionDead",
    "Heartbeat", "PreToolUse", "PostToolUse",
}


def _resolve_thresholds(fcfg: dict[str, Any]) -> tuple[int, int]:
    """根据 stop_sensitivity 解析最终的 milestone / summary 字数阈值。

    sensitivity != "normal" 时覆盖用户配的 stop_min_*_chars；
    sensitivity == "normal" 时尊重用户配置（即用户调过 chars 字段也保留）。
    """
    sens = (fcfg.get("stop_sensitivity") or "normal").strip().lower()
    if sens in STOP_SENSITIVITY_PRESETS and sens != "normal":
        preset = STOP_SENSITIVITY_PRESETS[sens]
        return int(preset["stop_min_milestone_chars"]), int(preset["stop_min_summary_chars"])
    return (
        int(fcfg.get("stop_min_milestone_chars", 6)),
        int(fcfg.get("stop_min_summary_chars", 8)),
    )


def should_notify(
    evt: dict[str, Any],
    summary: dict[str, Any] | None,
    cfg: dict[str, Any] | None = None,
    *,
    log_trace: bool = True,
) -> tuple[bool, str]:
    """返回 (是否推送, 拒推原因 / 推送原因)。

    log_trace=False 时不写 decision_log（用于"预览/合并去重"等不算独立决策的调用）。
    """
    ev_type = (evt.get("event") or "").strip()
    sid = evt.get("session_id") or ""

    # L14：per-session 静音 —— 前置于所有其它过滤
    sm_drop, sm_reason = _session_mute_check(sid, ev_type, cfg or {})
    if sm_drop:
        ok, reason = False, sm_reason
        if log_trace and sid:
            try:
                decision_log.append(
                    session_id=sid,
                    event_type=ev_type,
                    decision="drop",
                    reason=reason,
                    policy="filter",
                )
            except Exception:
                pass
        return ok, reason

    # L15：全局 quiet_hours —— 顺序在 session_mute 之后、_stop/_notification 决策之前
    qh_drop, qh_reason = _quiet_hours_check(ev_type, cfg or {})
    if qh_drop:
        ok, reason = False, qh_reason
        if log_trace and sid:
            try:
                decision_log.append(
                    session_id=sid,
                    event_type=ev_type,
                    decision="drop",
                    reason=reason,
                    policy="filter",
                )
            except Exception:
                pass
        return ok, reason

    if ev_type in _ALWAYS_TRUE:
        ok, reason = True, f"always_true:{ev_type}"
    elif ev_type in _ALWAYS_FALSE:
        ok, reason = False, f"always_false:{ev_type}"
    elif ev_type == "Notification":
        ok, reason = _notification_decision(evt, summary or {}, cfg or {})
    elif ev_type == "Stop":
        ok, reason = _stop_decision(evt, summary or {}, cfg or {})
    else:
        # 未知事件类型保守 → False（与 notify_policy.submit 的 fallback 一致）
        ok, reason = False, f"unknown_event:{ev_type}"
    if log_trace and sid:
        try:
            decision_log.append(
                session_id=sid,
                event_type=ev_type,
                decision=("push" if ok else "drop"),
                reason=reason,
                policy="filter",
            )
        except Exception:
            pass
    return ok, reason


def _notification_decision(
    evt: dict[str, Any],
    summary: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    """L05 跨事件 dedupe + L08 sidechain 过滤。

    Claude Code 在主回合 Stop 后 ~60s 还会派一条 Notification('waiting for your input')
    提醒。对用户而言这是同一交互回合的两面（已完成 → 等输入），不该推两次。
    L05 规则：last_stop_pushed_unix 距今 < N 分钟 且 message 是空话 → 吞。

    L08 规则：is_sidechain=True（hook 触发瞬间 subagents 目录有最近 5 秒活跃 jsonl）
    → 子 agent 内的权限确认 / idle prompt 用户切到终端通常已被父 session 恢复，
    推送只造成虚假打扰 → 吞。可在 config 用 filter_sidechain_notifications 关闭。
    """
    fcfg = cfg.get("notify_filter") or {}

    # L08 sidechain 过滤
    if fcfg.get("filter_sidechain_notifications", True) and evt.get("is_sidechain"):
        return False, "sidechain_active"

    suppress_min = float(fcfg.get("notif_suppress_after_stop_min", 3))
    filler_phrases = [p.strip().lower() for p in
                      (fcfg.get("notif_filler_phrases") or DEFAULT_FILLER_PHRASES) if p]

    last_stop_unix = float(summary.get("last_stop_pushed_unix") or 0)
    if last_stop_unix > 0:
        msg = (evt.get("message") or "").strip().lower()
        # L19：整句相等匹配，避免误吞带后缀的真权限请求
        is_filler = (not msg) or msg in filler_phrases
        if is_filler:
            return _idle_reminder_decision(evt, fcfg, last_stop_unix)
    return True, "notification_kept"


def _idle_reminder_decision(
    evt: dict[str, Any],
    fcfg: dict[str, Any],
    last_stop_unix: float,
) -> tuple[bool, str]:
    """L22：idle prompt 来时按 reminder 计数 + gap 阈值决定推/吞。

    规则：
    - reminder_minutes 列表为空 → 严格 1 次模式（永远吞副本，等同 L21）
    - 已发 reminder 数 >= len(reminder_minutes) → 达到上限，吞
    - 未达上限 + gap >= reminder_minutes[count] → 推（mark_sent）
    - 未达上限但 gap 不够 → 吞（等下一次 idle prompt 来再判）
    """
    sid = (evt.get("session_id") or "").strip()
    reminder_minutes = fcfg.get("notif_idle_reminder_minutes")
    if not isinstance(reminder_minutes, (list, tuple)):
        reminder_minutes = []
    reminder_minutes = [float(x) for x in reminder_minutes if x is not None]
    if not reminder_minutes:
        # 严格 1 次模式（用户配置 [] = 任务完成那次推完后绝不再推）
        return False, "notif_dup_until_next_task"

    sent = idle_reminder.get_count(sid)
    if sent >= len(reminder_minutes):
        return False, f"notif_idle_max_reminders({sent})"

    threshold_min = reminder_minutes[sent]
    gap_min = (time.time() - last_stop_unix) / 60.0
    if gap_min < threshold_min:
        return False, f"notif_idle_dup({gap_min:.1f}min<{threshold_min:.0f}min)"

    new_count = idle_reminder.mark_sent(sid)
    return True, f"notif_idle_reminder_{new_count}_at_{gap_min:.1f}min"


def _has_anchor(text: str) -> bool:
    """是否含锚词（结论性 / 状态性词汇）。复用 sources.py 的 _ANCHOR_KEYWORDS。

    导入失败 → 用最小内置兜底，不阻塞过滤逻辑。
    """
    if not text:
        return False
    try:
        from .sources import _ANCHOR_KEYWORDS as kws
    except Exception:
        kws = (
            "完成", "已完成", "已修复", "已实现", "通过", "成功", "失败", "报错",
            "fixed", "done", "passed", "failed", "completed",
        )
    s = text.lower()
    return any(kw.lower() in s for kw in kws)


def _stop_decision(
    evt: dict[str, Any],
    summary: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    """Stop 推送判定（L11 重构）。

    通过条件「以下任一」：
    1. milestone 长度 ≥ stop_min_milestone_chars
    2. summary 长度 ≥ stop_min_summary_chars 且 last_assistant 不命中黑词（整句相等）
    3. last_milestone / turn_summary / last_assistant 任一含锚词 → 直接放过
    4. 距离同 sid 上一次推送（Stop 推 ts 与 Notification 事件 ts 取 max）
       > stop_short_summary_grace_min → 长任务收尾即使一字也推
    5. 距离上一次 Notification > stop_min_gap_after_notification_min（保留旧兜底）
    """
    fcfg = (cfg.get("notify_filter") or {})
    min_milestone, min_summary = _resolve_thresholds(fcfg)
    min_gap_min = float(fcfg.get("stop_min_gap_after_notification_min", 5))
    grace_min = float(fcfg.get("stop_short_summary_grace_min", 8))
    blacklist = {w.strip().lower() for w in (fcfg.get("blacklist_words") or DEFAULT_BLACKLIST) if w}

    last_milestone = (summary.get("last_milestone") or "").strip()
    turn_summary = (summary.get("turn_summary") or "").strip()
    last_assistant = (
        evt.get("last_assistant_message")
        or summary.get("last_assistant_message")
        or ""
    ).strip()

    # 条件 1：里程碑够长
    if len(last_milestone) >= min_milestone:
        return True, f"stop_milestone_len={len(last_milestone)}"

    # 条件 2：summary 够长 + 不命中黑词表（整句严格相等）
    if len(turn_summary) >= min_summary and not _is_blacklisted(last_assistant, blacklist):
        return True, f"stop_summary_len={len(turn_summary)}"

    # 条件 3：含锚词 → 放过不计字数
    for src_name, src_text in (
        ("milestone", last_milestone),
        ("summary", turn_summary),
        ("last_assistant", last_assistant),
    ):
        if _has_anchor(src_text):
            return True, f"stop_anchor:{src_name}"

    # 条件 4：长任务时间窗 fallback —— 距离同 sid 上一次推送 > grace_min
    last_stop_unix = float(summary.get("last_stop_pushed_unix") or 0)
    last_notif_unix = float(summary.get("last_notification_unix") or 0)
    last_any_push = max(last_stop_unix, last_notif_unix)
    if grace_min > 0:
        if last_any_push <= 0:
            # 同 sid 从未推过任何东西 → 这是回合首推，直接放过
            return True, "stop_grace_first_push"
        gap_min = (time.time() - last_any_push) / 60.0
        if gap_min > grace_min:
            return True, f"stop_grace_gap={gap_min:.1f}min"

    # 条件 5：旧兜底，距离上一次 Notification > min_gap_min（cache miss 时仍能救）
    if last_notif_unix > 0:
        gap_min = (time.time() - last_notif_unix) / 60.0
        if gap_min > min_gap_min:
            return True, f"stop_gap_after_notif={gap_min:.1f}min"

    # 条件 6（L16）：last_assistant_message 长度兜底。
    # 新 session 刚 SessionStart 完，enrichment 还没来得及派生 milestone/turn_summary，
    # 此时短 Stop 会被 stop_low_signal 误吞。原始 last_assistant_message ≥ 12 字
    # 且不命中黑词，视为"用户视角有内容"放过。
    if len(last_assistant) >= 12 and not _is_blacklisted(last_assistant, blacklist):
        return True, f"stop_assistant_len={len(last_assistant)}"

    return False, "stop_low_signal"


# L14：per-session 静音范围，决定 stop_only 时哪些事件该被吞
_STOP_LIKE_EVENTS = {"Stop", "SubagentStop"}


def _session_mute_check(
    sid: str,
    ev_type: str,
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    """判定该 session 是否被用户手动静音（前置于 _stop_decision / _notification_decision）。

    返回 (drop, reason)。drop=True → 上层立即 return False 并写 decision_log。
    drop=False 时 reason 不使用。

    规则：
    - 命中 session_mutes[sid] 且 (until is None 或 now < until)：
      - scope=all → 一律 drop（reason='session_muted_all'）
      - scope=stop_only → 仅 Stop / SubagentStop drop；Notification / TimeoutSuspect 通过
    - 命中且 until 已过期 → 异步清理（best-effort），返回 not-muted
    """
    if not sid:
        return False, ""
    sm = (cfg.get("session_mutes") or {})
    entry = sm.get(sid)
    if not isinstance(entry, dict):
        return False, ""
    until = entry.get("until")
    if until is not None:
        try:
            if float(until) <= time.time():
                # 过期：best-effort 清理（写错不阻塞过滤）
                try:
                    from . import config as _cfg_mod
                    _cfg_mod.clear_session_mute(sid)
                except Exception:
                    pass
                return False, ""
        except Exception:
            return False, ""
    scope = (entry.get("scope") or "all").strip().lower()
    if scope == "stop_only":
        if ev_type in _STOP_LIKE_EVENTS:
            return True, "session_muted_stop_only"
        return False, ""
    # 默认 scope=all
    return True, "session_muted_all"


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    """解析 'HH:MM' → (hour, minute)。非法返回 None。"""
    if not s or ":" not in s:
        return None
    try:
        h_str, m_str = s.strip().split(":", 1)
        h = int(h_str)
        m = int(m_str)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        return None
    return None


def _now_in_tz(tz_str: str) -> datetime:
    """按 IANA tz 字符串取当前时间。tz 非法 / zoneinfo 不可用 → 回退本地 datetime.now()。"""
    if tz_str:
        try:
            from zoneinfo import ZoneInfo  # py 3.9+
            return datetime.now(ZoneInfo(tz_str))
        except Exception:
            pass
    return datetime.now()


def _in_quiet_window(now_h: int, now_m: int, start_h: int, start_m: int,
                     end_h: int, end_m: int) -> bool:
    """判定 (now_h, now_m) 是否在 [start, end) 时段内。

    跨午夜（start > end）时按 "start..24:00 ∪ 00:00..end" 处理。
    边界约定：start 时刻视为已进入 quiet；end 时刻视为已离开（半开区间 [start, end)）。
    start == end → 视为「不静音」（用户配反了，安全 default）。
    """
    cur = now_h * 60 + now_m
    s = start_h * 60 + start_m
    e = end_h * 60 + end_m
    if s == e:
        return False
    if s < e:
        # 同日窗口（如 13:00 → 17:00）
        return s <= cur < e
    # 跨午夜窗口（如 23:00 → 08:00）
    return cur >= s or cur < e


def _quiet_hours_check(ev_type: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    """L15：全局夜间不打扰判定。

    返回 (drop, reason)。drop=True → 上层立即 return False 并写 decision_log。

    规则：
    - enabled=False → 不静音（drop=False）
    - 当前时间不在 [start, end) 内 → 不静音
    - weekdays_only=True 且当前是周末 → 不静音
    - 事件在 passthrough_events 列表内 → 不静音（关键事件穿透）
    - 否则 → drop=True，reason='quiet_hours_HH:MM'（HH:MM 即当前时分，dashboard 看 trace 秒懂）
    """
    qh = cfg.get("quiet_hours") or {}
    if not qh.get("enabled"):
        return False, ""
    start = _parse_hhmm(qh.get("start") or "")
    end = _parse_hhmm(qh.get("end") or "")
    if not start or not end:
        # 配置不完整 → 安全默认：不静音（避免静默吞事件让用户看不到）
        return False, ""

    tz_str = (qh.get("tz") or "").strip()
    now = _now_in_tz(tz_str)

    if qh.get("weekdays_only"):
        # weekday(): Mon=0 .. Sun=6 → 5/6 为周末
        if now.weekday() >= 5:
            return False, ""

    if not _in_quiet_window(now.hour, now.minute, start[0], start[1], end[0], end[1]):
        return False, ""

    # 当前在 quiet 时段内 —— 检查 passthrough
    passthrough = qh.get("passthrough_events")
    if not isinstance(passthrough, (list, tuple)):
        passthrough = list(DEFAULT_QUIET_HOURS_PASSTHROUGH)
    if ev_type in set(passthrough):
        return False, ""

    return True, f"quiet_hours_{now.hour:02d}:{now.minute:02d}"


def _is_blacklisted(text: str, blacklist: set[str]) -> bool:
    """L11：判定 last_assistant_message 是否是「短纯应答」黑词。

    旧规则：strip + lower 后完全等于黑词，或长度 ≤ 4 字且不含标点/空格。
    L11 改动：黑词只比对**整句严格相等**（norm in blacklist），不再退化到子串匹配。
    "已完成端到端验证" 不再被 "已完成" 吞；"完成" 一字仍被吞。
    """
    if not text:
        return True  # 空回复也算无信息
    norm = text.strip().lower()
    if norm in blacklist:
        return True
    # 长度 ≤ 4 字 + 完全无标点空格 → 仍按短纯应答处理（边界保护）
    if len(text.strip()) <= 4:
        if not any(c.isspace() or c in "，。！？.!?,;；:：" for c in text.strip()):
            return True
    return False
