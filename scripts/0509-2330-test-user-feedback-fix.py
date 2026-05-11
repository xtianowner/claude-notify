"""L16 hotfix 用户反馈场景验证：
- "waiting for your input" Notification 在 Stop 后 60s+ 应该推（不再被 3min 默认窗吞）
- 短 Stop 但 last_assistant_message ≥ 12 字应过 stop_assistant_len 兜底
- 整体复盘用户提供的 trace 链路：hello Stop push ✓ / 后续 Notification 不再误吞

时间：2026-05-09 23:30
"""
import time
from backend.notify_filter import _stop_decision, _notification_decision, DEFAULT_FILTER_CFG


def test_l22_3cap_idle_reminders():
    """L22：测试 3 次推送节奏（5min/10min 阈值）。R11 起默认改为 [15,45]，
    本测试显式设 [5,10] 验证节奏机制本身，不依赖默认值。"""
    from backend import idle_reminder
    sid = "test-l22-3cap"
    idle_reminder.reset(sid)

    cfg_filter = dict(DEFAULT_FILTER_CFG)
    cfg_filter["notif_idle_reminder_minutes"] = [5, 10]
    cfg = {"notify_filter": cfg_filter}
    base = time.time()

    # 60s 后 idle prompt → gap=1min < 5min → 吞，count 仍 0
    evt = {"event": "Notification", "session_id": sid, "message": "Claude is waiting for your input"}
    ok, r = _notification_decision(evt, {"last_stop_pushed_unix": base - 60}, cfg)
    assert not ok and "notif_idle_dup" in r, f"1min 应吞 reason={r}"
    assert idle_reminder.get_count(sid) == 0

    # 5.5min 后 idle prompt → gap=5.5 >= 5min → 推（reminder 1），count=1
    ok, r = _notification_decision(evt, {"last_stop_pushed_unix": base - 330}, cfg)
    assert ok and "notif_idle_reminder_1" in r, f"5.5min 应推 reminder 1 reason={r}"
    assert idle_reminder.get_count(sid) == 1

    # 6min 后 idle prompt → reminder 1 已发；下一阈值是 10min；gap=6 < 10 → 吞
    ok, r = _notification_decision(evt, {"last_stop_pushed_unix": base - 360}, cfg)
    assert not ok and "notif_idle_dup" in r, f"6min 第 2 次应等 10min 阈值 reason={r}"
    assert idle_reminder.get_count(sid) == 1

    # 11min 后 idle prompt → gap=11 >= 10min → 推（reminder 2），count=2 = 上限
    ok, r = _notification_decision(evt, {"last_stop_pushed_unix": base - 660}, cfg)
    assert ok and "notif_idle_reminder_2" in r, f"11min 应推 reminder 2 reason={r}"
    assert idle_reminder.get_count(sid) == 2

    # 30min 后 idle prompt → 已达 max → 永远吞
    ok, r = _notification_decision(evt, {"last_stop_pushed_unix": base - 1800}, cfg)
    assert not ok and "notif_idle_max_reminders" in r, f"已达上限应吞 reason={r}"
    assert idle_reminder.get_count(sid) == 2

    idle_reminder.reset(sid)
    print(f"  PASS 3-cap reminder: 1min→drop, 5.5min→push#1, 6min→drop, 11min→push#2, 30min→drop")


def test_l22_reset_on_new_stop_push():
    """L22：新 Stop push 后 reminder 计数重置 → 下一回合重新走 3 次"""
    from backend import idle_reminder
    sid = "test-l22-reset"
    idle_reminder.reset(sid)
    idle_reminder.mark_sent(sid)
    idle_reminder.mark_sent(sid)
    assert idle_reminder.get_count(sid) == 2
    idle_reminder.reset(sid)
    assert idle_reminder.get_count(sid) == 0

    # reset 后又来 idle prompt → 视为第 1 次 reminder candidate
    # 显式设 [5,10] 让 5.5min 触发 reminder 1（R11 默认改为 [15,45]）
    cfg_filter = dict(DEFAULT_FILTER_CFG)
    cfg_filter["notif_idle_reminder_minutes"] = [5, 10]
    cfg = {"notify_filter": cfg_filter}
    evt = {"event": "Notification", "session_id": sid, "message": "Claude is waiting for your input"}
    ok, r = _notification_decision(evt, {"last_stop_pushed_unix": time.time() - 330}, cfg)
    assert ok and "notif_idle_reminder_1" in r, f"reset 后应能推 reason={r}"
    idle_reminder.reset(sid)
    print(f"  PASS reset 后计数清零，能再走 3 次")


def test_l22_strict_mode_empty_list():
    """L22：用户配置空列表 → 严格 1 次（任务完成那次推完后绝不再推）"""
    cfg_filter = dict(DEFAULT_FILTER_CFG)
    cfg_filter["notif_idle_reminder_minutes"] = []
    cfg = {"notify_filter": cfg_filter}
    evt = {"event": "Notification", "session_id": "test-strict", "message": "Claude is waiting for your input"}
    ok, r = _notification_decision(evt, {"last_stop_pushed_unix": time.time() - 600}, cfg)
    assert not ok and r == "notif_dup_until_next_task", f"空列表应严格 1 次 reason={r}"
    print(f"  PASS 空列表 → 严格 1 次模式")


def test_real_permission_notification_always_passes():
    """L18/L19：真权限请求（不在 filler_phrases）任何时候都推 —— 用户必须看到"""
    cfg = {"notify_filter": dict(DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 10}  # 10s 前刚推 Stop
    # 这句包含 "needs your attention" 子串，但不完整匹配（带后缀），不应被吞
    evt = {"event": "Notification", "message": "Claude Code needs your attention - Bash 'rm -rf /tmp/foo' needs approval"}
    ok, reason = _notification_decision(evt, summary, cfg)
    assert ok, f"带后缀的权限请求绝不能吞 reason={reason}"
    print(f"  PASS Bash approval (with attention prefix) @ 10s → push  ({reason})")


def test_l19_two_idle_prompt_variants_both_dropped():
    """L19+L22：Claude Code 两种 idle prompt message 都应被吞（< 5min 第一阈值前）"""
    from backend import idle_reminder
    cfg = {"notify_filter": dict(DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 60}
    for i, msg in enumerate(("Claude is waiting for your input", "Claude Code needs your attention")):
        sid = f"test-l19-{i}"
        idle_reminder.reset(sid)
        evt = {"event": "Notification", "session_id": sid, "message": msg}
        ok, reason = _notification_decision(evt, summary, cfg)
        assert not ok, f"{msg!r} 1min < 5min 阈值应吞 reason={reason}"
        assert "notif_idle_dup" in reason
        idle_reminder.reset(sid)
    print(f"  PASS L19 两种 idle prompt 在 1min 都被吞（< 5min 阈值）")


def test_short_stop_with_enough_assistant_msg_pushes():
    """L16 副修：短 Stop（无 milestone/summary、不含锚词、近期推过、近期无 notif）但
    last_assistant ≥ 12 字应过条件 6 兜底。"""
    cfg = {"notify_filter": dict(DEFAULT_FILTER_CFG)}
    # 让条件 1-5 都不满足：
    # - summary 缺 milestone/turn_summary → 条件 1/2 skip
    # - last_assistant 不含 anchor → 条件 3 skip
    # - last_stop_pushed_unix 距今 1min（< grace_min=8）→ 条件 4 skip
    # - 无 last_notification_unix → 条件 5 skip
    summary = {"last_stop_pushed_unix": time.time() - 60}
    evt = {
        "event": "Stop",
        "last_assistant_message": "好的请问需要确认什么内容呢具体一点",  # 17 字，无 anchor 词
    }
    ok, reason = _stop_decision(evt, summary, cfg)
    assert ok, f"应推，实际拒推 reason={reason}"
    assert "stop_assistant_len" in reason, f"应走条件 6 兜底，实际 reason={reason}"
    print(f"  PASS short Stop + assistant 17字 (条件6兜底) → push  ({reason})")


def test_stop_low_signal_still_blocks_garbage():
    """black hole 测试：近期推过 + last_assistant 太短或全黑词，仍应吞"""
    cfg = {"notify_filter": dict(DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 60}  # 1min 前推过，避开 grace
    # 太短（< 12 字 → 不走条件 6 兜底）
    evt = {"event": "Stop", "last_assistant_message": "好"}
    ok, reason = _stop_decision(evt, summary, cfg)
    assert not ok, f"短 Stop 应被吞，但 ok={ok} reason={reason}"
    assert reason == "stop_low_signal", f"reason={reason}"
    print(f"  PASS short '好' Stop → drop  ({reason})")


def test_user_real_scenario_replay():
    """复盘 21:30 用户实测：hello → "给一个需要确认的问题" 后续 Notification 推送链"""
    cfg = {"notify_filter": dict(DEFAULT_FILTER_CFG)}

    # 1. hello Stop 推（grace_first_push）
    summary1 = {}
    evt1 = {"event": "Stop", "last_assistant_message": "你好！有什么可以帮你的？"}
    ok1, r1 = _stop_decision(evt1, summary1, cfg)
    assert ok1, f"hello Stop 应推 reason={r1}"

    # 2. 假设 hello Stop 推送时间 = 21:30:56，60s 后用户输入"给一个需要确认的问题"
    #    Claude 回："好的，请问你需要我帮你确认什么？" → Stop 事件
    push_t = time.time() - 60
    summary2 = {"last_stop_pushed_unix": push_t}
    evt2 = {"event": "Stop", "last_assistant_message": "好的，请问你需要我帮你确认什么？"}
    ok2, r2 = _stop_decision(evt2, summary2, cfg)
    assert ok2, f"用户测试问题 Stop 应推 reason={r2}"

    # 3. Stop 后 60s, Claude Code 触发 idle prompt
    #    L22：1min < 5min 阈值 → 吞
    from backend import idle_reminder
    sid = "test-real-replay"
    idle_reminder.reset(sid)
    summary3 = {"last_stop_pushed_unix": time.time() - 60}
    evt3 = {"event": "Notification", "session_id": sid, "message": "Claude is waiting for your input"}
    ok3, r3 = _notification_decision(evt3, summary3, cfg)
    assert not ok3, f"idle prompt 应吞 reason={r3}"
    idle_reminder.reset(sid)

    print(f"  PASS 用户实测链：Stop1 push({r1}) → Stop2 push({r2}) → idle Notif drop({r3})")


def main():
    print("L16+L18+L19+L22 用户反馈修复验证（L22 = 3-cap idle reminder）")
    print("=" * 60)
    test_l22_3cap_idle_reminders()
    test_l22_reset_on_new_stop_push()
    test_l22_strict_mode_empty_list()
    test_real_permission_notification_always_passes()
    test_l19_two_idle_prompt_variants_both_dropped()
    test_short_stop_with_enough_assistant_msg_pushes()
    test_stop_low_signal_still_blocks_garbage()
    test_user_real_scenario_replay()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
