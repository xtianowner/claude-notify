"""R30+ Claude Desktop 桥接 watcher（macOS Accessibility 主路径）。

职责：
  1. 周期性枚举 Claude.app 主进程的所有窗口
  2. 用 AX 启发式推断窗口状态：generating / waiting_input / dialog
  3. 状态翻转合成事件（SessionStart / Notification / Stop / SessionEnd）
  4. 把事件 POST 到 backend /api/event（复用 source = desktop_app normalizer）

运行模式：
  - 作为 backend.app 的 asyncio task（推荐，与 liveness_watcher 同模式）
  - 独立调试：python -m backend.desktop_bridge --debug --post http://127.0.0.1:8787/api/event

前置：
  - macOS
  - pyobjc-framework-ApplicationServices 已装
  - 运行此模块的 python 解释器在「系统设置 → 隐私与安全 → 辅助功能」勾选

启发式 label 集（中英文）：
  - Stop 按钮：title/desc/help 含 "Stop response" / "Stop generating" / "停止" / "停止回复"
  - 输入框：role=AXTextArea/AXTextField + enabled + 上次为空（未填入）
注：Claude Desktop 高频升级，label 漂移属预期，运行时把"未识别窗口"也输出诊断行。
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

# AX 启发式
STOP_BUTTON_PATTERNS = (
    "stop response", "stop generating", "stop streaming",
    "停止", "停止生成", "停止回复", "停止响应",
)
INPUT_FIELD_ROLES = ("AXTextArea", "AXTextField")
INPUT_FIELD_HINTS = (
    "reply to claude", "message claude", "ask claude",
    "回复 claude", "向 claude 提问", "输入消息",
)
DIALOG_ROLES = ("AXSheet", "AXDialog")

# 状态翻转判定阈值
WAITING_DEBOUNCE_SEC = 1.5   # generating → idle 后多久才视为"真在等输入"
DEFAULT_POLL_INTERVAL = 1.0
WINDOW_DEAD_TIMEOUT_SEC = 3.0  # 窗口连续 N 秒拿不到 → 视为关闭


# ───────── AX 适配层（pyobjc 不可用时降级为 noop） ─────────
class AXUnavailable(Exception):
    pass


def _load_ax():
    try:
        from ApplicationServices import (  # type: ignore
            AXIsProcessTrusted,
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeNames,
            AXUIElementCopyAttributeValue,
        )
        return {
            "trusted": AXIsProcessTrusted,
            "app": AXUIElementCreateApplication,
            "names": AXUIElementCopyAttributeNames,
            "value": AXUIElementCopyAttributeValue,
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


def _find_claude_pid() -> Optional[int]:
    """ps 找 /Applications/Claude.app 主进程（非 Helper、无 --type）。"""
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


# ───────── 窗口状态 ─────────
@dataclass
class WindowSnapshot:
    pid: int
    window_id: str
    title: str
    generating: bool
    has_input: bool
    has_dialog: bool
    dialog_text: str = ""

    @property
    def state(self) -> str:
        if self.has_dialog:
            return "dialog"
        if self.generating:
            return "generating"
        return "idle"


@dataclass
class WindowTrack:
    """每个 Claude Desktop 窗口的内部状态机记录。"""
    session_id: str
    pid: int
    window_id: str
    title: str
    state: str = "unknown"           # generating / idle / dialog
    state_since: float = 0.0
    last_seen: float = 0.0
    last_event_emitted: str = ""     # 最近一次 emit 的 event 名（去重用）
    notification_emitted_in_idle: bool = False  # 当前 idle 段是否已发过 Notification
    history: list[str] = field(default_factory=list)  # 状态翻转 trace（debug）


def _stable_session_id(pid: int, window_id: str) -> str:
    h = hashlib.md5(f"desktop:{pid}:{window_id}".encode("utf-8")).hexdigest()
    return f"dsk-{h[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


# ───────── AX 树扫描（提取 generating / has_input） ─────────
def _scan_window(ax, win, max_depth: int = 6) -> dict[str, Any]:
    """递归读窗口子树，统计 Stop 按钮 / 输入框 / dialog。"""
    found = {"stop": False, "input": False, "dialog": False, "dialog_text": ""}

    def walk(el, depth):
        if depth > max_depth:
            return
        role = _ax_attr(ax, el, "AXRole") or ""
        title = (_ax_attr(ax, el, "AXTitle") or "").lower()
        desc = (_ax_attr(ax, el, "AXDescription") or "").lower()
        help_ = (_ax_attr(ax, el, "AXHelp") or "").lower()
        labels = f"{title} {desc} {help_}"

        if role in DIALOG_ROLES:
            found["dialog"] = True
            if not found["dialog_text"]:
                found["dialog_text"] = (
                    _ax_attr(ax, el, "AXTitle") or _ax_attr(ax, el, "AXDescription") or ""
                )

        if role == "AXButton":
            if any(p in labels for p in STOP_BUTTON_PATTERNS):
                # 还要看是否可见 / enabled —— 不可见的留作 generating 信号即可（Claude 在生成时 stop 通常 enabled）
                enabled = _ax_attr(ax, el, "AXEnabled")
                if enabled in (None, True):
                    found["stop"] = True

        if role in INPUT_FIELD_ROLES:
            enabled = _ax_attr(ax, el, "AXEnabled")
            placeholder = (_ax_attr(ax, el, "AXPlaceholderValue") or "").lower()
            value = _ax_attr(ax, el, "AXValue")
            looks_like_chat = any(h in placeholder for h in INPUT_FIELD_HINTS) or any(
                h in labels for h in INPUT_FIELD_HINTS
            )
            if enabled is not False and (looks_like_chat or value is not None):
                found["input"] = True

        if found["stop"] and found["input"] and found["dialog"]:
            return  # 三件都找到，提前剪枝

        for child in (_ax_attr(ax, el, "AXChildren") or []):
            walk(child, depth + 1)

    walk(win, 0)
    return found


def _enumerate_windows(ax, pid: int) -> list[WindowSnapshot]:
    app = ax["app"](pid)
    wins = _ax_attr(ax, app, "AXWindows") or []
    snaps = []
    for w in wins:
        title = _ax_attr(ax, w, "AXTitle") or ""
        # AXIdentifier 在 Claude.app 多为空；用 (pid, role+title+index) 退化
        ident = _ax_attr(ax, w, "AXIdentifier") or ""
        window_id = ident or f"{pid}:{title[:32]}"
        found = _scan_window(ax, w)
        snaps.append(WindowSnapshot(
            pid=pid,
            window_id=window_id,
            title=title,
            generating=found["stop"],
            has_input=found["input"],
            has_dialog=found["dialog"],
            dialog_text=found["dialog_text"] or "",
        ))
    return snaps


# ───────── 状态机驱动 ─────────
class DesktopBridge:
    def __init__(self,
                 post_event: Optional[Callable[[dict], None]] = None,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 debug: bool = False) -> None:
        self.post_event = post_event
        self.poll_interval = poll_interval
        self.debug = debug
        self.tracks: dict[str, WindowTrack] = {}
        self._ax = None
        self._ax_error: str = ""
        self._stop = False

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

    def _build_event(self, track: WindowTrack, event_name: str, message: str = "") -> dict:
        return {
            "ts": _now_iso(),
            "source": "desktop_app",
            "session_id": track.session_id,
            "event": event_name,
            "project": "Claude Desktop",
            "message": message or event_name,
            "claude_pid": track.pid,
            "window_id": track.window_id,
            "window_title": track.title,
            "raw": {
                "window_id": track.window_id,
                "window_title": track.title,
                "history_tail": track.history[-6:],
            },
        }

    def tick(self) -> None:
        """一轮扫描，更新状态机并 emit 事件。AX 不可用时安全 noop。"""
        if not self._ensure_ax():
            return
        pid = _find_claude_pid()
        if not pid:
            # Claude.app 没开 / 已退 → 把所有还活着的 track 标 SessionEnd
            self._reap_all(reason="claude_app_not_running")
            return
        try:
            snaps = _enumerate_windows(self._ax, pid)
        except Exception as e:
            log.warning("enumerate_windows 异常：%s", e)
            return

        now = time.time()
        seen_sids: set[str] = set()
        for snap in snaps:
            sid = _stable_session_id(snap.pid, snap.window_id)
            seen_sids.add(sid)
            track = self.tracks.get(sid)
            if track is None:
                track = WindowTrack(
                    session_id=sid, pid=snap.pid, window_id=snap.window_id,
                    title=snap.title, state="unknown", state_since=now,
                )
                self.tracks[sid] = track
                self._emit(self._build_event(track, "SessionStart",
                                             f"Claude Desktop 窗口开启: {snap.title or '(无标题)'}"))
            track.last_seen = now
            track.title = snap.title  # 标题可能变（切换 conversation）

            new_state = snap.state
            if new_state != track.state:
                track.history.append(f"{int(now)} {track.state}->{new_state}")
                # 状态翻转
                self._on_state_change(track, prev=track.state, curr=new_state,
                                       snap=snap, now=now)
                track.state = new_state
                track.state_since = now
                if new_state != "idle":
                    track.notification_emitted_in_idle = False
            else:
                # 状态稳定，看是否到了 Notification 阈值
                if (new_state == "idle"
                        and not track.notification_emitted_in_idle
                        and snap.has_input
                        and (now - track.state_since) >= WAITING_DEBOUNCE_SEC):
                    # 但首次出现就是 idle 不发 —— 避免开 dashboard 时一堆"等输入"误报
                    if track.last_event_emitted in {"SessionStart", "Stop"}:
                        self._emit(self._build_event(
                            track, "Notification",
                            f"Claude Desktop 等输入: {snap.title or '(无标题)'}"
                        ))
                        track.last_event_emitted = "Notification"
                        track.notification_emitted_in_idle = True

        # 没出现的 track → 候选 SessionEnd（带超时去抖）
        dead_sids = []
        for sid, track in self.tracks.items():
            if sid in seen_sids:
                continue
            if (now - track.last_seen) >= WINDOW_DEAD_TIMEOUT_SEC:
                dead_sids.append(sid)
        for sid in dead_sids:
            track = self.tracks.pop(sid)
            self._emit(self._build_event(track, "SessionEnd",
                                         f"Claude Desktop 窗口关闭: {track.title or '(无标题)'}"))

    def _on_state_change(self, track: WindowTrack, prev: str, curr: str,
                          snap: WindowSnapshot, now: float) -> None:
        # generating → idle = 回合结束（Stop 事件）
        if prev == "generating" and curr == "idle":
            self._emit(self._build_event(track, "Stop",
                                         f"Claude Desktop 回合结束: {snap.title or '(无标题)'}"))
            track.last_event_emitted = "Stop"
            return
        # * → dialog = 等用户确认（Notification）
        if curr == "dialog":
            msg = snap.dialog_text or "需要确认"
            self._emit(self._build_event(track, "Notification",
                                         f"Claude Desktop 等确认: {msg}"))
            track.last_event_emitted = "Notification"
            return
        # unknown → idle 首次 (启动 dashboard 时旧窗口) 不算事件，只置基线
        if prev == "unknown" and curr in ("idle", "generating"):
            track.last_event_emitted = "SessionStart"
            return

    def _reap_all(self, reason: str) -> None:
        for sid, track in list(self.tracks.items()):
            self._emit(self._build_event(track, "SessionEnd", f"Desktop bridge: {reason}"))
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
    ap.add_argument("--debug", action="store_true", help="打印每次 emit 的事件 JSON")
    ap.add_argument("--post", default=os.environ.get("CLAUDE_NOTIFY_URL",
                    "http://127.0.0.1:8787/api/event"),
                    help="backend 接收 URL，设为 'none' 仅 debug 不发")
    ap.add_argument("--interval", type=float, default=DEFAULT_POLL_INTERVAL)
    args = ap.parse_args()

    post_fn = None if args.post == "none" else _cli_post(args.post)
    bridge = DesktopBridge(post_event=post_fn, poll_interval=args.interval, debug=True)
    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
