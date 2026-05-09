"""LocalCLIProvider smoke test —— 真调本机 claude -p。

跑两轮：
- mode=oauth：走 Claude Max 订阅（不需要 API key，慢 11-15s）
- mode=auto：自动决策（如果 ANTHROPIC_API_KEY 在 → bare 模式 0.7s）

Run:
  /opt/miniconda3/bin/python3 scripts/0509-1700-test-local-cli-provider.py
"""
from __future__ import annotations
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.llm.local_cli_provider import LocalCLIProvider, _discover_claude_bin


async def run_one(mode: str, label: str) -> None:
    print("=" * 60)
    print(f"[{label}] mode={mode}")
    binary = _discover_claude_bin()
    print(f"  binary: {binary or '<not found>'}")
    if not binary:
        print("  ❌ claude not found on PATH/nvm")
        return

    p = LocalCLIProvider.from_config({
        "binary": binary,
        "model": "claude-haiku-4-5",
        "mode": mode,
        "timeout_s": 30,
    })
    print(f"  resolved mode: {p._resolve_mode()}")

    t0 = time.time()
    r = await p.complete(
        system="你是一名简洁助手，只用一句中文回答。",
        user="给我用一句话解释什么是 silence-then 节流策略。",
        max_tokens=120,
    )
    elapsed = time.time() - t0
    print(f"  elapsed: {elapsed:.2f}s  ok={r.ok}  reason={r.reason!r}  model={r.model}")
    print(f"  text: {r.text[:200]!r}")


async def main():
    has_key = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    print(f"ANTHROPIC_API_KEY present: {has_key}")
    print()

    # 优先测 OAuth（用户主诉求 = 免 API key 走 Claude Max）
    await run_one("oauth", "T1: 强制 OAuth 模式（走 Claude Max 订阅）")
    print()
    # auto：有 key 走 bare，没 key 走 oauth
    await run_one("auto", f"T2: auto 模式（→ {'bare' if has_key else 'oauth'}）")


if __name__ == "__main__":
    asyncio.run(main())
