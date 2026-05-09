from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import config as cfg_mod, event_store

log = logging.getLogger("claude-notify.feishu")

# 事件 → (emoji, 状态标签)。新模板把 emoji / 状态拆开，焦点放在 emoji + focus 之间。
EVENT_META = {
    "Notification":   ("🔔", "等你确认"),
    "Stop":           ("✅", "任务完成"),
    "SubagentStop":   ("🤝", "子 agent 完成"),
    "TimeoutSuspect": ("⚠️", "疑似 hang"),
    "SessionDead":    ("🪦", "会话已结束"),
    "SessionEnd":     ("👋", "会话退出"),
    "TestNotify":     ("🧪", "测试推送"),
}

DIVIDER = "─────────────────"


def _sign(secret: str, ts: int) -> str:
    string_to_sign = f"{ts}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _shorten_cwd(cwd: str) -> str:
    """/Users/<user>/foo/bar → ~/foo/bar；超过 2 段取末两段；空 → ''。"""
    if not cwd:
        return ""
    import os
    home = os.path.expanduser("~")
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        cwd = "~" + cwd[len(home):]
    parts = cwd.split(os.sep)
    parts = [p for p in parts if p]  # 去空
    if len(parts) <= 2:
        return cwd
    return ".../" + "/".join(parts[-2:])


def _format_hms(ts: str) -> str:
    """ISO 8601 → 'HH:MM:SS'（上海时区）；失败返回空串。"""
    try:
        from datetime import datetime, timezone, timedelta
        if ts.endswith("Z"):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(ts)
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    except Exception:
        return ""


def _format_text(evt: dict[str, Any], summary: dict[str, Any] | None, cfg: dict[str, Any]) -> str:
    """5 行紧凑模板（参考 OneSignal BLUF / Slack-Notify 模板）：

      L1  ✅ hello · 任务完成               ← emoji + focus + 状态
      L2  你好！有什么需要帮忙的？           ← 核心一句（条件渲染）
      L3  → 提下一轮 / 或归档                ← next_action（条件渲染）
      L4  ─────────────────                  ← 分隔
      L5  bypass · xhigh · 19:08:29          ← mode · effort · HH:MM:SS
      L6  f39866f5 · ~/Tian · ↗ dashboard   ← sid8 · cwd_short · 链接

    日期由飞书消息列表自带时间戳承担，正文只放 HH:MM:SS。
    """
    ev = evt.get("event") or "Event"
    emoji, status = EVENT_META.get(ev, ("📌", ev))
    sid = evt.get("session_id") or ""
    cwd = evt.get("cwd") or (summary or {}).get("cwd") or ""
    sm = summary or {}

    task_topic = (sm.get("task_topic") or "").strip()
    display_name = sm.get("display_name") or sid[:8]
    focus = task_topic or display_name

    waiting_for = sm.get("waiting_for") or ""
    last_action = sm.get("last_action") or ""
    last_milestone = sm.get("last_milestone") or ""
    next_action = sm.get("next_action") or ""
    permission_mode = sm.get("permission_mode") or evt.get("permission_mode") or ""
    effort_level = sm.get("effort_level") or evt.get("effort_level") or ""
    age_seconds = int(sm.get("age_seconds") or 0)
    age_min = max(1, age_seconds // 60)

    # L1：BLUF —— emoji + focus + 状态
    if focus:
        lines = [f"{emoji} {focus} · {status}"]
    else:
        lines = [f"{emoji} {status}"]

    # L2 + L3：核心信息 + 下一步（按事件类型）
    if ev == "Notification":
        if waiting_for:
            lines.append(_truncate(waiting_for, 80))
        if next_action:
            lines.append(f"→ {next_action}")
        elif waiting_for:
            lines.append("→ 确认")
    elif ev == "TimeoutSuspect":
        action_hint = f"（上次 {last_action}）" if last_action else ""
        lines.append(f"卡 {age_min} 分钟{action_hint}")
        lines.append(f"→ {next_action or '进终端检查'}")
    elif ev in ("Stop", "SubagentStop"):
        if last_milestone:
            lines.append(_truncate(last_milestone, 100))
        elif last_action:
            lines.append(f"上一步: {last_action}")
        if next_action:
            lines.append(f"→ {next_action}")
    elif ev == "SessionDead":
        lines.append(evt.get("message") or "会话疑似已结束")
    elif ev == "SessionEnd":
        pass  # L1 已经说了"会话退出"
    else:
        msg = evt.get("message") or ""
        if msg:
            lines.append(_truncate(msg, 100))

    # 分隔
    lines.append(DIVIDER)

    # L5：mode · effort · HH:MM:SS
    meta_bits = [b for b in (permission_mode, effort_level) if b]
    hms = _format_hms((evt.get("ts") or "").strip())
    if hms:
        meta_bits.append(hms)
    if meta_bits:
        lines.append(" · ".join(meta_bits))

    # L6：sid8 · cwd_short · ↗ dashboard
    tail_bits = []
    if sid:
        tail_bits.append(sid[:8])
    cwd_short = _shorten_cwd(cwd)
    if cwd_short:
        tail_bits.append(cwd_short)
    if sid:
        tail_bits.append(f"↗ http://127.0.0.1:8787/#s={sid}")
    if tail_bits:
        lines.append(" · ".join(tail_bits))

    return "\n".join(lines)


async def _post(webhook: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(webhook, json=body)
        try:
            return r.json()
        except Exception:
            return {"status_code": r.status_code, "text": r.text[:200]}


EVENT_SHORT = {
    "Stop":           "任务回合结束",
    "SubagentStop":   "子 agent 完成",
    "Notification":   "等你确认",
    "TimeoutSuspect": "疑似 hang",
    "SessionDead":    "会话已结束",
    "SessionEnd":     "会话退出",
}


async def send_events_combined(events: list[dict[str, Any]]) -> dict[str, Any]:
    """合并多个 silence 期内累积的事件为一条卡片（按最新事件主格式渲染 + 历史事件 footnote）。

    - events 至少 1 条；为 1 条时退化为 send_event(events[0])
    - 多条时取最新（events[-1]）作为「主事件」，更早的事件作为单行历史提示
    - 不影响 enrichments / event_store 写入（这些已在 dispatch 链路上游做完）
    """
    if not events:
        return {"ok": False, "reason": "empty"}
    if len(events) == 1:
        return await send_event(events[0])

    main_evt = events[-1]
    earlier = events[:-1]

    cfg = cfg_mod.load()
    if cfg.get("muted"):
        return {"ok": False, "reason": "muted"}
    snooze = (cfg.get("snooze_until") or "").strip()
    if snooze:
        try:
            from .event_store import parse_iso
            snooze_unix = parse_iso(snooze)
            if snooze_unix > time.time():
                return {"ok": False, "reason": "snoozed", "until": snooze}
        except Exception:
            pass
    webhook = (cfg.get("feishu_webhook") or "").strip()
    if not webhook:
        return {"ok": False, "reason": "no_webhook"}

    summary = None
    sid = main_evt.get("session_id")
    if sid:
        try:
            for s in event_store.list_sessions(active_window_minutes=int(cfg.get("active_window_minutes") or 30)):
                if s["session_id"] == sid:
                    summary = s
                    break
        except Exception:
            log.exception("list_sessions for combined summary failed")

    base_text = _format_text(main_evt, summary, cfg)

    # 在 DIVIDER 行（视觉分隔）前插入合并提示，让"另含 N 个先发事件"和主内容同区呈现。
    prior = "、".join(EVENT_SHORT.get(e.get("event") or "", e.get("event") or "?") for e in earlier)
    merge_line = f"另含 {len(earlier)} 个先发事件: {prior}"

    lines = base_text.split("\n")
    try:
        insert_at = lines.index(DIVIDER)
    except ValueError:
        # DIVIDER 不存在（极端 fallback），插到尾部前一行
        insert_at = max(0, len(lines) - 1)
    lines.insert(insert_at, merge_line)
    text = "\n".join(lines)

    body: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
    secret = (cfg.get("feishu_secret") or "").strip()
    if secret:
        ts = int(time.time())
        body["timestamp"] = str(ts)
        body["sign"] = _sign(secret, ts)
    try:
        resp = await _post(webhook, body)
        return {"ok": True, "merged": len(events), "resp": resp}
    except Exception as e:
        log.exception("combined send failed")
        return {"ok": False, "reason": str(e)[:120]}


async def send_event(evt: dict[str, Any]) -> dict[str, Any]:
    """直接发送（不再做策略判断；策略由 notify_policy 接管）。"""
    cfg = cfg_mod.load()
    if cfg.get("muted"):
        return {"ok": False, "reason": "muted"}
    snooze = (cfg.get("snooze_until") or "").strip()
    if snooze:
        try:
            from .event_store import parse_iso
            snooze_unix = parse_iso(snooze)
            if snooze_unix > time.time():
                return {"ok": False, "reason": "snoozed", "until": snooze}
        except Exception:
            pass
    webhook = (cfg.get("feishu_webhook") or "").strip()
    if not webhook:
        return {"ok": False, "reason": "no_webhook"}

    summary = None
    sid = evt.get("session_id")
    if sid:
        try:
            for s in event_store.list_sessions(active_window_minutes=int(cfg.get("active_window_minutes") or 30)):
                if s["session_id"] == sid:
                    summary = s
                    break
        except Exception:
            log.exception("list_sessions for summary failed")

    text = _format_text(evt, summary, cfg)
    body: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": text},
    }
    secret = (cfg.get("feishu_secret") or "").strip()
    if secret:
        ts = int(time.time())
        body["timestamp"] = str(ts)
        body["sign"] = _sign(secret, ts)

    try:
        resp = await _post(webhook, body)
        ok = resp.get("StatusCode") == 0 or resp.get("code") == 0 or resp.get("status_code") == 200
        return {"ok": bool(ok), "reason": "sent" if ok else "feishu_rejected", "response": resp}
    except Exception as e:
        return {"ok": False, "reason": f"exception: {e}"}


async def send_test() -> dict[str, Any]:
    return await send_event({
        "event": "TestNotify",
        "project": "claude-notify",
        "cwd": str(Path(__file__).resolve().parent.parent),
        "cwd_short": "00-Tian-Project/claude-notify",
        "message": "claude-notify 测试推送 — 你看到这条说明配置成功 ✅",
        "session_id": "test-session-0001",
    })
