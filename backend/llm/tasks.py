"""LLM 业务任务：大标题（session topic）+ 事件实质摘要（event summary）。

职责：
- 准备 prompt（system + user）
- 选 transcript 上下文窗口
- 调 provider.complete
- 返回纯文本（不含 markdown 装饰）

不负责：缓存（在 enrichments.py）、调度（在 app.py 的 dispatch）。
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any

from .base import LLMProvider, LLMResult

log = logging.getLogger("claude-notify.llm.tasks")

# ───────────── Session topic（大标题） ─────────────
_TOPIC_SYSTEM = (
    "你是一名简明的会话标题生成器。给定一个开发会话的最近多轮对话，输出一句话回答："
    "「这个 session 当前在解决什么问题」。"
    "\n要求："
    "\n- 输出纯文本（不带 markdown / 引号 / emoji）"
    "\n- ≤ 24 个汉字（或 ≤ 36 个 ASCII 字符）"
    "\n- 用名词短语，例如「项目X 重构数据库架构」，不要用完整句子"
    "\n- 不要说「正在做」「正在处理」之类的进行时副词"
    "\n- 优先保留领域专有名词 / 项目名 / 模块名（如 sjai / d2 / vendor）"
    "\n- 不要复述用户原话碎片"
)


async def summarize_session_topic(
    provider: LLMProvider,
    recent_user_prompts: list[str],
    last_assistant: str = "",
) -> str:
    if not recent_user_prompts:
        return ""
    bullets = "\n".join(f"- {p}" for p in recent_user_prompts[:5])
    user_msg = f"最近 user prompts（倒序，最新在前）：\n{bullets}"
    if last_assistant:
        user_msg += f"\n\n最近 assistant 回应（参考）：\n{last_assistant[:500]}"
    user_msg += "\n\n请输出标题（≤ 24 字汉字）："
    res: LLMResult = await provider.complete(_TOPIC_SYSTEM, user_msg, max_tokens=80)
    if not res.ok or not res.text:
        return ""
    return _strip_one_line(res.text, max_chars=36)


# ───────────── Event summary（事件实质摘要） ─────────────
_EVENT_SYSTEM = (
    "你是一名事件摘要器。给定一个 Claude Code hook 事件 + 该 session 最近上下文，"
    "输出一句话告诉用户「这次事件实质上发生了什么」。"
    "\n要求："
    "\n- 输出纯文本（不带 markdown / 引号 / emoji）"
    "\n- ≤ 30 个汉字（或 ≤ 50 个 ASCII 字符）"
    "\n- 形式：角色: 具体动作。例如：「子 agent 完成: 修复了前端边框冲突」"
    "\n  「等用户授权: rm -rf node_modules」、「主任务结束: v2 dashboard 上线」"
    "\n- 不要说「Claude 完成了一回合」「子 agent 已完成」这种空泛话"
    "\n- 看不出实质做了什么 → 输出空字符串"
)


async def summarize_event(
    provider: LLMProvider,
    event_type: str,
    event_message: str,
    raw: dict[str, Any] | None,
    transcript_tail: str = "",
) -> str:
    """根据事件类型 + raw + transcript 末尾上下文，输出实质摘要。"""
    if event_type in ("Heartbeat", "PreToolUse", "SessionStart"):
        return ""  # 这些事件无实质内容，不摘要
    user_msg = (
        f"事件类型: {event_type}\n"
        f"事件消息: {event_message or '(none)'}\n"
    )
    raw_brief = _format_raw_for_prompt(raw or {})
    if raw_brief:
        user_msg += f"hook 原始数据: {raw_brief}\n"
    if transcript_tail:
        user_msg += f"\nsession 最近上下文（末尾 ~800 字）：\n{transcript_tail[:800]}\n"
    user_msg += "\n请输出实质摘要（≤ 30 字汉字，形式 \"角色: 动作\"）："
    res: LLMResult = await provider.complete(_EVENT_SYSTEM, user_msg, max_tokens=80)
    if not res.ok or not res.text:
        return ""
    return _strip_one_line(res.text, max_chars=50)


# LLM 不严格遵守 system prompt 的常见自创字面（应该输出空字符串但输出了这些）
_LLM_GARBAGE = {
    "（空）", "（空字符串）", "(空)", "(空字符串)",
    "无", "无内容", "无信息", "无实质", "看不出实质", "无法判断", "不明",
    "null", "none", "n/a", "na", "n.a.", "empty", "empty string",
    "（无）", "(无)", "（无实质）", "暂无内容", "暂无", "暂无信息",
    "—", "--", "...", "…",
}


def _strip_one_line(s: str, max_chars: int = 36) -> str:
    """规整单行：去 markdown 装饰、合并空白、过滤 LLM 自创垃圾词、截断。"""
    if not s:
        return ""
    # 去常见 markdown 标记
    for ch in ("**", "__", "`", "#", ">"):
        s = s.replace(ch, "")
    # 折行 + 多空白合并
    s = " ".join(s.split())
    # 去常见包裹引号
    for q in ('"', "'", "「", "」", "“", "”", "‘", "’"):
        if s.startswith(q): s = s[1:]
        if s.endswith(q): s = s[:-1]
    s = s.strip().strip(":：·-—")
    s = s.strip()
    # 过滤 LLM 不规范返回（明明要求空字符串结果给了字面占位词）
    if s.lower() in _LLM_GARBAGE or s in _LLM_GARBAGE:
        return ""
    if len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    return s


def _format_raw_for_prompt(raw: dict[str, Any]) -> str:
    """从 hook raw payload 抽对 LLM 有用的字段。"""
    if not isinstance(raw, dict):
        return ""
    keep = {}
    for k in ("hook_event_name", "tool_name", "tool_input", "stop_reason",
             "subagent_type", "task_description", "reason"):
        if k in raw and raw[k]:
            keep[k] = raw[k]
    if not keep:
        return ""
    try:
        return json.dumps(keep, ensure_ascii=False)[:400]
    except Exception:
        return str(keep)[:400]
