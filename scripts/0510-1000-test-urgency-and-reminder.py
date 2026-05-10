#!/usr/bin/env python3
"""Round 7 测试：飞书 Notification reminder 模板差异化 + reason 解析

覆盖：
- _parse_reminder_reason: 解析 "notif_idle_reminder_N_at_X.Xmin" → (N, X.X)
- _format_text Notification: reminder_index = 0/1/2 三种渲染分支
- 真权限请求（reminder_index=0 / 缺失）保持原 emoji + "等你确认"
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.feishu import _format_text  # noqa: E402
from backend.notify_policy import _parse_reminder_reason  # noqa: E402


# ─── reason 解析 ───
def test_parse_reminder_reason_first():
    idx, gap = _parse_reminder_reason("notif_idle_reminder_1_at_5.0min")
    assert idx == 1, f"idx={idx}"
    assert gap == 5.0, f"gap={gap}"


def test_parse_reminder_reason_second():
    idx, gap = _parse_reminder_reason("notif_idle_reminder_2_at_10.4min")
    assert idx == 2
    assert abs(gap - 10.4) < 0.01


def test_parse_reminder_reason_third():
    idx, gap = _parse_reminder_reason("notif_idle_reminder_3_at_15.7min")
    assert idx == 3
    assert abs(gap - 15.7) < 0.01


def test_parse_reminder_reason_real_permission():
    """真权限请求 reason='notification_kept' → (0, None)"""
    idx, gap = _parse_reminder_reason("notification_kept")
    assert idx == 0
    assert gap is None


def test_parse_reminder_reason_empty():
    idx, gap = _parse_reminder_reason("")
    assert idx == 0
    assert gap is None


def test_parse_reminder_reason_garbage():
    idx, gap = _parse_reminder_reason("notif_idle_dup(3.2min<5min)")
    assert idx == 0
    assert gap is None


# ─── _format_text Notification 三档 ───
def _make_notification_evt(extra=None):
    evt = {
        "event": "Notification",
        "session_id": "abc1234567890",
        "cwd": "/Users/tian/code/claude-notify",
        "ts": "2026-05-10T10:00:00+08:00",
        "message": "Claude Code is waiting for your input",
    }
    if extra:
        evt.update(extra)
    return evt


def _make_summary():
    return {
        "alias": "",
        "task_topic": "修 reminder 模板",
        "waiting_for": "审阅这条改动",
        "next_action": "确认或调整",
    }


def test_format_text_real_permission_no_reminder_index():
    """真权限：无 reminder_index → 🔔 ... 等你确认（保持现状）"""
    evt = _make_notification_evt()
    text = _format_text(evt, _make_summary(), {})
    l1 = text.split("\n")[0]
    assert "🔔" in l1, f"L1 应有 🔔: {l1}"
    assert "等你确认" in l1, f"L1 应含'等你确认': {l1}"
    # 不应该出现 reminder 提示词
    assert "还在等你" not in l1
    assert "已等" not in l1


def test_format_text_first_reminder():
    """第 1 次 reminder：🔔 还在等你（5 分钟）"""
    evt = _make_notification_evt({"reminder_index": 1, "reminder_gap_min": 5.0})
    text = _format_text(evt, _make_summary(), {})
    l1 = text.split("\n")[0]
    assert "🔔" in l1, f"L1 应有 🔔: {l1}"
    assert "还在等你" in l1, f"L1 应含'还在等你': {l1}"
    assert "5 分钟" in l1, f"L1 应含具体时长'5 分钟': {l1}"
    assert "等你确认" not in l1, f"L1 不该有原'等你确认': {l1}"


def test_format_text_second_reminder():
    """第 2 次 reminder：🚨 已等 10 分钟"""
    evt = _make_notification_evt({"reminder_index": 2, "reminder_gap_min": 10.4})
    text = _format_text(evt, _make_summary(), {})
    l1 = text.split("\n")[0]
    assert "🚨" in l1, f"L1 应升级为 🚨: {l1}"
    assert "已等" in l1, f"L1 应含'已等': {l1}"
    assert "10 分钟" in l1, f"L1 应四舍五入到 10 分钟: {l1}"
    assert "🔔" not in l1, f"L1 不应再有 🔔（已升级 emoji）: {l1}"


def test_format_text_third_reminder_emoji_critical():
    """第 3 次（如未来扩展）：≥2 都用 🚨"""
    evt = _make_notification_evt({"reminder_index": 3, "reminder_gap_min": 15.7})
    text = _format_text(evt, _make_summary(), {})
    l1 = text.split("\n")[0]
    assert "🚨" in l1
    assert "16 分钟" in l1, f"四舍五入应为 16 分钟: {l1}"


def test_format_text_reminder_with_project_prefix():
    """reminder 也保留项目前缀（multi-session 识别）"""
    evt = _make_notification_evt({
        "reminder_index": 1,
        "reminder_gap_min": 5.0,
        "cwd": "/Users/tian/code/ragSystem",
    })
    text = _format_text(evt, {"alias": ""}, {})
    l1 = text.split("\n")[0]
    assert l1.startswith("[ragSystem] "), f"L1 应有 [ragSystem] 前缀: {l1}"
    assert "🔔" in l1
    assert "还在等你" in l1


def test_format_text_reminder_index_zero_treated_as_real():
    """reminder_index=0 显式传入 → 视同真权限请求"""
    evt = _make_notification_evt({"reminder_index": 0})
    text = _format_text(evt, _make_summary(), {})
    l1 = text.split("\n")[0]
    assert "🔔" in l1
    assert "等你确认" in l1
    assert "还在等你" not in l1


def test_format_text_reminder_no_gap_fallback():
    """reminder_index=1 但 gap 缺失 → 用'片刻'兜底，不抛异常"""
    evt = _make_notification_evt({"reminder_index": 1})
    text = _format_text(evt, _make_summary(), {})
    l1 = text.split("\n")[0]
    assert "🔔" in l1
    assert "还在等你" in l1


# ─── frontend urgencyBadge 阈值（仅作为常量回归测试） ───
def test_urgency_thresholds_documented():
    """阈值常量与文档同步：< 2min 不显示 / 2-5 hint / 5-15 warn / ≥15 critical
    waiting/suspect 升一档，饱和到 critical。

    这里没法直接调 JS；写一个 Python 版镜像函数，确保阈值口径不漂移。
    """
    def _level(status, age_sec):
        if status not in ("idle", "waiting", "suspect"):
            return None
        if age_sec < 120:
            return None
        if age_sec < 300:
            base = 0
        elif age_sec < 900:
            base = 1
        else:
            base = 2
        if status in ("waiting", "suspect"):
            base = min(2, base + 1)
        return base

    # idle 档位
    assert _level("idle", 60) is None
    assert _level("idle", 150) == 0   # hint
    assert _level("idle", 350) == 1   # warn
    assert _level("idle", 1200) == 2  # critical

    # waiting 升一档
    assert _level("waiting", 60) is None        # 仍 < 2min
    assert _level("waiting", 150) == 1          # hint → warn
    assert _level("waiting", 350) == 2          # warn → critical
    assert _level("waiting", 1200) == 2         # critical 饱和

    # suspect 同 waiting
    assert _level("suspect", 350) == 2

    # 非目标 status 不显示
    assert _level("running", 1200) is None
    assert _level("ended", 1200) is None
    assert _level("dead", 1200) is None


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
            failed.append(name)
        except Exception as e:
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
            failed.append(name)
    print()
    if failed:
        print(f"{len(failed)}/{len(tests)} failed")
        sys.exit(1)
    print(f"all {len(tests)} passed")


if __name__ == "__main__":
    main()
