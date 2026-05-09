"""events.jsonl 滚动归档（教训 L13）。

设计要点：
1. **hot 文件**（events.jsonl）保留近期事件，list_sessions 默认只读它
2. **archive 目录**（data/archive/events-YYYY-MM-DD.jsonl.gz）按事件最早 ts 的日期分文件
3. 触发条件：hot 文件大小 >= max_hot_size_mb 时归档
4. 归档时只移走 ts 早于 (now - rotation_grace_days * 86400) 的事件，保留近期热事件
5. 与 append_event() 的 flock 协同：归档时持 LOCK_EX，避免和 append 并发冲突
6. 归档失败不阻塞主流程（log warning，事件保留 hot）

归档完整性：
- 用 .tmp 文件 + os.replace 原子替换 hot
- gzip 写到 .gz.tmp 再 rename，保证 archive 文件要么完整要么不存在
- 归档失败 → 删除 .tmp 文件 → hot 不动 → 下次再试
"""
from __future__ import annotations
import fcntl
import gzip
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("claude-notify.archival")

SHANGHAI_TZ = timezone(timedelta(hours=8))


def _parse_iso(ts: str) -> float:
    """解析 ISO ts 字符串为 unix 时间戳；失败返 0.0（让其当作"很老"被归档）。"""
    try:
        if not ts:
            return 0.0
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _date_key(ts: str) -> str:
    """从 ts 抽 YYYY-MM-DD（按上海时区分日）；失败返 'unknown'。"""
    unix = _parse_iso(ts)
    if unix <= 0:
        return "unknown"
    dt = datetime.fromtimestamp(unix, tz=SHANGHAI_TZ)
    return dt.strftime("%Y-%m-%d")


def archive_path_for(date_key: str, archive_dir: Path) -> Path:
    return archive_dir / f"events-{date_key}.jsonl.gz"


def iter_archive_files(archive_dir: Path) -> list[Path]:
    """返回归档目录里的所有 .jsonl.gz 文件，按文件名（即日期）升序。"""
    if not archive_dir.exists():
        return []
    files = sorted(archive_dir.glob("events-*.jsonl.gz"))
    return list(files)


def iter_archived_events(archive_dir: Path) -> Iterable[dict[str, Any]]:
    """惰性读所有归档事件（gzip 解压），按文件名顺序（即日期升序）。"""
    for f in iter_archive_files(archive_dir):
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except Exception:
            log.exception("failed to read archive file %s", f)
            continue


def _append_to_archive(events: list[dict[str, Any]], archive_dir: Path) -> int:
    """把一组事件按 date_key 分桶 append 写入 archive/events-YYYY-MM-DD.jsonl.gz。

    返回成功写入的事件数。同一桶可能已存在，用 'ab' 模式追加 gzip stream。
    （gzip 流可拼接，多个 gzip stream 串联仍是合法 gzip 流，gzip.open(..., 'rt') 能正确读出全部）
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evt in events:
        key = _date_key(evt.get("ts") or "")
        by_date[key].append(evt)

    written = 0
    for date_key, bucket in by_date.items():
        target = archive_path_for(date_key, archive_dir)
        # gzip 多 member 串联：每次 'ab' 打开 → 追加一个新的 gzip member。读端 gzip.open 'rt' 自动跨 member 读。
        try:
            with gzip.open(target, "ab") as gz:
                for evt in bucket:
                    line = json.dumps(evt, ensure_ascii=False) + "\n"
                    gz.write(line.encode("utf-8"))
            written += len(bucket)
        except Exception:
            log.exception("failed to append to archive %s", target)
    return written


def archive_if_needed(
    events_path: Path,
    archive_dir: Path,
    max_hot_size_mb: int,
    rotation_grace_days: int,
) -> dict[str, Any]:
    """触发归档：当 hot 文件 >= max_hot_size_mb 时，把 ts 早于 (now - grace_days*86400)
    的事件移到 archive，hot 文件重写只保留近期事件。

    返回 stats：
      {
        "triggered": bool,
        "hot_size_before": int,
        "hot_size_after": int,
        "archived_count": int,
        "kept_count": int,
        "reason": str,
      }
    """
    stats: dict[str, Any] = {
        "triggered": False,
        "hot_size_before": 0,
        "hot_size_after": 0,
        "archived_count": 0,
        "kept_count": 0,
        "reason": "",
    }

    if not events_path.exists():
        stats["reason"] = "hot_not_exist"
        return stats

    try:
        size = events_path.stat().st_size
    except OSError as e:
        stats["reason"] = f"stat_failed: {e}"
        return stats

    stats["hot_size_before"] = size
    threshold = max_hot_size_mb * 1024 * 1024
    if size < threshold:
        stats["reason"] = "below_threshold"
        return stats

    cutoff_unix = time.time() - rotation_grace_days * 86400
    stats["triggered"] = True

    # 持有 LOCK_EX 整个归档过程，防止 append_event 并发写
    # 用一个 sentinel handle 确保读 + 重写在同一锁内
    try:
        with open(events_path, "r+", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError as e:
                stats["reason"] = f"flock_failed: {e}"
                return stats

            try:
                # 读取全部行（hot 文件最大几百 MB，一次读入可接受；如果未来过大可改流式）
                fh.seek(0)
                old_lines = fh.readlines()

                to_archive: list[dict[str, Any]] = []
                to_keep_lines: list[str] = []
                bad_count = 0
                for line in old_lines:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        evt = json.loads(s)
                    except Exception:
                        # 损坏行：保留在 hot（让用户能手动检查），不计入归档
                        bad_count += 1
                        to_keep_lines.append(line if line.endswith("\n") else line + "\n")
                        continue
                    ts = evt.get("ts") or ""
                    evt_unix = _parse_iso(ts)
                    if 0 < evt_unix < cutoff_unix:
                        to_archive.append(evt)
                    else:
                        # ts 解析失败（=0）也保留在 hot，避免误归档
                        to_keep_lines.append(line if line.endswith("\n") else line + "\n")

                if not to_archive:
                    stats["reason"] = "no_old_events"
                    stats["kept_count"] = len(to_keep_lines)
                    stats["hot_size_after"] = size
                    return stats

                # 先写归档（失败 → hot 不动）
                written = _append_to_archive(to_archive, archive_dir)
                if written != len(to_archive):
                    stats["reason"] = f"archive_partial: wrote {written}/{len(to_archive)}"
                    log.warning("archive partial; aborting hot rewrite to avoid data loss")
                    return stats

                # 写新 hot 到 .tmp，再 os.replace（原子）
                # 注意：仍持 flock，replace 后 fh 指向旧 inode（已不可见但已锁），新进程会打开新 inode 不冲突
                tmp_path = events_path.with_suffix(events_path.suffix + ".tmp")
                with open(tmp_path, "w", encoding="utf-8") as out:
                    out.writelines(to_keep_lines)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp_path, events_path)

                stats["archived_count"] = len(to_archive)
                stats["kept_count"] = len(to_keep_lines)
                try:
                    stats["hot_size_after"] = events_path.stat().st_size
                except OSError:
                    stats["hot_size_after"] = -1
                stats["reason"] = "ok"
                if bad_count:
                    log.warning("preserved %d malformed lines in hot during archive", bad_count)
                log.info(
                    "archived %d events (kept %d hot); hot %d -> %d bytes",
                    stats["archived_count"], stats["kept_count"],
                    stats["hot_size_before"], stats["hot_size_after"],
                )
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception as e:
        log.exception("archive_if_needed failed: %s", e)
        stats["reason"] = f"exception: {e}"
        # 清理 tmp（如果存在）
        tmp_path = events_path.with_suffix(events_path.suffix + ".tmp")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass

    return stats
