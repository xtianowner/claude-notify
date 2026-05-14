from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfg_mod
from . import aliases, decision_log, enrichments, event_store, feishu, liveness_watcher, notes, notify_policy
from . import llm as llm_mod
from . import transcript_reader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("claude-notify")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_SRC = PROJECT_ROOT / "frontend"


class WSHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket):
        async with self._lock:
            self.clients.add(ws)

    async def remove(self, ws: WebSocket):
        async with self._lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]):
        data = json.dumps(payload, ensure_ascii=False)
        dead: list[WebSocket] = []
        async with self._lock:
            targets = list(self.clients)
        for ws in targets:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.clients.discard(ws)


hub = WSHub()


class PushBuffer:
    """L42 / R17：最近 N 秒的 push_event 环形缓冲，用于 WS 断线重连补发。

    - 每条 payload 落库时打上 `_unix` 字段；超过 retention 的旧条目滚出
    - since_ts (ISO) 给定时返回严格大于该 ts 的条目（前端 lastPushTs 之后的）
    - 客户端连接时不灌历史（since 空 → 不补发）；只有重连且明示 since_ts 才走补发
    - feishu 是兜底通道；这里只保 ~60s，足以覆盖 Chrome Memory Saver / 网络抖动
    """

    def __init__(self, retention_seconds: float = 60.0, cap: int = 200) -> None:
        self.retention = retention_seconds
        self.items: deque[dict[str, Any]] = deque(maxlen=cap)
        self._lock = asyncio.Lock()

    async def add(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            self.items.append(payload)
            self._gc_unlocked()

    async def since_iso(self, since_iso: str) -> list[dict[str, Any]]:
        if not since_iso:
            return []
        try:
            since_unix = event_store.parse_iso(since_iso)
        except Exception:
            return []
        # parse_iso 解析失败时返回 0.0；不可信 since_ts 不要回放整个 buffer
        if since_unix <= 0:
            return []
        async with self._lock:
            self._gc_unlocked()
            return [it for it in self.items if it.get("_unix", 0.0) > since_unix]

    def _gc_unlocked(self) -> None:
        cutoff = time.time() - self.retention
        while self.items and self.items[0].get("_unix", 0.0) < cutoff:
            self.items.popleft()


push_buffer = PushBuffer()


def _strip_internal(s: dict[str, Any]) -> dict[str, Any]:
    s.pop("suspect_pushed_for_unix", None)
    s.pop("dead_pushed_for_unix", None)
    return s


def _session_summary(session_id: str) -> dict[str, Any] | None:
    cfg = cfg_mod.load()
    win = int(cfg.get("active_window_minutes") or 30)
    for s in event_store.list_sessions(active_window_minutes=win):
        if s["session_id"] == session_id:
            return _strip_internal(s)
    return None


async def _on_watcher_event(evt: dict[str, Any]):
    """liveness_watcher 触发的合成事件（TimeoutSuspect / SessionDead）"""
    summary = _session_summary(evt.get("session_id", ""))
    await hub.broadcast({"type": "event", "event": evt, "session": summary})
    await notify_policy.get_dispatcher().submit(evt)


async def _on_push_for_browser(evt: dict[str, Any], reason: str):
    """notify_policy dispatcher 决定推送时调（飞书开关无关）。
    给 dashboard 广播一条 push_event，前端按 push_channels.browser 决定是否弹 toast。

    L42 / R17：同步写 push_buffer 供 WS 断线重连补发；info 日志记录当前 WS clients 数量。
    """
    sid = evt.get("session_id") or ""
    summary = _session_summary(sid) if sid else None
    title = (summary or {}).get("alias") or sid[:8] if sid else "claude-notify"
    body = (summary or {}).get("turn_summary") \
        or (summary or {}).get("last_milestone") \
        or (summary or {}).get("last_assistant_message") \
        or evt.get("message") \
        or ""
    ts = evt.get("ts") or event_store.now_iso()
    payload = {
        "type": "push_event",
        "session_id": sid,
        "event_type": evt.get("event") or "",
        "reason": reason,
        "title": title,
        "body": (body or "")[:200],
        "ts": ts,
        "status": (summary or {}).get("status") or "",
        "_unix": time.time(),  # ring buffer GC 用，broadcast 前 strip
    }
    await push_buffer.add(dict(payload))
    log.info("push_event broadcast clients=%d sid=%s ev=%s reason=%s",
             len(hub.clients), sid[:8], payload["event_type"], reason)
    payload.pop("_unix", None)
    await hub.broadcast(payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = cfg_mod.load()
    interval = int(cfg.get("liveness_interval_seconds") or 30)
    # 教训 L13：启动时尝试一次滚动归档（events.jsonl 太大就把老事件 gzip 落到 data/archive/）
    try:
        stats = event_store.archive_if_needed()
        if stats.get("triggered") and stats.get("reason") == "ok":
            log.info("startup archive: %d events archived, %d kept hot",
                     stats.get("archived_count", 0), stats.get("kept_count", 0))
        elif stats.get("reason") not in ("below_threshold", "hot_not_exist", "disabled"):
            log.warning("startup archive: %s", stats)
    except Exception:
        log.exception("startup archive failed; continuing without archival")
    # L14：启动时清理过期 session_mutes（避免长时间静音残留）
    try:
        n = cfg_mod.prune_expired_session_mutes()
        if n:
            log.info("startup pruned %d expired session_mutes", n)
    except Exception:
        log.exception("startup prune session_mutes failed")
    task = asyncio.create_task(liveness_watcher.watch_loop(_on_watcher_event, interval_seconds=interval))
    # L41 / R16：注入浏览器推送 listener，让 notify_policy 在每次决定 push 时
    # 给 dashboard WS 广播一条 push_event。前端按 push_channels.browser 决定是否弹通知。
    notify_policy.get_dispatcher().set_push_listener(_on_push_for_browser)
    log.info("claude-notify backend started; data=%s", cfg_mod.DATA_DIR)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except Exception:
            pass


app = FastAPI(title="claude-notify", lifespan=lifespan)


@app.post("/api/event")
async def post_event(request: Request):
    # R24 / L49：B1+B2 修复
    # - 非法 JSON → 400（之前抛 500）
    # - 必须是 dict 且带 session_id（之前空 body / 缺字段会写 session_id="unknown" 污染 events.jsonl）
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be valid JSON")
    if not isinstance(raw, dict):
        raise HTTPException(400, "request body must be a JSON object")
    sid = (raw.get("session_id") or "").strip() if isinstance(raw.get("session_id"), str) else raw.get("session_id")
    if not sid:
        raise HTTPException(400, "session_id is required")
    from_hook = bool(raw.pop("from_hook", False))
    evt = event_store.normalize_incoming(raw)
    if not from_hook:
        event_store.append_event(evt)
    asyncio.create_task(_dispatch(evt))
    return {"ok": True, "ts": evt["ts"], "from_hook": from_hook}


async def _dispatch(evt: dict[str, Any]):
    summary = _session_summary(evt.get("session_id", ""))
    await hub.broadcast({"type": "event", "event": evt, "session": summary})
    await notify_policy.get_dispatcher().submit(evt)
    # 异步触发 LLM 摘要（topic + event），不阻塞 dispatch
    asyncio.create_task(_llm_enrich(evt, summary))


async def _llm_enrich(evt: dict[str, Any], summary: dict[str, Any] | None):
    """对一个事件触发 LLM 摘要（topic + event），结果写 enrichments + WS 推更新。"""
    cfg = cfg_mod.load()
    llm_cfg = cfg.get("llm") or {}
    if not llm_cfg.get("enabled"):
        return
    provider = llm_mod.get_provider()
    if provider is None:
        return

    sid = evt.get("session_id") or ""
    ts = evt.get("ts") or ""
    ev_type = evt.get("event") or ""
    transcript_path = (summary or {}).get("transcript_path") or evt.get("transcript_path") or ""
    msg = evt.get("message") or ""
    raw = evt.get("raw") or {}

    updated = False

    # ── Topic: 仅在 transcript 存在 + cache miss 时算 ──
    if llm_cfg.get("topic_enabled", True) and transcript_path:
        try:
            tx = Path(transcript_path)
            if tx.exists():
                mtime_ns = tx.stat().st_mtime_ns
                tk = enrichments.topic_key(transcript_path, mtime_ns)
                if not enrichments.get(tk):
                    recent = transcript_reader.recent_user_prompts(transcript_path, n=5)
                    if recent:
                        last_assistant = (summary or {}).get("last_assistant_message") or ""
                        topic = await llm_mod.tasks.summarize_session_topic(
                            provider, recent, last_assistant)
                        if topic:
                            enrichments.set(tk, topic, kind="topic")
                            updated = True
        except Exception:
            log.exception("topic enrich failed for sid=%s", sid)

    # ── Event summary: 仅对 Stop/SubagentStop/Notification/TimeoutSuspect/SessionDead/SessionEnd 算 ──
    summarizable = ev_type in (
        "Stop", "SubagentStop", "Notification",
        "TimeoutSuspect", "SessionDead", "SessionEnd",
    )
    if llm_cfg.get("event_summary_enabled", True) and summarizable and sid and ts:
        try:
            ek = enrichments.event_key(sid, ts, ev_type)
            if not enrichments.get(ek):
                tail = ""
                if transcript_path:
                    tail = _read_transcript_tail(transcript_path, max_chars=800)
                event_msg = await llm_mod.tasks.summarize_event(
                    provider, ev_type, msg, raw, tail)
                if event_msg:
                    enrichments.set(ek, event_msg, kind="event")
                    updated = True
        except Exception:
            log.exception("event enrich failed for sid=%s ts=%s", sid, ts)

    # 触发更新通知
    if updated:
        new_summary = _session_summary(sid)
        await hub.broadcast({
            "type": "enrichment_updated",
            "session_id": sid,
            "event_ts": ts,
            "event_type": ev_type,
            "session": new_summary,
        })


def _read_transcript_tail(path: str, max_chars: int = 800) -> str:
    """读 transcript 末尾的 user/assistant 文本片段（用于 event 摘要的上下文）。"""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        # 简单：读最后 N KB，提取最近若干条 user/assistant text
        size = p.stat().st_size
        offset = max(0, size - 16 * 1024)
        with open(p, "rb") as f:
            f.seek(offset)
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()
        texts: list[str] = []
        import json as _json
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except Exception:
                continue
            t = rec.get("type")
            if t not in ("user", "assistant"):
                continue
            if rec.get("isSidechain"):
                continue
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, str):
                texts.append(f"[{t}] {content}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(f"[{t}] {block.get('text', '')}")
            if sum(len(x) for x in texts) > max_chars:
                break
        # 翻回正序
        texts.reverse()
        joined = "\n".join(texts)
        return joined[-max_chars:]
    except Exception:
        return ""


@app.get("/api/sessions")
def list_sessions(include_terminal: bool = True, include_archive: bool = False):
    cfg = cfg_mod.load()
    win = int(cfg.get("active_window_minutes") or 30)
    sessions = event_store.list_sessions(active_window_minutes=win, include_archive=include_archive)
    out = []
    for s in sessions:
        if not include_terminal and s.get("terminal"):
            continue
        out.append(_strip_internal(s))
    return out


@app.get("/api/events")
def list_events(session_id: str | None = None, limit: int = 200, include_archive: bool = False):
    return event_store.read_events_for(
        session_id=session_id,
        limit=max(1, min(limit, 1000)),
        include_archive=include_archive,
    )


@app.get("/api/sessions/{session_id}/decisions")
def list_decisions(session_id: str, limit: int = 50):
    """L12：返回该 sid 的全部推送决策记录（最多 50 条），按 ts 倒序最新在前。"""
    return decision_log.all_for(session_id, limit=limit)


@app.get("/api/sessions/{session_id}/alias")
def get_alias(session_id: str):
    return aliases.get(session_id)


@app.post("/api/sessions/{session_id}/alias")
async def set_alias(session_id: str, request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "alias body must be object")
    alias = body.get("alias")
    note = body.get("note")
    saved = aliases.set_alias(session_id, alias=alias, note=note)
    summary = _session_summary(session_id)
    if summary:
        await hub.broadcast({"type": "session_updated", "session": summary})
    return {"ok": True, "alias": saved}


def _build_focus_script(tty: str) -> str:
    """把 tty 拼进 AppleScript（osascript -e 不支持 on run argv，改 inline 注入）。

    设计要点（教训 L03）：**不能调 `tell application "System Events"`** —— 它要求 Accessibility 权限，
    Python backend 进程通常没获得 → osascript 静默挂起 → timeout。
    改成对各终端 app 直接 `try ... end try`，应用没装时 try 吃掉错误，继续下一个。
    """
    safe_tty = tty.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'set targetTty to "{safe_tty}"\n'
        'try\n'
        '  tell application "iTerm2"\n'
        '    repeat with theWindow in windows\n'
        '      repeat with theTab in tabs of theWindow\n'
        '        repeat with theSession in sessions of theTab\n'
        '          if tty of theSession is targetTty then\n'
        '            select theTab\n'
        '            select theSession\n'
        '            activate\n'
        '            return "iterm2:ok"\n'
        '          end if\n'
        '        end repeat\n'
        '      end repeat\n'
        '    end repeat\n'
        '  end tell\n'
        'end try\n'
        'try\n'
        '  tell application "Terminal"\n'
        '    repeat with w in windows\n'
        '      repeat with t in tabs of w\n'
        '        if tty of t is targetTty then\n'
        '          set selected of t to true\n'
        '          set frontmost of w to true\n'
        '          activate\n'
        '          return "terminal:ok"\n'
        '        end if\n'
        '      end repeat\n'
        '    end repeat\n'
        '  end tell\n'
        'end try\n'
        'return "not_found"\n'
    )


@app.post("/api/sessions/{session_id}/focus-terminal")
async def focus_terminal(session_id: str):
    summary = _session_summary(session_id)
    if not summary:
        raise HTTPException(404, "session not found")
    tty = (summary.get("tty") or "").strip()
    if not tty:
        return {"ok": False, "reason": "no_tty",
                "hint": "hook 还没记录到这个 session 的 tty；触发任意工具调用产生一次 hook 后即可"}
    script = _build_focus_script(tty)
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        result = (out or b"").decode().strip()
        stderr = (err or b"").decode()[:200]
        if result.endswith(":ok"):
            return {"ok": True, "tty": tty, "app": result.split(":", 1)[0]}
        if result == "not_found":
            return {"ok": False, "reason": "tty_not_in_known_terminal",
                    "tty": tty,
                    "hint": "iTerm2 / Terminal.app 中都找不到该 tty；可能那个终端已经关闭，或在 VSCode integrated terminal / Termius 等不支持 AppleScript 的环境"}
        return {"ok": False, "reason": "osascript_failed", "tty": tty, "stderr": stderr, "result": result}
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "osascript_timeout"}
    except FileNotFoundError:
        return {"ok": False, "reason": "osascript_not_available"}
    except Exception as e:
        return {"ok": False, "reason": f"exception: {e}"}


@app.post("/api/sessions/{session_id}/mute")
async def mute_session(session_id: str, request: Request):
    """L14：给单个 session 设临时/永久静音。

    body:
      minutes: number | null  // 静音多少分钟；null 或缺省 = 永久（直到手动解除）
      scope:   "all" | "stop_only"  // all 默认；stop_only 只静 Stop/SubagentStop
      label:   string?         // 备注（可选）
    """
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(400, "session_id required")
    body: dict[str, Any] = {}
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    minutes = body.get("minutes", None)
    scope = (body.get("scope") or "all").strip().lower()
    if scope not in ("all", "stop_only"):
        scope = "all"
    label = (body.get("label") or "user-set").strip() or "user-set"
    until: float | None
    if minutes is None:
        until = None
    else:
        try:
            mins = float(minutes)
        except Exception:
            raise HTTPException(400, "minutes must be number or null")
        if mins <= 0:
            # 等价于解除静音
            cfg_mod.clear_session_mute(sid)
            summary = _session_summary(sid)
            if summary:
                await hub.broadcast({"type": "session_updated", "session": summary})
            return {"ok": True, "muted": False, "session_id": sid}
        import time as _t
        until = _t.time() + mins * 60
    entry = cfg_mod.set_session_mute(sid, until=until, scope=scope, label=label)
    summary = _session_summary(sid)
    if summary:
        await hub.broadcast({"type": "session_updated", "session": summary})
    return {
        "ok": True,
        "muted": True,
        "session_id": sid,
        "mute": {
            "until": entry.get("until"),
            "scope": entry.get("scope"),
            "muted_at_iso": entry.get("muted_at_iso"),
            "label": entry.get("label"),
        },
    }


@app.delete("/api/sessions/{session_id}/mute")
async def unmute_session(session_id: str):
    """L14：解除某 session 的静音。"""
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(400, "session_id required")
    cleared = cfg_mod.clear_session_mute(sid)
    summary = _session_summary(sid)
    if summary:
        await hub.broadcast({"type": "session_updated", "session": summary})
    return {"ok": True, "session_id": sid, "muted": False, "was_muted": cleared}


@app.get("/api/config")
def get_config():
    cfg = cfg_mod.load()
    return cfg_mod.public_view(cfg)


@app.post("/api/config")
async def update_config(request: Request):
    # R24 / L49 / B2：非法 JSON → 400（之前抛 500）
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be valid JSON")
    if not isinstance(patch, dict):
        raise HTTPException(400, "config patch must be an object")
    if "feishu_webhook" in patch and isinstance(patch["feishu_webhook"], str):
        patch["feishu_webhook"] = patch["feishu_webhook"].strip()
    # R22 / L45：tab_reuse_mode 白名单
    if "tab_reuse_mode" in patch:
        if patch["tab_reuse_mode"] not in cfg_mod.TAB_REUSE_MODE_VALUES:
            raise HTTPException(400, f"tab_reuse_mode must be one of {sorted(cfg_mod.TAB_REUSE_MODE_VALUES)}")
    saved = cfg_mod.save(patch)
    return cfg_mod.public_view(saved)


@app.get("/api/notes")
def get_notes():
    """读 data/notes.md 全量。文件不存在 → 空字符串，不抛。"""
    try:
        content, size, updated_at = notes.read()
        return {"content": content, "size": size, "updated_at": updated_at}
    except Exception:
        log.exception("notes.read failed")
        return JSONResponse({"error": "internal"}, status_code=500)


@app.put("/api/notes")
async def put_notes(request: Request):
    """全量覆盖写 data/notes.md。

    错误码：
      400  body 非 dict / content 非 str
      413  超过 256 KB
      500  其它内部错
    成功后向所有 WS 客户端广播 {type: "notes_updated", source: "put", size, updated_at}。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body_must_be_object"}, status_code=400)
    content = body.get("content")
    if not isinstance(content, str):
        return JSONResponse({"error": "content_must_be_string"}, status_code=400)
    try:
        size, updated_at = notes.write(content)
    except notes.NotesOversizeError:
        return JSONResponse(
            {"error": "oversize", "max": notes.MAX_SIZE_BYTES},
            status_code=413,
        )
    except Exception:
        log.exception("notes.write failed")
        return JSONResponse({"error": "internal"}, status_code=500)
    # WS 广播（复用 hub.broadcast）
    try:
        await hub.broadcast({
            "type": "notes_updated",
            "source": "put",
            "size": size,
            "updated_at": updated_at,
        })
    except Exception:
        log.exception("notes_updated broadcast failed")
    return {"ok": True, "size": size, "updated_at": updated_at}


@app.post("/api/test-notify")
async def test_notify():
    # L41 / R16：广播一份 push_event 给 dashboard（无视 feishu 开关），
    # 让用户即便关掉飞书也能用 "测试推送" 验证浏览器通知。
    await _on_push_for_browser(
        {
            "event": "TestNotify",
            "session_id": "",
            "ts": "",
            "message": "测试推送（确认渠道工作）",
        },
        "test_notify",
    )
    result = await feishu.send_test()
    return result


# R27 / L52：飞书 ↗ 链接专用入口。Chrome 接收外部 URL 时的 tab 选择策略是它自己定的
# （它只看最右匹配 host 的 tab，不向前扫描），dashboard JS 没机会干预。改走 osascript
# 主动控制 Chrome：扫所有 window 找 dashboard tab → activate + 切 window 前台；同时
# 通过 WS 广播 open_intent 让那个 dashboard 自动 openDrawer(sid)。
# 找不到 dashboard tab 时退化为 HTML meta redirect 到 /?s=<sid>，让 Chrome 走正常加载。
def _build_chrome_focus_script() -> str:
    """扫 Chrome 所有 window/tab 找 dashboard tab → activate。

    匹配规则：URL starts with `http://127.0.0.1:8787/`，且**不是** `/o/...` 这种 endpoint。
    后者是飞书 ↗ 链接刚打开的那个 tab 自身（backend 正在处理它的请求），把它当 dashboard
    会让 osascript 误返回 OK，backend 回"已切到 dashboard"页，dashboard JS 根本没加载
    （L53 / R28 bug）。

    返回 "OK" 表示找到并激活；"NOT_FOUND" 表示没现成 dashboard tab。
    需要 Chrome Automation 权限（第一次调用时 macOS 弹"Python wants to control Chrome"）。
    不依赖 System Events（教训 L02：System Events 要 Accessibility，Python backend 没权限会挂死）。
    """
    return (
        'try\n'
        '  tell application "Google Chrome"\n'
        '    repeat with w in (every window)\n'
        '      set tabIndex to 1\n'
        '      repeat with t in (every tab of w)\n'
        '        try\n'
        '          set theURL to URL of t\n'
        '          if (theURL starts with "http://127.0.0.1:8787/") and (not (theURL starts with "http://127.0.0.1:8787/o/")) then\n'
        '            set active tab index of w to tabIndex\n'
        '            set index of w to 1\n'
        '            activate\n'
        '            return "OK"\n'
        '          end if\n'
        '        end try\n'
        '        set tabIndex to tabIndex + 1\n'
        '      end repeat\n'
        '    end repeat\n'
        '    return "NOT_FOUND"\n'
        '  end tell\n'
        'end try\n'
        'return "ERROR"\n'
    )


async def _osascript_focus_chrome_dashboard() -> tuple[bool, str]:
    """成功返回 (True, "OK")。失败返回 (False, reason)。reason 用于日志。"""
    script = _build_chrome_focus_script()
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return False, "timeout"
        result = (stdout or b"").decode("utf-8").strip()
        err_text = (stderr or b"").decode("utf-8")[:200]
        if result == "OK":
            return True, "OK"
        if result == "NOT_FOUND":
            return False, "not_found"
        return False, f"failed:{result}:{err_text}"
    except FileNotFoundError:
        return False, "no_osascript"
    except Exception as e:
        log.warning("osascript chrome focus exception: %s", e)
        return False, f"exception:{e!s}"


@app.get("/o/{sid}", response_class=HTMLResponse)
async def open_via_chrome(sid: str):
    """飞书 ↗ 链接打开 dashboard 的入口（R27 / L52）。

    流程：
      1. 调 osascript 把 Chrome 现有 dashboard tab 切前台（解 Chrome 不向前扫匹配 tab 的痛）
      2. 通过 WS 广播 open_intent，dashboard 自动 openDrawer(sid)（保证 drawer 同步）
      3. osascript 成功 → 返回轻量 close 页（Chrome 多半不让脚本 close 用户开的 tab，留着等用户 ⌘W）
         osascript 失败/无 dashboard tab → meta refresh 到 /?s=<sid> 兜底，让 Chrome 加载 dashboard
    """
    sid = (sid or "").strip()
    if not sid:
        return HTMLResponse("<p>missing sid</p>", status_code=400)

    # 1. osascript 切 Chrome dashboard tab
    activated, reason = await _osascript_focus_chrome_dashboard()
    log.info("open_via_chrome sid=%s activated=%s reason=%s", sid[:8], activated, reason)

    # 2. WS 广播 open_intent（不依赖 osascript 成功；dashboard 收到就 openDrawer）
    try:
        await hub.broadcast({"type": "open_intent", "sid": sid})
    except Exception:
        log.exception("open_intent broadcast failed")

    # 3. 返回 HTML
    if activated:
        # 成功路径：Chrome 已切到 dashboard tab，这个新 tab 是被点击产生的副产物
        # 试图 window.close（多半被 Chrome 拒绝），失败时显示提示让用户手动 ⌘W
        return HTMLResponse(
            "<!doctype html><html lang=zh><head><meta charset=utf-8>"
            "<title>已切到 dashboard</title>"
            "<style>body{font-family:-apple-system,system-ui;padding:2em;color:#333;line-height:1.5}"
            ".muted{color:#777;font-size:.9em}</style></head>"
            "<body>"
            "<h3>✓ 已切到 dashboard</h3>"
            "<p>已让现有 dashboard 窗口接管，drawer 会自动打开对应 session。</p>"
            "<p class='muted'>此 tab 可关闭：<kbd>⌘W</kbd>（Chrome 不让脚本静默关用户打开的 tab）。</p>"
            "<script>setTimeout(()=>{try{window.close()}catch(e){}}, 100);</script>"
            "</body></html>"
        )
    else:
        # Fallback：osascript 失败 / 无现成 dashboard tab → 走正常 dashboard 加载
        # 用 meta refresh 而非 HTTP 302 是为了让 URL bar 最终落到 /?s=<sid>，避免 Chrome 把
        # /o/<sid> 当历史保留
        # 反正 dashboard 加载后 main() 会把 ?s= 转写为 #s= 走 BroadcastChannel 仲裁
        from html import escape as _html_escape
        sid_safe = _html_escape(sid)
        return HTMLResponse(
            f"<!doctype html><html lang=zh><head><meta charset=utf-8>"
            f"<title>正在打开 dashboard…</title>"
            f"<meta http-equiv='refresh' content='0;url=/?s={sid_safe}'>"
            f"</head><body>"
            f"<p>正在加载 dashboard… (<a href='/?s={sid_safe}'>未跳转点这里</a>)</p>"
            f"</body></html>"
        )


# Round 9 (L27)：首次接入引导用的健康检查。0 sessions 时前端渲染检查清单卡。
@app.get("/api/health/setup")
def health_setup():
    cfg = cfg_mod.load()
    settings_path = Path.home() / ".claude" / "settings.json"
    # R24 / B3：与 scripts/install-hooks.py 的 EVENTS_NORMAL + PreToolUse 心跳保持一致
    expected = ["Notification", "Stop", "SubagentStop", "SessionStart", "SessionEnd",
                "UserPromptSubmit", "PreToolUse"]
    detected: list[str] = []
    settings_exists = settings_path.exists()
    if settings_exists:
        try:
            data = json.loads(settings_path.read_text("utf-8"))
            hooks_root = (data.get("hooks") or {}) if isinstance(data, dict) else {}
            for ev in expected:
                arr = hooks_root.get(ev) or []
                if not isinstance(arr, list):
                    continue
                for matcher in arr:
                    inner = (matcher or {}).get("hooks") or []
                    if any("hook-notify.py" in ((h or {}).get("command") or "") for h in inner):
                        detected.append(ev)
                        break
        except Exception:
            log.exception("health_setup: settings.json parse failed")
    # 至少 3 个核心 hook 装上算装好（用户可能选择性启用 / 老版本只装了前 4 个）
    # 旧用户检测到 3-4/7 仍 hooks_registered=True；前端可显示"缺 N 个，重装 hook 生效完整"
    hooks_registered = len(detected) >= 3
    webhook_configured = bool((cfg.get("feishu_webhook") or "").strip())
    try:
        sessions = event_store.list_sessions(active_window_minutes=60) or []
    except Exception:
        sessions = []
    return {
        "hooks_registered": hooks_registered,
        "hooks_detected": detected,
        "hooks_expected": expected,
        "settings_exists": settings_exists,
        "webhook_configured": webhook_configured,
        "session_count": len(sessions),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    since_ts = (ws.query_params.get("since_ts") or "").strip()
    await hub.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "ts": event_store.now_iso()}))
        # L42 / R17：断线重连补发 —— 客户端把 lastPushTs 写在 since_ts 上，
        # 我们回放 ring buffer 中 ts > since_ts 的 push_event。
        # ring buffer 只留 ~60s，过窗的丢失场景由飞书兜底。
        if since_ts:
            try:
                missed = await push_buffer.since_iso(since_ts)
                for m in missed:
                    out = {k: v for k, v in m.items() if not k.startswith("_")}
                    await ws.send_text(json.dumps(out, ensure_ascii=False))
                if missed:
                    log.info("ws resume: replayed %d push_event since=%s", len(missed), since_ts)
            except Exception:
                log.exception("ws resume failed since=%s", since_ts)
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws error")
    finally:
        await hub.remove(ws)


def _mount_frontend():
    if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
        log.info("mounted frontend dist: %s", FRONTEND_DIST)
        return
    if (FRONTEND_SRC / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_SRC), html=True), name="frontend")
        log.info("mounted frontend src: %s", FRONTEND_SRC)
        return

    @app.get("/", response_class=HTMLResponse)
    async def fallback_index():
        return HTMLResponse(
            "<h1>claude-notify backend running</h1>"
            "<p>frontend not built yet. backend OK on /api/*.</p>"
        )
    log.warning("no frontend found; serving fallback index")


_mount_frontend()


def main():
    import uvicorn
    uvicorn.run(
        "backend.app:app",
        host="127.0.0.1",
        port=8787,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
