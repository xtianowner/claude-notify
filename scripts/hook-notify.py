#!/usr/bin/env python3
"""claude-notify hook — 由 Claude Code 触发，stdin 收 JSON。

行为：
1. 写一行 jsonl 到 data/events.jsonl（flock 保护，兜底）
2. 异步 POST 到 http://127.0.0.1:8787/api/event（fire-and-forget）
3. 立即 exit 0；后端没起也不影响 Claude Code 主流程

参数：
  --heartbeat   把 event 强制改成 Heartbeat（用于 PreToolUse，不会推飞书）
"""
from __future__ import annotations
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EVENTS_PATH = DATA_DIR / "events.jsonl"
BACKEND_URL = os.environ.get("CLAUDE_NOTIFY_URL", "http://127.0.0.1:8787/api/event")

# R11 Bug B：菜单类 Notification（select prompt / Type something）是"真等输入"
# 不能被 idle_dup 阈值吞。检测后给 evt.raw.menu_detected=True，
# notify_filter._idle_reminder_decision 看到该标记直接 bypass dup。
# 正则不强求行首（transcript JSONL 一行一 record，菜单文本里换行是 \n 字面）
MENU_PROMPT_RE = re.compile(r"❯\s*\d+\.|Type something")


def _detect_menu_prompt(transcript_path: str) -> bool:
    """读 transcript 末 20 行整行子串匹配菜单 prompt（R11 主线版）。

    JSONL 的 escape 换行（"\\n"）原样保留即可——MENU_PROMPT_RE 不要求行首锚定，
    `❯ 1.` / `Type something` 在 escape 字串里仍能命中。失败一律 False。
    """
    if not transcript_path:
        return False
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-20:]
        joined = "".join(lines)
        return bool(MENU_PROMPT_RE.search(joined))
    except Exception:
        return False


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def main() -> int:
    # 反馈循环防护：LocalCLIProvider 启子进程时设 CLAUDE_NOTIFY_LLM_CHILD=1。
    # 子进程也是 Claude Code，会触发 ~/.claude/settings.json 注册的全局 hook。
    # 如果不过滤，每次 LLM 调用都会写一批新 events、被 backend 误识别成新 session、
    # 又触发新 LLM 调用 → 无限递归把 dashboard 灌满垃圾卡片。
    if os.environ.get("CLAUDE_NOTIFY_LLM_CHILD") == "1":
        return 0

    is_heartbeat = "--heartbeat" in sys.argv

    raw_text = ""
    try:
        if not sys.stdin.isatty():
            raw_text = sys.stdin.read()
    except Exception:
        raw_text = ""

    payload: dict = {}
    if raw_text.strip():
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            payload = {"raw_text": raw_text[:500]}

    cwd = (
        payload.get("cwd")
        or payload.get("project_dir")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    project = Path(cwd).name if cwd else ""
    cwd_short = _cwd_tail(cwd, 2)
    event_name = "Heartbeat" if is_heartbeat else (payload.get("hook_event_name") or "Unknown")

    eff = payload.get("effort")
    effort_level = (eff or {}).get("level") if isinstance(eff, dict) else (eff if isinstance(eff, str) else None)

    claude_pid = os.getppid()
    # payload 可显式带 tty（测试 / 非 claude_code source 自行上报）；否则用 ps 查 ppid
    tty = (payload.get("tty") or "").strip() or _get_tty(claude_pid)

    evt = {
        "ts": now_iso(),
        "source": payload.get("source") or "claude_code",
        "session_id": payload.get("session_id") or "unknown",
        "event": event_name,
        "cwd": cwd,
        "cwd_short": cwd_short,
        "project": project,
        "transcript_path": payload.get("transcript_path") or "",
        "message": payload.get("message") or _summarize(payload, is_heartbeat),
        "claude_pid": claude_pid,
        "hook_pid": os.getpid(),
        "tty": tty,
        "permission_mode": payload.get("permission_mode"),
        "effort_level": effort_level,
        "last_assistant_message": payload.get("last_assistant_message"),
        "reason": payload.get("reason"),
        "raw": payload,
    }

    # R11 Bug B：Notification 事件检测菜单 prompt，标 raw.menu_detected
    # 让后端 notify_filter bypass idle_dup（菜单是真等输入，不是 Stop 后副本）
    if event_name == "Notification":
        if _detect_menu_prompt(evt.get("transcript_path") or ""):
            if not isinstance(evt.get("raw"), dict):
                evt["raw"] = {}
            evt["raw"]["menu_detected"] = True

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(json.dumps(evt, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        body = dict(evt)
        body["from_hook"] = True
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        proc = subprocess.Popen(
            [
                "curl", "-sS", "-m", "1",
                "-H", "content-type: application/json",
                "--data-binary", "@-",
                BACKEND_URL,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            proc.stdin.write(data)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
    except Exception:
        pass

    return 0


def _summarize(payload: dict, is_heartbeat: bool) -> str:
    if is_heartbeat:
        tool = payload.get("tool_name") or ""
        return f"heartbeat: {tool}" if tool else "heartbeat"
    ev = payload.get("hook_event_name") or ""
    if ev == "Notification":
        return payload.get("message") or "需要确认"
    if ev == "Stop":
        return "任务一回合结束"
    if ev == "SubagentStop":
        return "子 agent 完成"
    if ev == "SessionStart":
        return "会话开始"
    if ev == "SessionEnd":
        return f"会话结束 ({payload.get('reason') or 'normal'})"
    return ev or "事件"


def _cwd_tail(cwd: str, n: int = 2) -> str:
    if not cwd:
        return ""
    parts = [p for p in Path(cwd).parts if p not in ("/", "")]
    return "/".join(parts[-n:]) if parts else ""


def _get_tty(pid: int) -> str:
    """取 Claude Code 主进程的 tty（用于 dashboard 一键 focus 终端）。
    macOS `ps -p <pid> -o tty=` 返回形如 'ttys001' / '?' / '??'。
    """
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "tty="],
            stderr=subprocess.DEVNULL, text=True, timeout=0.5,
        ).strip()
        if out and out not in {"?", "??", "-"}:
            return out if out.startswith("/dev/") else f"/dev/{out}"
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
