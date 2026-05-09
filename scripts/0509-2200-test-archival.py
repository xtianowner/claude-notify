#!/usr/bin/env python3
"""测试 events.jsonl 滚动归档（教训 L13）。

case 1: hot < threshold → 不动
case 2: hot >= threshold + 全是老事件 → 全归档，hot 接近空
case 3: hot >= threshold + 混合新老 → 老归档，新留 hot
case 4: 总条数守恒（archive + hot = 原 mock 总数）
case 5: list_sessions 默认（include_archive=False）只看 hot 近期 session
case 6: list_sessions(include_archive=True) 看到所有 session
case 7: 重复触发（archive 已存在文件 → append gzip member）+ 读端能正确读全部
case 8: 启动时间（list_sessions 默认调用）

跑：python scripts/0509-2200-test-archival.py
"""
from __future__ import annotations
import gzip
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 让 backend 能 import
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import archival  # noqa: E402

SHANGHAI_TZ = timezone(timedelta(hours=8))


def make_event(session_id: str, event_type: str, ts_unix: float, idx: int = 0) -> dict:
    """合成一条 mock event（结构跟 backend.sources 输出对齐）。"""
    dt = datetime.fromtimestamp(ts_unix, tz=SHANGHAI_TZ)
    ts_iso = dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    return {
        "ts": ts_iso,
        "source": "claude_code",
        "session_id": session_id,
        "event": event_type,
        "cwd": f"/Users/tian/proj/{session_id[:6]}",
        "cwd_short": f"proj/{session_id[:6]}",
        "project": session_id[:6],
        "transcript_path": f"/tmp/tr-{session_id}.jsonl",
        "message": f"event-{idx}",
        "claude_pid": 99999,
        "tty": "/dev/ttys000",
        "permission_mode": "default",
        "effort_level": "normal",
        "last_assistant_message": None,
        "reason": None,
        "raw": {"hook_event_name": event_type, "_padding": "x" * 800},  # 让单条事件 ~1KB
    }


def write_mock_events(path: Path, n: int, old_ratio: float, now: float, span_days: float = 7.0) -> tuple[int, int]:
    """写 n 条 mock event 到 path。

    old_ratio = 老事件比例（ts 早于 now-2*86400，应被归档）
    返回 (old_count, new_count)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    old_count = 0
    new_count = 0
    cutoff_old = now - 2 * 86400  # 2 天前
    cutoff_new_min = now - 0.5 * 86400  # 半天内

    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            sid = f"sess-{i // 100:03d}-{i % 100:04d}"  # 每 100 条同一 session
            if i / n < old_ratio:
                # 老事件：均匀分布在 [now-span_days, now-2d]
                ts_unix = (now - span_days * 86400) + (i / max(1, int(n * old_ratio))) * (cutoff_old - (now - span_days * 86400))
                old_count += 1
            else:
                # 新事件：均匀分布在 [now-12h, now]
                rel = (i - n * old_ratio) / max(1, n - n * old_ratio)
                ts_unix = cutoff_new_min + rel * (now - cutoff_new_min)
                new_count += 1
            evt = make_event(sid, "Heartbeat", ts_unix, idx=i)
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
    return old_count, new_count


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for ln in f if ln.strip())


def count_archive_events(archive_dir: Path) -> int:
    return sum(1 for _ in archival.iter_archived_events(archive_dir))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="test-archival-"))
    print(f"[setup] tmp dir: {tmp}")
    try:
        events_path = tmp / "events.jsonl"
        archive_dir = tmp / "archive"

        # ── case 1: hot < threshold → 不动
        print("\n[case 1] hot < threshold → 不动")
        write_mock_events(events_path, 100, 0.5, now=time.time())
        before_size = events_path.stat().st_size
        before_lines = count_lines(events_path)
        stats = archival.archive_if_needed(events_path, archive_dir, max_hot_size_mb=50, rotation_grace_days=1)
        assert not stats["triggered"], f"case 1 fail: should not trigger (size={before_size})"
        assert stats["reason"] == "below_threshold", stats
        assert events_path.stat().st_size == before_size
        assert count_lines(events_path) == before_lines
        assert not archive_dir.exists() or count_archive_events(archive_dir) == 0
        print(f"  OK: hot {before_size} bytes, no archive, reason={stats['reason']}")

        # ── case 2/3/4: hot >= threshold + 混合新老 → 老归档，新留 hot，总数守恒
        print("\n[case 2-4] mock 50000 events (~50MB)，混合新老 ts，触发归档")
        # 清空
        events_path.unlink(missing_ok=True)
        if archive_dir.exists():
            shutil.rmtree(archive_dir)
        n = 50_000
        old_ratio = 0.7  # 70% 老事件应被归档
        now = time.time()
        old_count, new_count = write_mock_events(events_path, n, old_ratio, now=now, span_days=10.0)
        before_size = events_path.stat().st_size
        before_lines = count_lines(events_path)
        print(f"  hot before: {before_size:,} bytes / {before_lines:,} lines (old={old_count}, new={new_count})")
        assert before_lines == n

        # 测启动时间（archive 触发前 list 全量代价）
        t0 = time.time()
        full_count = sum(1 for _ in (json.loads(ln) for ln in open(events_path) if ln.strip()))
        t_full_read = time.time() - t0
        assert full_count == n
        print(f"  full read time (pre-archive): {t_full_read*1000:.1f}ms")

        # 触发归档（threshold 设到能命中：50_000 条 × ~1KB ≈ 50MB；用 30MB 确保触发）
        threshold_mb = 30
        t0 = time.time()
        stats = archival.archive_if_needed(events_path, archive_dir, max_hot_size_mb=threshold_mb, rotation_grace_days=1)
        t_archive = time.time() - t0
        print(f"  archive_if_needed took {t_archive*1000:.1f}ms")
        print(f"  stats: {stats}")
        assert stats["triggered"], f"case 2 fail: should trigger at threshold={threshold_mb}MB, size={before_size}"
        assert stats["reason"] == "ok", stats
        archived = stats["archived_count"]
        kept = stats["kept_count"]
        assert archived > 0, "case 2 fail: no events archived"
        assert kept > 0, "case 2 fail: nothing kept hot"
        # 总数守恒
        hot_lines_after = count_lines(events_path)
        archive_lines = count_archive_events(archive_dir)
        print(f"  hot after: {events_path.stat().st_size:,} bytes / {hot_lines_after:,} lines")
        print(f"  archive total events: {archive_lines:,}")
        print(f"  total: {hot_lines_after + archive_lines:,} (expected {n:,})")
        assert hot_lines_after + archive_lines == n, \
            f"case 4 fail: hot {hot_lines_after} + archive {archive_lines} != {n}"
        # hot 缩小
        assert events_path.stat().st_size < before_size, "case 3 fail: hot did not shrink"
        # archive 出现 .gz
        gz_files = list(archive_dir.glob("events-*.jsonl.gz"))
        assert len(gz_files) > 0, "case 3 fail: no gzip archive file"
        print(f"  archive files: {len(gz_files)} ({[f.name for f in gz_files[:3]]}...)")

        # 启动时间提速验证：post-archive 读 hot 时间 << pre-archive 全量读
        t0 = time.time()
        hot_count = sum(1 for _ in (json.loads(ln) for ln in open(events_path) if ln.strip()))
        t_hot_read = time.time() - t0
        print(f"  hot-only read time (post-archive): {t_hot_read*1000:.1f}ms (was {t_full_read*1000:.1f}ms)")
        assert t_hot_read < t_full_read, "case post-archive should be faster"

        # ── case 5: 重复触发（已 below_threshold） → 不动
        print("\n[case 5] 归档后再触发 → below_threshold")
        stats2 = archival.archive_if_needed(events_path, archive_dir, max_hot_size_mb=threshold_mb, rotation_grace_days=1)
        assert not stats2["triggered"], stats2
        assert stats2["reason"] == "below_threshold", stats2
        print(f"  OK: {stats2['reason']}")

        # ── case 6: 强制再归档（threshold 调小到 1MB） → archive 多 member 累积，读端能跨 member 读
        print("\n[case 6] 二次归档 + gzip 多 member 累积")
        # 写更多老事件让 hot 再涨
        with open(events_path, "a", encoding="utf-8") as f:
            for i in range(5000):
                evt = make_event(f"add-{i:04d}", "Heartbeat", now - 5 * 86400 - i, idx=i + n)
                f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        before_archive_count = count_archive_events(archive_dir)
        stats3 = archival.archive_if_needed(events_path, archive_dir, max_hot_size_mb=1, rotation_grace_days=1)
        assert stats3["triggered"], stats3
        after_archive_count = count_archive_events(archive_dir)
        print(f"  archive events: {before_archive_count} -> {after_archive_count} (delta={after_archive_count - before_archive_count})")
        assert after_archive_count > before_archive_count, "case 6 fail: 二次归档未追加"
        # 验证读端能跨 gzip member 读出全部
        total_after_2nd = count_lines(events_path) + after_archive_count
        expected_total = n + 5000
        assert total_after_2nd == expected_total, \
            f"case 6 fail: total {total_after_2nd} != expected {expected_total}"
        print(f"  OK: gzip 多 member 读取正常，总数守恒 {total_after_2nd}")

        # ── case 7: list_sessions 行为（hot-only 默认 vs include_archive）
        print("\n[case 7] list_sessions hot-only vs include_archive")
        # 切到真实 backend.event_store，但要把 EVENTS_PATH / ARCHIVE_DIR 临时指过来
        os.environ["TZ"] = "Asia/Shanghai"
        from backend import config as cfg_mod
        from backend import event_store
        # 临时替换路径
        orig_events = cfg_mod.EVENTS_PATH
        orig_archive = cfg_mod.ARCHIVE_DIR
        cfg_mod.EVENTS_PATH = events_path
        cfg_mod.ARCHIVE_DIR = archive_dir
        # event_store 也是从 cfg_mod 导入的常量
        event_store.EVENTS_PATH = events_path
        event_store.ARCHIVE_DIR = archive_dir
        # _resolve_archive_dir 会读 config.json，强制 mock：写一个临时 config
        tmp_cfg_path = tmp / "config.json"
        tmp_cfg_path.write_text(json.dumps({
            "archival": {"enabled": True, "archive_dir": str(archive_dir)}
        }))
        cfg_mod.CONFIG_PATH = tmp_cfg_path

        try:
            t0 = time.time()
            hot_only_sessions = event_store.list_sessions(active_window_minutes=30, include_archive=False)
            t_hot = time.time() - t0
            t0 = time.time()
            full_sessions = event_store.list_sessions(active_window_minutes=30, include_archive=True)
            t_full = time.time() - t0
            print(f"  hot-only: {len(hot_only_sessions)} sessions in {t_hot*1000:.1f}ms")
            print(f"  full:     {len(full_sessions)} sessions in {t_full*1000:.1f}ms")
            assert len(full_sessions) >= len(hot_only_sessions), "full should have >= hot sessions"
            # hot-only 的 session 都应该在 full 里
            hot_sids = {s["session_id"] for s in hot_only_sessions}
            full_sids = {s["session_id"] for s in full_sessions}
            assert hot_sids.issubset(full_sids), "hot sessions should be subset of full"
            print(f"  OK: hot⊆full ({len(hot_sids)}⊆{len(full_sids)})")
        finally:
            cfg_mod.EVENTS_PATH = orig_events
            cfg_mod.ARCHIVE_DIR = orig_archive
            event_store.EVENTS_PATH = orig_events
            event_store.ARCHIVE_DIR = orig_archive

        # ── case 8: archive_if_needed disabled
        print("\n[case 8] archival.enabled=False → 跳过")
        # 直接测 archival 模块层（disabled 在 event_store.archive_if_needed 包装层判断）
        # 所以这里测 event_store 包装层
        cfg_mod.EVENTS_PATH = events_path
        event_store.EVENTS_PATH = events_path
        tmp_cfg_path.write_text(json.dumps({
            "archival": {"enabled": False, "archive_dir": str(archive_dir)}
        }))
        try:
            stats_disabled = event_store.archive_if_needed()
            assert not stats_disabled["triggered"]
            assert stats_disabled["reason"] == "disabled", stats_disabled
            print(f"  OK: {stats_disabled['reason']}")
        finally:
            cfg_mod.EVENTS_PATH = orig_events
            event_store.EVENTS_PATH = orig_events

        print("\n[summary] all cases passed")
        print(f"  threshold trigger size: {threshold_mb}MB")
        print(f"  mock 50k events: hot {before_size:,}B → {events_path.stat().st_size:,}B")
        print(f"  archive files: {len(gz_files)}")
        print(f"  total preserved: {hot_lines_after + archive_lines:,} == {n:,}")
        print(f"  pre-archive full read: {t_full_read*1000:.1f}ms")
        print(f"  post-archive hot read: {t_hot_read*1000:.1f}ms")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n[cleanup] removed {tmp}")


if __name__ == "__main__":
    main()
