"""Test silence-then merge dispatcher (B 方案).

Run: /opt/miniconda3/bin/python3 scripts/0509-1620-test-silence-merge.py
"""
from __future__ import annotations
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import notify_policy, feishu


# ── stub feishu.send_event / send_events_combined ──
sent: list[dict] = []


async def fake_send_event(evt):
    sent.append({"kind": "single", "event": evt.get("event"), "ts": evt.get("ts")})
    return {"ok": True}


async def fake_send_events_combined(events):
    sent.append({
        "kind": "combined",
        "n": len(events),
        "types": [e.get("event") for e in events],
    })
    return {"ok": True, "merged": len(events)}


feishu.send_event = fake_send_event
feishu.send_events_combined = fake_send_events_combined

# v3: 旁路 should_notify（这个测试只验证 silence-then 合并行为，should_notify 有自己的单测）
from backend import notify_filter
notify_filter.should_notify = lambda evt, summary, cfg: (True, "test_bypass")

# ── stub config 让 silence:1 生效（缩短测试时间） ──
import backend.config as cfg_mod

orig_load = cfg_mod.load


def _patched_load():
    c = orig_load()
    c["notify_policy"] = {
        "Stop": "silence:1",
        "SubagentStop": "silence:1",
        "Notification": "immediate",
        "TimeoutSuspect": "immediate",
    }
    c["muted"] = False
    return c


cfg_mod.load = _patched_load


async def case(name, do):
    print(f"--- {name} ---")
    sent.clear()
    notify_policy._singleton = None  # 新 dispatcher
    await do()
    print(f"  sent: {sent}")
    print()


async def main():
    # case 1: 单 Stop → 1.0s 后单发
    await case("single Stop", lambda: _seq([
        ("Stop", "sid-A", 0.0),
    ]))
    assert sent == [{"kind": "single", "event": "Stop", "ts": "t0"}], sent

    # case 2: 1s 内连续 2 个 Stop → 合并成 1 条 combined（n=2）
    await case("Stop x2 within window", lambda: _seq([
        ("Stop", "sid-B", 0.0),
        ("Stop", "sid-B", 0.3),
    ]))
    assert sent == [{"kind": "combined", "n": 2, "types": ["Stop", "Stop"]}], sent

    # case 3: Stop + Stop + SubagentStop in 1s → 合并成 1 条 combined（n=3）
    await case("Stop+Stop+SubagentStop", lambda: _seq([
        ("Stop", "sid-C", 0.0),
        ("Stop", "sid-C", 0.3),
        ("SubagentStop", "sid-C", 0.5),
    ]))
    assert sent == [{"kind": "combined", "n": 3,
                     "types": ["Stop", "Stop", "SubagentStop"]}], sent

    # case 4: ACTIVITY (Heartbeat) cancels pending → 不发任何
    await case("Heartbeat cancels pending Stop", lambda: _seq([
        ("Stop", "sid-D", 0.0),
        ("Heartbeat", "sid-D", 0.3),
    ]))
    assert sent == [], sent

    # case 5: Notification 立即发 + 取消 pending
    await case("Notification preempts pending Stop", lambda: _seq([
        ("Stop", "sid-E", 0.0),
        ("Notification", "sid-E", 0.2),
    ]))
    # 顺序：Notification 立即发，Stop pending 被 cancel
    assert sent == [{"kind": "single", "event": "Notification", "ts": "t1"}], sent

    # case 6: 不同 sid 互不干扰
    await case("Independent sids", lambda: _seq([
        ("Stop", "sid-F", 0.0),
        ("Stop", "sid-G", 0.2),
    ]))
    # 两边都各自单发
    types = sorted([(s["kind"], s["event"]) for s in sent])
    assert types == [("single", "Stop"), ("single", "Stop")], sent

    print("=" * 60)
    print("ALL CASES PASS")


async def _seq(events: list[tuple[str, str, float]]):
    """events: [(ev_type, sid, delay_after_start)] —— 异步 submit 后等 1.5s 让 silence:1 倒计时完成"""
    d = notify_policy.get_dispatcher()
    t0 = time.time()
    for i, (et, sid, dly) in enumerate(events):
        wait = (t0 + dly) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        await d.submit({"event": et, "session_id": sid, "ts": f"t{i}"})
    await asyncio.sleep(1.5)


if __name__ == "__main__":
    asyncio.run(main())
