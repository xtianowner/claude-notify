#!/usr/bin/env python3
"""L10 — 飞书消息「完成了什么」内容质量测试。

喂 5+ 真实 last_assistant_message 样本（采自 data/events.jsonl 最近 Stop），
对比改前（单行 last_milestone = clean_summary）vs 改后（conclusion + task_line）。

不真发飞书，只 assert 字符串结构。
"""
from __future__ import annotations
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import sources, feishu


# 5 个真实样本（采自本地 data/events.jsonl 最近 Stop）+ 2 个边界样本
SAMPLES: list[tuple[str, str, str]] = [
    # (case_name, task_topic, last_assistant_message)
    (
        "task_5_done",
        "Round 2·A 飞书内容重构",
        "全部 5 项任务完成。\n\n## 改动总结\n\n### 1. 飞书模板：9 行 → 6 行（BLUF 优先）\n\n```\n旧                                  新\n─────────────────────────────────────────────────\n✅ [Claude] 任务完成  ⚫              ✅ hello · 任务完成\n```\n\n**执行环境**：local mac（darwin 24.5）",
    ),
    (
        "e2e_pass",
        "OAuth cache 验证",
        "**端到端验证通过**：\n\n| 调用 | 延迟 | 说明 |\n|---|---|---|\n| #1 (cache creation) | 17.76s | 首次写 cache（贵）|\n| #2 (cache hit) | **6.94s** | cache 命中，速度减 60% |\n\n延迟从 17.76s 降到 6.94s 验证 cache 在工作。\n\n**执行环境**：local mac（darwin 24.5）",
    ),
    (
        "all_tunnel",
        "config GET/PUT 测试",
        "全部跑通。GET/PUT 实测通过：改完 max_tokens / timeout / fallback 后 dashboard 立即读到新值，api_key 用 `__keep__` 占位符保留原值。\n\n执行环境：local Mac（macOS Darwin 24.5.0，conda env `rag-system-backend`）",
    ),
    (
        "heading_only_first",
        "OAuth 模式优化",
        "## 改动总结\n\n### 核心认知错误已修正\n\n之前 L04 的修法在 OAuth 模式下**根本没生效**——我同时传了 `--system-prompt` 和 `--exclude-dynamic-system-prompt-sections`。\n\n**执行环境**：local mac（darwin 24.5）",
    ),
    (
        "tweak",
        "抽屉事件流倒序",
        "改完。一处微调：\n\n**`renderDrawerEvents()`** —— 抽屉事件流改为**倒序**（最新置顶）：\n- 渲染：`filtered.slice().reverse()`\n\n**纯前端，不需重启 backend**。",
    ),
    (
        "hello",
        "hello",
        "你好！有什么需要帮忙的？",
    ),
    (
        "math",
        "1+1=?",
        "1+1=2。",
    ),
    (
        "filler_then_real",
        "改 X",
        "好的，我已修复 X。重启后生效。",
    ),
]


def run_old_path(last_assistant: str) -> str:
    """改前路径：clean_summary 取首句（即 _derive_turn_summary 的逻辑）。"""
    return sources.clean_summary(last_assistant, max_len=80)


def run_new_path(last_assistant: str) -> str:
    """改后路径：extract_conclusion 按行抽锚词句 / 段落首行。"""
    return sources.extract_conclusion(last_assistant, max_len=90)


def assert_format_text_structure(text: str, ev: str = "Stop") -> None:
    """飞书文本基本结构断言：≥ 4 行；含 emoji + status；含分隔线。"""
    lines = text.split("\n")
    assert len(lines) >= 4, f"{ev} 飞书文本应 ≥ 4 行，实际 {len(lines)}: {lines!r}"
    # L23：L1 可能以 [项目名] 前缀开头（multi-session 区分），emoji 在前缀之后
    l1_core = lines[0]
    if l1_core.startswith("["):
        rb = l1_core.find("] ")
        if rb >= 0:
            l1_core = l1_core[rb + 2:]
    assert l1_core.startswith("✅") or l1_core.startswith("🤝"), f"L1 应有 emoji，实际 {lines[0]!r}"
    assert " · 任务完成" in lines[0] or " · 子 agent 完成" in lines[0], f"L1 应含状态，实际 {lines[0]!r}"
    assert any("─" in ln for ln in lines), f"应含分隔线，实际 {text!r}"


def main() -> None:
    print("=" * 72)
    print("L10 — 飞书 conclusion 抽取对比")
    print("=" * 72)

    for case_name, topic, raw in SAMPLES:
        old = run_old_path(raw)
        new = run_new_path(raw)
        print(f"\n[{case_name}] topic={topic!r}")
        print(f"  raw  (头 60): {raw[:60]!r}")
        print(f"  OLD  : {old}")
        print(f"  NEW  : {new}")

        # 基本断言：抽出的 conclusion 不能为空
        assert new, f"[{case_name}] NEW 不应为空"
        # 不能含 markdown 残留（粗体星号 / heading 井号）
        assert "**" not in new, f"[{case_name}] NEW 不应含粗体残留: {new!r}"
        assert not new.startswith("#"), f"[{case_name}] NEW 不应以 heading 开头: {new!r}"
        # footer 不应进 conclusion
        assert "执行环境" not in new, f"[{case_name}] NEW 不应含 footer: {new!r}"

    # 锚词命中场景的具体期望
    cases_with_expected: list[tuple[str, str, list[str]]] = [
        # (case_name, sample_raw, must_contain_any_of)
        ("task_5_done", SAMPLES[0][2], ["全部", "完成"]),
        ("e2e_pass",    SAMPLES[1][2], ["端到端", "通过"]),
        ("all_tunnel",  SAMPLES[2][2], ["跑通", "通过"]),
        ("heading_first", SAMPLES[3][2], ["核心", "修正"]),
        ("filler",      SAMPLES[7][2], ["修复"]),
    ]
    for case_name, raw, must_any in cases_with_expected:
        new = run_new_path(raw)
        hit = any(kw in new for kw in must_any)
        assert hit, f"[{case_name}] NEW 应含 {must_any} 之一，实际 {new!r}"
    print("\n[anchor-hit assertions] PASS")

    # ───── 飞书 _format_text 整体结构 ─────
    print("\n" + "=" * 72)
    print("飞书 _format_text 整体渲染（task_line + conclusion 两块）")
    print("=" * 72)
    for case_name, topic, raw in SAMPLES[:5]:
        evt = {
            "event": "Stop",
            "session_id": "abcdef0123456789",
            "ts": "2026-05-09T20:50:00+08:00",
            "cwd": "/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify",
            "permission_mode": "bypass",
            "effort_level": "high",
            "last_assistant_message": raw,
        }
        summary = {
            "task_topic": topic,
            "display_name": topic,
            "recent_user_prompt": topic,
            "first_user_prompt": topic,
            "last_assistant_message": raw,
            "last_milestone": run_old_path(raw),
            "next_action": "提下一轮 / 或归档",
            "last_action": "",
            "permission_mode": "bypass",
            "effort_level": "high",
            "age_seconds": 0,
        }
        text = feishu._format_text(evt, summary, {})
        print(f"\n[{case_name}] feishu output:")
        for ln in text.split("\n"):
            print(f"  | {ln}")
        assert_format_text_structure(text, "Stop")
        # 应有 ✓ 前缀的 conclusion 行
        assert any(ln.startswith("✓ ") for ln in text.split("\n")), f"[{case_name}] 缺 ✓ conclusion: {text!r}"

    # ───── 重叠去重 ─────
    print("\n" + "=" * 72)
    print("topic / conclusion 重叠去重")
    print("=" * 72)
    # topic 与 conclusion 一致 → 应只保留一行
    overlap_evt = {
        "event": "Stop",
        "session_id": "0123456701234567",
        "ts": "2026-05-09T20:50:00+08:00",
        "cwd": "/tmp",
        "last_assistant_message": "修复 cache 命中已完成。",
    }
    overlap_summary = {
        "task_topic": "修复 cache 命中",
        "display_name": "修复 cache 命中",
        "last_assistant_message": "修复 cache 命中已完成。",
        "last_milestone": "",
        "next_action": "",
        "last_action": "",
        "permission_mode": "",
        "effort_level": "",
        "age_seconds": 0,
    }
    text = feishu._format_text(overlap_evt, overlap_summary, {})
    print(text)
    pin_lines = [ln for ln in text.split("\n") if ln.startswith("📌")]
    check_lines = [ln for ln in text.split("\n") if ln.startswith("✓")]
    # focus(L1) 和 task_topic 一致 → task_line 应被略掉，仅保留 conclusion
    assert len(pin_lines) == 0, f"重叠时应略掉 📌 行，实际 {pin_lines!r}"
    assert len(check_lines) == 1, f"应保留一行 ✓，实际 {check_lines!r}"

    # 有 alias：focus=alias，L2 task_line 应取 task_topic 补充信息
    e2 = {
        "event": "Stop",
        "session_id": "ffffffffffffffff",
        "ts": "2026-05-09T20:50:00+08:00",
        "cwd": "/tmp",
        "last_assistant_message": "全部 5 项任务完成。后续等用户验收。",
    }
    s2 = {
        "task_topic": "重构飞书内容质量",  # L2
        "display_name": "claude-notify",   # alias-like → focus=display_name (因为 task_topic 也算 focus 优先) 实际 focus=task_topic
        # 真实场景模拟：用户配过 alias，display_name=alias；focus 取 task_topic
        "alias": "claude-notify",
        "first_user_prompt": "把飞书消息看不出完成了什么这个 bug 修了",
        "last_assistant_message": e2["last_assistant_message"],
        "last_milestone": "",
        "next_action": "提下一轮",
        "last_action": "",
        "permission_mode": "bypass",
        "effort_level": "high",
        "age_seconds": 0,
    }
    text2 = feishu._format_text(e2, s2, {})
    print("\n有 alias / task_topic 与 focus 不同的样本：")
    print(text2)
    # focus 实际取 task_topic（feishu 逻辑：focus = task_topic or display_name），
    # 所以 task_line 该降级到 first_user_prompt（而不是再次显示 task_topic）
    pin_lines2 = [ln for ln in text2.split("\n") if ln.startswith("📌")]
    assert len(pin_lines2) == 1, f"应保留 1 行 📌（first_user_prompt），实际 {pin_lines2!r}"
    assert "把飞书消息" in pin_lines2[0] or "看不出" in pin_lines2[0], (
        f"📌 应取 first_user_prompt（与 focus 不同），实际 {pin_lines2!r}"
    )
    assert any(ln.startswith("✓") for ln in text2.split("\n")), "应保留 ✓"

    print("\n" + "=" * 72)
    print("ALL PASS")
    print("=" * 72)


if __name__ == "__main__":
    main()
