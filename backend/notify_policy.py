"""推送策略：silence-then 节流 + 立即推送 + 合并机制。

策略由 config.notify_policy 字典控制，每个事件类型映射到：
  - "immediate"  立即推送
  - "silence:N"  N 秒静默后推送（期间累积同 session 事件，到点合并发出）
  - "off"        不推送

行为：
- 活动事件（Heartbeat / PreToolUse / PostToolUse）会取消 pending（用户和 Claude 还在
  来回交互、还在干活，没必要打扰）
- 非活动事件命中 silence:N 时：
  - 若 sid 已有 pending → append 到 events list，**不**重新调度，**不**延后 fire_at；
    倒计时到点时把所有累积事件合并成一条卡片（feishu.send_events_combined）
  - 若 sid 无 pending → 起新 task 等 N 秒
- immediate / Notification / TimeoutSuspect 立即推送 + 取消 pending
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Any

from . import config as cfg_mod, feishu, notify_filter

log = logging.getLogger("claude-notify.notify")

ACTIVITY_EVENTS = {"Heartbeat", "PreToolUse", "PostToolUse"}


class _Pending:
    __slots__ = ("events", "task", "fire_at")

    def __init__(self, evt: dict[str, Any], fire_at: float):
        self.events: list[dict[str, Any]] = [evt]
        self.task: asyncio.Task | None = None
        self.fire_at: float = fire_at


class NotifyDispatcher:
    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()
        # 跨事件 dedupe（教训 L05）：Stop 推送成功的 unix ts，按 sid 索引。
        # Notification 入 submit 时注入到 summary，让 notify_filter 能感知"刚刚推过 Stop"。
        self._last_stop_pushed_at: dict[str, float] = {}

    async def submit(self, evt: dict[str, Any]) -> dict[str, Any]:
        ev_type = evt.get("event") or ""
        sid = evt.get("session_id") or ""
        cfg = cfg_mod.load()

        # 活动事件：取消 pending（用户还在交互，别打扰）
        if ev_type in ACTIVITY_EVENTS:
            await self._cancel(sid)
            return {"ok": False, "reason": "activity_cancels_pending"}

        policy_map = cfg.get("notify_policy") or {}
        # 保守 fallback：未在 policy 里显式列出的事件类型 → off（不推），避免新事件类型默认骚扰用户。
        policy = (policy_map.get(ev_type) or "off").strip().lower()

        if policy == "off":
            return {"ok": False, "reason": "policy_off"}

        # 推送过滤（v3 spec §4）：注 immediate / silence 路径都要过滤；
        # silence 路径会在 _wait_and_send 真正发送前再过滤一次（届时 enrichments 可能新增 milestone）
        summary = self._summary_with_dedupe(sid)

        if ev_type in {"Notification", "TimeoutSuspect"}:
            await self._cancel(sid)
            ok, reason = notify_filter.should_notify(evt, summary, cfg)
            if not ok:
                return {"ok": False, "reason": f"filtered:{reason}"}
            return await feishu.send_event(evt)

        if policy == "immediate":
            await self._cancel(sid)
            ok, reason = notify_filter.should_notify(evt, summary, cfg)
            if not ok:
                return {"ok": False, "reason": f"filtered:{reason}"}
            result = await feishu.send_event(evt)
            self._record_stop_push(ev_type, sid, result)
            return result

        if policy.startswith("silence:"):
            try:
                delay = float(policy.split(":", 1)[1])
            except Exception:
                delay = 10.0
            merged = await self._schedule_or_merge(sid, evt, delay)
            return {
                "ok": True,
                "reason": (f"merged_into_pending_{merged}_events" if merged > 1
                           else f"scheduled_silence_{int(delay)}s"),
            }

        # 兜底：立即推
        await self._cancel(sid)
        return await feishu.send_event(evt)

    async def _schedule_or_merge(self, sid: str, evt: dict[str, Any], delay: float) -> int:
        """如果 sid 有 pending → append，不变 fire_at；否则起新 task。返回累积事件数。"""
        async with self._lock:
            existing = self._pending.get(sid)
            if existing is not None:
                existing.events.append(evt)
                return len(existing.events)
            fire_at = time.time() + delay
            pending = _Pending(evt, fire_at)
            pending.task = asyncio.create_task(self._wait_and_send(sid, pending))
            self._pending[sid] = pending
            return 1

    async def _wait_and_send(self, sid: str, pending: _Pending) -> None:
        try:
            now = time.time()
            wait = max(0.0, pending.fire_at - now)
            await asyncio.sleep(wait)

            # snapshot events under lock，并清掉 pending（防止后续 submit 又 append 漏发）
            async with self._lock:
                stored = self._pending.get(sid)
                if stored is not pending:
                    return  # 被 cancel 或被替换
                events = list(pending.events)
                self._pending.pop(sid, None)

            # 真正发送前再过滤一次：silence 倒计时期间 enrichments 可能新写入 milestone
            cfg = cfg_mod.load()
            summary = self._summary_with_dedupe(sid)
            try:
                if len(events) == 1:
                    ok, reason = notify_filter.should_notify(events[0], summary, cfg)
                    if not ok:
                        log.info("silence-then filtered sid=%s reason=%s", sid, reason)
                        return
                    result = await feishu.send_event(events[0])
                    self._record_stop_push(events[0].get("event") or "", sid, result)
                else:
                    # 多事件合并：只要有一条通过过滤就推（合并卡片）
                    passed = [e for e in events
                              if notify_filter.should_notify(e, summary, cfg)[0]]
                    if not passed:
                        log.info("silence-then all-filtered sid=%s n=%d", sid, len(events))
                        return
                    result = await feishu.send_events_combined(passed)
                    # 合并卡片若包含 Stop，也算 Stop 已推过
                    if any((e.get("event") or "") == "Stop" for e in passed):
                        self._record_stop_push("Stop", sid, result)
            except Exception:
                log.exception("delayed send failed sid=%s n=%d", sid, len(events))
        except asyncio.CancelledError:
            return

    async def _cancel(self, sid: str) -> None:
        async with self._lock:
            p = self._pending.pop(sid, None)
            if p and p.task:
                p.task.cancel()

    def _summary_for(self, sid: str) -> dict[str, Any] | None:
        """同步从 event_store 拿当前 session 聚合摘要，用于 should_notify 判别。"""
        if not sid:
            return None
        try:
            from . import event_store
            for s in event_store.list_sessions(active_window_minutes=60):
                if s.get("session_id") == sid:
                    return s
        except Exception:
            log.exception("summary lookup failed sid=%s", sid)
        return None

    def _summary_with_dedupe(self, sid: str) -> dict[str, Any] | None:
        """summary + 注入 last_stop_pushed_unix（dispatcher 内存表，不依赖 event_store）。"""
        s = self._summary_for(sid) or {}
        ts = self._last_stop_pushed_at.get(sid)
        if ts:
            s = dict(s)
            s["last_stop_pushed_unix"] = ts
        return s or None

    def _record_stop_push(self, ev_type: str, sid: str, result: dict[str, Any] | None) -> None:
        """Stop 推送成功后写 ts，供 Notification dedupe 用。"""
        if ev_type != "Stop" or not sid:
            return
        if not (result and result.get("ok")):
            return
        self._last_stop_pushed_at[sid] = time.time()
        # 简单防内存膨胀：> 256 条时清掉最老的一半
        if len(self._last_stop_pushed_at) > 256:
            for k in sorted(self._last_stop_pushed_at,
                            key=lambda x: self._last_stop_pushed_at[x])[:128]:
                self._last_stop_pushed_at.pop(k, None)


_singleton: NotifyDispatcher | None = None


def get_dispatcher() -> NotifyDispatcher:
    global _singleton
    if _singleton is None:
        _singleton = NotifyDispatcher()
    return _singleton
