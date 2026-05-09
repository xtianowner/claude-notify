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
import os
import re
import time
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
    "完成", "已完成", "已修复", "已修好", "已修正", "已实现", "已上线", "已通过",
    "修完", "改完", "搞定", "搞完",
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


# 去掉 LLM 答复尾部常见的"执行环境/本轮文件" footer（Tian 全局规则要求标注，但
# 飞书一行展示空间紧张，纯环境声明不属于"完成了什么"）。
_FOOTER_PREFIXES = (
    "**执行环境**", "执行环境",
    "**本轮", "本轮创建", "本轮修改",
    "**修改文件**", "修改文件",
    "**文件路径**", "文件路径",
)


def _is_footer_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    for prefix in _FOOTER_PREFIXES:
        if s.startswith(prefix):
            return True
    return False


def _is_structural_line(line: str) -> bool:
    """纯结构（heading / 列表前缀 / 表格 / 分隔线 / 仅 emoji）—— 没有结论实质。"""
    s = line.strip()
    if not s:
        return True
    # 纯分隔线
    if re.fullmatch(r"[-=_*─—\s]{3,}", s):
        return True
    # 仅 heading 没内容（空 ##）
    if re.fullmatch(r"#{1,6}\s*", s):
        return True
    # 表格行
    if _RE_TABLE_ROW.match(s):
        return True
    # 仅引号开头无内容
    if re.fullmatch(r">\s*", s):
        return True
    return False


def _line_lines(raw: str) -> list[str]:
    """切原始 last_assistant_message → 实质性行列表（保留原文，不做 markdown 净化）。

    - 跳过空行
    - 跳过代码块内部（` ``` ` 之间）
    - 跳过 footer / 纯结构行
    """
    if not raw:
        return []
    lines: list[str] = []
    in_code = False
    for ln in str(raw).split("\n"):
        stripped = ln.rstrip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not stripped.strip():
            continue
        if _is_footer_line(stripped):
            continue
        if _is_structural_line(stripped):
            continue
        lines.append(stripped.strip())
    return lines


def extract_conclusion(raw: str, max_len: int = 90) -> str:
    """从 last_assistant_message 抽出"完成了什么"的结论句。

    优先级（命中即用）：
    1. 前 6 条实质行内 → 含锚词（"完成 / 修复 / 跑通 / 通过 / fixed / done / passed"）的那一条
    2. 前 3 条实质行内 → 第一条非寒暄的"段落首行"（heading 也保留：去掉 `#` 前缀后参选）
    3. fallback：clean_summary(raw)

    与 clean_summary 的区别：clean_summary 先把整段 markdown 拼成一行再切句，
    适合"流水段落"；extract_conclusion 按"行"看，更贴合 Claude 实测的"首行就是结论"
    风格（"全部 N 项任务完成"、"**端到端验证通过**："、"改完。"）。
    """
    if not raw:
        return ""
    lines = _line_lines(raw)
    if not lines:
        return clean_summary(raw, max_len=max_len)

    def _polish(line: str) -> str:
        # 去 heading / 粗体 / 斜体 / 链接 / inline code 标记，保留实质内容
        s = line
        s = _RE_HEADING.sub("", s)
        s = _RE_BOLD.sub(r"\1", s)
        s = _RE_ITALIC.sub(r"\1", s)
        s = _RE_INLINE_CODE.sub(r"\1", s)
        s = _RE_LINK.sub(r"\1", s)
        s = _RE_ULIST.sub("", s)
        s = _RE_OLIST.sub("", s)
        s = _RE_BLOCKQUOTE.sub("", s)
        s = _RE_WS.sub(" ", s).strip()
        s = _strip_filler_prefix(s)
        # 去掉句末冒号 / 多余分隔
        s = s.rstrip(" ：:、—-")
        return s

    # (1) 锚词命中（前 6 行任一含锚词 → 即用）
    for ln in lines[:6]:
        was_heading = bool(_RE_HEADING.match(ln))
        polished = _polish(ln)
        if not polished or _is_filler(polished):
            continue
        if _has_anchor(polished):
            if len(polished) > max_len:
                polished = polished[: max_len - 1].rstrip() + "…"
            return polished
        # 标记一下以便后续 fallback 时认得（不在此处返回）
        _ = was_heading

    # (2) 第一条非寒暄实质行；但若是"短小标题"（≤ 8 字 + 原文是 # 开头）→ 视为结构占位，跳过
    for idx, ln in enumerate(lines[:5]):
        was_heading = bool(_RE_HEADING.match(ln))
        polished = _polish(ln)
        if not polished or _is_filler(polished):
            continue
        # 短标题（"改动总结"、"修改"）= 占位标记，无实质 → 优先看下一行
        if was_heading and len(polished) <= 8 and idx < len(lines) - 1:
            continue
        if len(polished) > max_len:
            polished = polished[: max_len - 1].rstrip() + "…"
        return polished

    # (3) 仍空 → 取 lines[0] polished 兜底
    if lines:
        polished = _polish(lines[0])
        if polished:
            if len(polished) > max_len:
                polished = polished[: max_len - 1].rstrip() + "…"
            return polished

    # (4) 最后 fallback
    return clean_summary(raw, max_len=max_len)


def topic_overlaps_conclusion(topic: str, conclusion: str, threshold: float = 0.5) -> bool:
    """判断 task_topic 与 conclusion 前缀是否高度重复（用于飞书去重）。

    语义：把短串作为基准，看它在长串前缀中的字符重合比例 ≥ threshold 即视为重复。
    主要场景："hello" 任务的 conclusion 是 "你好！..." → 不重叠（保留两行）；
            "修复 X" 任务的 conclusion 是 "修复 X 完成" → 重叠（只显示 conclusion）。
    """
    if not topic or not conclusion:
        return False
    t = topic.strip().lower()
    c = conclusion.strip().lower()
    if not t or not c:
        return False
    # 完全包含
    short, long = (t, c) if len(t) <= len(c) else (c, t)
    if short and short in long[: len(short) + 8]:
        return True
    # 前缀重合字符数 / 短串长度
    common = 0
    for a, b in zip(short, long):
        if a == b:
            common += 1
        else:
            break
    return common / max(1, len(short)) >= threshold


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

# 教训 L08：Claude Code Notification hook payload 没有原生 sidechain / parent_session 字段。
# 子 agent transcript 写到独立目录 <parent_transcript_dir>/<sid>/subagents/agent-*.jsonl，
# 父 transcript 是 <parent_transcript_dir>/<sid>.jsonl。Notification 触发瞬间，活跃子 agent
# 的 jsonl mtime 会非常新（毫秒级）—— 用这个做启发式。
SIDECHAIN_ACTIVE_WINDOW_SEC = 5.0


def detect_sidechain_active(transcript_path: str, now: float | None = None) -> bool:
    """启发式：判断 hook 触发瞬间是否有活跃子 agent。

    输入 hook 给的 transcript_path（永远指向父 transcript：<sid>.jsonl）。
    检查同级 <sid>/subagents/ 目录是否有 agent-*.jsonl 在最近 SIDECHAIN_ACTIVE_WINDOW_SEC
    秒内被写过。任何失败静默返回 False（保守 = 不过滤 = 不冤推空）。

    边界：
    - 父 agent 在子 agent 刚结束 5 秒内触发的真实 Notification 会被误吞（罕见，且用户可关）
    - hook payload 没给 transcript_path → 返回 False
    - subagents 目录不存在或为空 → False（无活跃子 agent）
    """
    if not transcript_path:
        return False
    try:
        p = Path(transcript_path)
        # 父 transcript 是 <dir>/<sid>.jsonl，sub 目录是 <dir>/<sid>/subagents/
        sid = p.stem  # 去掉 .jsonl
        sub_dir = p.parent / sid / "subagents"
        if not sub_dir.is_dir():
            return False
        cutoff = (now if now is not None else time.time()) - SIDECHAIN_ACTIVE_WINDOW_SEC
        with os.scandir(sub_dir) as it:
            for entry in it:
                name = entry.name
                if not (name.startswith("agent-") and name.endswith(".jsonl")):
                    continue
                try:
                    if entry.stat().st_mtime >= cutoff:
                        return True
                except Exception:
                    continue
    except Exception:
        return False
    return False


def _normalize_claude_code(raw: dict[str, Any]) -> dict[str, Any]:
    """Claude Code hook payload → 内部统一字段。"""
    cwd = raw.get("cwd") or raw.get("project_dir") or ""
    project = raw.get("project") or (Path(cwd).name if cwd else "")
    cwd_short = _cwd_tail(cwd, 2)
    event = raw.get("event") or raw.get("hook_event_name") or "Unknown"
    msg = raw.get("message") or _summarize_claude(raw)
    transcript_path = raw.get("transcript_path") or ""
    # sidechain 启发式只对 Notification 跑（Stop / SessionEnd 等不需要）
    is_sidechain = detect_sidechain_active(transcript_path) if event == "Notification" else False
    return {
        "source": CLAUDE_CODE,
        "session_id": raw.get("session_id") or "unknown",
        "event": event,
        "cwd": cwd,
        "cwd_short": cwd_short,
        "project": project,
        "message": msg,
        "transcript_path": transcript_path,
        "claude_pid": raw.get("claude_pid"),
        "hook_pid": raw.get("hook_pid"),
        "tty": raw.get("tty") or "",
        "permission_mode": (raw.get("raw") or {}).get("permission_mode") or raw.get("permission_mode"),
        "effort_level": _extract_effort(raw),
        "last_assistant_message": (raw.get("raw") or {}).get("last_assistant_message")
            or raw.get("last_assistant_message"),
        "reason": (raw.get("raw") or {}).get("reason") or raw.get("reason"),
        "is_sidechain": is_sidechain,
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
