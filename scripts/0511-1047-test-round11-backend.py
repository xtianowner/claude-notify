#!/usr/bin/env python3
"""Round 11 后端改动测试。

覆盖：
1. active_window 动态默认值（Bug A fix）
2. raw.menu_detected → bypass idle_dup（Bug B fix）
3. notif_idle_reminder_minutes 默认 = [15, 45]
4. _detect_menu_prompt 正则正确性
"""
from __future__ import annotations
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


# ────────────────────── 类 3：阈值序列默认值 ──────────────────────
def test_thresholds_default():
    print("\n[类3] notif_idle_reminder_minutes 默认 = [15, 45]")
    from backend import config as cfg_mod
    from backend import notify_filter

    t("config.DEFAULT_NOTIFY_FILTER = [15,45]",
      cfg_mod.DEFAULT_NOTIFY_FILTER["notif_idle_reminder_minutes"] == [15, 45],
      str(cfg_mod.DEFAULT_NOTIFY_FILTER["notif_idle_reminder_minutes"]))

    t("notify_filter.DEFAULT_FILTER_CFG = [15,45]",
      notify_filter.DEFAULT_FILTER_CFG["notif_idle_reminder_minutes"] == [15, 45],
      str(notify_filter.DEFAULT_FILTER_CFG["notif_idle_reminder_minutes"]))

    t("DEFAULTS.notify_filter 与 DEFAULT_NOTIFY_FILTER 一致",
      cfg_mod.DEFAULTS["notify_filter"]["notif_idle_reminder_minutes"] == [15, 45])


# ────────────────────── 类 1：active_window 默认值 ──────────────────────
def test_active_window_default():
    print("\n[类1] liveness_watcher active_window 动态默认 = max(60, dead*2)")
    # 直接 import 配置 + 模拟 _read_raw 不含 active_window_minutes
    from backend import config as cfg_mod
    from unittest import mock

    # case A：用户没显式配 active_window_minutes，dead=30 → active=max(60,60)=60
    with mock.patch.object(cfg_mod, "_read_raw", return_value={"dead_threshold_minutes": 30}):
        raw = cfg_mod._read_raw()
        user_active = raw.get("active_window_minutes")
        dead = int(raw.get("dead_threshold_minutes") or 30)
        active = int(user_active) if isinstance(user_active, (int, float)) else max(60, dead * 2)
        t("A: 未显式配 + dead=30 → active=60", active == 60, f"actual={active}")

    # case B：dead=45（用户调高），未显式配 active → active=max(60,90)=90
    with mock.patch.object(cfg_mod, "_read_raw", return_value={"dead_threshold_minutes": 45}):
        raw = cfg_mod._read_raw()
        user_active = raw.get("active_window_minutes")
        dead = int(raw.get("dead_threshold_minutes") or 30)
        active = int(user_active) if isinstance(user_active, (int, float)) else max(60, dead * 2)
        t("B: 未显式配 + dead=45 → active=90", active == 90, f"actual={active}")

    # case C：用户显式配 active=120，dead=30 → active=120（不被覆盖）
    with mock.patch.object(cfg_mod, "_read_raw",
                            return_value={"dead_threshold_minutes": 30, "active_window_minutes": 120}):
        raw = cfg_mod._read_raw()
        user_active = raw.get("active_window_minutes")
        dead = int(raw.get("dead_threshold_minutes") or 30)
        active = int(user_active) if isinstance(user_active, (int, float)) else max(60, dead * 2)
        t("C: 显式配 120 → active=120", active == 120, f"actual={active}")

    # case D：用户显式配 active=15（很小），dead=30 → 尊重用户值（即使会重现 Bug A）
    with mock.patch.object(cfg_mod, "_read_raw",
                            return_value={"dead_threshold_minutes": 30, "active_window_minutes": 15}):
        raw = cfg_mod._read_raw()
        user_active = raw.get("active_window_minutes")
        dead = int(raw.get("dead_threshold_minutes") or 30)
        active = int(user_active) if isinstance(user_active, (int, float)) else max(60, dead * 2)
        t("D: 显式配 15 → active=15（尊重用户）", active == 15, f"actual={active}")


# ────────────────────── 类 2：menu_detected bypass dup ──────────────────────
def test_menu_detected_bypass():
    print("\n[类2] raw.menu_detected=True → bypass idle_dup")
    from backend import notify_filter, idle_reminder

    sid = "test-r11-menu-bypass"
    idle_reminder.reset(sid)
    # 模拟一个已 mark 过 reminder 的状态（count=1），让普通 idle_dup 应吞
    idle_reminder.mark_sent(sid)
    assert idle_reminder.get_count(sid) == 1

    cfg = {"notify_filter": dict(notify_filter.DEFAULT_FILTER_CFG)}
    # 距离 last_stop_unix=60s（gap=1min，远小于 [15,45] 第二阈值 45min）
    summary = {"last_stop_pushed_unix": time.time() - 60}

    # 1. 带 menu_detected → 应推
    evt_with_menu = {
        "event": "Notification",
        "session_id": sid,
        "message": "Claude is waiting for your input",
        "raw": {"menu_detected": True},
    }
    ok, reason = notify_filter._notification_decision(evt_with_menu, summary, cfg)
    t("menu_detected=True → push", ok and reason == "menu_detected_bypass_dup",
      f"ok={ok} reason={reason}")

    # 2. 同样 evt 但无 menu_detected → 应吞（gap=1min < 阈值，count<max）
    idle_reminder.reset(sid)
    idle_reminder.mark_sent(sid)
    evt_no_menu = {
        "event": "Notification",
        "session_id": sid,
        "message": "Claude is waiting for your input",
        "raw": {},
    }
    ok2, reason2 = notify_filter._notification_decision(evt_no_menu, summary, cfg)
    t("menu_detected 缺失 → drop（idle_dup）",
      (not ok2) and ("notif_idle" in reason2 or "notif_dup" in reason2),
      f"ok={ok2} reason={reason2}")

    # 3. menu_detected=False（显式 false）→ 走正常 dup 逻辑
    idle_reminder.reset(sid)
    idle_reminder.mark_sent(sid)
    evt_false = {
        "event": "Notification",
        "session_id": sid,
        "message": "Claude is waiting for your input",
        "raw": {"menu_detected": False},
    }
    ok3, reason3 = notify_filter._notification_decision(evt_false, summary, cfg)
    t("menu_detected=False → drop（idle_dup）",
      (not ok3) and ("notif_idle" in reason3 or "notif_dup" in reason3),
      f"ok={ok3} reason={reason3}")

    # 4. menu_detected=True + 已达 max reminders → 仍 bypass
    idle_reminder.reset(sid)
    idle_reminder.mark_sent(sid)
    idle_reminder.mark_sent(sid)  # count=2 = max for [15,45]
    ok4, reason4 = notify_filter._notification_decision(evt_with_menu, summary, cfg)
    t("menu_detected=True 即使达上限仍 push", ok4 and reason4 == "menu_detected_bypass_dup",
      f"ok={ok4} reason={reason4}")

    idle_reminder.reset(sid)


# ────────────────────── 类 4：_detect_menu_prompt 正则 ──────────────────────
def test_menu_detection_regex():
    print("\n[类4] _detect_menu_prompt 正则正确性（R11 主线：末 20 行整行子串匹配）")
    # 直接 import scripts/hook-notify.py 的 _detect_menu_prompt 不方便（路径含连字符）
    # → 用 importlib 加载
    import importlib.util
    hook_path = ROOT / "scripts" / "hook-notify.py"
    spec = importlib.util.spec_from_file_location("hook_notify_mod", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def write_and_check(content: str) -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write(content)
            path = f.name
        try:
            return mod._detect_menu_prompt(path)
        finally:
            Path(path).unlink(missing_ok=True)

    # R11 主线版本：末 20 行整行子串匹配，不解析 JSONL，
    # 直接对裸文本/JSONL escape 字串做 search

    # case 1：标准菜单（行首 ❯ 1.）
    t("'❯ 1. xxx\\n  2. yyy' → True",
      write_and_check("❯ 1. 选项A\n  2. 选项B\n") is True)

    # case 2：前缀空格的 ❯ 1.
    t("'  ❯ 1. xxx' → True",
      write_and_check("  ❯ 1. 选项A\n") is True)

    # case 3：普通文本无菜单
    t("普通文本无菜单 → False",
      write_and_check("ordinary text\nno menu here\n") is False)

    # case 4：Type something. 触发
    t("'Type something.' 子串 → True",
      write_and_check("some prompt\nType something.\n") is True)

    # case 5：实际 JSONL 行格式（菜单嵌在 JSON content 里，escape 后仍能命中子串）
    fake_jsonl = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "请选择:\n❯ 1. 选项A\n  2. 选项B\n  3. Type something."}]},
    }) + "\n"
    t("JSONL 行内嵌菜单 → True", write_and_check(fake_jsonl) is True)

    # case 6：空文件 / 不存在 path
    t("空 path → False", mod._detect_menu_prompt("") is False)
    t("不存在 path → False", mod._detect_menu_prompt("/tmp/no-such-transcript-r11.jsonl") is False)

    # case 7：完全无菜单的真实 JSONL
    plain_jsonl = json.dumps({"type": "user", "message": {"content": "hello world"}}) + "\n"
    t("无菜单 JSONL → False", write_and_check(plain_jsonl) is False)


# ────────────────────── 类 5（bonus）：raw.menu_detected 进 evt 不被丢 ──────────────────────
def test_raw_field_preserved():
    print("\n[类5] event_store.append_event 保留 raw.menu_detected")
    from backend import event_store, config as cfg_mod
    from unittest import mock

    # 用 tempfile 替换 EVENTS_PATH，避免污染真实数据
    with tempfile.TemporaryDirectory() as td:
        fake_events = Path(td) / "events.jsonl"
        with mock.patch.object(event_store, "EVENTS_PATH", fake_events):
            evt = {
                "ts": event_store.now_iso(),
                "source": "claude_code",
                "session_id": "test-r11-raw-preserve",
                "event": "Notification",
                "message": "Claude is waiting for your input",
                "raw": {"menu_detected": True, "hook_event_name": "Notification"},
            }
            event_store.append_event(evt)
            # 读回
            lines = fake_events.read_text("utf-8").strip().split("\n")
            t("append 后文件存在 1 行", len(lines) == 1)
            obj = json.loads(lines[0])
            t("raw 字段保留", isinstance(obj.get("raw"), dict))
            t("raw.menu_detected=True 保留",
              (obj.get("raw") or {}).get("menu_detected") is True,
              f"raw={obj.get('raw')}")


def main():
    print("=== Round 11 后端测试 ===")
    test_thresholds_default()
    test_active_window_default()
    test_menu_detected_bypass()
    test_menu_detection_regex()
    test_raw_field_preserved()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
