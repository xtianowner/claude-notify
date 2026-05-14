"""端到端 enrich 链路（LocalCLIProvider）—— 用本机 claude -p 跑 topic + event 摘要。

不动 data/config.json，monkeypatch 注入 local_cli provider config。

Run: /opt/miniconda3/bin/python3 scripts/0509-1710-test-local-cli-e2e.py
"""
from __future__ import annotations
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="local-cli-e2e-"))
    print(f"tmp dir: {tmp}")

    # 1) 注入 LLM config = local_cli, mode=oauth（不依赖 API key）
    import backend.config as cfg_mod
    orig_load = cfg_mod.load

    def _patched_load():
        c = orig_load()
        c["llm"] = {
            "enabled": True,
            "provider": "local_cli",
            "local_cli": {
                "binary": "",          # 自动探测
                "model": "claude-haiku-4-5",
                "mode": "oauth",       # 强制 OAuth，证明免 API key 可用
                "timeout_s": 40,
            },
            "topic_enabled": True,
            "event_summary_enabled": True,
        }
        return c

    cfg_mod.load = _patched_load

    # 2) enrichments 写临时
    import backend.enrichments as enr
    enr.ENRICH_PATH = tmp / "enrichments.jsonl"
    enr._cache.clear()

    # 3) 假 transcript（让 topic 分支启动）
    fake_transcript = tmp / "fake.jsonl"
    fake_transcript.write_text(
        '{"type":"user","message":{"content":"配置栏 UI 字体超出模块栏了"},"timestamp":"2026-05-09T15:00:00Z"}\n'
        '{"type":"user","message":{"content":"做 OpenAI 兼容接口测试"},"timestamp":"2026-05-09T15:01:00Z"}\n'
        '{"type":"user","message":{"content":"修 task_topic 中文乱切 bug"},"timestamp":"2026-05-09T15:02:00Z"}\n',
        encoding="utf-8",
    )

    # 4) 真触发 _llm_enrich（同 backend 调用路径）
    from backend.app import _llm_enrich
    fake_evt = {
        "session_id": "test-sid-localcli",
        "ts": "2026-05-09T15:02:30+08:00",
        "event": "Notification",
        "message": "Claude is requesting permission to run: rm -rf /tmp/foo && pip install -e .",
        "transcript_path": str(fake_transcript),
        "raw": {"hook_event_name": "Notification", "reason": "permission"},
    }
    fake_summary = {
        "session_id": "test-sid-localcli",
        "transcript_path": str(fake_transcript),
        "last_assistant_message": "已经完成 LocalCLIProvider，可以走本机 claude -p。",
    }

    print("=" * 60)
    print("跑 _llm_enrich() ...（OAuth 模式，预期 ~15-30s）")
    t0 = time.time()
    asyncio.run(_llm_enrich(fake_evt, fake_summary))
    elapsed = time.time() - t0
    print(f"  total elapsed: {elapsed:.2f}s")

    # 5) 检查 enrichments
    print("=" * 60)
    cache_file = enr.ENRICH_PATH
    if not cache_file.exists() or not cache_file.read_text().strip():
        print("❌ enrichments 文件未创建或空")
        print("注意：OAuth 冷启动可能很慢，topic+event 串行跑两次会加倍。")
        print("Top-level _llm_enrich exception 一般会被吞，看 stderr 上的 logging warn。")
        return 1
    content = cache_file.read_text("utf-8").strip()
    print(f"enrichments.jsonl ({len(content)} bytes):")
    print(content)

    import json
    lines = [json.loads(l) for l in content.split("\n") if l.strip()]
    kinds = sorted({l.get("kind") for l in lines})
    print()
    print(f"kinds: {kinds}")
    for l in lines:
        print(f"  [{l['kind']}] {l['value']!r}")
    print()
    if "topic" in kinds:
        print("✅ topic 写入成功（LocalCLIProvider → 本机 claude -p → enrichments 落盘）")
    if "event" in kinds:
        print("✅ event 写入成功")
    if "topic" not in kinds:
        print("⚠️ topic 未写入（可能 LLM 输出空 / timeout / claude 未登录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
