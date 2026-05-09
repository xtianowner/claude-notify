"""OpenAI Chat Completion 兼容 Provider —— 通吃所有遵循该协议的 endpoint。

覆盖：
- OpenAI 官方  (https://api.openai.com)
- DeepSeek    (https://api.deepseek.com)
- Qwen / 通义 (https://dashscope.aliyuncs.com/compatible-mode)
- Moonshot Kimi (https://api.moonshot.cn)
- 自托管 vLLM (任意 base_url)
- Ollama 本地 (http://localhost:11434/v1)
- OpenRouter / Together / Groq / 任何 OpenAI proxy

只需 base_url + api_key + model 三个字段即可切换。

为不引入 openai SDK 依赖（避免与 anthropic SDK 重复），直接用 httpx 调 REST。
"""
from __future__ import annotations
import logging
import os
from typing import Any

import httpx

from .base import LLMResult

log = logging.getLogger("claude-notify.llm.openai")

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com"
DEFAULT_TIMEOUT_S = 8.0


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_BASE_URL, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_s = timeout_s

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "OpenAICompatibleProvider":
        api_key = (cfg.get("api_key") or "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("no openai api_key (config or OPENAI_API_KEY)")
        model = (cfg.get("model") or "").strip() or DEFAULT_MODEL
        base_url = (cfg.get("base_url") or "").strip() or DEFAULT_BASE_URL
        timeout_s = float(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
        return cls(api_key=api_key, model=model, base_url=base_url, timeout_s=timeout_s)

    async def complete(self, system: str, user: str, max_tokens: int = 200) -> LLMResult:
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(url, headers=headers, json=body)
                if r.status_code >= 400:
                    return LLMResult(
                        text="", ok=False,
                        reason=f"http_{r.status_code}: {r.text[:120]}",
                        model=self.model,
                    )
                data = r.json()
                choices = data.get("choices") or []
                if not choices:
                    return LLMResult(text="", ok=False, reason="empty_choices", model=self.model)
                text = (choices[0].get("message") or {}).get("content") or ""
                usage = data.get("usage") or {}
                return LLMResult(
                    text=text.strip(),
                    ok=True,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    model=self.model,
                )
        except Exception as e:
            log.warning("openai-compat complete failed: %s", e)
            return LLMResult(text="", ok=False, reason=str(e)[:120], model=self.model)
