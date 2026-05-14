"""Smoke test: OpenAICompatibleProvider against any OpenAI-compat endpoint.

凭据从 env 读，绝不硬编码。

Run:
  TEST_OPENAI_API_KEY=sk-... \\
  TEST_OPENAI_BASE_URL=https://your-endpoint \\
  TEST_OPENAI_MODEL=your-model \\
  /opt/miniconda3/bin/python3 scripts/0509-1530-test-openai-compat.py
"""
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.llm.openai_provider import OpenAICompatibleProvider
from backend.llm.tasks import summarize_session_topic, summarize_event


def _cfg() -> dict:
    api_key = os.environ.get("TEST_OPENAI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: 需要 env TEST_OPENAI_API_KEY")
        sys.exit(2)
    return {
        "api_key": api_key,
        "model": os.environ.get("TEST_OPENAI_MODEL", "gpt-4o-mini"),
        "base_url": os.environ.get("TEST_OPENAI_BASE_URL", "https://api.openai.com"),
        "timeout_s": float(os.environ.get("TEST_OPENAI_TIMEOUT_S", "12")),
    }


async def main():
    cfg = _cfg()
    print(f"target: {cfg['base_url']} | model={cfg['model']}")
    print("=" * 60)
    print("[1/3] raw provider.complete()")
    p = OpenAICompatibleProvider.from_config(cfg)
    r = await p.complete(
        system="你是一个简洁助手，只用一句中文回答。",
        user="claude-notify 项目用什么语言写？假设是 Python+JS。",
        max_tokens=80,
    )
    print(f"  ok={r.ok}  model={r.model}  in={r.input_tokens}  out={r.output_tokens}")
    print(f"  text: {r.text!r}")
    if not r.ok:
        print(f"  reason: {r.reason}")
        return 1

    print("=" * 60)
    print("[2/3] summarize_session_topic()")
    topic = await summarize_session_topic(
        provider=p,
        recent_user_prompts=[
            "配置栏的UI有问题，字体超过模块栏了",
            "做一下 OpenAI 兼容接口测试",
            "修一下 task_topic 的中文乱切 bug",
        ],
        last_assistant="已经完成 OpenAI 兼容 provider，可以走任意 OpenAI 协议 endpoint。",
    )
    print(f"  topic: {topic!r}")

    print("=" * 60)
    print("[3/3] summarize_event()")
    summary = await summarize_event(
        provider=p,
        event_type="Notification",
        event_message="Claude is waiting for your input.",
        raw={"hook_event_name": "Notification", "reason": "permission"},
        transcript_tail="user: 帮我把 deploy 流程跑一遍\nassistant: 我准备执行 rm -rf node_modules && npm ci，需要授权",
    )
    print(f"  summary: {summary!r}")

    print("=" * 60)
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
