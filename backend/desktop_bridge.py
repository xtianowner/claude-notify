"""R30+R33 Claude Desktop 桥接 watcher（macOS Accessibility 主路径）。

R33 重大重写：
  原计划"在每个会话窗口里找 Stop 按钮"在 Electron + Chromium 上行不通——
  Claude.app 默认根本不暴露 web 内容到 AX 树。即使 set AXManualAccessibility=True 强行
  唤醒 a11y 子系统，会话**内部**元素（输入框 / Stop 按钮）的 label 也未必稳定可读。

  改为更稳健的信号源：**侧栏 Recents 列表**。
  每个会话项 = 一个 AXButton，子树含 AXApplicationStatus 节点，其 AXDescription 给出
  状态字（实测见到 'Running' / 'Idle'，后续可能见到 'Done' / 'Blocked' / 'Waiting' 等）。
  这种结构的好处：
  - 一次扫描拿到所有会话 + 状态，不需要逐窗口监控
  - 多会话天然支持
  - 会话名直接可读（按钮 title 含状态前缀 + name）

状态映射：
  - RUNNING_KEYWORDS 命中 → generating
  - BLOCKED_KEYWORDS 命中 → waiting_input（Notification）
  - 其它 → idle

事件合成：
  - 首次见到一个 session → SessionStart（不触发推送）
  - generating → idle = Stop 事件
  - * → blocked = Notification
  - session 连续 WINDOW_DEAD_TIMEOUT_SEC 秒未见 = SessionEnd

注意：
  - 必须 set AXManualAccessibility=True 才能拿到 web 内容（Electron 默认拒绝）
  - 当前 python 必须在 系统设置 → 隐私与安全 → 辅助功能 里有权限
  - 上述任一不满足 → tick() 安全 noop
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional

log = logging.getLogger("claude-notify.desktop_bridge")

# R33 状态启发式（基于真实 AX dump 观察 + 保守推断）
RUNNING_KEYWORDS = ("running", "generating", "thinking", "working", "进行中", "运行中", "生成中")
BLOCKED_KEYWORDS = ("blocked", "waiting", "needs input", "needs review", "waiting for", "等输入", "等待", "需要", "需要您")
# 其它（idle / done / completed / 空）→ idle

# Recents 容器识别（来自 sidebar landmark）
RECENTS_ANCHOR_TITLE = ("recents", "最近")  # 侧栏 "Recents" 按钮的 title 关键字

# 状态翻转判定阈值
DEFAULT_POLL_INTERVAL = 1.0
WINDOW_DEAD_TIMEOUT_SEC = 8.0  # 会话从 Recents 列表消失超过 N 秒 → SessionEnd
HEARTBEAT_EVERY_TICKS = 30     # 每 30 个 tick (≈30s) 发一次 Heartbeat 保 session 不被算 dead


# ───────── AX 适配层 ─────────
class AXUnavailable(Exception):
    pass


def _load_ax():
    try:
        from ApplicationServices import (  # type: ignore
            AXIsProcessTrusted,
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            AXUIElementSetAttributeValue,
        )
        return {
            "trusted": AXIsProcessTrusted,
            "app": AXUIElementCreateApplication,
            "value": AXUIElementCopyAttributeValue,
            "set": AXUIElementSetAttributeValue,
        }
    except ImportError as e:
        raise AXUnavailable(str(e))


def _ax_attr(ax, el, name: str):
    try:
        err, val = ax["value"](el, name, None)
        if err != 0:
            return None
        return val
    except Exception:
        return None


def _ax_set(ax, el, name: str, value) -> bool:
    try:
        err = ax["set"](el, name, value)
        return err == 0
    except Exception:
        return False


def _find_claude_pid() -> Optional[int]:
    try:
        out = subprocess.check_output(
            ["ps", "-Ao", "pid,command"], text=True, timeout=2
        )
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if "/Applications/Claude.app/Contents/MacOS/Claude" not in line:
            continue
        if "Helper" in line or "--type=" in line:
            continue
        parts = line.split(None, 1)
        try:
            return int(parts[0])
        except Exception:
            continue
    return None


# ───────── Recents 解析 ─────────
@dataclass
class SessionSnapshot:
    name: str               # 会话名（按钮 title 剥掉状态前缀后）
    status_raw: str         # AX 原始状态字（"Running" / "Idle" / ...）
    state: str              # 映射后："generating" / "idle" / "waiting_input"
    pid: int
    # name + pid 复合 hash 作为稳定 session_id


def _classify_state(status_raw: str) -> str:
    s = (status_raw or "").lower().strip()
    if not s:
        return "idle"
    if any(k in s for k in RUNNING_KEYWORDS):
        return "generating"
    if any(k in s for k in BLOCKED_KEYWORDS):
        return "waiting_input"
    return "idle"


def _stable_session_id(pid: int, name: str) -> str:
    h = hashlib.md5(f"desktop:{pid}:{name}".encode("utf-8")).hexdigest()
    return f"dsk-{h[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _extract_session_items(ax, root) -> list[SessionSnapshot]:
    """递归找侧栏 Recents 列表里的 session button。

    结构（实测）：
      [AXButton] t='Running Analyze Claude Desktop integration feasibi'
        [AXGroup/AXApplicationStatus] d='Running'
        [AXStaticText] v='Analyze Claude Desktop integration feasibility'

    或：
      [AXButton] t='Idle General coding session'
        [AXImage] d='Idle'
        [AXStaticText] v='General coding session'

    启发式：role=AXButton 且首个子节点 role==AXGroup 子角色 AXApplicationStatus，
    或首个子节点 role==AXImage 且 desc 看起来是状态字。
    """
    results: list[SessionSnapshot] = []

    def looks_like_status(node) -> Optional[str]:
        sub = _ax_attr(ax, node, "AXSubrole") or ""
        role = _ax_attr(ax, node, "AXRole") or ""
        if sub == "AXApplicationStatus" or role == "AXImage":
            return _ax_attr(ax, node, "AXDescription") or ""
        return None

    def visit(el, depth):
        if depth > 18:
            return
        role = _ax_attr(ax, el, "AXRole") or ""
        if role == "AXButton":
            kids = _ax_attr(ax, el, "AXChildren") or []
            kids = list(kids)
            # 找子节点里的状态信号
            status_raw = ""
            name_text = ""
            for k in kids:
                s = looks_like_status(k)
                if s is not None and not status_raw:
                    status_raw = s
                if (_ax_attr(ax, k, "AXRole") or "") == "AXStaticText":
                    v = _ax_attr(ax, k, "AXValue") or ""
                    if v and len(v) > len(name_text):
                        name_text = v
            if status_raw:
                title = _ax_attr(ax, el, "AXTitle") or ""
                name = name_text or title.split(status_raw, 1)[-1].strip() or title
                if name:
                    results.append(SessionSnapshot(
                        name=name.strip(),
                        status_raw=status_raw.strip(),
                        state=_classify_state(status_raw),
                        pid=0,  # 上层赋值
                    ))
                    # 不再下钻这个 button 的子节点
                    return
        for k in (_ax_attr(ax, el, "AXChildren") or []):
            visit(k, depth + 1)

    visit(root, 0)
    return results


def _enumerate_sessions(ax, pid: int) -> list[SessionSnapshot]:
    app = ax["app"](pid)
    # R33 关键：先唤醒 Chromium a11y（每个 tick 都 set 无副作用，幂等）
    _ax_set(ax, app, "AXManualAccessibility", True)
    sessions: list[SessionSnapshot] = []
    for w in (_ax_attr(ax, app, "AXWindows") or []):
        snaps = _extract_session_items(ax, w)
        for s in snaps:
            s.pid = pid
            sessions.append(s)
    # 去重（同名 session 可能在 Recents 出现多次）
    seen = set()
    unique: list[SessionSnapshot] = []
    for s in sessions:
        if s.name in seen:
            continue
        seen.add(s.name)
        unique.append(s)
    return unique


# ───────── 状态机 track ─────────
@dataclass
class SessionTrack:
    session_id: str
    pid: int
    name: str
    state: str = "unknown"
    state_since: float = 0.0
    last_seen: float = 0.0
    last_event_emitted: str = ""
    history: list[str] = field(default_factory=list)


class DesktopBridge:
    def __init__(self,
                 post_event: Optional[Callable[[dict], None]] = None,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 debug: bool = False) -> None:
        self.post_event = post_event
        self.poll_interval = poll_interval
        self.debug = debug
        self.tracks: dict[str, SessionTrack] = {}
        self._ax = None
        self._ax_error: str = ""
        self._stop = False
        self._tick_count = 0
        self.last_status_values: set[str] = set()  # 诊断用：累计观察到的所有 raw 状态字

    def _ensure_ax(self) -> bool:
        if self._ax is not None:
            return True
        try:
            self._ax = _load_ax()
        except AXUnavailable as e:
            self._ax_error = f"ApplicationServices 不可用：{e}"
            return False
        if not self._ax["trusted"]():
            self._ax_error = (
                f"python 未获辅助功能权限。系统设置 → 隐私与安全 → 辅助功能 加入 "
                f"{sys.executable} 后重启 backend。"
            )
            return False
        return True

    def _emit(self, evt: dict) -> None:
        if self.debug:
            log.info("emit: %s", json.dumps(evt, ensure_ascii=False))
        if self.post_event:
            try:
                self.post_event(evt)
            except Exception as e:
                log.warning("post_event 失败: %s", e)

    def _build_event(self, track: SessionTrack, event_name: str, message: str = "",
                     status_raw: str = "") -> dict:
        return {
            "ts": _now_iso(),
            "source": "desktop_app",
            "session_id": track.session_id,
            "event": event_name,
            "project": "Claude Desktop",
            "window_title": track.name,
            "window_id": track.session_id,
            "conversation_id": track.name,
            "message": message or f"{track.name} {event_name}",
            "last_assistant_message": message or track.name,
            "claude_pid": track.pid,
            "raw": {
                "name": track.name,
                "status_raw": status_raw,
                "history_tail": track.history[-6:],
            },
        }

    def tick(self) -> None:
        if not self._ensure_ax():
            return
        pid = _find_claude_pid()
        if not pid:
            self._reap_all("claude_app_not_running")
            return
        try:
            snaps = _enumerate_sessions(self._ax, pid)
        except Exception as e:
            log.warning("enumerate_sessions 异常：%s", e)
            return

        now = time.time()
        seen_sids: set[str] = set()
        for snap in snaps:
            sid = _stable_session_id(snap.pid, snap.name)
            seen_sids.add(sid)
            self.last_status_values.add(snap.status_raw)
            track = self.tracks.get(sid)
            if track is None:
                track = SessionTrack(
                    session_id=sid, pid=snap.pid, name=snap.name,
                    state="unknown", state_since=now,
                )
                self.tracks[sid] = track
                self._emit(self._build_event(
                    track, "SessionStart",
                    f"Claude Desktop 会话出现: {snap.name}",
                    status_raw=snap.status_raw,
                ))
                track.last_event_emitted = "SessionStart"
                # 首见时根据当前 state 立即追发对应事件，让 backend status 推断准确：
                #   idle  → 发 Stop  → derive_status="idle"
                #   waiting_input → 发 Notification → derive_status="waiting"
                #   generating → 不追发（SessionStart 默认推断为 running，正好）
                if snap.state == "idle":
                    self._emit(self._build_event(
                        track, "Stop",
                        f"Claude Desktop 当前 idle: {snap.name}",
                        status_raw=snap.status_raw,
                    ))
                    track.last_event_emitted = "Stop"
                elif snap.state == "waiting_input":
                    self._emit(self._build_event(
                        track, "Notification",
                        f"Claude Desktop 等输入: {snap.name} ({snap.status_raw})",
                        status_raw=snap.status_raw,
                    ))
                    track.last_event_emitted = "Notification"
            track.last_seen = now
            track.name = snap.name

            new_state = snap.state
            if new_state != track.state:
                track.history.append(f"{int(now)} {track.state}->{new_state} ({snap.status_raw!r})")
                self._on_state_change(track, prev=track.state, curr=new_state, snap=snap)
                track.state = new_state
                track.state_since = now

        # 消失超时 → SessionEnd
        dead = []
        for sid, t in self.tracks.items():
            if sid in seen_sids:
                continue
            if (now - t.last_seen) >= WINDOW_DEAD_TIMEOUT_SEC:
                dead.append(sid)
        for sid in dead:
            t = self.tracks.pop(sid)
            self._emit(self._build_event(
                t, "SessionEnd", f"Claude Desktop 会话从列表消失: {t.name}"
            ))

        # 周期 Heartbeat：保活 + 让 liveness_watcher 不把 session 算 dead
        self._tick_count += 1
        if self._tick_count % HEARTBEAT_EVERY_TICKS == 0:
            for sid in seen_sids:
                t = self.tracks.get(sid)
                if not t:
                    continue
                self._emit(self._build_event(t, "Heartbeat", f"{t.name} alive"))

    def _on_state_change(self, track: SessionTrack, prev: str, curr: str,
                          snap: SessionSnapshot) -> None:
        if prev == "unknown":
            # 首次发现 SessionStart 已发，不再二次 emit
            return
        if prev == "generating" and curr == "idle":
            self._emit(self._build_event(
                track, "Stop",
                f"Claude Desktop 回合结束: {snap.name}",
                status_raw=snap.status_raw,
            ))
            track.last_event_emitted = "Stop"
            return
        if curr == "waiting_input":
            self._emit(self._build_event(
                track, "Notification",
                f"Claude Desktop 等输入: {snap.name} ({snap.status_raw})",
                status_raw=snap.status_raw,
            ))
            track.last_event_emitted = "Notification"
            return
        # idle → generating：用户开始新一轮，不 emit（避免噪音）

    def _reap_all(self, reason: str) -> None:
        for sid, t in list(self.tracks.items()):
            self._emit(self._build_event(t, "SessionEnd", f"Desktop bridge: {reason}"))
            del self.tracks[sid]

    async def run_forever(self) -> None:
        log.info("desktop_bridge 启动 poll=%.1fs", self.poll_interval)
        while not self._stop:
            try:
                await asyncio.to_thread(self.tick)
            except Exception as e:
                log.warning("tick 异常：%s", e)
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._stop = True


# ───────── CLI 调试入口 ─────────
def _cli_post(url: str) -> Callable[[dict], None]:
    import httpx
    client = httpx.Client(timeout=1.0)

    def send(evt: dict):
        body = dict(evt)
        body["from_hook"] = False
        try:
            client.post(url, json=body)
        except Exception as e:
            print(f"[post err] {e}", file=sys.stderr)

    return send


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--post", default=os.environ.get("CLAUDE_NOTIFY_URL",
                    "http://127.0.0.1:8787/api/event"))
    ap.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL)
    args = ap.parse_args()

    post_fn = None if args.post == "none" else _cli_post(args.post)
    bridge = DesktopBridge(post_event=post_fn, poll_interval=args.interval, debug=True)
    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        pass
    print("\n累计观察到的 status_raw 值：", sorted(bridge.last_status_values), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
