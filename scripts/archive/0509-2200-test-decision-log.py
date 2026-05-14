#!/usr/bin/env python3
"""测试 L12 决策 trace 落盘 + API 返回（不联网）。

覆盖：
1. notify_filter.should_notify 触发各种 reason → 写入 push_decisions.jsonl
2. decision_log.recent_for / all_for 返回结构正确（按 ts 倒序）
3. event_store.list_sessions 注入 recent_decisions 字段
4. trim 阈值触发后只保留每个 sid 最近 PER_SESSION_MAX 条
"""
from __future__ import annotations
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

# 让 backend 用临时目录（避免污染真实 data/）
TMP_ROOT = Path(tempfile.mkdtemp(prefix="cn-test-trace-"))
TMP_DATA = TMP_ROOT / "data"
TMP_DATA.mkdir()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 提前 monkey-patch config 路径，让 decision_log / event_store 都用临时目录
from backend import config as cfg_mod
cfg_mod.DATA_DIR = TMP_DATA
cfg_mod.CONFIG_PATH = TMP_DATA / "config.json"
cfg_mod.EVENTS_PATH = TMP_DATA / "events.jsonl"
# 写默认 config
cfg_mod.save({})

from backend import decision_log, notify_filter

# 同步 decision_log 模块的 path（它在 import 时已绑定）
decision_log.DECISIONS_PATH = TMP_DATA / "push_decisions.jsonl"


def banner(title: str):
    print(f"\n=== {title} ===")


def assert_eq(got, want, msg=""):
    if got != want:
        print(f"  FAIL {msg}: got={got!r} want={want!r}")
        sys.exit(1)
    print(f"  OK {msg}")


def assert_truthy(got, msg=""):
    if not got:
        print(f"  FAIL {msg}: got={got!r}")
        sys.exit(1)
    print(f"  OK {msg}")


# ── case 1: notify_filter 写决策 ──
banner("case 1: notify_filter writes decisions")
decision_log.reset_for_test()

# Stop 含锚词 → push
ok, reason = notify_filter.should_notify(
    {"event": "Stop", "session_id": "sid-A",
     "last_assistant_message": "已完成端到端验证"},
    {"last_milestone": "", "turn_summary": ""},
    {},
)
assert_truthy(ok, "Stop with anchor → push")
assert_truthy("anchor" in reason or "summary_len" in reason, f"reason={reason}")

# Stop 短回复无锚词 + 1 分钟前刚推过 → drop（grace 8min 不到 + 黑词命中）
ok, reason = notify_filter.should_notify(
    {"event": "Stop", "session_id": "sid-A",
     "last_assistant_message": "ok"},
    {"last_milestone": "", "turn_summary": "ok",
     "last_stop_pushed_unix": time.time() - 60,  # 1 分钟前刚推过
     "last_notification_unix": time.time() - 60},
    {},
)
assert_eq(ok, False, "Stop short blacklisted (ok) → drop")
assert_truthy(reason in ("stop_low_signal",), f"reason={reason}")

# Notification sidechain → drop
ok, reason = notify_filter.should_notify(
    {"event": "Notification", "session_id": "sid-A",
     "is_sidechain": True,
     "message": "waiting for your input"},
    {},
    {"notify_filter": {"filter_sidechain_notifications": True}},
)
assert_eq(ok, False, "Notification sidechain → drop")
assert_eq(reason, "sidechain_active", "sidechain reason matched")

# TestNotify → push always
ok, reason = notify_filter.should_notify(
    {"event": "TestNotify", "session_id": "sid-B"},
    {}, {},
)
assert_eq(ok, True, "TestNotify → always push")

# ── case 2: decision_log 读路径 ──
banner("case 2: decision_log read path")
recent_a = decision_log.recent_for("sid-A", limit=5)
assert_truthy(len(recent_a) >= 3, f"sid-A recent_for got {len(recent_a)} records")
assert_eq(recent_a[0]["event"], "Notification", "newest first (Notification)")
# 最新在前 → ts 单调递减
ts_seq = [r["ts"] for r in recent_a]
assert_truthy(ts_seq == sorted(ts_seq, reverse=True), "recent_for is sorted desc")

all_a = decision_log.all_for("sid-A")
assert_eq(len(all_a), 3, "sid-A all_for returns 3")

# 检查必含字段
for rec in all_a:
    for f in ("ts", "session_id", "event", "decision", "reason"):
        assert_truthy(f in rec, f"record has {f}")
    assert_eq(rec["session_id"], "sid-A", "sid matches")

# 不同 sid 隔离
all_b = decision_log.all_for("sid-B")
assert_eq(len(all_b), 1, "sid-B isolated")
assert_eq(all_b[0]["event"], "TestNotify", "sid-B has TestNotify")

# ── case 3: event_store.list_sessions 注入 ──
banner("case 3: event_store injects recent_decisions")
from backend import event_store
event_store.EVENTS_PATH = TMP_DATA / "events.jsonl"
# 写一条 events.jsonl 让 sid-A 出现
event_store.append_event({
    "event": "Stop", "session_id": "sid-A",
    "ts": "2026-05-09T22:00:00+08:00",
    "source": "claude_code",
    "project": "test", "cwd": "/tmp",
})
sessions = event_store.list_sessions(active_window_minutes=60 * 24 * 30)
sid_sessions = [s for s in sessions if s["session_id"] == "sid-A"]
assert_eq(len(sid_sessions), 1, "sid-A in list_sessions")
sa = sid_sessions[0]
assert_truthy("recent_decisions" in sa, "recent_decisions field exists")
assert_truthy(len(sa["recent_decisions"]) >= 1, "recent_decisions populated")
print(f"  recent_decisions sample: {sa['recent_decisions'][0]}")

# ── case 4: trim 行为 ──
banner("case 4: per-session trim caps at PER_SESSION_MAX")
decision_log.reset_for_test()
# 灌 100 条到同一个 sid
for i in range(100):
    notify_filter.should_notify(
        {"event": "TestNotify", "session_id": "sid-trim"},
        {}, {},
    )
# 强制 trim
decision_log._trim_locked()
all_trimmed = decision_log.all_for("sid-trim", limit=200)
assert_truthy(len(all_trimmed) <= decision_log.PER_SESSION_MAX,
              f"trimmed to {len(all_trimmed)} (cap={decision_log.PER_SESSION_MAX})")
assert_eq(len(all_trimmed), decision_log.PER_SESSION_MAX,
          f"exactly {decision_log.PER_SESSION_MAX} kept")

# ── case 5: notify_policy off path ──
banner("case 5: notify_policy off path writes drop trace")
decision_log.reset_for_test()
from backend import notify_policy

# 模拟一个 SubagentStop（默认 policy_map 里 off）
# 我们直接调 _trace 短路验证（submit 需要 cfg load）
notify_policy.NotifyDispatcher._trace("sid-P", "SubagentStop", "drop", "policy_off", "off")
recent_p = decision_log.recent_for("sid-P", limit=5)
assert_eq(len(recent_p), 1, "policy off trace written")
assert_eq(recent_p[0]["decision"], "drop", "decision=drop")
assert_eq(recent_p[0]["reason"], "policy_off", "reason=policy_off")
assert_eq(recent_p[0]["policy"], "off", "policy=off")

print("\nAll tests passed.")
print(f"tmp data dir: {TMP_ROOT}")
