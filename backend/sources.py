"""事件来源（source）路由 stub。

为未来兼容 codex 等其它 agent 工具预留接口：
  - 每条事件携带 `source` 字段（默认 'claude_code'）
  - 后端 normalize 时按 source 路由到对应规范化函数
  - 飞书推送时按 source 选择展示样式

本模块还提供文本净化工具 `clean_summary`，给 event_store 派生 turn_summary
与飞书推送共用 —— 让 LLM 输出的 markdown / emoji / 列表 / 代码块都能转成
人类一眼可读的"首句概要"。
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Any, Callable


# ───────────── 文本净化 ─────────────
_RE_FENCED_CODE = re.compile(r"```[\s\S]*?```")
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_RE_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*")
_RE_BLOCKQUOTE = re.compile(r"^\s*>\s+", re.MULTILINE)
_RE_ULIST = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_RE_OLIST = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_RE_LINK = re.compile(r"!?\[([^\]]+)\]\([^)]+\)")
_RE_WS = re.compile(r"\s+")
# 句子分割：CJK 句末符 + 英文 .!? 必须后跟空白或结尾
_RE_SENTENCE_SPLIT = re.compile(r"(?<=[。！？])|(?<=[\.\!\?])(?=\s|$)", re.UNICODE)

# 寒暄前缀（开头命中即跳过这一句）—— 实测 Claude 答复常以"好的/明白"起头，下一句才是结论
_FILLER_PREFIXES = (
    "好的", "好", "明白", "明白了", "收到", "嗯",
    "ok", "okay", "got it", "sure", "alright", "understood",
    "我来", "让我", "我会", "i'll", "i will", "let me",
)

# 锚词：含这些的句子优先选（结论性 / 状态性）
_ANCHOR_KEYWORDS = (
    "完成", "已完成", "已修复", "已修好", "已实现", "已上线", "已通过",
    "跑通", "通过了", "通过", "成功", "失败", "出错", "报错",
    "fixed", "done", "passed", "failed", "completed", "implemented",
    "ready", "shipped", "merged",
)


def _split_sentences(s: str) -> list[str]:
    """按句末标点切句子（保留标点）；过滤空白。"""
    parts = _RE_SENTENCE_SPLIT.split(s)
    return [p.strip() for p in parts if p.strip()]


def _is_filler(sentence: str) -> bool:
    """整句都是寒暄（无实质内容）—— 剥离寒暄前缀和标点后剩余 ≤ 3 字。"""
    if not sentence:
        return True
    rest = _strip_filler_prefix(sentence).strip(" ，,。.！!？?:：；;").strip()
    return len(rest) <= 3


def _strip_filler_prefix(s: str) -> str:
    """剥掉开头的寒暄前缀（"好的，" / "明白，" / "ok，" 等）保留实质内容。

    "好的，我已经完成 X。" → "我已经完成 X。"
    "好的。" → 剩"" → 留原文（让 _is_filler 判定为 filler 即可）
    """
    if not s:
        return ""
    raw = s.strip()
    low = raw.lower()
    # 第一次匹配到 prefix 后立即决策 —— 否则会被更短的 prefix（"好"）误剥
    for prefix in _FILLER_PREFIXES:
        plow = prefix.lower()
        if low.startswith(plow):
            tail = raw[len(prefix):].lstrip(" ，,。.！!？?:：；;").lstrip()
            if tail and len(tail) >= 4:
                return tail
            return raw  # tail 过短 → 保留原文，停止尝试更短前缀
    return raw


def _has_anchor(sentence: str) -> bool:
    s = sentence.lower()
    return any(kw in s for kw in _ANCHOR_KEYWORDS)


def clean_summary(raw: str, max_len: int = 80) -> str:
    """把 LLM 输出净化成单行首句概要：去 markdown → 切句 → 跳寒暄 / 优先锚词句 → Unicode 边界截断。

    句子选择优先级（教训 L07）：
    1. 含锚词（"完成 / 修复 / 跑通 / fixed / passed"）的第一句
    2. 第一个非寒暄句（跳过"好的/嗯/明白"）
    3. fallback 取首句
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    # (1) 代码块整段替换为占位（保意图不保实现）
    s = _RE_FENCED_CODE.sub(" <代码块> ", s)
    s = _RE_INLINE_CODE.sub(r"\1", s)
    # (2) markdown 标记符（保留内容）
    s = _RE_HEADING.sub("", s)
    s = _RE_BOLD.sub(r"\1", s)
    s = _RE_ITALIC.sub(r"\1", s)
    s = _RE_BLOCKQUOTE.sub("", s)
    s = _RE_ULIST.sub("", s)
    s = _RE_OLIST.sub("", s)
    s = _RE_TABLE_ROW.sub(" ", s)
    s = _RE_LINK.sub(r"\1", s)
    # (3) 折行 + 多空白合并
    s = _RE_WS.sub(" ", s).strip()
    if not s:
        return ""
    # (4) 切句子，按优先级选最有信息量的一句
    sentences = _split_sentences(s)
    chosen = ""
    if sentences:
        # 优先级 1：含锚词的第一句
        for sent in sentences[:5]:  # 只看前 5 句，避免远距离锚词
            if _has_anchor(sent):
                chosen = sent
                break
        # 优先级 2：第一个非寒暄句
        if not chosen:
            for sent in sentences[:3]:
                if not _is_filler(sent):
                    chosen = sent
                    break
        # 优先级 3：fallback 取第一句
        if not chosen:
            chosen = sentences[0]
    else:
        chosen = s
    # (4.5) 剥离选中句子的寒暄前缀（"好的，<实质>" → "<实质>"）
    chosen = _strip_filler_prefix(chosen)
    # (5) Unicode 边界截断
    if len(chosen) > max_len:
        chosen = chosen[: max_len - 1].rstrip() + "…"
    out = chosen.strip()
    # 兜底：净化后过短（如全是 markdown 符号），用原文截断版
    if len(out) < 4:
        s2 = re.sub(r"\s+", " ", str(raw)).strip()
        if len(s2) > max_len:
            s2 = s2[: max_len - 1] + "…"
        return s2
    return out

CLAUDE_CODE = "claude_code"
CODEX = "codex"
GENERIC = "generic"


def _normalize_claude_code(raw: dict[str, Any]) -> dict[str, Any]:
    """Claude Code hook payload → 内部统一字段。"""
    cwd = raw.get("cwd") or raw.get("project_dir") or ""
    project = raw.get("project") or (Path(cwd).name if cwd else "")
    cwd_short = _cwd_tail(cwd, 2)
    event = raw.get("event") or raw.get("hook_event_name") or "Unknown"
    msg = raw.get("message") or _summarize_claude(raw)
    return {
        "source": CLAUDE_CODE,
        "session_id": raw.get("session_id") or "unknown",
        "event": event,
        "cwd": cwd,
        "cwd_short": cwd_short,
        "project": project,
        "message": msg,
        "transcript_path": raw.get("transcript_path") or "",
        "claude_pid": raw.get("claude_pid"),
        "hook_pid": raw.get("hook_pid"),
        "tty": raw.get("tty") or "",
        "permission_mode": (raw.get("raw") or {}).get("permission_mode") or raw.get("permission_mode"),
        "effort_level": _extract_effort(raw),
        "last_assistant_message": (raw.get("raw") or {}).get("last_assistant_message")
            or raw.get("last_assistant_message"),
        "reason": (raw.get("raw") or {}).get("reason") or raw.get("reason"),
        "raw": raw.get("raw") if isinstance(raw.get("raw"), dict) else raw,
    }


def _normalize_codex(raw: dict[str, Any]) -> dict[str, Any]:
    # TODO: 接入 codex 时实现
    return _normalize_generic(raw, source=CODEX)


def _normalize_generic(raw: dict[str, Any], source: str = GENERIC) -> dict[str, Any]:
    cwd = raw.get("cwd") or ""
    return {
        "source": source,
        "session_id": raw.get("session_id") or "unknown",
        "event": raw.get("event") or "Unknown",
        "cwd": cwd,
        "cwd_short": _cwd_tail(cwd, 2),
        "project": raw.get("project") or (Path(cwd).name if cwd else ""),
        "message": raw.get("message") or "",
        "transcript_path": raw.get("transcript_path") or "",
        "claude_pid": raw.get("claude_pid"),
        "hook_pid": raw.get("hook_pid"),
        "permission_mode": raw.get("permission_mode"),
        "effort_level": raw.get("effort_level"),
        "last_assistant_message": raw.get("last_assistant_message"),
        "reason": raw.get("reason"),
        "raw": raw.get("raw") if isinstance(raw.get("raw"), dict) else raw,
    }


_REGISTRY: dict[str, Callable[[dict], dict]] = {
    CLAUDE_CODE: _normalize_claude_code,
    CODEX: _normalize_codex,
    GENERIC: _normalize_generic,
}


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    src = (raw.get("source") or CLAUDE_CODE).lower()
    fn = _REGISTRY.get(src) or _normalize_generic
    return fn(raw)


def _cwd_tail(cwd: str, segments: int = 2) -> str:
    if not cwd:
        return ""
    parts = [p for p in Path(cwd).parts if p not in ("/", "")]
    return "/".join(parts[-segments:]) if parts else ""


def _summarize_claude(raw: dict[str, Any]) -> str:
    ev = raw.get("hook_event_name") or raw.get("event") or ""
    if ev == "Notification":
        return raw.get("message") or "需要确认"
    if ev == "Stop":
        return "任务一回合结束"
    if ev == "SubagentStop":
        return "子 agent 完成"
    if ev == "SessionStart":
        return "会话开始"
    if ev == "SessionEnd":
        return f"会话结束 ({raw.get('reason') or 'normal'})"
    if ev == "PreToolUse":
        tool = raw.get("tool_name") or "tool"
        return f"PreToolUse: {tool}"
    return ev or "事件"


def _extract_effort(raw: dict[str, Any]) -> str | None:
    eff = (raw.get("raw") or {}).get("effort") or raw.get("effort")
    if isinstance(eff, dict):
        return eff.get("level")
    if isinstance(eff, str):
        return eff
    return None
