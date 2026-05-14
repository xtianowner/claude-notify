#!/usr/bin/env python3
"""L14：per-session 静音单元测试。

覆盖：
1. scope=all 永久 → 所有事件 drop（含 Notification / Stop / TimeoutSuspect）
2. scope=stop_only 30 分钟 → Stop/SubagentStop drop，Notification/TimeoutSuspect 通过
3. until 已过期 → 视为未静音 + 自动清理 config
4. 进程启动 prune_expired_session_mutes 行为
5. set_session_mute / clear_session_mute 写盘形态
"""
from __future__ import annotations
import json
import sys
import time
import unittest
from pathlib import Path

# 让 backend 模块可 import（项目根加入 sys.path）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试期间用临时 data dir 隔离（避免污染真实 config）
import tempfile
import os

_TMP_DIR = tempfile.mkdtemp(prefix="claude-notify-l14-")
os.environ["CLAUDE_NOTIFY_DATA_DIR_OVERRIDE"] = _TMP_DIR

# Monkey-patch DATA_DIR / CONFIG_PATH 之前先 import config
from backend import config as cfg_mod  # noqa: E402

cfg_mod.DATA_DIR = Path(_TMP_DIR)
cfg_mod.CONFIG_PATH = cfg_mod.DATA_DIR / "config.json"
cfg_mod.DATA_DIR.mkdir(parents=True, exist_ok=True)

# decision_log 也指向临时目录，避免污染真实 push_decisions.jsonl
from backend import decision_log  # noqa: E402
decision_log.DECISIONS_PATH = cfg_mod.DATA_DIR / "push_decisions.jsonl"

from backend import notify_filter  # noqa: E402


def _evt(ev_type: str, sid: str = "S1", **kw) -> dict:
    e = {"event": ev_type, "session_id": sid, "ts": "2026-05-09T23:00:00+08:00"}
    e.update(kw)
    return e


class TestSessionMute(unittest.TestCase):
    def setUp(self):
        # 清空 config（每个 case 独立）
        if cfg_mod.CONFIG_PATH.exists():
            cfg_mod.CONFIG_PATH.unlink()
        if decision_log.DECISIONS_PATH.exists():
            decision_log.DECISIONS_PATH.unlink()

    def test_scope_all_permanent_drops_all(self):
        """scope=all 永久 → Notification / Stop / TimeoutSuspect 全部 drop。"""
        cfg_mod.set_session_mute("S1", until=None, scope="all")
        cfg = cfg_mod.load()
        for ev in ("Notification", "Stop", "TimeoutSuspect", "TestNotify"):
            ok, reason = notify_filter.should_notify(_evt(ev), {}, cfg, log_trace=False)
            self.assertFalse(ok, f"{ev} should be dropped")
            self.assertEqual(reason, "session_muted_all", f"{ev} reason mismatch")

    def test_scope_stop_only_30min(self):
        """scope=stop_only → 仅 Stop/SubagentStop drop，Notification 通过。"""
        cfg_mod.set_session_mute("S1", until=time.time() + 30 * 60, scope="stop_only")
        cfg = cfg_mod.load()

        # Stop / SubagentStop drop
        for ev in ("Stop", "SubagentStop"):
            ok, reason = notify_filter.should_notify(_evt(ev), {}, cfg, log_trace=False)
            self.assertFalse(ok, f"{ev} should be dropped under stop_only")
            self.assertEqual(reason, "session_muted_stop_only")

        # Notification / TimeoutSuspect 不被 mute 拦截（会进各自 decision 路径）
        ok, reason = notify_filter.should_notify(
            _evt("Notification", message="please input"), {}, cfg, log_trace=False)
        self.assertTrue(ok, "Notification should pass under stop_only")
        self.assertNotEqual(reason, "session_muted_stop_only")

        ok, reason = notify_filter.should_notify(
            _evt("TimeoutSuspect"), {}, cfg, log_trace=False)
        self.assertTrue(ok, "TimeoutSuspect should pass under stop_only")

    def test_other_session_unaffected(self):
        """静音 S1 不影响 S2。"""
        cfg_mod.set_session_mute("S1", until=None, scope="all")
        cfg = cfg_mod.load()
        ok, reason = notify_filter.should_notify(
            _evt("Notification", sid="S2", message="please input"), {}, cfg, log_trace=False)
        self.assertTrue(ok, f"S2 should not be muted, got reason={reason}")

    def test_expired_auto_clears(self):
        """until 已过期 → 视为未静音 + 自动清理 config.session_mutes。"""
        # 写入 1 秒前过期的项
        cfg_mod.set_session_mute("S1", until=time.time() - 5, scope="all")
        # 验证写入成功
        cfg = cfg_mod.load()
        self.assertIn("S1", cfg.get("session_mutes") or {})

        # should_notify 命中 → 触发清理
        ok, reason = notify_filter.should_notify(
            _evt("Stop"), {}, cfg, log_trace=False)
        # 走到 _stop_decision（已清理 session_mute），结果可能 push 也可能 drop（看 stop 阈值）
        # 关键：reason 不应是 session_muted_*
        self.assertNotIn("session_muted", reason)

        # 重新 load → entry 已被清理
        cfg2 = cfg_mod.load()
        self.assertNotIn("S1", cfg2.get("session_mutes") or {},
                         "expired entry should be cleared")

    def test_prune_expired(self):
        """启动时 prune_expired_session_mutes 清理过期项。"""
        # 同时写两条：一条已过期，一条尚未过期
        cfg_mod.set_session_mute("S_old", until=time.time() - 10, scope="all")
        cfg_mod.set_session_mute("S_new", until=time.time() + 600, scope="all")
        cfg_mod.set_session_mute("S_perm", until=None, scope="stop_only")

        n = cfg_mod.prune_expired_session_mutes()
        self.assertEqual(n, 1, "should prune exactly 1 expired entry")

        cfg = cfg_mod.load()
        sm = cfg.get("session_mutes") or {}
        self.assertNotIn("S_old", sm)
        self.assertIn("S_new", sm)
        self.assertIn("S_perm", sm, "permanent (until=None) should not be pruned")

    def test_set_and_clear_persistence(self):
        """set_session_mute / clear_session_mute 写盘形态。"""
        until_ts = time.time() + 5 * 60
        entry = cfg_mod.set_session_mute(
            "S1", until=until_ts, scope="stop_only", label="test-label")

        self.assertEqual(entry["scope"], "stop_only")
        self.assertEqual(entry["label"], "test-label")
        self.assertAlmostEqual(entry["until"], until_ts, places=2)
        self.assertIn("muted_at_iso", entry)

        # 持久化形态
        raw = json.loads(cfg_mod.CONFIG_PATH.read_text())
        self.assertIn("S1", raw["session_mutes"])
        self.assertEqual(raw["session_mutes"]["S1"]["scope"], "stop_only")

        # clear
        cleared = cfg_mod.clear_session_mute("S1")
        self.assertTrue(cleared)
        raw2 = json.loads(cfg_mod.CONFIG_PATH.read_text())
        self.assertNotIn("S1", raw2.get("session_mutes") or {})

        # clear 不存在的 sid → False
        self.assertFalse(cfg_mod.clear_session_mute("S_does_not_exist"))

    def test_invalid_scope_falls_back_to_all(self):
        """非法 scope 字符串 → fallback 到 all。"""
        entry = cfg_mod.set_session_mute("S1", until=None, scope="garbage")
        self.assertEqual(entry["scope"], "all")

    def test_mute_check_no_sid(self):
        """没有 session_id 时 _session_mute_check 返回 (False, '')。"""
        drop, reason = notify_filter._session_mute_check("", "Stop", {})
        self.assertFalse(drop)
        self.assertEqual(reason, "")


def _run_endpoint_form_check():
    """API endpoint 形态检查（不真启 server，调 FastAPI 路由对象）。

    确认 mute_session / unmute_session 在 app.py 注册。
    """
    print("\n--- API endpoint form check ---")
    from backend import app as app_mod
    expected = [
        ("/api/sessions/{session_id}/mute", "POST"),
        ("/api/sessions/{session_id}/mute", "DELETE"),
    ]
    for path, method in expected:
        match = None
        for r in app_mod.app.routes:
            if getattr(r, "path", None) != path:
                continue
            methods = getattr(r, "methods", None) or set()
            if method in methods:
                match = r
                break
        if match is None:
            print(f"  FAIL: {method} {path} 未注册")
            return False
        print(f"  OK:   {method} {path} -> {match.endpoint.__name__}")
    return True


if __name__ == "__main__":
    print(f"Using temp data dir: {_TMP_DIR}")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestSessionMute)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    api_ok = _run_endpoint_form_check()

    if not result.wasSuccessful() or not api_ok:
        sys.exit(1)
    print("\nAll session-mute tests passed.")
