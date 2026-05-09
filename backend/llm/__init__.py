"""LLM 摘要子系统。

设计：
- Provider 抽象（base.LLMProvider）：保持模型/厂商可替换
- 当前实现：Anthropic Haiku 4.5（默认）
- 预留槽位：openai / ollama / 本地（cli claude -p）
- 失败优雅 fallback：LLM 不可用时调用方退化到启发式

业务任务：
- tasks.summarize_session_topic — 大标题（"此 session 在解决：xxx"）
- tasks.summarize_event — 事件实质摘要（"测试 agent 完成: 前端边框显示有误"）

调用频率：
- 大标题：transcript mtime 变 + cache hit miss 时算一次
- 事件摘要：每个 Stop / SubagentStop / Notification 算一次
- 缓存层在 enrichments.py（持久化 jsonl）
"""
from __future__ import annotations
from .base import LLMProvider, LLMResult, get_provider
from . import tasks

__all__ = ["LLMProvider", "LLMResult", "get_provider", "tasks"]
