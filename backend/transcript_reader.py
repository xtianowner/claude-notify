"""读取 Claude Code transcript jsonl，提取 user prompt。

提供：
  - first_user_prompt: 首条非 sidechain user message（display_name 兜底）
  - recent_user_prompts: 最近 N 条 user prompt（task_topic 启发式输入）

只读不写；带 (path, mtime, size) LRU 缓存避免反复 IO。
任何失败静默返回空串/空列表，不阻塞主流程。
"""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path

MAX_PROMPT_LEN = 200
SCAN_LINE_LIMIT = 80          # 找首条 user 时，只扫前 80 行
TAIL_SCAN_LINES = 600         # 找最近 N 条 user 时，从尾部扫 600 行（够 transcript 尾部 ≥ 3 个 user）


def first_user_prompt(transcript_path: str) -> str:
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    try:
        st = p.stat()
    except Exception:
        return ""
    return _cached_first_prompt(str(p), st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=512)
def _cached_first_prompt(path: str, mtime_ns: int, size: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= SCAN_LINE_LIMIT:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "user":
                    continue
                if rec.get("isSidechain"):
                    continue
                msg = rec.get("message") or {}
                content = msg.get("content")
                text = _extract_text(content)
                if text:
                    return _truncate(text, MAX_PROMPT_LEN)
    except Exception:
        return ""
    return ""


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text") or ""
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()
    return ""


def _truncate(s: str, n: int) -> str:
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def recent_user_prompts(transcript_path: str, n: int = 3) -> list[str]:
    """取最近 N 条 user prompt（去 sidechain），按时间倒序（最新在前）。"""
    if not transcript_path or n <= 0:
        return []
    p = Path(transcript_path)
    try:
        st = p.stat()
    except Exception:
        return []
    return list(_cached_recent_prompts(str(p), st.st_mtime_ns, st.st_size, n))


@lru_cache(maxsize=512)
def _cached_recent_prompts(path: str, mtime_ns: int, size: int, n: int) -> tuple[str, ...]:
    """从尾部扫 transcript，收集最近 N 条非 sidechain user message。"""
    out: list[str] = []
    try:
        # 简单做法：从头读，保留最后 TAIL_SCAN_LINES 行（jsonl 一般不太大）
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            tail: list[str] = []
            for line in f:
                tail.append(line)
                if len(tail) > TAIL_SCAN_LINES:
                    tail.pop(0)
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "user":
                continue
            if rec.get("isSidechain"):
                continue
            content = (rec.get("message") or {}).get("content")
            text = _extract_text(content)
            if text:
                out.append(_truncate(text, MAX_PROMPT_LEN))
                if len(out) >= n:
                    break
    except Exception:
        return tuple()
    return tuple(out)
