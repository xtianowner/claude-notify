#!/usr/bin/env python3
"""R12 测试：liveness_watcher 对 inactive ghost session 的 dead 收尾。

修复前 bug：
  - session 只有一个 SessionStart 事件（用户启动 claude 后秒退）
  - derive_status("SessionStart") 默认 → "running"
  - age > active_window_minutes（默认 60min）→ active=False
  - liveness_watcher line 120 直接 skip → 永远到不了 dead 判定
  - dashboard 上永久显示"运行中"，无法清除

修复后：dead 判定提到 active skip 之前；只要 pid 死且 transcript 没了，就标 dead。
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import event_store, liveness_watcher  # noqa: E402


class TestInactiveDeadGap(unittest.TestCase):
    """R12 核心场景：inactive 且 pid 死 + transcript 不存在 → 必须被标 dead。"""

    def setUp(self):
        # 构造一个老旧 SessionStart-only session 进 list_sessions 返回值
        self.now = time.time()
        self.fake_session = {
            "session_id": "ghost-0001",
            "source": "claude_code",
            "project": "claude-notify",
            "cwd": "/tmp/x",
            "cwd_short": "x",
            "transcript_path": "/tmp/non-existent-transcript-xyz.jsonl",
            "claude_pid": 99999999,  # 几乎不可能存在的 PID
            "tty": "",
            "last_event": "SessionStart",
            "last_event_kind": "SessionStart",
            "last_event_unix": self.now - 70 * 60,  # 70 分钟前
            "status": "running",
            "active": False,  # 70min > 60min active_window
            "terminal": False,
            "suspect_pushed_for_unix": 0.0,
            "dead_pushed_for_unix": 0.0,
        }

    def test_inactive_pid_dead_tx_missing_marks_dead(self):
        """核心 case：inactive + pid 死 + transcript 不存在 → SessionDead 被发出。"""
        appended = []
        emitted = []

        async def fake_on_event(evt):
            emitted.append(evt)

        def fake_list_sessions(**kw):
            return [dict(self.fake_session)]

        async def runner():
            # 把 sleep 立刻打断，跑一轮就退出
            cancel_after = [False]

            real_sleep = asyncio.sleep

            async def stub_sleep(t):
                if not cancel_after[0]:
                    cancel_after[0] = True
                    return
                raise asyncio.CancelledError()

            with mock.patch.object(asyncio, "sleep", stub_sleep):
                with mock.patch.object(event_store, "list_sessions", fake_list_sessions):
                    with mock.patch.object(event_store, "append_event", lambda e: appended.append(e)):
                        try:
                            await liveness_watcher.watch_loop(fake_on_event, interval_seconds=1)
                        except asyncio.CancelledError:
                            pass

        asyncio.run(runner())

        dead_evts = [e for e in appended if e["event"] == "SessionDead"]
        self.assertEqual(len(dead_evts), 1, f"应该恰好发 1 条 SessionDead, 实际 {len(appended)} 条: {[e['event'] for e in appended]}")
        evt = dead_evts[0]
        self.assertEqual(evt["session_id"], "ghost-0001")
        self.assertIn("pid_gone", evt.get("reason", ""))
        self.assertFalse(evt["raw"]["active"], "raw.active 应记录原 active=False，便于排查")

    def test_inactive_alive_tx_present_no_event(self):
        """对照 case：inactive 但 pid 还在 + transcript 还在 → 不应发 SessionDead 也不发 Suspect。

        这种 session 仍然 dormant，但既没 dead 证据，也不该刷 suspect 噪音。
        """
        # 准备一个真实存在的临时 transcript（mtime 刚刚）
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("{}\n")
            tx_path = f.name
        try:
            sess = dict(self.fake_session)
            sess["transcript_path"] = tx_path
            sess["claude_pid"] = os.getpid()  # 用自己的 PID 保证活

            appended = []

            async def fake_on_event(evt):
                pass

            def fake_list_sessions(**kw):
                return [sess]

            async def runner():
                cancel_after = [False]

                async def stub_sleep(t):
                    if not cancel_after[0]:
                        cancel_after[0] = True
                        return
                    raise asyncio.CancelledError()

                with mock.patch.object(asyncio, "sleep", stub_sleep):
                    with mock.patch.object(event_store, "list_sessions", fake_list_sessions):
                        with mock.patch.object(event_store, "append_event", lambda e: appended.append(e)):
                            try:
                                await liveness_watcher.watch_loop(fake_on_event, interval_seconds=1)
                            except asyncio.CancelledError:
                                pass

            asyncio.run(runner())
            # 不该发任何事件：suspect 被 active=False 拦下；dead 因 pid_alive + tx_age<60 不命中
            self.assertEqual([e["event"] for e in appended], [], f"不应发任何事件，实际：{[e['event'] for e in appended]}")
        finally:
            try:
                os.unlink(tx_path)
            except FileNotFoundError:
                pass

    def test_inactive_dead_not_double_pushed(self):
        """幂等：同一 session 在第二轮巡检不会再发 dead（dead_pushed_for_unix 去重）。"""
        sess = dict(self.fake_session)
        # 模拟第一轮已经写过 dead 标记
        sess["dead_pushed_for_unix"] = sess["last_event_unix"]

        appended = []

        async def fake_on_event(evt):
            pass

        def fake_list_sessions(**kw):
            return [sess]

        async def runner():
            cancel_after = [False]

            async def stub_sleep(t):
                if not cancel_after[0]:
                    cancel_after[0] = True
                    return
                raise asyncio.CancelledError()

            with mock.patch.object(asyncio, "sleep", stub_sleep):
                with mock.patch.object(event_store, "list_sessions", fake_list_sessions):
                    with mock.patch.object(event_store, "append_event", lambda e: appended.append(e)):
                        try:
                            await liveness_watcher.watch_loop(fake_on_event, interval_seconds=1)
                        except asyncio.CancelledError:
                            pass

        asyncio.run(runner())
        self.assertEqual([e["event"] for e in appended], [], "已 push 过的 dead 不该重发")


class TestDeadMsgInfHandling(unittest.TestCase):
    """R12 防回归：_dead_msg 必须能处理 tx_age=inf（transcript 文件不存在）。

    旧版 `int(tx_age // 60)` 对 inf 抛 ValueError，被 watch_loop except Exception 吞掉，
    导致 SessionDead 永远发不出来，ghost 卡死。
    """

    def test_dead_msg_pid_dead_tx_inf(self):
        msg = liveness_watcher._dead_msg(pid_alive=False, tx_age=float("inf"), dead_threshold_min=30)
        self.assertIsInstance(msg, str)
        self.assertIn("transcript", msg)
        self.assertNotIn("inf", msg.lower())

    def test_dead_msg_pid_alive_tx_inf(self):
        msg = liveness_watcher._dead_msg(pid_alive=True, tx_age=float("inf"), dead_threshold_min=30)
        self.assertIsInstance(msg, str)

    def test_dead_msg_normal_path(self):
        msg = liveness_watcher._dead_msg(pid_alive=False, tx_age=35 * 60, dead_threshold_min=30)
        self.assertIn("35", msg)


class TestActiveStillWorks(unittest.TestCase):
    """回归保护：R11 修过的 active session 路径仍然正常。"""

    def test_active_pid_dead_marks_dead(self):
        """active session + pid 死 + transcript 静默 60s+ → dead（R11 fast path）。"""
        now = time.time()
        sess = {
            "session_id": "active-pid-dead",
            "source": "claude_code",
            "project": "x", "cwd": "/tmp/x", "cwd_short": "x",
            "transcript_path": "/tmp/no-such-tx.jsonl",
            "claude_pid": 99999998,
            "tty": "",
            "last_event": "Stop", "last_event_kind": "Stop",
            "last_event_unix": now - 15 * 60,  # 15min, 在 active_window 内
            "status": "idle",
            "active": True,
            "terminal": False,
            "suspect_pushed_for_unix": 0.0,
            "dead_pushed_for_unix": 0.0,
        }

        appended = []

        async def fake_on_event(evt):
            pass

        def fake_list_sessions(**kw):
            return [sess]

        async def runner():
            cancel_after = [False]

            async def stub_sleep(t):
                if not cancel_after[0]:
                    cancel_after[0] = True
                    return
                raise asyncio.CancelledError()

            with mock.patch.object(asyncio, "sleep", stub_sleep):
                with mock.patch.object(event_store, "list_sessions", fake_list_sessions):
                    with mock.patch.object(event_store, "append_event", lambda e: appended.append(e)):
                        try:
                            await liveness_watcher.watch_loop(fake_on_event, interval_seconds=1)
                        except asyncio.CancelledError:
                            pass

        asyncio.run(runner())
        dead = [e for e in appended if e["event"] == "SessionDead"]
        self.assertEqual(len(dead), 1, f"active+pid_dead 应该照常报 dead，实际：{[e['event'] for e in appended]}")


if __name__ == "__main__":
    import io
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)
    print(stream.getvalue())
    print(f"\n=== {result.testsRun} tests, {len(result.failures)} failures, {len(result.errors)} errors ===")
    sys.exit(0 if (not result.failures and not result.errors) else 1)
