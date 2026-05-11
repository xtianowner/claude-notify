#!/usr/bin/env python3
"""R11 hotfix 测试：menu_detected 两类 bug 修复。

Bug 1：_detect_menu_prompt 只看最后一条 assistant message，不被历史误命中
Bug 2：event_store.list_sessions 在 raw.menu_detected=true 时强制 status=waiting
Bug 3（regression）：notify_filter bypass_dup 主功能仍工作
"""
from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def t(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _load_hook_notify():
    """importlib 加载 scripts/hook-notify.py（路径含连字符）"""
    hook_path = ROOT / "scripts" / "hook-notify.py"
    spec = importlib.util.spec_from_file_location("hook_notify_hotfix", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _asst_line(text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def _user_line(text: str) -> str:
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": text},
    }) + "\n"


def _write_jsonl(content: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write(content)
        return f.name


# ───────── 类 1：_detect_menu_prompt 只看最后一条 assistant ─────────
def test_detect_menu_last_assistant_only():
    print("\n[类1] _detect_menu_prompt 只看最后一条 assistant message")
    hn = _load_hook_notify()

    # 1.1 历史 assistant 含菜单但最后一条 assistant 不含 → False
    jsonl1 = (
        _user_line("解释菜单是什么")
        + _asst_line("我来解释菜单：❯ 1. 选项A\n  2. 选项B")
        + _user_line("好的")
        + _asst_line("已经完成，没有菜单")
    )
    p1 = _write_jsonl(jsonl1)
    try:
        t("历史 assistant 引用菜单 + 最后一条 assistant 不含菜单 → False",
          hn._detect_menu_prompt(p1) is False)
    finally:
        Path(p1).unlink(missing_ok=True)

    # 1.2 历史 assistant 不含 + 最后一条 assistant 含菜单 → True
    jsonl2 = (
        _user_line("hi")
        + _asst_line("讨论菜单是什么")
        + _user_line("展示菜单")
        + _asst_line("❯ 1. 选项A\n  2. 选项B\n  5. Type something.")
    )
    p2 = _write_jsonl(jsonl2)
    try:
        t("最后一条 assistant 含完整菜单 → True", hn._detect_menu_prompt(p2) is True)
    finally:
        Path(p2).unlink(missing_ok=True)

    # 1.3 transcript 末尾是 user message（不会发生但应稳健）→ 返回最后一条 assistant 的判定
    jsonl3 = (
        _asst_line("❯ 1. xxx")
        + _user_line("好")
    )
    p3 = _write_jsonl(jsonl3)
    try:
        t("末尾是 user / 倒数第一条 assistant 含菜单 → True",
          hn._detect_menu_prompt(p3) is True)
    finally:
        Path(p3).unlink(missing_ok=True)

    # 1.4 没有 assistant message → False
    jsonl4 = _user_line("hello") + _user_line("world")
    p4 = _write_jsonl(jsonl4)
    try:
        t("无 assistant message → False", hn._detect_menu_prompt(p4) is False)
    finally:
        Path(p4).unlink(missing_ok=True)

    # 1.5 assistant content 是 string（不是 list）也支持
    jsonl5 = json.dumps({
        "type": "assistant",
        "message": {"content": "❯ 1. 选项A"},
    }) + "\n"
    p5 = _write_jsonl(jsonl5)
    try:
        t("assistant content=string 也支持 → True", hn._detect_menu_prompt(p5) is True)
    finally:
        Path(p5).unlink(missing_ok=True)

    # 1.6 corrupt JSONL 行夹杂，不阻塞遍历
    jsonl6 = (
        "this is not json\n"
        + _user_line("hi")
        + _asst_line("❯ 1. xxx")
        + "another corrupt line\n"
    )
    p6 = _write_jsonl(jsonl6)
    try:
        t("夹杂 corrupt 行 → 仍能识别 assistant 菜单 → True",
          hn._detect_menu_prompt(p6) is True)
    finally:
        Path(p6).unlink(missing_ok=True)


# ───────── 类 2：list_sessions 在 menu_detected=true 时翻 waiting ─────────
def test_list_sessions_menu_detected_status():
    print("\n[类2] event_store.list_sessions menu_detected=true → status=waiting")
    from backend import event_store
    from unittest import mock

    sid = "test-r11-hotfix-status"

    # 2.1 Stop + Notification(filler + menu_detected=true) → waiting
    events1 = [
        {
            "ts": "2026-05-11T11:30:00+08:00",
            "session_id": sid,
            "source": "claude_code",
            "event": "Stop",
            "message": "",
            "raw": {},
        },
        {
            "ts": "2026-05-11T11:30:10+08:00",
            "session_id": sid,
            "source": "claude_code",
            "event": "Notification",
            "message": "Claude is waiting for your input",
            "raw": {"menu_detected": True},
        },
    ]
    with mock.patch.object(event_store, "iter_events", return_value=iter(events1)):
        sessions = event_store.list_sessions()
    sess1 = next((s for s in sessions if s["session_id"] == sid), None)
    t("session 存在", sess1 is not None)
    if sess1:
        t("status=waiting（menu_detected 翻回 waiting）",
          sess1.get("status") == "waiting",
          f"actual_status={sess1.get('status')} last_event={sess1.get('last_event')}")
        t("menu_detected 字段冒泡 = True",
          sess1.get("menu_detected") is True,
          f"actual={sess1.get('menu_detected')}")

    # 2.2 Stop + Notification(filler, menu_detected 缺失) → idle（旧 L20 行为保留）
    events2 = [
        {
            "ts": "2026-05-11T11:30:00+08:00",
            "session_id": sid + "-no-menu",
            "source": "claude_code",
            "event": "Stop",
            "message": "",
            "raw": {},
        },
        {
            "ts": "2026-05-11T11:30:10+08:00",
            "session_id": sid + "-no-menu",
            "source": "claude_code",
            "event": "Notification",
            "message": "Claude is waiting for your input",
            "raw": {},
        },
    ]
    with mock.patch.object(event_store, "iter_events", return_value=iter(events2)):
        sessions = event_store.list_sessions()
    sess2 = next((s for s in sessions if s["session_id"] == sid + "-no-menu"), None)
    t("L20 行为保留：filler 无 menu_detected → status=idle",
      sess2 is not None and sess2.get("status") == "idle",
      f"actual_status={sess2.get('status') if sess2 else None}")

    # 2.3 Stop + Notification(menu_detected=false 显式) → idle
    events3 = [
        {
            "ts": "2026-05-11T11:30:00+08:00",
            "session_id": sid + "-false",
            "source": "claude_code",
            "event": "Stop",
            "message": "",
            "raw": {},
        },
        {
            "ts": "2026-05-11T11:30:10+08:00",
            "session_id": sid + "-false",
            "source": "claude_code",
            "event": "Notification",
            "message": "Claude is waiting for your input",
            "raw": {"menu_detected": False},
        },
    ]
    with mock.patch.object(event_store, "iter_events", return_value=iter(events3)):
        sessions = event_store.list_sessions()
    sess3 = next((s for s in sessions if s["session_id"] == sid + "-false"), None)
    t("menu_detected=False（显式）→ status=idle",
      sess3 is not None and sess3.get("status") == "idle",
      f"actual_status={sess3.get('status') if sess3 else None}")


# ───────── 类 3：notify_filter bypass_dup 仍工作（regression smoke）─────────
def test_notify_filter_bypass_smoke():
    print("\n[类3] notify_filter bypass_dup 仍工作（regression smoke）")
    from backend import notify_filter, idle_reminder

    sid = "test-r11-hotfix-bypass-smoke"
    idle_reminder.reset(sid)
    idle_reminder.mark_sent(sid)  # count=1

    cfg = {"notify_filter": dict(notify_filter.DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 60}

    evt_with_menu = {
        "event": "Notification",
        "session_id": sid,
        "message": "Claude is waiting for your input",
        "raw": {"menu_detected": True},
    }
    ok, reason = notify_filter._notification_decision(evt_with_menu, summary, cfg)
    t("menu_detected=True 仍触发 menu_detected_bypass_dup",
      ok and reason == "menu_detected_bypass_dup",
      f"ok={ok} reason={reason}")
    idle_reminder.reset(sid)


def main():
    print("=== R11 Hotfix 测试 ===")
    test_detect_menu_last_assistant_only()
    test_list_sessions_menu_detected_status()
    test_notify_filter_bypass_smoke()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
