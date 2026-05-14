"""测试 Stop 推送阈值放宽（教训 L11）。

覆盖：
- 字数过阈值（fast / normal / strict 三档）
- 时间窗 fallback（grace_min）
- 锚词放过
- 黑词整句相等 vs 子串
- sensitivity 三档实际阈值

运行：python scripts/0509-2100-test-stop-threshold.py
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import notify_filter  # noqa: E402


def make_cfg(
    sensitivity: str = "normal",
    grace_min: float = 8.0,
    notif_gap_min: float = 5.0,
) -> dict:
    return {
        "notify_filter": {
            **notify_filter.DEFAULT_FILTER_CFG,
            "stop_sensitivity": sensitivity,
            "stop_short_summary_grace_min": grace_min,
            "stop_min_gap_after_notification_min": notif_gap_min,
        }
    }


def stop_evt(last_assistant: str = "") -> dict:
    return {"event": "Stop", "session_id": "sid-test", "last_assistant_message": last_assistant}


def summary(
    milestone: str = "",
    turn_summary: str = "",
    last_assistant: str = "",
    last_stop_pushed_unix: float = 0,
    last_notification_unix: float = 0,
) -> dict:
    return {
        "last_milestone": milestone,
        "turn_summary": turn_summary,
        "last_assistant_message": last_assistant,
        "last_stop_pushed_unix": last_stop_pushed_unix,
        "last_notification_unix": last_notification_unix,
    }


# ---------- 测试 case ----------

CASES: list[tuple[str, dict, dict, dict, bool, str | None]] = []

# Case 1: normal 档 + summary 8 字 → 通过（旧 15 字阈值会拒）
CASES.append((
    "normal_summary_8_chars_pass",
    stop_evt("已经处理完毕，看一下"),
    summary(turn_summary="已经处理完毕，看一下", last_assistant="已经处理完毕，看一下",
            last_stop_pushed_unix=time.time() - 60),
    make_cfg("normal"),
    True,
    "stop_summary_len",
))

# Case 2: normal 档 + summary 5 字 + 无锚词 + 刚推过 → 拒
CASES.append((
    "normal_summary_5_chars_no_anchor_recent_push_reject",
    stop_evt("看一下啊"),
    summary(turn_summary="看一下啊", last_assistant="看一下啊",
            last_stop_pushed_unix=time.time() - 60,  # 1 min 前刚推
            last_notification_unix=time.time() - 60),
    make_cfg("normal", grace_min=8.0),
    False,
    "stop_low_signal",
))

# Case 3: 时间窗 fallback —— summary 极短但距上次推送 > 8 min → 通过
CASES.append((
    "grace_window_fallback_pass_even_2_chars",
    stop_evt("好"),
    summary(turn_summary="好", last_assistant="好",
            last_stop_pushed_unix=time.time() - 600,  # 10 min 前
            last_notification_unix=time.time() - 600),
    make_cfg("normal", grace_min=8.0),
    True,
    "stop_grace_gap",
))

# Case 4: 锚词放过 —— last_assistant 含"完成"，summary 仅 3 字也通过
CASES.append((
    "anchor_keyword_pass_short_summary",
    stop_evt("已完成"),
    summary(turn_summary="已完成", last_assistant="已完成",
            last_stop_pushed_unix=time.time() - 60),
    make_cfg("normal"),
    True,
    "stop_anchor",
))

# Case 5: fast 档 + 4 字 summary → 通过
# 注意：last_assistant 加句号让 _is_blacklisted 不走"短无标点"分支
CASES.append((
    "fast_4_chars_pass",
    stop_evt("处理结果。"),
    summary(turn_summary="处理结果。", last_assistant="处理结果。",
            last_stop_pushed_unix=time.time() - 60),
    make_cfg("fast"),
    True,
    "stop_summary_len",
))

# Case 6: strict 档 + 8 字 summary → 拒（strict 要求 20 字）
CASES.append((
    "strict_8_chars_reject",
    stop_evt("处理完毕检查一下"),
    summary(turn_summary="处理完毕检查一下", last_assistant="处理完毕检查一下",
            last_stop_pushed_unix=time.time() - 60,
            last_notification_unix=time.time() - 60),
    # 注意：last_assistant 含"完毕"不是锚词；turn_summary 不含锚词；
    # 距上次推送只 1 min，grace 不触发；strict 阈值 20 字 → 拒
    make_cfg("strict", grace_min=20.0),
    False,
    "stop_low_signal",
))

# Case 7: 黑词「整句相等」 —— "完成"单字仍被吞
CASES.append((
    "blacklist_exact_short_reject",
    stop_evt("完成"),
    summary(turn_summary="完成完成完成完成完成",  # 10 字过 normal summary 阈值
            last_assistant="完成",
            last_stop_pushed_unix=time.time() - 60,
            last_notification_unix=time.time() - 60),
    # last_assistant="完成" 命中黑词 → summary 路径被拒
    # 但"完成"是锚词 → anchor 条件直接放过
    make_cfg("normal", grace_min=20.0),
    True,
    "stop_anchor",
))

# Case 8: 黑词不再吞「带内容长句」 —— "已完成端到端验证"
CASES.append((
    "blacklist_substring_no_longer_swallows_long_sentence",
    stop_evt("已完成端到端验证"),
    summary(turn_summary="已完成端到端验证",
            last_assistant="已完成端到端验证",
            last_stop_pushed_unix=time.time() - 60),
    make_cfg("normal"),
    True,
    None,  # 通过任一条件即可（summary_len 或 anchor）
))

# Case 9: 首推（同 sid 从未推过任何东西）—— 极短也通过
CASES.append((
    "first_push_no_prior_push_pass",
    stop_evt("好"),
    summary(turn_summary="好", last_assistant="好",
            last_stop_pushed_unix=0, last_notification_unix=0),
    make_cfg("normal", grace_min=8.0),
    True,
    "stop_grace_first_push",
))

# Case 10: sensitivity=normal 时尊重用户自定义阈值
# last_assistant 加句号避开"短无标点"黑词分支
CASES.append((
    "normal_respects_custom_chars",
    stop_evt("两字。"),
    summary(turn_summary="两字。", last_assistant="两字。",
            last_stop_pushed_unix=time.time() - 60,
            last_notification_unix=time.time() - 60),
    {"notify_filter": {
        **notify_filter.DEFAULT_FILTER_CFG,
        "stop_sensitivity": "normal",
        "stop_min_summary_chars": 2,  # 用户调到 2
        "stop_min_milestone_chars": 2,
        "stop_short_summary_grace_min": 20.0,
    }},
    True,
    "stop_summary_len",
))


# ---------- sensitivity 档位实际阈值断言 ----------

def assert_sensitivity_levels() -> None:
    print("\n--- sensitivity 三档实际阈值 ---")
    rows = []
    for sens in ("fast", "normal", "strict"):
        fcfg = {**notify_filter.DEFAULT_FILTER_CFG, "stop_sensitivity": sens}
        ms, sm = notify_filter._resolve_thresholds(fcfg)
        rows.append((sens, ms, sm))
        print(f"  {sens:7s}  milestone={ms:3d}  summary={sm:3d}")
    expected = {"fast": (4, 4), "normal": (6, 8), "strict": (20, 20)}
    for sens, ms, sm in rows:
        e_ms, e_sm = expected[sens]
        assert (ms, sm) == (e_ms, e_sm), f"sens={sens} got=({ms},{sm}) want=({e_ms},{e_sm})"
    print("  OK")


# ---------- 主入口 ----------

def main() -> int:
    fails = 0
    print(f"running {len(CASES)} cases...")
    for name, evt, summ, cfg, want_ok, want_reason_substr in CASES:
        ok, reason = notify_filter.should_notify(evt, summ, cfg)
        passed = (ok == want_ok) and (
            want_reason_substr is None or want_reason_substr in reason
        )
        mark = "OK" if passed else "FAIL"
        print(f"  [{mark}] {name}  ok={ok}  reason={reason}")
        if not passed:
            fails += 1
            print(f"         expected ok={want_ok} reason~{want_reason_substr!r}")

    try:
        assert_sensitivity_levels()
    except AssertionError as e:
        print(f"  [FAIL] sensitivity_levels  {e}")
        fails += 1

    print(f"\nresult: {len(CASES) + 1 - fails}/{len(CASES) + 1} pass")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
