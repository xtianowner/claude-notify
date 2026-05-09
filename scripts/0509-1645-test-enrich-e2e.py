"""端到端 enrich 链路测试：用 OpenAI 兼容 provider 注入 + 模拟 hook 事件 + 查 enrichment cache。

不改 data/config.json，用 monkeypatch 注入 LLM 配置。

Run:
  TEST_OPENAI_API_KEY=sk-... \\
  TEST_OPENAI_BASE_URL=https://tian2api.xtianowner.com \\
  TEST_OPENAI_MODEL=gpt-5.5 \\
  /opt/miniconda3/bin/python3 scripts/0509-1645-test-enrich-e2e.py
"""
from __future__ import annotations
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    api_key = os.environ.get("TEST_OPENAI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: 需要 env TEST_OPENAI_API_KEY")
        return 2
    base_url = os.environ.get("TEST_OPENAI_BASE_URL", "https://api.openai.com")
    model = os.environ.get("TEST_OPENAI_MODEL", "gpt-4o-mini")

    # ── 用临时目录隔离 enrichments.jsonl ──
    tmp = Path(tempfile.mkdtemp(prefix="enrich-e2e-"))
    print(f"tmp dir: {tmp}")

    # 1) 把全局 config 替换成开启 LLM + 用户给的 OpenAI 兼容凭据
    import backend.config as cfg_mod
    orig_load = cfg_mod.load

    def _patched_load():
        c = orig_load()
        c["llm"] = {
            "enabled": True,
            "provider": "openai",
            "openai": {
                "api_key": api_key,
                "model": model,
                "base_url": base_url,
                "timeout_s": 12,
            },
            "topic_enabled": True,
            "event_summary_enabled": True,
        }
        return c

    cfg_mod.load = _patched_load

    # 2) enrichments 路径换成临时
    import backend.enrichments as enr
    enr.ENRICH_PATH = tmp / "enrichments.jsonl"
    enr._cache.clear()

    # 3) 造一个假 transcript 文件（让 _llm_enrich 拿到 transcript_path 走 topic 分支）
    fake_transcript = tmp / "fake.jsonl"
    fake_transcript.write_text(
        '{"type":"user","message":{"content":"配置栏 UI 字体超出模块栏了"},"timestamp":"2026-05-09T15:00:00Z"}\n'
        '{"type":"user","message":{"content":"做 OpenAI 兼容接口测试"},"timestamp":"2026-05-09T15:01:00Z"}\n'
        '{"type":"user","message":{"content":"修 task_topic 中文乱切 bug"},"timestamp":"2026-05-09T15:02:00Z"}\n',
        encoding="utf-8",
    )

    # 4) 触发 _llm_enrich
    from backend.app import _llm_enrich

    fake_evt = {
        "session_id": "test-sid-e2e",
        "ts": "2026-05-09T15:02:30+08:00",
        "event": "Notification",
        "message": "Claude is requesting permission to run: rm -rf /tmp/foo && pip install -e .",
        "transcript_path": str(fake_transcript),
        "raw": {"hook_event_name": "Notification", "reason": "permission"},
    }
    fake_summary = {
        "session_id": "test-sid-e2e",
        "transcript_path": str(fake_transcript),
        "last_assistant_message": "已经完成 OpenAI 兼容 provider，可以走任意 OpenAI 协议 endpoint。",
    }

    print("=" * 60)
    print("跑 _llm_enrich() ...")
    t0 = time.time()
    asyncio.run(_llm_enrich(fake_evt, fake_summary))
    elapsed = time.time() - t0
    print(f"  elapsed: {elapsed:.2f}s")

    # 5) 查 enrichments cache
    print("=" * 60)
    cache_file = enr.ENRICH_PATH
    if not cache_file.exists():
        print(f"❌ enrichments 文件未创建: {cache_file}")
        return 1
    content = cache_file.read_text("utf-8").strip()
    print(f"enrichments.jsonl ({len(content)} bytes):")
    print(content)
    print("=" * 60)

    # 6) 解析行 + 断言
    import json
    lines = [json.loads(l) for l in content.split("\n") if l.strip()]
    kinds = sorted({l.get("kind") for l in lines})
    print(f"  kinds present: {kinds}")
    assert "topic" in kinds, "topic 没写入"
    assert "event" in kinds, "event 没写入"
    print()
    print("[topic] 内容:", next(l["value"] for l in lines if l.get("kind") == "topic"))
    print("[event] 内容:", next(l["value"] for l in lines if l.get("kind") == "event"))
    print()
    print("✅ E2E enrich 链路通过：provider 调通 → tasks.py 生成文本 → enrichments.jsonl 落盘")
    return 0


if __name__ == "__main__":
    sys.exit(main())
