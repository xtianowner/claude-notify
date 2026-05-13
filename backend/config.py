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
ARCHIVE_DIR = DATA_DIR / "archive"

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
    # v3 spec §4 + 教训 L11：Stop 推送过滤阈值。
    # L11 把字数阈值大幅放宽（旧 12/15 → 6/8），并加入"时间窗 fallback + 锚词放过"
    # 两条新通过条件，解决"短回合直接漏推"的 P1 bug。
    "stop_min_milestone_chars": 6,
    "stop_min_summary_chars": 8,
    "stop_min_gap_after_notification_min": 5,
    # L11：长任务收尾兜底窗。距离同 sid 上一次任意推送 > N 分钟 → 即使 summary 极短也推
    "stop_short_summary_grace_min": 8,
    # L11：sensitivity 字符串 "fast" | "normal" | "strict"。
    #   fast → milestone/summary 阈值都 4，几乎全推
    #   normal → 6/8（默认值，本字段为 normal 时尊重 stop_min_*_chars 自定义）
    #   strict → 都 20，仅推有内容回合
    # 非 normal 时 sensitivity 覆盖 stop_min_*_chars 字段值。
    "stop_sensitivity": "normal",
    # 跨事件 dedupe（教训 L05/L16/L18/L21/L22）：
    # L22 最终设计 —— 每个任务最多 3 次推送：
    #   1. 任务完成的 Stop 推送（由 _stop_decision 决定）
    #   2. Stop 后 N 分钟用户无反应 → idle reminder 1 次
    #   3. Stop 后 M 分钟用户还无反应 → idle reminder 最后一次
    # `notif_idle_reminder_minutes`：每次 idle reminder 距 Stop push 的最小 gap。
    #   [] = 严格 1 次（只推完成那次，类似 L21）
    #   [15, 45] = 默认 2 次提醒（在 15min 和 45min 阈值后 各推 1 次） — Round 11 放宽
    # 下一次新 Stop push → 重置该 sid 的 reminder 计数
    "notif_idle_reminder_minutes": [15, 45],
    # 兼容字段（已废弃，保留是为了用户配置回退；新代码不读）
    "notif_dedup_until_next_stop": True,
    "notif_suppress_after_stop_min": 3,
    # L19：Claude Code 实际有 2 种 idle prompt 文本，旧 default 只覆盖了一种导致漏吞。
    # 完整列表（每条 strip+lower 后整句相等才算 filler，避免误吞带后缀的真权限请求）：
    #   - "Claude is waiting for your input"
    #   - "Claude Code needs your attention"  ← 之前漏配
    "notif_filler_phrases": [
        "claude is waiting for your input",
        "claude code needs your attention",
        "press to continue",
        "press enter",
    ],
    # L11：黑词表只对"整句严格相等"生效（norm in blacklist），不做子串匹配，
    # 防"已完成端到端验证"被"已完成"误吞
    "blacklist_words": [
        "ok", "yes", "no", "好", "好的", "已完成", "完成", "收到",
        "嗯", "是", "不", "对", "行", "可以", "thanks", "thank you", "ack",
    ],
    # 教训 L08：子 agent 内触发的 Notification 用户切到终端看不到提示
    # （父 session 已恢复 / 子 agent 自处理）→ 默认过滤。
    # 关掉：本字段设 false（推送恢复全量 Notification）
    "filter_sidechain_notifications": True,
}

DEFAULT_ARCHIVAL = {
    # 教训 L13：events.jsonl 长期 append 会无界增长（重度用户 100+ session/天 × 几周 → 100MB+）
    # → list_sessions 启动时全量读，内存占用大、查询卡。
    # 滚动归档：hot 文件保留近期事件（list_sessions 默认只读它），老事件 gzip 落到 data/archive/。
    "enabled": True,
    "max_hot_size_mb": 50,       # hot 文件 >= 此大小触发归档检查
    "rotation_grace_days": 1,    # 只归档 ts 早于 N 天前的事件，保留近期热事件
    "archive_dir": "data/archive",
}

DEFAULT_QUIET_HOURS = {
    # L15 — 夜间不打扰：start-end 时段内吞掉非关键事件，但保留"真等输入 / 疑似 hang"穿透
    # enabled=False 时整段逻辑短路（与旧版完全等价）
    # 跨午夜处理：start > end（如 23:00→08:00）时按 "start..24:00 + 00:00..end" 理解
    # tz 走 zoneinfo（IANA 字符串）；非法或缺失 → 回退本地时区
    "enabled": False,
    "start": "23:00",
    "end": "08:00",
    "tz": "Asia/Shanghai",
    # 这些事件类型穿透 quiet_hours（Notification = 真等输入；TimeoutSuspect = 疑挂；都是关键事件）
    "passthrough_events": ["Notification", "TimeoutSuspect"],
    # weekdays_only=True 时仅周一~周五生效，周末不静音
    "weekdays_only": False,
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

DEFAULT_PUSH_CHANNELS = {
    # L41 / R16：渠道开关。两者均默认开。
    # - feishu：飞书 webhook 推送；关掉后 feishu.send_event 不发起 HTTP 请求
    # - browser：浏览器桌面通知（dashboard 打开时）；关掉后前端不调 Notification API
    # 两者独立，可只开 browser 完全脱离飞书使用。
    "feishu": True,
    "browser": True,
}

# L45 / R22：飞书 ↗ 链接打开 dashboard 时的 tab 复用模式
# - "focus_old"（默认）：旧 tab 强制 window.focus() 切前台 + 接管 hash/drawer；
#                       新 tab 显示 2.5s 自动消失的提示，无需用户操作
# - "user_choice"：新 tab 弹遮罩 + 两按钮（关闭此 tab / 仍在此 tab 加载），让用户选
# 浏览器硬限制：脚本不能让新 tab 静默 close，也不能关用户输入 URL/点链接打开的 tab。
# 因此"自动关旧 tab + 新 tab 接手"做不到，本配置不提供该选项。
DEFAULT_TAB_REUSE_MODE = "focus_old"
TAB_REUSE_MODE_VALUES = {"focus_old", "user_choice"}

DEFAULTS: dict[str, Any] = {
    "push_channels": DEFAULT_PUSH_CHANNELS,
    "tab_reuse_mode": DEFAULT_TAB_REUSE_MODE,
    "feishu_webhook": "",
    "feishu_secret": "",
    "timeout_minutes": 5,
    "dead_threshold_minutes": 30,
    "active_window_minutes": 30,
    "liveness_per_state_timeout": DEFAULT_LIVENESS_PER_STATE_TIMEOUT,
    "archival": DEFAULT_ARCHIVAL,
    "notify_policy": DEFAULT_NOTIFY_POLICY,
    "notify_filter": DEFAULT_NOTIFY_FILTER,
    "muted": False,
    "snooze_until": "",
    # L14：per-session 静音字典（按 session_id 索引）。dashboard 卡片右上 🔕 按钮维护。
    # 结构：{ "<sid>": {"until": <unix_ts | None>, "scope": "all" | "stop_only",
    #                   "muted_at": <unix_ts>, "muted_at_iso": "<...>", "label": "<...>"} }
    # - until=None 表示永久静音（直到用户手动解除）
    # - scope=all：拒推该 sid 的所有事件（Stop / Notification / TimeoutSuspect 一律 drop）
    # - scope=stop_only：只静 Stop / SubagentStop，Notification / TimeoutSuspect 仍推
    # 过期项由 notify_filter._session_mute_check 命中时清理，进程启动时 prune_expired_session_mutes
    "session_mutes": {},
    # L15：全局夜间不打扰
    "quiet_hours": DEFAULT_QUIET_HOURS,
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


_SENTINEL = object()


def _strip_defaults(value: Any, default: Any) -> Any:
    """递归剪枝：value 与 default 相同的字段/子树返回 _SENTINEL（剪掉）。
    用于 save() 时只持久化用户真正改动的字段，避免 DEFAULTS 后续变化被 config.json
    里的"伪保存"旧 default 值锁死（教训 L17）。

    - dict vs dict：逐字段递归；DEFAULTS 没有的字段一律保留（用户新加字段）
    - 标量 / list：== 比较，相等返回 _SENTINEL，否则保留原值
    """
    if isinstance(value, dict) and isinstance(default, dict):
        out = {}
        for k, v in value.items():
            d = default.get(k, _SENTINEL)
            if d is _SENTINEL:
                out[k] = v
                continue
            stripped = _strip_defaults(v, d)
            if stripped is not _SENTINEL:
                out[k] = stripped
        return out if out else _SENTINEL
    if value == default:
        return _SENTINEL
    return value


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """持久化用户配置 patch 到 config.json，仅保留与 DEFAULTS 不同的字段（教训 L17）。

    - 与 DEFAULTS 完全相等的字段不落盘 → 未来 DEFAULTS 改动老用户自动跟上
    - 用户主动调成与 default 不同的值 → 序列化保留
    - patch 显式覆盖前一次保存的字段 → 以本次为准（reverse merge into raw）
    - 返回值是 full view（DEFAULTS merged）供前端立即渲染
    """
    with _lock:
        existing_raw = _read_raw()
        merged_raw = _merge(existing_raw, patch)
        merged_raw.pop("events_enabled", None)
        full_view = _merge(DEFAULTS, merged_raw)
        full_view.pop("events_enabled", None)
        # 反向剪枝：只保留与 DEFAULTS 不同的部分写入文件
        to_persist = _strip_defaults(merged_raw, DEFAULTS)
        if to_persist is _SENTINEL or not isinstance(to_persist, dict):
            to_persist = {}
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(to_persist, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, CONFIG_PATH)
        return full_view


def _write_session_mutes(sm: dict[str, Any]) -> None:
    """绕过 save 的深合并，对 session_mutes 字段用"整体替换"语义直接写盘。

    必要：因为 save() 对 dict 做深 merge（用于 notify_policy 等部分更新），
    导致 pop 一个 key 后 merge 会从 disk 把它合回来。session_mutes 需要 set/del 语义。
    """
    raw = _read_raw()
    raw["session_mutes"] = sm
    raw.pop("events_enabled", None)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, CONFIG_PATH)


def set_session_mute(
    session_id: str,
    *,
    until: float | None,
    scope: str = "all",
    label: str = "user-set",
) -> dict[str, Any]:
    """L14：写一条 per-session 静音记录到 config.session_mutes[sid]。

    - until=None 表示永久（直到用户手动解除）
    - scope: "all"（默认，全部事件 drop）| "stop_only"（仅 Stop/SubagentStop drop）
    返回写入的 entry。
    """
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id required")
    sc = (scope or "all").strip().lower()
    if sc not in ("all", "stop_only"):
        sc = "all"
    import time as _t
    from datetime import datetime, timezone, timedelta
    now = _t.time()
    sh_tz = timezone(timedelta(hours=8))
    entry = {
        "until": (None if until is None else float(until)),
        "scope": sc,
        "muted_at": now,
        "muted_at_iso": datetime.fromtimestamp(now, sh_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "label": label or "user-set",
    }
    with _lock:
        sm = dict(_read_raw().get("session_mutes") or {})
        sm[sid] = entry
        _write_session_mutes(sm)
    return entry


def clear_session_mute(session_id: str) -> bool:
    """删除 session_mutes[sid]。返回是否实际删除（False=本来就不存在）。"""
    sid = (session_id or "").strip()
    if not sid:
        return False
    with _lock:
        sm = dict(_read_raw().get("session_mutes") or {})
        if sid not in sm:
            return False
        sm.pop(sid, None)
        _write_session_mutes(sm)
    return True


def prune_expired_session_mutes() -> int:
    """清理已过期 session_mutes 项（until <= now 且 until 非 None）。返回清理数量。"""
    import time as _t
    now = _t.time()
    with _lock:
        sm = dict(_read_raw().get("session_mutes") or {})
        expired = [sid for sid, e in sm.items()
                   if isinstance(e, dict)
                   and e.get("until") is not None
                   and float(e.get("until") or 0) <= now]
        if not expired:
            return 0
        for sid in expired:
            sm.pop(sid, None)
        _write_session_mutes(sm)
    return len(expired)


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
