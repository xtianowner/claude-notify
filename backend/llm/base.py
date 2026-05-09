"""LLM Provider 抽象接口。

所有 provider 必须实现 `complete(system, user, max_tokens) -> LLMResult` 异步方法。
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .. import config as cfg_mod

log = logging.getLogger("claude-notify.llm")


@dataclass
class LLMResult:
    text: str
    ok: bool
    reason: str = ""           # 失败原因（rate_limit / no_api_key / network / etc.）
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def complete(self, system: str, user: str, max_tokens: int = 200) -> LLMResult:
        ...


def get_provider() -> LLMProvider | None:
    """根据 config 返回 provider；不可用返回 None。"""
    cfg = cfg_mod.load()
    llm = cfg.get("llm") or {}
    if not llm.get("enabled"):
        return None
    name = (llm.get("provider") or "anthropic").lower()
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        try:
            return AnthropicProvider.from_config(llm.get("anthropic") or {})
        except Exception as e:
            log.warning("anthropic provider unavailable: %s", e)
            return None
    if name == "openai":
        # OpenAI Chat Completion 协议（通吃 OpenAI / DeepSeek / Qwen / Ollama / 自托管 vLLM 等）
        from .openai_provider import OpenAICompatibleProvider
        try:
            return OpenAICompatibleProvider.from_config(llm.get("openai") or {})
        except Exception as e:
            log.warning("openai-compat provider unavailable: %s", e)
            return None
    if name == "local_cli":
        from .local_cli_provider import LocalCLIProvider
        try:
            return LocalCLIProvider.from_config(llm.get("local_cli") or {})
        except Exception as e:
            log.warning("local_cli provider unavailable: %s", e)
            return None
    if name == "ollama":
        log.info("provider ollama not implemented yet (use openai-compat with base_url=http://localhost:11434)")
        return None
    log.warning("unknown provider: %s", name)
    return None
