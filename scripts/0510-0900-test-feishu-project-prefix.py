#!/usr/bin/env python3
"""测试飞书 L1 项目名前置 + dashboard 二级排序逻辑

覆盖：
- _extract_project_label: alias / cwd 末段 / cwd_short / 空 四档优先级
- _format_text: L1 行格式 [project] emoji focus · status，多事件类型
- 二级排序模拟：waiting/idle 老的往前，running 新的往前
"""
from __future__ import annotations
import sys
from pathlib import Path

# 让 backend 包可导入
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.feishu import _extract_project_label, _format_text  # noqa: E402


def test_extract_project_label_alias_priority():
    """alias 优先级最高（即使有 cwd）"""
    evt = {"cwd": "/Users/tian/Documents/00-Tian/00-Tian-Project/ragSystem"}
    summary = {"alias": "调 ETL"}
    assert _extract_project_label(evt, summary) == "调 ETL"


def test_extract_project_label_cwd_tail():
    """无 alias → cwd 末段"""
    evt = {"cwd": "/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify"}
    summary = {"alias": ""}
    assert _extract_project_label(evt, summary) == "claude-notify"


def test_extract_project_label_cwd_short_fallback():
    """无 alias 无 cwd → cwd_short fallback"""
    evt = {"cwd_short": "~/foo/bar"}
    summary = {}
    assert _extract_project_label(evt, summary) == "~/foo/bar"


def test_extract_project_label_empty():
    """都没有 → 空串"""
    evt = {}
    summary = None
    assert _extract_project_label(evt, summary) == ""


def test_extract_project_label_truncate():
    """超过 18 字符截断"""
    evt = {"cwd": "/very/long/path/this-is-a-very-long-project-name-indeed"}
    summary = {}
    label = _extract_project_label(evt, summary)
    assert len(label) <= 18
    assert label.endswith("…")


def test_format_text_stop_with_project_prefix():
    """Stop 事件 L1 行：[claude-notify] ✅ focus · 任务完成"""
    evt = {
        "event": "Stop",
        "session_id": "abcdef1234567890",
        "cwd": "/Users/tian/code/claude-notify",
        "ts": "2026-05-10T08:00:00+08:00",
    }
    summary = {
        "task_topic": "修 feishu 内容",
        "alias": "",
        "last_assistant_message": "已完成 5 项修改",
    }
    text = _format_text(evt, summary, {})
    l1 = text.split("\n")[0]
    assert l1.startswith("[claude-notify] "), f"L1 缺少项目前缀: {l1}"
    assert "✅" in l1
    assert "修 feishu 内容" in l1
    assert "任务完成" in l1


def test_format_text_notification_with_alias():
    """Notification + 用户 alias → L1 用 alias"""
    evt = {
        "event": "Notification",
        "session_id": "xyz",
        "cwd": "/Users/tian/code/something",
        "message": "Claude needs your approval",
        "ts": "2026-05-10T08:00:00+08:00",
    }
    summary = {
        "alias": "调 ETL 管道",
        "task_topic": "重写 loader",
        "waiting_for": "approve disk write",
    }
    text = _format_text(evt, summary, {})
    l1 = text.split("\n")[0]
    assert l1.startswith("[调 ETL 管道] "), f"L1 应使用 alias 作为前缀: {l1}"
    assert "🔔" in l1


def test_format_text_no_project_no_brackets():
    """完全无 cwd / 无 alias → 不出现空 []"""
    evt = {
        "event": "TestNotify",
        "session_id": "test-0",
        "message": "hi",
        "ts": "2026-05-10T08:00:00+08:00",
    }
    summary = None
    text = _format_text(evt, summary, {})
    l1 = text.split("\n")[0]
    assert not l1.startswith("[]"), f"无项目时不应出现空方括号: {l1}"
    # 应直接以 emoji 开头
    assert l1.startswith("🧪") or l1.startswith("📌"), f"L1 应以 emoji 起头: {l1}"


def test_format_text_timeout_with_project():
    """TimeoutSuspect 也带项目前缀"""
    evt = {
        "event": "TimeoutSuspect",
        "session_id": "sid",
        "cwd": "/path/to/etl-pipeline",
        "ts": "2026-05-10T08:00:00+08:00",
    }
    summary = {"alias": "", "age_seconds": 600, "next_action": "进终端"}
    text = _format_text(evt, summary, {})
    l1 = text.split("\n")[0]
    assert l1.startswith("[etl-pipeline] "), f"L1 缺少项目前缀: {l1}"
    assert "⚠️" in l1


def test_format_text_subagent_stop_with_project():
    """SubagentStop 也带项目前缀"""
    evt = {
        "event": "SubagentStop",
        "session_id": "sid",
        "cwd": "/Users/foo/myproj",
        "ts": "2026-05-10T08:00:00+08:00",
    }
    summary = {"alias": ""}
    text = _format_text(evt, summary, {})
    l1 = text.split("\n")[0]
    assert l1.startswith("[myproj] "), f"L1 缺少项目前缀: {l1}"
    assert "🤝" in l1


# ─── 排序逻辑模拟（dashboard 二级排序） ───
def test_sort_waiting_oldest_first():
    """waiting status 内：等最久的（旧 ts）往前"""
    STATUS_WEIGHT = {"waiting": 0, "suspect": 1, "running": 2, "idle": 3, "ended": 4, "dead": 5}
    STALE_FIRST = {"waiting", "suspect", "idle"}
    sessions = [
        {"id": "new", "status": "waiting", "ts": 1700},
        {"id": "old", "status": "waiting", "ts": 1000},
        {"id": "mid", "status": "waiting", "ts": 1500},
    ]
    sessions.sort(key=lambda s: (
        STATUS_WEIGHT[s["status"]],
        s["ts"] if s["status"] in STALE_FIRST else -s["ts"],
    ))
    ids = [s["id"] for s in sessions]
    assert ids == ["old", "mid", "new"], f"等最久的应排前: {ids}"


def test_sort_running_newest_first():
    """running status 内：最近活动（新 ts）往前"""
    STATUS_WEIGHT = {"waiting": 0, "suspect": 1, "running": 2, "idle": 3, "ended": 4, "dead": 5}
    STALE_FIRST = {"waiting", "suspect", "idle"}
    sessions = [
        {"id": "old", "status": "running", "ts": 1000},
        {"id": "new", "status": "running", "ts": 2000},
        {"id": "mid", "status": "running", "ts": 1500},
    ]
    sessions.sort(key=lambda s: (
        STATUS_WEIGHT[s["status"]],
        s["ts"] if s["status"] in STALE_FIRST else -s["ts"],
    ))
    ids = [s["id"] for s in sessions]
    assert ids == ["new", "mid", "old"], f"最近活动应排前: {ids}"


def test_sort_mixed_status_weight_dominates():
    """跨 status：先按 weight，再按二级"""
    STATUS_WEIGHT = {"waiting": 0, "suspect": 1, "running": 2, "idle": 3, "ended": 4, "dead": 5}
    STALE_FIRST = {"waiting", "suspect", "idle"}
    sessions = [
        {"id": "running-new", "status": "running", "ts": 9999},
        {"id": "waiting-old", "status": "waiting", "ts": 1000},
        {"id": "idle-old",   "status": "idle",    "ts": 500},
        {"id": "waiting-new", "status": "waiting", "ts": 8000},
    ]
    sessions.sort(key=lambda s: (
        STATUS_WEIGHT[s["status"]],
        s["ts"] if s["status"] in STALE_FIRST else -s["ts"],
    ))
    ids = [s["id"] for s in sessions]
    # waiting 在 running/idle 之前；waiting 内部老的优先
    assert ids == ["waiting-old", "waiting-new", "running-new", "idle-old"], f"排序错误: {ids}"


def main():
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
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
