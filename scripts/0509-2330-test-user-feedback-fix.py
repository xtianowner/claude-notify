"""L16 hotfix 用户反馈场景验证：
- "waiting for your input" Notification 在 Stop 后 60s+ 应该推（不再被 3min 默认窗吞）
- 短 Stop 但 last_assistant_message ≥ 12 字应过 stop_assistant_len 兜底
- 整体复盘用户提供的 trace 链路：hello Stop push ✓ / 后续 Notification 不再误吞

时间：2026-05-09 23:30
"""
import time
from backend.notify_filter import _stop_decision, _notification_decision, DEFAULT_FILTER_CFG


def test_notification_after_stop_30s_pushes():
    """L16 主修：Stop 后 31s 来的 'waiting for your input' Notification 应推（不被 dedup 吞）"""
    cfg = {"notify_filter": dict(DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 31}  # 31s 前推过 Stop
    evt = {"event": "Notification", "message": "Claude is waiting for your input"}
    ok, reason = _notification_decision(evt, summary, cfg)
    assert ok, f"应推，实际拒推 reason={reason}"
    assert reason == "notification_kept"
    print(f"  PASS Stop+31s waiting-for-input → push  ({reason})")


def test_notification_within_30s_still_dropped():
    """30s 内的 idle prompt 副本仍应吞（dedup 保留有效场景）"""
    cfg = {"notify_filter": dict(DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 15}  # 15s 前刚推过
    evt = {"event": "Notification", "message": "Claude is waiting for your input"}
    ok, reason = _notification_decision(evt, summary, cfg)
    assert not ok
    assert "notif_dedup_after_stop" in reason
    print(f"  PASS Stop+15s waiting-for-input → drop  ({reason})")


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

    # 3. Stop 后 60s, Claude Code 触发 "waiting for your input" Notification
    #    L16 之前：被 3min suppress 吞；L16 之后：> 30s 推
    summary3 = {"last_stop_pushed_unix": time.time() - 60}
    evt3 = {"event": "Notification", "message": "Claude is waiting for your input"}
    ok3, r3 = _notification_decision(evt3, summary3, cfg)
    assert ok3, f"60s 后 Notification 应推 reason={r3}"

    print(f"  PASS 用户实测链：Stop1 push({r1}) → Stop2 push({r2}) → Notif push({r3})")


def main():
    print("L16 用户反馈 hotfix 验证")
    print("=" * 60)
    test_notification_after_stop_30s_pushes()
    test_notification_within_30s_still_dropped()
    test_short_stop_with_enough_assistant_msg_pushes()
    test_stop_low_signal_still_blocks_garbage()
    test_user_real_scenario_replay()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
