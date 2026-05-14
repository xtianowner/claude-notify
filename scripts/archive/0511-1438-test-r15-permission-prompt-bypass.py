#!/usr/bin/env python3
"""L40 / Round 15：notification_type=='permission_prompt' bypass dup 过滤。

bug：用户用 Claude Code 内置 AskUserQuestion tool 触发的菜单问询，
hook-notify 的 _detect_menu_prompt 不会识别（菜单结构在 tool_input.questions
里而非 assistant text），导致 raw.menu_detected=False，按 idle dup 阈值被吞。

修法：Claude Code Notification 事件 raw 里有官方信号
`notification_type: "permission_prompt"`（含 AskUserQuestion / 工具授权 /
未来新问询 tool），用它做 bypass 比 menu 文本识别更可靠。

测试 4 个 case：
  1. notification_type=permission_prompt + 没到 idle 阈值 → bypass dup 放行
  2. menu_detected=True（R11 旧路径）→ 仍 bypass（回归保护）
  3. 普通 idle filler，没 menu / 没 permission → 走原 idle dup 过滤
  4. event_store list_sessions：permission_prompt Notification 应让
     session.menu_detected=True（dashboard 🔥 徽亮起）
"""
from __future__ import annotations
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def t(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_filter_permission_prompt_bypass():
    print("\n[1] notify_filter: permission_prompt → bypass dup")
    from backend import notify_filter, idle_reminder

    sid = "test-r15-perm-prompt"
    idle_reminder.reset(sid)
    idle_reminder.mark_sent(sid)  # count=1 已用

    cfg = {"notify_filter": dict(notify_filter.DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 60}  # 1min ago，远没到 15min

    evt = {
        "event": "Notification",
        "session_id": sid,
        "message": "Claude Code needs your attention",  # filler 短语
        "raw": {"notification_type": "permission_prompt"},
    }
    ok, reason = notify_filter._notification_decision(evt, summary, cfg)
    t("permission_prompt 触发 bypass", ok and reason == "permission_prompt_bypass_dup",
      f"ok={ok} reason={reason}")
    idle_reminder.reset(sid)


def test_filter_menu_detected_still_works():
    print("\n[2] notify_filter: menu_detected=True 仍 bypass（回归保护）")
    from backend import notify_filter, idle_reminder

    sid = "test-r15-menu-still"
    idle_reminder.reset(sid)
    idle_reminder.mark_sent(sid)

    cfg = {"notify_filter": dict(notify_filter.DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 60}

    evt = {
        "event": "Notification",
        "session_id": sid,
        "message": "Claude is waiting for your input",
        "raw": {"menu_detected": True},
    }
    ok, reason = notify_filter._notification_decision(evt, summary, cfg)
    t("menu_detected=True 触发 bypass", ok and reason == "menu_detected_bypass_dup",
      f"ok={ok} reason={reason}")
    idle_reminder.reset(sid)


def test_filter_plain_idle_still_filtered():
    print("\n[3] notify_filter: 普通 idle filler 仍走 dup 过滤")
    from backend import notify_filter, idle_reminder

    sid = "test-r15-plain-idle"
    idle_reminder.reset(sid)
    idle_reminder.mark_sent(sid)

    cfg = {"notify_filter": dict(notify_filter.DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 60}

    evt = {
        "event": "Notification",
        "session_id": sid,
        "message": "Claude is waiting for your input",
        "raw": {},  # 没 menu，没 notification_type
    }
    ok, reason = notify_filter._notification_decision(evt, summary, cfg)
    t("普通 idle filler 被 dup 吞", (not ok) and reason.startswith("notif_idle_dup"),
      f"ok={ok} reason={reason}")
    idle_reminder.reset(sid)


def test_event_store_session_menu_detected_for_permission_prompt():
    print("\n[4] event_store: permission_prompt → session.menu_detected=True（🔥 徽）")
    from backend import event_store

    sid = "test-r15-event-store-perm"
    events = [
        {
            "ts": "2026-05-11T14:30:00+08:00",
            "session_id": sid,
            "source": "claude_code",
            "event": "Stop",
            "message": "",
            "raw": {},
        },
        {
            "ts": "2026-05-11T14:30:10+08:00",
            "session_id": sid,
            "source": "claude_code",
            "event": "Notification",
            "message": "Claude Code needs your attention",
            "raw": {"notification_type": "permission_prompt"},
        },
    ]
    with mock.patch.object(event_store, "iter_events", return_value=iter(events)):
        sessions = event_store.list_sessions()
    sess = next((s for s in sessions if s["session_id"] == sid), None)
    t("session 存在", sess is not None)
    if sess:
        t("permission_prompt → menu_detected=True",
          sess.get("menu_detected") is True,
          f"actual={sess.get('menu_detected')}")
        t("permission_prompt → status=waiting",
          sess.get("status") == "waiting",
          f"actual={sess.get('status')}")


def main():
    print("=== R15 / L40 permission_prompt bypass 测试 ===")
    test_filter_permission_prompt_bypass()
    test_filter_menu_detected_still_works()
    test_filter_plain_idle_still_filtered()
    test_event_store_session_menu_detected_for_permission_prompt()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
