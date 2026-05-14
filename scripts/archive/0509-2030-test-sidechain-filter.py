#!/usr/bin/env python3
"""教训 L08 验证 — 子 agent Notification 应被过滤。

测试三种场景：
  T1: 主 session Notification（subagents 目录不存在 / 无活跃 jsonl）→ 推送
  T2: 子 agent Notification（subagents 目录里有最近 5s 内 mtime 的 agent-*.jsonl）→ 过滤
  T3: 配置关闭 filter_sidechain_notifications=false → 即便 sidechain 也推送

不依赖 backend 启动，纯 unit。
"""
from __future__ import annotations
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.sources import detect_sidechain_active, _normalize_claude_code  # noqa: E402
from backend.notify_filter import should_notify  # noqa: E402


def _make_fake_transcript(workdir: Path, sid: str, has_sidechain: bool, age_sec: float = 0.5) -> str:
    """造一份 transcript 布局：
    workdir/<sid>.jsonl  父 transcript
    workdir/<sid>/subagents/agent-XXXX.jsonl  子 agent transcript（可选）
    返回父 transcript 的绝对路径。
    """
    parent_jsonl = workdir / f"{sid}.jsonl"
    parent_jsonl.write_text('{"type":"user"}\n', encoding="utf-8")
    if has_sidechain:
        sub_dir = workdir / sid / "subagents"
        sub_dir.mkdir(parents=True, exist_ok=True)
        agent_jsonl = sub_dir / "agent-deadbeef.jsonl"
        agent_jsonl.write_text('{"isSidechain":true,"type":"user"}\n', encoding="utf-8")
        # 设 mtime 为 age_sec 秒前
        now = time.time()
        os.utime(agent_jsonl, (now - age_sec, now - age_sec))
    return str(parent_jsonl)


def _make_payload(transcript_path: str, message: str = "Claude is waiting for your input") -> dict:
    """模拟 hook-notify.py 写入 events.jsonl 后 backend 看到的 envelope。"""
    return {
        "source": "claude_code",
        "session_id": "ssn-test",
        "event": "Notification",
        "cwd": "/tmp/proj",
        "transcript_path": transcript_path,
        "message": message,
        "raw": {
            "session_id": "ssn-test",
            "transcript_path": transcript_path,
            "cwd": "/tmp/proj",
            "hook_event_name": "Notification",
            "message": message,
            "notification_type": "idle_prompt",
        },
    }


def assert_eq(name: str, got, want):
    if got == want:
        print(f"  PASS {name}: {got!r}")
    else:
        print(f"  FAIL {name}: got={got!r} want={want!r}")
        sys.exit(1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # === T1: 主 session Notification ===
        print("[T1] main-session Notification (no subagents dir)")
        tp = _make_fake_transcript(tmp_path, "ssn-main", has_sidechain=False)
        evt = _normalize_claude_code(_make_payload(tp))
        assert_eq("is_sidechain", evt.get("is_sidechain"), False)
        ok, reason = should_notify(evt, summary={}, cfg=None)
        assert_eq("should_notify", ok, True)
        assert_eq("reason", reason, "notification_kept")

        # === T2: sidechain active（子 agent 刚写过 jsonl）===
        print("[T2] sidechain Notification (recent subagents jsonl)")
        tp = _make_fake_transcript(tmp_path, "ssn-sub", has_sidechain=True, age_sec=0.5)
        # detect_sidechain_active 直接验
        assert_eq("detect_sidechain_active", detect_sidechain_active(tp), True)
        evt = _normalize_claude_code(_make_payload(tp))
        assert_eq("is_sidechain", evt.get("is_sidechain"), True)
        ok, reason = should_notify(evt, summary={}, cfg=None)
        assert_eq("should_notify", ok, False)
        assert_eq("reason", reason, "sidechain_active")

        # === T3: sidechain 但用户关了 filter ===
        print("[T3] sidechain Notification + filter disabled")
        cfg_off = {"notify_filter": {"filter_sidechain_notifications": False}}
        ok, reason = should_notify(evt, summary={}, cfg=cfg_off)
        assert_eq("should_notify", ok, True)
        assert_eq("reason", reason, "notification_kept")

        # === T4: subagents jsonl 太老（超过 5s 窗）→ 不视为 active ===
        print("[T4] sidechain jsonl older than window")
        tp = _make_fake_transcript(tmp_path, "ssn-old", has_sidechain=True, age_sec=10.0)
        assert_eq("detect_sidechain_active(stale)", detect_sidechain_active(tp), False)

        # === T5: hook payload 没 transcript_path → 退化 False ===
        print("[T5] empty transcript_path")
        assert_eq("detect_sidechain_active(empty)", detect_sidechain_active(""), False)

        # === T6: Stop 事件不跑 sidechain 检测（性能）===
        print("[T6] Stop event skips detection")
        tp = _make_fake_transcript(tmp_path, "ssn-stop", has_sidechain=True, age_sec=0.5)
        stop_payload = _make_payload(tp, message="任务完成")
        stop_payload["event"] = "Stop"
        stop_payload["raw"]["hook_event_name"] = "Stop"
        evt_stop = _normalize_claude_code(stop_payload)
        assert_eq("is_sidechain(Stop)", evt_stop.get("is_sidechain"), False)

        print("\nALL PASS")
        return 0


if __name__ == "__main__":
    sys.exit(main())
