"""Session 真实生命周期巡检（取代旧 timeout_watcher）。

每 interval 秒扫一次活跃 session：
  1) PID liveness：对 claude_pid 做 os.kill(pid, 0)，失败 → 进程消失
  2) Transcript mtime：transcript_path 距今多久未更新
  3) 综合判定：
     - 进程消失 且 transcript stale ≥ dead_threshold_minutes → SessionDead
     - 仍活 但事件/transcript 静默 ≥ timeout_minutes → TimeoutSuspect
     - 已是终态（ended/dead） → 跳过

每个状态对每个 session 仅 push 一次（用 suspect_pushed_for_unix / dead_pushed_for_unix 去重）。
后续若该 session 又出现新事件（resume），可重置去重位（自然由更晚的事件 ts 触发）。
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from typing import Awaitable, Callable

from . import config as cfg_mod
from . import event_store

log = logging.getLogger("claude-notify.liveness")

OnEvent = Callable[[dict], Awaitable[None]]


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        pid_int = int(pid)
    except Exception:
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _transcript_age(path: str) -> float:
    if not path:
        return float("inf")
    try:
        st = os.stat(path)
        return max(0.0, time.time() - st.st_mtime)
    except FileNotFoundError:
        return float("inf")
    except Exception:
        return float("inf")


async def watch_loop(on_event: OnEvent, interval_seconds: int = 30):
    log.info("liveness watcher started, interval=%ss", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            cfg = cfg_mod.load()
            active_window = int(cfg.get("active_window_minutes") or 30)
            timeout_min = int(cfg.get("timeout_minutes") or 5)
            dead_threshold_min = int(cfg.get("dead_threshold_minutes") or 30)
            sessions = event_store.list_sessions(active_window_minutes=active_window)
            now = time.time()

            for s in sessions:
                if s["status"] in {"ended", "dead"}:
                    continue
                if not s.get("active"):
                    continue

                last_evt_age = now - s["last_event_unix"]
                tx_age = _transcript_age(s.get("transcript_path", ""))
                effective_age = min(last_evt_age, tx_age) if tx_age != float("inf") else last_evt_age

                pid_alive = _pid_alive(s.get("claude_pid"))

                # 1) SessionDead 判定
                # Fast path：PID 死 + transcript 静默 ≥ 60 秒 → 立即标 dead（用户关 terminal 后 1-2 分钟即识别）
                # Slow path：transcript 静默 ≥ dead_threshold_minutes（兜底，处理 PID 仍存在但失活的边缘场景）
                pid_dead_fast = (not pid_alive) and tx_age >= 60
                tx_only_slow = tx_age >= dead_threshold_min * 60
                if pid_dead_fast or tx_only_slow:
                    if s.get("dead_pushed_for_unix", 0) >= s["last_event_unix"]:
                        continue
                    evt = {
                        "ts": event_store.now_iso(),
                        "source": s.get("source") or "claude_code",
                        "session_id": s["session_id"],
                        "event": "SessionDead",
                        "cwd": s.get("cwd", ""),
                        "cwd_short": s.get("cwd_short", ""),
                        "project": s.get("project", ""),
                        "transcript_path": s.get("transcript_path", ""),
                        "claude_pid": s.get("claude_pid"),
                        "message": _dead_msg(pid_alive, tx_age, dead_threshold_min),
                        "reason": _dead_reason(pid_alive, tx_age, dead_threshold_min),
                        "raw": {
                            "detector": "liveness_watcher",
                            "pid_alive": pid_alive,
                            "transcript_age_sec": int(tx_age) if tx_age != float("inf") else -1,
                            "last_event_age_sec": int(last_evt_age),
                        },
                    }
                    event_store.append_event(evt)
                    try:
                        await on_event(evt)
                    except Exception:
                        log.exception("on_event(SessionDead) failed")
                    continue

                # 2) TimeoutSuspect 判定（弱信号；可能在跑长任务）
                if effective_age >= timeout_min * 60:
                    if s.get("suspect_pushed_for_unix", 0) >= s["last_event_unix"]:
                        continue
                    evt = {
                        "ts": event_store.now_iso(),
                        "source": s.get("source") or "claude_code",
                        "session_id": s["session_id"],
                        "event": "TimeoutSuspect",
                        "cwd": s.get("cwd", ""),
                        "cwd_short": s.get("cwd_short", ""),
                        "project": s.get("project", ""),
                        "transcript_path": s.get("transcript_path", ""),
                        "claude_pid": s.get("claude_pid"),
                        "message": f"已 {int(effective_age // 60)} 分钟无新事件 / transcript 无更新（阈值 {timeout_min} 分钟）",
                        "raw": {
                            "detector": "liveness_watcher",
                            "pid_alive": pid_alive,
                            "transcript_age_sec": int(tx_age) if tx_age != float("inf") else -1,
                            "last_event_age_sec": int(last_evt_age),
                        },
                    }
                    event_store.append_event(evt)
                    try:
                        await on_event(evt)
                    except Exception:
                        log.exception("on_event(TimeoutSuspect) failed")
        except asyncio.CancelledError:
            log.info("liveness watcher cancelled")
            return
        except Exception:
            log.exception("liveness iteration failed")


def _dead_reason(pid_alive: bool, tx_age: float, dead_threshold_min: int) -> str:
    parts = []
    if not pid_alive:
        parts.append("pid_gone")
    if tx_age >= dead_threshold_min * 60:
        parts.append("transcript_stale")
    return "+".join(parts) or "unknown"


def _dead_msg(pid_alive: bool, tx_age: float, dead_threshold_min: int) -> str:
    if not pid_alive and tx_age != float("inf") and tx_age >= dead_threshold_min * 60:
        return f"会话已结束（pid 不在 + transcript {int(tx_age // 60)} 分钟未更新）"
    if not pid_alive:
        return "会话已结束（pid 不在）"
    return f"会话疑似结束（transcript {int(tx_age // 60)} 分钟未更新）"
