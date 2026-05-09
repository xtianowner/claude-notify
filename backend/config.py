from __future__ import annotations
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
EVENTS_PATH = DATA_DIR / "events.jsonl"

# notify_policy 取值：
#   "immediate"   立即推送
#   "silence:N"   一段 N 秒静默期后推送（期间有同 session 任意活动则取消）
#   "off"         不推
DEFAULT_NOTIFY_POLICY = {
    # 用户预期：只在需要手动介入（确认/补充信息）才推。
    # 任何未列在此处的事件类型 dispatch 时按 off 处理（见 notify_policy.submit() 的 fallback）。
    "Notification":  "immediate",   # 等输入 / 等授权 → 必推
    "TimeoutSuspect": "immediate",  # 疑挂 → 必推
    "Stop":          "silence:12",  # 主 agent 完成 = 等下一步 → 推（12s 静默）
    "SubagentStop":  "off",         # 子 agent 完成 ≠ 主流程结束，主 agent 还在跑，不打扰
    "SessionStart":  "off",         # 开会话本身不要求用户动作
    "SessionEnd":    "off",
    "SessionDead":   "off",
    "Heartbeat":     "off",
    "PreToolUse":    "off",
    "PostToolUse":   "off",
    "TestNotify":    "immediate",
}

DEFAULT_NOTIFY_FILTER = {
    # v3 spec §4：Stop 推送过滤阈值，避免「OK」这种短回复也被推
    "stop_min_milestone_chars": 12,
    "stop_min_summary_chars": 15,
    "stop_min_gap_after_notification_min": 5,
    # 跨事件 dedupe（教训 L05）：Stop 推过后 N 分钟内，同 sid 的 Notification 若是空话则吞
    "notif_suppress_after_stop_min": 3,
    "notif_filler_phrases": [
        "waiting for your input",
        "press to continue",
        "press enter",
    ],
    "blacklist_words": [
        "ok", "yes", "no", "好", "好的", "已完成", "完成", "收到",
        "嗯", "是", "不", "对", "行", "可以", "thanks", "thank you", "ack",
    ],
    # 教训 L08：子 agent 内触发的 Notification 用户切到终端看不到提示
    # （父 session 已恢复 / 子 agent 自处理）→ 默认过滤。
    # 关掉：本字段设 false（推送恢复全量 Notification）
    "filter_sidechain_notifications": True,
}

DEFAULT_LIVENESS_PER_STATE_TIMEOUT = {
    # L09 — 按 last_event_kind 分状态设阈值，避免「长 tool 执行」误判 hang
    # enabled=False 时退化为旧逻辑（timeout_minutes 一刀切）
    "enabled": True,
    "Notification_minutes": 5,         # 等用户输入：与原 timeout 一致
    "Stop_minutes": 5,                 # 主回合结束等下一步
    "SubagentStop_minutes": 5,
    "PreToolUse_minutes": 15,          # 长 tool 执行（curl 大文件 / build / LLM 慢响应）容忍 15 分钟
    "PostToolUse_minutes": 10,         # 思考下一步
    "Heartbeat_minutes": 10,
    "SessionStart_minutes": 10,
    "default_minutes": 10,             # 其它未列名事件
    # PreToolUse 状态额外保护：transcript_path 在最近 N 秒内有 mtime 更新 → 视为活，继续等
    "pretool_transcript_alive_seconds": 60,
}

DEFAULTS: dict[str, Any] = {
    "feishu_webhook": "",
    "feishu_secret": "",
    "timeout_minutes": 5,
    "dead_threshold_minutes": 30,
    "active_window_minutes": 30,
    "liveness_per_state_timeout": DEFAULT_LIVENESS_PER_STATE_TIMEOUT,
    "notify_policy": DEFAULT_NOTIFY_POLICY,
    "notify_filter": DEFAULT_NOTIFY_FILTER,
    "muted": False,
    "snooze_until": "",
    "display": {
        "show_first_prompt": True,
        "show_last_assistant": True,
    },
    "llm": {
        "enabled": False,           # 默认关闭，dashboard 启用
        "provider": "local_cli",    # local_cli（默认，Claude Code 用户零配置） | anthropic | openai
        "anthropic": {
            "api_key": "",          # 也可走 ANTHROPIC_API_KEY 环境变量
            "model": "claude-haiku-4-5",
            "base_url": "",
            "timeout_s": 8,
        },
        "openai": {
            "api_key": "",          # 也可走 OPENAI_API_KEY 环境变量
            "model": "gpt-4o-mini", # 切到 deepseek-chat / qwen-turbo / 自托管模型 任意
            "base_url": "https://api.openai.com",  # DeepSeek: https://api.deepseek.com / Ollama: http://localhost:11434
            "timeout_s": 8,
        },
        "local_cli": {
            "binary": "",           # 留空自动探测（PATH 或 nvm）
            "model": "claude-haiku-4-5",
            "timeout_s": 30,        # OAuth 冷启动 11-15s 留余量
            "mode": "auto",         # auto | bare（快，需 ANTHROPIC_API_KEY） | oauth（慢，走 Claude Max 订阅）
            "extra_args": [],       # 额外 claude CLI 参数（如 ["--allow-dangerously-skip-permissions"]）
        },
        # 默认配置（教训 L07）：启发式已经覆盖 90% 场景；LLM 仅在用户主动启用时调用。
        # - topic_enabled 默认 False：实测 LLM 给 "新建会话" 这种空泛标题反而劣化，
        #   启发式 _topic_from_single_prompt(recent_user_prompt) 信息量更大。
        # - event_summary_enabled 默认 True：last_milestone 是 LLM 真正有价值的场景
        #   （从长 assistant 答复里抽核心动作），但只在 llm.enabled=True 时才生效。
        "topic_enabled": False,
        "event_summary_enabled": True,
    },
}

_lock = RLock()


def _read_raw() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict[str, Any]:
    with _lock:
        merged = _merge(DEFAULTS, _read_raw())
        # 旧字段向新结构迁移：events_enabled → notify_policy（一次性）
        legacy = merged.pop("events_enabled", None)
        if isinstance(legacy, dict):
            np = dict(merged.get("notify_policy") or DEFAULT_NOTIFY_POLICY)
            for k, enabled in legacy.items():
                if not enabled:
                    np[k] = "off"
            merged["notify_policy"] = np
        return merged


def save(patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        current = _merge(DEFAULTS, _read_raw())
        merged = _merge(current, patch)
        merged.pop("events_enabled", None)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, CONFIG_PATH)
        return merged


def public_view(cfg: dict[str, Any]) -> dict[str, Any]:
    """脱敏视图：webhook 只显示前后几位"""
    out = dict(cfg)
    wh = cfg.get("feishu_webhook") or ""
    if wh:
        if len(wh) > 24:
            out["feishu_webhook_masked"] = wh[:32] + "…" + wh[-6:]
        else:
            out["feishu_webhook_masked"] = "***"
        out["feishu_webhook_set"] = True
    else:
        out["feishu_webhook_masked"] = ""
        out["feishu_webhook_set"] = False
    return out
