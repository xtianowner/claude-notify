"""L20 测试：dashboard status 在一次任务内不被 idle prompt Notification 副本切换。

用户痛点：发 hello → Stop（status=idle 回合结束）→ 60s 后 idle prompt → status=waiting（等输入）
→ 用户感觉一次任务"变化两次"。修复后 idle prompt 副本不更新 last_event，status 保持 idle。
"""
import time
from backend.event_store import _is_idle_prompt_dup, parse_iso


def make_evt(ev, ts_iso, message=""):
    return {"event": ev, "ts": ts_iso, "message": message}


def test_idle_prompt_after_stop_marked_dup():
    # Stop @ 2026-05-09 22:14:13 → idle prompt @ 22:15:18 (gap 65s = 1.08min)
    last_stop_unix = parse_iso("2026-05-09T22:14:13+08:00")
    evt = make_evt("Notification", "2026-05-09T22:15:18+08:00", "Claude is waiting for your input")
    assert _is_idle_prompt_dup(evt, last_stop_unix), "1min gap idle prompt 应识别为副本"
    print("  PASS Stop+65s 'is waiting for your input' → is_idle_dup=True")


def test_second_idle_variant_also_marked_dup():
    last_stop_unix = parse_iso("2026-05-09T22:14:13+08:00")
    evt = make_evt("Notification", "2026-05-09T22:15:18+08:00", "Claude Code needs your attention")
    assert _is_idle_prompt_dup(evt, last_stop_unix)
    print("  PASS Stop+65s 'needs your attention' → is_idle_dup=True")


def test_real_permission_request_not_dup():
    """带后缀的真权限请求不算副本"""
    last_stop_unix = parse_iso("2026-05-09T22:14:13+08:00")
    evt = make_evt(
        "Notification",
        "2026-05-09T22:15:18+08:00",
        "Claude Code needs your attention - Bash 'rm -rf /tmp' needs approval",
    )
    assert not _is_idle_prompt_dup(evt, last_stop_unix), "带后缀的权限请求应正常更新 status"
    print("  PASS 'needs your attention - Bash...' (suffix) → is_idle_dup=False")


def test_long_idle_after_3min_not_dup():
    """3min+ 后视为长时间等待提醒，不算副本"""
    last_stop_unix = parse_iso("2026-05-09T22:14:13+08:00")
    evt = make_evt("Notification", "2026-05-09T22:18:30+08:00", "Claude is waiting for your input")
    assert not _is_idle_prompt_dup(evt, last_stop_unix), "4min+ idle 视为新提醒"
    print("  PASS Stop+4min idle → is_idle_dup=False（视为新提醒）")


def test_no_prior_stop_not_dup():
    """如果没推过 Stop，Notification 是真等输入"""
    evt = make_evt("Notification", "2026-05-09T22:14:13+08:00", "Claude is waiting for your input")
    assert not _is_idle_prompt_dup(evt, 0.0)
    print("  PASS 没有 prior Stop → is_idle_dup=False")


def test_non_notification_not_dup():
    """非 Notification 事件直接 False"""
    evt = make_evt("Stop", "2026-05-09T22:14:13+08:00")
    assert not _is_idle_prompt_dup(evt, parse_iso("2026-05-09T22:13:00+08:00"))
    print("  PASS 非 Notification 事件 → is_idle_dup=False")


def main():
    print("L20 dashboard status 稳定性验证")
    print("=" * 60)
    test_idle_prompt_after_stop_marked_dup()
    test_second_idle_variant_also_marked_dup()
    test_real_permission_request_not_dup()
    test_long_idle_after_3min_not_dup()
    test_no_prior_stop_not_dup()
    test_non_notification_not_dup()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
