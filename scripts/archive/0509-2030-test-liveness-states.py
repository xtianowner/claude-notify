#!/usr/bin/env python3
"""L09 — TimeoutSuspect 分状态阈值测试。

构造 5 种 last_event_kind 场景，每种走一次 watch_loop，验证：
  - Notification 5 分钟前  → 应报（waiting bucket）
  - PreToolUse 8 分钟前 + transcript 6 分钟前 mtime → 不报（< 15 分钟阈值）
  - PreToolUse 18 分钟前 + transcript 老 → 应报（pretool bucket）
  - PreToolUse 18 分钟前 + transcript 30s 前刚写 → 不报（活性兜底）
  - PostToolUse 12 分钟前 → 应报（idle bucket，10 分钟阈值）
  - 关闭 enabled=False → 走 timeout_minutes(5) 一刀切：PreToolUse 8 分钟前 → 应报

不真等 5 分钟，直接 monkey-patch event_store.list_sessions 注入"假装很久没动"的 session。
"""
from __future__ import annotations
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import liveness_watcher, event_store, config as cfg_mod


def _make_session(
    sid: str,
    last_event_kind: str,
    last_event_ago_sec: float,
    transcript_age_sec: float | None = None,
    last_event_for_status: str | None = None,
):
    """构造一条 fake session dict，模拟 list_sessions 返回。"""
    now = time.time()
    sess = {
        "session_id": sid,
        "source": "claude_code",
        "project": "test",
        "cwd": "/tmp/test",
        "cwd_short": "test",
        "last_event": last_event_for_status or last_event_kind,
        "last_event_kind": last_event_kind,
        "last_event_ts": "2026-05-09T20:00:00+08:00",
        "last_event_unix": now - last_event_ago_sec,
        "transcript_path": "",
        "claude_pid": os.getpid(),  # 自身 PID 永远活
        "status": "running",
        "active": True,
        "suspect_pushed_for_unix": 0.0,
        "dead_pushed_for_unix": 0.0,
        "age_seconds": int(last_event_ago_sec),
    }
    if transcript_age_sec is not None:
        # 写一个真实临时文件，控制 mtime
        tx = tempfile.NamedTemporaryFile(prefix=f"transcript-{sid}-", suffix=".jsonl", delete=False)
        tx.write(b'{"role":"user"}\n')
        tx.close()
        target_mtime = now - transcript_age_sec
        os.utime(tx.name, (target_mtime, target_mtime))
        sess["transcript_path"] = tx.name
    return sess


async def _run_one_iteration(fake_sessions: list[dict], cfg_override: dict | None = None):
    """跑一轮 watch_loop iteration（不进 sleep 循环），返回收集到的事件。"""
    collected: list[dict] = []

    async def on_event(evt):
        collected.append(evt)

    base_cfg = cfg_mod.load()
    if cfg_override:
        # 浅合并到 liveness_per_state_timeout
        merged = dict(base_cfg)
        merged["liveness_per_state_timeout"] = {
            **(base_cfg.get("liveness_per_state_timeout") or {}),
            **cfg_override,
        }
    else:
        merged = base_cfg

    appended: list[dict] = []

    def _fake_append(evt):
        appended.append(evt)

    with (
        mock.patch.object(liveness_watcher.cfg_mod, "load", return_value=merged),
        mock.patch.object(liveness_watcher.event_store, "list_sessions", return_value=fake_sessions),
        mock.patch.object(liveness_watcher.event_store, "append_event", side_effect=_fake_append),
    ):
        # 不直接调 watch_loop（含 while True + sleep），手工跑判定逻辑
        # 解法：调用 watch_loop 但 interval=0 + asyncio.wait_for(0.1 秒)
        try:
            await asyncio.wait_for(
                liveness_watcher.watch_loop(on_event, interval_seconds=0),
                timeout=0.5,
            )
        except asyncio.TimeoutError:
            pass  # 我们只想跑 1-2 圈

    return collected, appended


def _print_case(name: str, expected_fire: bool, fired: bool, evt: dict | None):
    ok = "PASS" if expected_fire == fired else "FAIL"
    bucket = (evt or {}).get("raw", {}).get("threshold_bucket", "—") if evt else "—"
    print(f"  [{ok}] {name}: expected_fire={expected_fire} fired={fired} bucket={bucket}")


async def main():
    print("L09 — TimeoutSuspect 分状态阈值测试")
    print("=" * 60)

    cases = []

    # case 1: Notification 5 分 30 秒前 → 应报（waiting bucket，5 分钟阈值）
    s1 = _make_session("s-notif", "Notification", 5 * 60 + 30, transcript_age_sec=5 * 60 + 30,
                       last_event_for_status="Notification")
    fired1, _ = await _run_one_iteration([s1])
    fired1_evt = next((e for e in fired1 if e.get("session_id") == "s-notif"), None)
    cases.append(("Notification 5min30s", True, bool(fired1_evt), fired1_evt))

    # case 2: PreToolUse 8 分钟前 + transcript 6 分前更新 → 不报（< 15 阈值）
    s2 = _make_session("s-pretool-short", "PreToolUse", 8 * 60, transcript_age_sec=6 * 60,
                       last_event_for_status="Stop")
    fired2, _ = await _run_one_iteration([s2])
    fired2_evt = next((e for e in fired2 if e.get("session_id") == "s-pretool-short"), None)
    cases.append(("PreToolUse 8min (tool 在跑)", False, bool(fired2_evt), fired2_evt))

    # case 3: PreToolUse 18 分钟前 + transcript 17 分前 mtime（也老）→ 应报
    s3 = _make_session("s-pretool-stuck", "PreToolUse", 18 * 60, transcript_age_sec=17 * 60,
                       last_event_for_status="Stop")
    fired3, _ = await _run_one_iteration([s3])
    fired3_evt = next((e for e in fired3 if e.get("session_id") == "s-pretool-stuck"), None)
    cases.append(("PreToolUse 18min (transcript 也老)", True, bool(fired3_evt), fired3_evt))

    # case 4: PreToolUse 18 分钟前但 transcript 30s 前刚写 → 不报（活性兜底）
    s4 = _make_session("s-pretool-alive", "PreToolUse", 18 * 60, transcript_age_sec=30,
                       last_event_for_status="Stop")
    fired4, _ = await _run_one_iteration([s4])
    fired4_evt = next((e for e in fired4 if e.get("session_id") == "s-pretool-alive"), None)
    cases.append(("PreToolUse 18min (transcript 还在活)", False, bool(fired4_evt), fired4_evt))

    # case 5: PostToolUse 12 分钟前 → 应报（idle bucket 10 分钟）
    s5 = _make_session("s-post", "PostToolUse", 12 * 60, transcript_age_sec=12 * 60,
                       last_event_for_status="Stop")
    fired5, _ = await _run_one_iteration([s5])
    fired5_evt = next((e for e in fired5 if e.get("session_id") == "s-post"), None)
    cases.append(("PostToolUse 12min", True, bool(fired5_evt), fired5_evt))

    # case 6: 关闭 enabled=False → 走 timeout_minutes 一刀切（不分 last_kind）。
    # 用 12 分钟（既 > 用户磁盘 timeout_minutes=10，也 < dead_threshold_minutes=30，避免误报 SessionDead）
    # 同时这是 PreToolUse —— 在新逻辑下会因 PreToolUse_minutes=15 不报；关掉 per_state 才报。
    s6 = _make_session("s-legacy", "PreToolUse", 12 * 60, transcript_age_sec=12 * 60,
                       last_event_for_status="Stop")
    fired6, _ = await _run_one_iteration([s6], cfg_override={"enabled": False})
    fired6_evt = next((e for e in fired6 if e.get("session_id") == "s-legacy" and e.get("event") == "TimeoutSuspect"), None)
    cases.append(("legacy 退化（enabled=False）PreToolUse 12min", True, bool(fired6_evt), fired6_evt))

    # case 6b: 同样 session 但 enabled=True → 不报（PreToolUse_minutes=15 > 12）
    s6b = _make_session("s-legacy-b", "PreToolUse", 12 * 60, transcript_age_sec=12 * 60,
                        last_event_for_status="Stop")
    fired6b, _ = await _run_one_iteration([s6b])
    fired6b_evt = next((e for e in fired6b if e.get("session_id") == "s-legacy-b" and e.get("event") == "TimeoutSuspect"), None)
    cases.append(("per_state 启用 PreToolUse 12min（< 15 阈值）", False, bool(fired6b_evt), fired6b_evt))

    # case 7: PostToolUse 6 分钟前 → 不报（idle bucket 10 分钟，未到）
    s7 = _make_session("s-post-short", "PostToolUse", 6 * 60, transcript_age_sec=6 * 60,
                       last_event_for_status="Stop")
    fired7, _ = await _run_one_iteration([s7])
    fired7_evt = next((e for e in fired7 if e.get("session_id") == "s-post-short"), None)
    cases.append(("PostToolUse 6min (思考中)", False, bool(fired7_evt), fired7_evt))

    print()
    fail_count = 0
    for name, expected, actual, evt in cases:
        _print_case(name, expected, actual, evt)
        if expected != actual:
            fail_count += 1

    print()
    print("=" * 60)
    print(f"结果: {len(cases) - fail_count}/{len(cases)} pass")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
