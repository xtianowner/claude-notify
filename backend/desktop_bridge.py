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

# R36 Claude Desktop 顶部 Mode 切换：Chat / Cowork / Code 三个 button
# 实测唯一信号：当前激活 tab 的 button 有 AXARIACurrent='page'（aria-current=page）
ALLOWED_MODES = ("Code", "Cowork")   # 我们只采这两个；Chat 完全忽略
ALL_MODES = ("Chat", "Cowork", "Code")

# Recents 容器识别（来自 sidebar landmark）
RECENTS_ANCHOR_TITLE = ("recents", "最近")  # 侧栏 "Recents" 按钮的 title 关键字

# 状态翻转判定阈值
DEFAULT_POLL_INTERVAL = 1.0
# R34 注：Claude Desktop 的 sidebar Recents 列表 a11y 暴露不稳定 ——
# Chromium virtual scroll 离屏元素不暴露 / 用户折叠 sidebar / 切换不同区段时 AX tree 抖动。
# 这里用大窗口避免误 reap：会话从 Recents 列表消失超过 N 秒才 SessionEnd。
WINDOW_DEAD_TIMEOUT_SEC = 60.0
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
            AXUIElementPerformAction,
        )
        return {
            "trusted": AXIsProcessTrusted,
            "app": AXUIElementCreateApplication,
            "value": AXUIElementCopyAttributeValue,
            "set": AXUIElementSetAttributeValue,
            "press": AXUIElementPerformAction,
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
    mode: str = ""          # R36：所属 tab — "Code" / "Cowork"
    # (pid, mode, name) 复合 hash 作为稳定 session_id


def _classify_state(status_raw: str) -> str:
    s = (status_raw or "").lower().strip()
    if not s:
        return "idle"
    if any(k in s for k in RUNNING_KEYWORDS):
        return "generating"
    if any(k in s for k in BLOCKED_KEYWORDS):
        return "waiting_input"
    return "idle"


def _stable_session_id(pid: int, name: str, mode: str = "") -> str:
    # R36：mode 进入 hash，让 Code 与 Cowork 同名 session 互不串
    h = hashlib.md5(f"desktop:{pid}:{mode}:{name}".encode("utf-8")).hexdigest()
    return f"dsk-{h[:12]}"


def _detect_active_mode(ax, app_el) -> Optional[str]:
    """R36：从顶部 Mode 切换按钮组找当前激活的 tab。
    AX 信号：选中的 button 有 AXARIACurrent='page'；未选中的没这个属性。
    返回 "Chat"/"Cowork"/"Code" 之一；找不到返回 None（采集器应跳过该 tick）。
    """
    found: dict[str, bool] = {}

    def visit(el, depth):
        if depth > 18:
            return
        role = _ax_attr(ax, el, "AXRole") or ""
        if role == "AXButton":
            desc = _ax_attr(ax, el, "AXDescription") or ""
            if desc in ALL_MODES:
                cur = _ax_attr(ax, el, "AXARIACurrent")
                # AXARIACurrent 非 None 即视为激活；实测值是 'page'
                found[desc] = bool(cur)
                return  # 不下钻 Mode button
        for k in (_ax_attr(ax, el, "AXChildren") or []):
            visit(k, depth + 1)

    for w in (_ax_attr(ax, app_el, "AXWindows") or []):
        visit(w, 0)

    for label, active in found.items():
        if active:
            return label
    return None


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


def _enumerate_sessions(ax, pid: int) -> tuple[Optional[str], list[SessionSnapshot]]:
    """R36：返回 (active_mode, sessions)。
    active_mode is None：找不到激活 tab → 跳过这一轮（保持现有 tracks 不动）
    active_mode == "Chat"：明确丢弃这一轮 Recents（用户在浏览 chat，不采）
    active_mode in ALLOWED_MODES：采 Recents 并打 mode 标签
    """
    app = ax["app"](pid)
    # R33 关键：先唤醒 Chromium a11y（每个 tick 都 set 无副作用，幂等）
    _ax_set(ax, app, "AXManualAccessibility", True)
    active_mode = _detect_active_mode(ax, app)
    if active_mode not in ALLOWED_MODES:
        return active_mode, []
    sessions: list[SessionSnapshot] = []
    for w in (_ax_attr(ax, app, "AXWindows") or []):
        snaps = _extract_session_items(ax, w)
        for s in snaps:
            s.pid = pid
            s.mode = active_mode
            sessions.append(s)
    # 去重（同名 session 可能在 Recents 出现多次）
    seen = set()
    unique: list[SessionSnapshot] = []
    for s in sessions:
        if s.name in seen:
            continue
        seen.add(s.name)
        unique.append(s)
    return active_mode, unique


# ───────── 状态机 track ─────────
@dataclass
class SessionTrack:
    session_id: str
    pid: int
    name: str
    mode: str = ""           # R36：所属 tab
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
        # R38：用户在 dashboard 手动 mark-dead 一个 desktop session 后，bridge 仍能在
        # Recents 看到它（毕竟 UI 还在）。若不抑制，下一个 tick 重新 SessionStart 会把
        # status 翻回 idle/running，抵消 mark-dead 操作。
        # 解决：把被 mark-dead 的 sid 放进 _forgotten，tick 跳过；只有 AX 上看到该 session
        # 状态翻成 generating（用户在 Claude.app 里重新提问）时解除 forgotten。
        self._forgotten: set[str] = set()
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
        # R36：project 字段按 mode 分组（"Claude Desktop · Code" / "...Cowork"），
        # dashboard 的"按项目"分组视图自然把 Desktop 卡片按 mode 拆开，与 CLI 也不会混。
        project = f"Claude Desktop · {track.mode}" if track.mode else "Claude Desktop"
        return {
            "ts": _now_iso(),
            "source": "desktop_app",
            "session_id": track.session_id,
            "event": event_name,
            "project": project,
            "window_title": track.name,
            "window_id": track.session_id,
            "conversation_id": track.name,
            "message": message or f"{track.name} {event_name}",
            "last_assistant_message": message or track.name,
            "claude_pid": track.pid,
            "mode": track.mode,
            "raw": {
                "name": track.name,
                "mode": track.mode,
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
            active_mode, snaps = _enumerate_sessions(self._ax, pid)
        except Exception as e:
            log.warning("enumerate_sessions 异常：%s", e)
            return

        # R36：active_mode 不在白名单（None / Chat）→ 不更新 seen，也不 reap，保持现状
        if active_mode not in ALLOWED_MODES:
            self._tick_count += 1
            return

        now = time.time()
        seen_sids: set[str] = set()
        for snap in snaps:
            sid = _stable_session_id(snap.pid, snap.name, snap.mode)
            # R38：被用户 mark-dead 过的 sid，只有"重新开始生成"才能复活
            if sid in self._forgotten:
                if snap.state == "generating":
                    self._forgotten.discard(sid)
                else:
                    # 仍然 skip：不创建 track，不 emit
                    continue
            seen_sids.add(sid)
            self.last_status_values.add(snap.status_raw)
            track = self.tracks.get(sid)
            if track is None:
                track = SessionTrack(
                    session_id=sid, pid=snap.pid, name=snap.name, mode=snap.mode,
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
        # R36：只 reap 与当前 active_mode 相同的 tracks
        # （否则用户切到 Code 时，Cowork 的 tracks 会被错杀；反之亦然）
        dead = []
        for sid, t in self.tracks.items():
            if sid in seen_sids:
                continue
            if t.mode != active_mode:
                continue
            if (now - t.last_seen) >= WINDOW_DEAD_TIMEOUT_SEC:
                dead.append(sid)
        for sid in dead:
            t = self.tracks.pop(sid)
            self._emit(self._build_event(
                t, "SessionEnd", f"Claude Desktop 会话从列表消失: {t.name}"
            ))

        # R39：Heartbeat 已撤掉。
        # 原意是保活，但 backend derive_status 的"活动证据胜过等待"规则（L44）会因 Heartbeat
        # 把 Notification 后状态翻成 running，污染 dashboard。
        # liveness_watcher 已跳过 desktop_app source（R33 改动），不发 Heartbeat 也不会被算 dead。
        self._tick_count += 1

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

    def restore_forgotten_from_events(self, sids: list[str]) -> int:
        """R39：backend 启动时把"events.jsonl 里最近被用户 mark-dead 的 desktop_app sid"
        注入 _forgotten，让 mark-dead 跨 backend 重启稳定。"""
        n = 0
        for sid in sids:
            if sid and sid not in self._forgotten:
                self._forgotten.add(sid)
                n += 1
        return n

    def forget_session(self, session_id: str) -> bool:
        """R38：用户 mark-dead 后调用，让 bridge 抑制后续对此 sid 的事件。
        直到 AX 上看到该 session 状态变 generating（用户重新提问）才解除。"""
        existed = session_id in self.tracks
        self.tracks.pop(session_id, None)
        self._forgotten.add(session_id)
        return existed

    # ───────── R34：点击跳转（dashboard "→ Desktop" 按钮调用） ─────────
    def _find_session_button(self, app_el, target_name: str):
        """重扫 AX 树定位匹配 name 的 sidebar Recents AXButton。

        启发式：AXButton 子节点含 AXApplicationStatus（或 AXImage 描述是状态字），
        且某个 AXStaticText 子节点的 AXValue 等于 target_name。
        """
        ax = self._ax
        found: list = [None]

        def looks_like_session_button(el) -> Optional[str]:
            """返回 button 内 AXStaticText 拼出的 name；非 session 按钮返回 None。"""
            kids = list(_ax_attr(ax, el, "AXChildren") or [])
            has_status = False
            text_value = ""
            for k in kids:
                sub = _ax_attr(ax, k, "AXSubrole") or ""
                role = _ax_attr(ax, k, "AXRole") or ""
                if sub == "AXApplicationStatus":
                    has_status = True
                elif role == "AXImage":
                    desc = _ax_attr(ax, k, "AXDescription") or ""
                    # 实测 idle 用 AXImage d='Idle'；running 走 AXApplicationStatus
                    if desc and len(desc) < 20:
                        has_status = True
                elif role == "AXStaticText":
                    v = _ax_attr(ax, k, "AXValue") or ""
                    if v and len(v) > len(text_value):
                        text_value = v
            return text_value if (has_status and text_value) else None

        def visit(el, depth):
            if depth > 18 or found[0] is not None:
                return
            role = _ax_attr(ax, el, "AXRole") or ""
            if role == "AXButton":
                name = looks_like_session_button(el)
                if name and name.strip() == target_name.strip():
                    found[0] = el
                    return
                # 如果是 session button 但 name 不匹配，不再下钻（性能）
                if name:
                    return
            for k in (_ax_attr(ax, el, "AXChildren") or []):
                visit(k, depth + 1)

        for w in (_ax_attr(ax, app_el, "AXWindows") or []):
            visit(w, 0)
        return found[0]

    def _find_mode_button(self, app_el, mode: str):
        """找 Mode 切换 button（Chat/Cowork/Code）。"""
        if not mode:
            return None
        found = [None]

        def visit(el, depth):
            if depth > 18 or found[0] is not None:
                return
            role = _ax_attr(self._ax, el, "AXRole") or ""
            if role == "AXButton":
                desc = _ax_attr(self._ax, el, "AXDescription") or ""
                if desc == mode:
                    found[0] = el
                    return
            for k in (_ax_attr(self._ax, el, "AXChildren") or []):
                visit(k, depth + 1)

        for w in (_ax_attr(self._ax, app_el, "AXWindows") or []):
            visit(w, 0)
        return found[0]

    def activate_session(self, session_id: str) -> dict:
        """切到 Claude Desktop + 点击侧栏对应会话项。

        R36：若 track.mode 与当前 active_mode 不一致，先 AXPress 对应 Mode tab
        切过去，再 AXPress session button。
        """
        if not self._ensure_ax():
            return {"ok": False, "reason": "ax_unavailable", "detail": self._ax_error}
        track = self.tracks.get(session_id)
        if not track:
            return {"ok": False, "reason": "session_not_tracked",
                    "hint": "bridge 还没追踪到这个 session；等一个 poll cycle 后再试"}
        pid = _find_claude_pid()
        if not pid:
            return {"ok": False, "reason": "claude_app_not_running"}

        # 先把 Claude.app 拉到前台（不阻塞）
        try:
            subprocess.Popen(["open", "-a", "Claude"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        app_el = self._ax["app"](pid)
        _ax_set(self._ax, app_el, "AXManualAccessibility", True)
        time.sleep(0.4)

        # R36：当前 active_mode 与 track.mode 不一致 → 先切 tab
        active_mode = _detect_active_mode(self._ax, app_el)
        if track.mode and active_mode != track.mode:
            mode_btn = self._find_mode_button(app_el, track.mode)
            if mode_btn is not None:
                try:
                    self._ax["press"](mode_btn, "AXPress")
                    time.sleep(0.4)  # 等 Recents 列表重渲染
                except Exception:
                    pass

        btn = self._find_session_button(app_el, track.name)
        if btn is None:
            return {
                "ok": False, "reason": "button_not_found",
                "hint": ("侧栏 Recents 找不到该会话项。可能：1) 侧栏被折叠了 → 按 ⌘B 展开后再试；"
                         "2) 该会话已被滚出 Recents 列表（Claude Desktop 只显示最近的几条）；"
                         "3) 会话名改了，bridge 下个 tick 会重新对齐"),
                "session_name": track.name,
                "mode": track.mode,
            }
        try:
            err = self._ax["press"](btn, "AXPress")
            if err != 0:
                return {"ok": False, "reason": "ax_press_failed", "err": int(err),
                        "session_name": track.name}
        except Exception as e:
            return {"ok": False, "reason": "ax_press_exception", "detail": str(e)}

        return {"ok": True, "session_name": track.name,
                "session_id": session_id, "mode": track.mode}


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
