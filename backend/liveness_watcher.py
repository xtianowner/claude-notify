"""Session 真实生命周期巡检（取代旧 timeout_watcher）。

每 interval 秒扫一次活跃 session：
  1) PID liveness：对 claude_pid 做 os.kill(pid, 0)，失败 → 进程消失
  2) Transcript mtime：transcript_path 距今多久未更新
  3) 综合判定：
     - 进程消失 且 transcript stale ≥ dead_threshold_minutes → SessionDead
     - 仍活 但静默超阈值 → TimeoutSuspect（阈值按 last_event_kind 分状态选取）
     - 已是终态（ended/dead） → 跳过

L09 — 分状态超时：旧版只看「最后事件时间戳」，没分类型。结果"长 tool 执行 8 分钟"
被当 hang 误推。新逻辑参考 last_event_kind：
  - Notification / Stop / SubagentStop  →  原 timeout（5 分钟）  这是「等用户/等下一回合」状态
  - PreToolUse                          →  pretool 阈值（15 分钟）+ transcript 活性兜底
  - PostToolUse / Heartbeat / 其它      →  idle 阈值（10 分钟）
若 config.liveness_per_state_timeout.enabled=False，退化为单一 timeout_minutes（旧逻辑）。

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


# 与 event_store.NON_STATUS_EVENTS 概念独立：那是 status 派生用，这里是 liveness 阈值选择用
_WAITING_KINDS = {"Notification", "Stop", "SubagentStop"}


def _pick_timeout_seconds(
    last_event_kind: str,
    per_state_cfg: dict,
    fallback_timeout_min: int,
) -> tuple[int, str]:
    """根据最后事件类型返回 (阈值秒数, 命中 bucket 名)。

    bucket 名给到事件 raw 字段，便于排查「为什么这次报/这次不报」。
    """
    if not per_state_cfg or not per_state_cfg.get("enabled", True):
        return fallback_timeout_min * 60, "legacy_uniform"

    kind = last_event_kind or ""
    # 优先精确匹配 <Kind>_minutes
    key = f"{kind}_minutes"
    if kind and key in per_state_cfg and isinstance(per_state_cfg[key], (int, float)):
        return int(per_state_cfg[key]) * 60, kind
    # fallback 到 default_minutes
    default_min = per_state_cfg.get("default_minutes")
    if isinstance(default_min, (int, float)):
        return int(default_min) * 60, "default"
    # 配置缺失再退到老 timeout_minutes
    return fallback_timeout_min * 60, "legacy_fallback"


async def watch_loop(on_event: OnEvent, interval_seconds: int = 30):
    log.info("liveness watcher started, interval=%ss", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            cfg = cfg_mod.load()
            timeout_min = int(cfg.get("timeout_minutes") or 5)
            dead_threshold_min = int(cfg.get("dead_threshold_minutes") or 30)
            # R11 Bug A fix：当 active_window == dead_threshold 时，静默 30min 的 session 刚
            # 好被 list_sessions 过滤掉，永远进不来 dead/suspect 判定循环（用户截图心跳
            # 31min 没动但仍是 suspect 的根因）。仅当用户没显式配置时走动态默认
            # max(60, dead*2)，保证 dead 判定窗口必然覆盖。
            # 直接读 raw config 区分"用户显式 vs DEFAULTS 合并值"
            _raw = cfg_mod._read_raw()
            _user_active = _raw.get("active_window_minutes")
            active_window = int(_user_active) if isinstance(_user_active, (int, float)) else max(60, dead_threshold_min * 2)
            per_state_cfg = cfg.get("liveness_per_state_timeout") or {}
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
                # L09 — 按 last_event_kind 选阈值：长 tool 执行 (PreToolUse) 给更宽容忍
                last_kind = s.get("last_event_kind") or s.get("last_event") or ""
                threshold_sec, bucket = _pick_timeout_seconds(
                    last_kind, per_state_cfg, timeout_min
                )

                # PreToolUse 状态：transcript 还在被写 → 视为活，跳过本轮（不报）
                # 用「pretool_transcript_alive_seconds」配置（默认 60s）
                if last_kind == "PreToolUse" and per_state_cfg.get("enabled", True):
                    alive_window = int(per_state_cfg.get("pretool_transcript_alive_seconds", 60) or 0)
                    if alive_window > 0 and tx_age != float("inf") and tx_age < alive_window:
                        # transcript 仍被 Claude 写 → 不报警
                        continue

                if effective_age >= threshold_sec:
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
                        "message": f"已 {int(effective_age // 60)} 分钟无新事件 / transcript 无更新（阈值 {threshold_sec // 60} 分钟，state={bucket}）",
                        "raw": {
                            "detector": "liveness_watcher",
                            "pid_alive": pid_alive,
                            "transcript_age_sec": int(tx_age) if tx_age != float("inf") else -1,
                            "last_event_age_sec": int(last_evt_age),
                            "last_event_kind": last_kind,
                            "threshold_bucket": bucket,
                            "threshold_sec": threshold_sec,
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
