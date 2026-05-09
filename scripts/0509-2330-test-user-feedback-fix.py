"""L16 hotfix 用户反馈场景验证：
- "waiting for your input" Notification 在 Stop 后 60s+ 应该推（不再被 3min 默认窗吞）
- 短 Stop 但 last_assistant_message ≥ 12 字应过 stop_assistant_len 兜底
- 整体复盘用户提供的 trace 链路：hello Stop push ✓ / 后续 Notification 不再误吞

时间：2026-05-09 23:30
"""
import time
from backend.notify_filter import _stop_decision, _notification_decision, DEFAULT_FILTER_CFG


def test_l21_idle_prompt_dropped_until_next_task():
    """L21：默认配置 dedup_until_next_stop=True，Stop 推过的 idle prompt 永远吞"""
    cfg = {"notify_filter": dict(DEFAULT_FILTER_CFG)}
    # 60s 前推过 Stop
    summary = {"last_stop_pushed_unix": time.time() - 60}
    evt = {"event": "Notification", "message": "Claude is waiting for your input"}
    ok, reason = _notification_decision(evt, summary, cfg)
    assert not ok and reason == "notif_dup_until_next_task", f"60s 应吞 reason={reason}"
    # 30 分钟后（远超旧 3min 阈值）也应吞
    summary30 = {"last_stop_pushed_unix": time.time() - 1800}
    ok30, r30 = _notification_decision(evt, summary30, cfg)
    assert not ok30 and r30 == "notif_dup_until_next_task", f"30min 也应吞 reason={r30}"
    print(f"  PASS Stop+60s idle → drop ({reason}); Stop+30min idle → drop ({r30})")


def test_l21_legacy_window_mode_when_disabled():
    """L21：用户关闭严格模式（dedup_until_next_stop=False）→ 退回 3min 窗口"""
    cfg_filter = dict(DEFAULT_FILTER_CFG)
    cfg_filter["notif_dedup_until_next_stop"] = False
    cfg = {"notify_filter": cfg_filter}
    # 窗口内吞
    summary60 = {"last_stop_pushed_unix": time.time() - 60}
    evt = {"event": "Notification", "message": "Claude is waiting for your input"}
    ok60, r60 = _notification_decision(evt, summary60, cfg)
    assert not ok60 and "notif_dup_idle_prompt" in r60
    # 窗口外推
    summary200 = {"last_stop_pushed_unix": time.time() - 200}
    ok200, r200 = _notification_decision(evt, summary200, cfg)
    assert ok200, f"3min+ 应推 reason={r200}"
    print(f"  PASS legacy window: 60s→drop({r60}), 200s→push({r200})")


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
    """L19：Claude Code 两种 idle prompt message 都应被吞（L21 改为永久 dedup）"""
    cfg = {"notify_filter": dict(DEFAULT_FILTER_CFG)}
    summary = {"last_stop_pushed_unix": time.time() - 60}
    for msg in ("Claude is waiting for your input", "Claude Code needs your attention"):
        evt = {"event": "Notification", "message": msg}
        ok, reason = _notification_decision(evt, summary, cfg)
        assert not ok, f"{msg!r} 应被吞，实际推送 reason={reason}"
        # L21 默认走 notif_dup_until_next_task；L18/L19 旧名 notif_dup_idle_prompt 仅在
        # notif_dedup_until_next_stop=False 时出现
        assert reason == "notif_dup_until_next_task"
    print(f"  PASS L19 两种 idle prompt 均 drop（L21 严格模式）")


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
    #    L21：永久 dedup until next Stop
    summary3 = {"last_stop_pushed_unix": time.time() - 60}
    evt3 = {"event": "Notification", "message": "Claude is waiting for your input"}
    ok3, r3 = _notification_decision(evt3, summary3, cfg)
    assert not ok3, f"idle prompt 应吞 reason={r3}"

    print(f"  PASS 用户实测链：Stop1 push({r1}) → Stop2 push({r2}) → idle Notif drop({r3})")


def main():
    print("L16+L18+L19+L21 用户反馈修复验证（含 L21 严格 1 次模式）")
    print("=" * 60)
    test_l21_idle_prompt_dropped_until_next_task()
    test_l21_legacy_window_mode_when_disabled()
    test_real_permission_notification_always_passes()
    test_l19_two_idle_prompt_variants_both_dropped()
    test_short_stop_with_enough_assistant_msg_pushes()
    test_stop_low_signal_still_blocks_garbage()
    test_user_real_scenario_replay()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
