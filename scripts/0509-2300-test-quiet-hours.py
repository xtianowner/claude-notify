#!/usr/bin/env python3
"""L15 — quiet_hours 单测。

覆盖：
1. enabled=False  → 全部通过（旧行为）
2. quiet 时段内 + 非 passthrough 事件（Stop） → drop，reason=quiet_hours_HH:MM
3. quiet 时段内 + passthrough 事件（Notification / TimeoutSuspect） → 通过
4. quiet 时段外（白天） → 全部通过
5. 跨午夜 23:00-08:00：00:30 / 23:30 / 07:00 在内；08:30 / 14:00 / 22:30 在外
6. weekdays_only=True：周末（Sat/Sun）跳过 quiet_hours，工作日生效
7. 同日窗口（13:00-17:00 午休）：14:00 在内、12:30 / 17:30 在外
8. 配置不完整（start 缺失）→ 不静音（安全 default）
9. start == end → 不静音
10. 决策日志：drop 时 reason 写入 trace（端到端 should_notify 不直接调 _quiet_hours_check）
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from unittest.mock import patch

# 让脚本能 import 到 backend 包
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from backend import notify_filter  # noqa: E402

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def fake_now(year=2026, month=5, day=11, hour=2, minute=30):
    """构造一个可注入的 datetime（默认: 周一 02:30，处于 23:00-08:00 quiet 内）。"""
    # 2026-05-11 是周一（weekday=0）
    return lambda tz_str=None: datetime(year, month, day, hour, minute)


def run_case(name: str, expect_drop: bool, ev_type: str, qh_cfg: dict,
             now_dt: datetime, *, expect_reason_prefix: str | None = None) -> bool:
    cfg = {"quiet_hours": qh_cfg}
    with patch.object(notify_filter, "_now_in_tz", lambda tz: now_dt):
        drop, reason = notify_filter._quiet_hours_check(ev_type, cfg)
    ok = (drop == expect_drop)
    if ok and expect_reason_prefix is not None:
        ok = reason.startswith(expect_reason_prefix)
    mark = PASS if ok else FAIL
    detail = f"drop={drop} reason={reason!r}"
    print(f"  {mark} {name}: {detail}")
    return ok


def main() -> int:
    print("== L15 quiet_hours 单测 ==")
    failures = 0

    base_qh = {
        "enabled": True,
        "start": "23:00",
        "end": "08:00",
        "tz": "Asia/Shanghai",
        "passthrough_events": ["Notification", "TimeoutSuspect"],
        "weekdays_only": False,
    }
    monday_0230 = datetime(2026, 5, 11, 2, 30)   # 周一 02:30，跨午夜窗内
    monday_1430 = datetime(2026, 5, 11, 14, 30)  # 周一 14:30，跨午夜窗外
    monday_2330 = datetime(2026, 5, 11, 23, 30)  # 周一 23:30，跨午夜窗内（start 一侧）
    monday_0700 = datetime(2026, 5, 11, 7, 0)    # 周一 07:00，跨午夜窗内（end 一侧）
    monday_0830 = datetime(2026, 5, 11, 8, 30)   # 周一 08:30，已离开窗
    saturday_0230 = datetime(2026, 5, 9, 2, 30)  # 周六 02:30
    sunday_0230 = datetime(2026, 5, 10, 2, 30)   # 周日 02:30

    print("\n[case 1] enabled=False → 全部通过")
    qh = {**base_qh, "enabled": False}
    failures += not run_case("Stop @ 02:30 enabled=false", False, "Stop", qh, monday_0230)
    failures += not run_case("Notification @ 02:30 enabled=false", False, "Notification", qh, monday_0230)

    print("\n[case 2] 跨午夜窗内 + 非 passthrough → drop")
    failures += not run_case("Stop @ 02:30", True, "Stop", base_qh, monday_0230,
                             expect_reason_prefix="quiet_hours_02:30")
    failures += not run_case("SubagentStop @ 23:30", True, "SubagentStop", base_qh, monday_2330,
                             expect_reason_prefix="quiet_hours_23:30")
    failures += not run_case("Heartbeat @ 07:00", True, "Heartbeat", base_qh, monday_0700,
                             expect_reason_prefix="quiet_hours_07:00")

    print("\n[case 3] 跨午夜窗内 + passthrough → 通过")
    failures += not run_case("Notification @ 02:30 (passthrough)", False, "Notification", base_qh, monday_0230)
    failures += not run_case("TimeoutSuspect @ 07:00 (passthrough)", False, "TimeoutSuspect", base_qh, monday_0700)

    print("\n[case 4] 窗外（白天） → 全部通过")
    failures += not run_case("Stop @ 14:30", False, "Stop", base_qh, monday_1430)
    failures += not run_case("Stop @ 08:30", False, "Stop", base_qh, monday_0830)

    print("\n[case 5] 跨午夜边界")
    failures += not run_case("Stop @ 22:30 (still day)", False, "Stop", base_qh, datetime(2026, 5, 11, 22, 30))
    failures += not run_case("Stop @ 23:00 (start边界，进入)", True, "Stop", base_qh, datetime(2026, 5, 11, 23, 0),
                             expect_reason_prefix="quiet_hours_23:00")
    failures += not run_case("Stop @ 08:00 (end边界，离开)", False, "Stop", base_qh, datetime(2026, 5, 11, 8, 0))

    print("\n[case 6] weekdays_only=True")
    qh_wd = {**base_qh, "weekdays_only": True}
    failures += not run_case("Stop @ Mon 02:30 weekdays_only", True, "Stop", qh_wd, monday_0230,
                             expect_reason_prefix="quiet_hours_")
    failures += not run_case("Stop @ Sat 02:30 weekdays_only (周末跳过)", False, "Stop", qh_wd, saturday_0230)
    failures += not run_case("Stop @ Sun 02:30 weekdays_only (周末跳过)", False, "Stop", qh_wd, sunday_0230)

    print("\n[case 7] 同日窗口（午休 13:00-17:00）")
    qh_lunch = {**base_qh, "start": "13:00", "end": "17:00"}
    failures += not run_case("Stop @ 14:30 (午休内)", True, "Stop", qh_lunch, monday_1430,
                             expect_reason_prefix="quiet_hours_14:30")
    failures += not run_case("Stop @ 12:30 (午休前)", False, "Stop", qh_lunch, datetime(2026, 5, 11, 12, 30))
    failures += not run_case("Stop @ 17:30 (午休后)", False, "Stop", qh_lunch, datetime(2026, 5, 11, 17, 30))
    failures += not run_case("Notification @ 14:30 passthrough", False, "Notification", qh_lunch, monday_1430)

    print("\n[case 8] 配置不完整 → 不静音（安全 default）")
    failures += not run_case("Stop @ 02:30 start 空", False, "Stop",
                             {**base_qh, "start": ""}, monday_0230)
    failures += not run_case("Stop @ 02:30 end 非法", False, "Stop",
                             {**base_qh, "end": "abc"}, monday_0230)

    print("\n[case 9] start == end → 不静音")
    failures += not run_case("Stop @ 02:30 start==end", False, "Stop",
                             {**base_qh, "start": "10:00", "end": "10:00"}, monday_0230)

    print("\n[case 10] 端到端：should_notify 触发 quiet_hours drop")
    cfg_full = {"quiet_hours": base_qh, "session_mutes": {}}
    evt = {"event": "Stop", "session_id": "sid-test-qh", "ts": "2026-05-11T02:30:00+08:00"}
    summary = {}
    with patch.object(notify_filter, "_now_in_tz", lambda tz: monday_0230):
        ok, reason = notify_filter.should_notify(evt, summary, cfg_full, log_trace=False)
    e2e_ok = (not ok) and reason.startswith("quiet_hours_")
    mark = PASS if e2e_ok else FAIL
    print(f"  {mark} should_notify(Stop @ 02:30) → ok={ok} reason={reason!r}")
    if not e2e_ok:
        failures += 1

    # 端到端：Notification 在 quiet 内仍通过
    evt2 = {"event": "Notification", "session_id": "sid-test-qh-n", "message": "请确认权限",
            "ts": "2026-05-11T02:30:00+08:00"}
    with patch.object(notify_filter, "_now_in_tz", lambda tz: monday_0230):
        ok2, reason2 = notify_filter.should_notify(evt2, {}, cfg_full, log_trace=False)
    e2e2_ok = ok2 and reason2 == "notification_kept"
    mark = PASS if e2e2_ok else FAIL
    print(f"  {mark} should_notify(Notification @ 02:30) → ok={ok2} reason={reason2!r}")
    if not e2e2_ok:
        failures += 1

    print()
    if failures == 0:
        print(f"{PASS} all cases passed")
        return 0
    print(f"{FAIL} {failures} case(s) failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
