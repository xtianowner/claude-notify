from __future__ import annotations
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfg_mod
from . import aliases, enrichments, event_store, feishu, liveness_watcher, notify_policy
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = cfg_mod.load()
    interval = int(cfg.get("liveness_interval_seconds") or 30)
    task = asyncio.create_task(liveness_watcher.watch_loop(_on_watcher_event, interval_seconds=interval))
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
    raw = await request.json()
    from_hook = bool(isinstance(raw, dict) and raw.pop("from_hook", False))
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
def list_sessions(include_terminal: bool = True):
    cfg = cfg_mod.load()
    win = int(cfg.get("active_window_minutes") or 30)
    sessions = event_store.list_sessions(active_window_minutes=win)
    out = []
    for s in sessions:
        if not include_terminal and s.get("terminal"):
            continue
        out.append(_strip_internal(s))
    return out


@app.get("/api/events")
def list_events(session_id: str | None = None, limit: int = 200):
    return event_store.read_events_for(session_id=session_id, limit=max(1, min(limit, 1000)))


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


@app.get("/api/config")
def get_config():
    cfg = cfg_mod.load()
    return cfg_mod.public_view(cfg)


@app.post("/api/config")
async def update_config(request: Request):
    patch = await request.json()
    if not isinstance(patch, dict):
        raise HTTPException(400, "config patch must be an object")
    if "feishu_webhook" in patch and isinstance(patch["feishu_webhook"], str):
        patch["feishu_webhook"] = patch["feishu_webhook"].strip()
    saved = cfg_mod.save(patch)
    return cfg_mod.public_view(saved)


@app.post("/api/test-notify")
async def test_notify():
    result = await feishu.send_test()
    return result


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    await hub.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "ts": event_store.now_iso()}))
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
