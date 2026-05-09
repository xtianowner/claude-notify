"""Anthropic Provider — 默认 claude-haiku-4-5。

API key 来源优先级：
1. config.llm.anthropic.api_key（dashboard 配置，存 data/config.json）
2. ANTHROPIC_API_KEY 环境变量
3. 都没 → from_config 抛异常 → 上层 fallback 到启发式
"""
from __future__ import annotations
import logging
import os
from typing import Any

from .base import LLMResult

log = logging.getLogger("claude-notify.llm.anthropic")

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT_S = 8.0


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 base_url: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S):
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise RuntimeError("anthropic SDK not installed") from e
        self._client = AsyncAnthropic(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout_s,
        )
        self.model = model

    @classmethod
    def from_config(cls, ant_cfg: dict[str, Any]) -> "AnthropicProvider":
        api_key = (ant_cfg.get("api_key") or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("no anthropic api_key (config or ANTHROPIC_API_KEY)")
        model = (ant_cfg.get("model") or "").strip() or DEFAULT_MODEL
        base_url = (ant_cfg.get("base_url") or "").strip() or None
        timeout_s = float(ant_cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
        return cls(api_key=api_key, model=model, base_url=base_url, timeout_s=timeout_s)

    async def complete(self, system: str, user: str, max_tokens: int = 200) -> LLMResult:
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text_parts = []
            for block in (resp.content or []):
                if getattr(block, "type", "") == "text":
                    text_parts.append(getattr(block, "text", "") or "")
            text = "".join(text_parts).strip()
            usage = getattr(resp, "usage", None)
            return LLMResult(
                text=text,
                ok=True,
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                model=self.model,
            )
        except Exception as e:
            log.warning("anthropic complete failed: %s", e)
            return LLMResult(text="", ok=False, reason=str(e)[:120], model=self.model)
