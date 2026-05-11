from __future__ import annotations
import fcntl
import hashlib
import json
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

import re
from . import aliases, archival, decision_log, enrichments, sources, stopwords, transcript_reader
from .config import EVENTS_PATH, ARCHIVE_DIR, PROJECT_ROOT

SHANGHAI_TZ = timezone(timedelta(hours=8))
COLOR_TOKEN_COUNT = 8


def now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def parse_iso(ts: str) -> float:
    try:
        if not ts:
            return 0.0
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


_write_lock = RLock()


def append_event(evt: dict[str, Any]) -> None:
    EVENTS_PATH.parent.mkdir(exist_ok=True)
    line = json.dumps(evt, ensure_ascii=False)
    with _write_lock:
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass


def iter_events(include_archive: bool = False) -> Iterable[dict[str, Any]]:
    """迭代事件流。

    默认只读 hot 文件（events.jsonl）—— 配合 archive_if_needed() 控大小，list_sessions 性能稳定。
    include_archive=True 时先读归档（按日期升序）再读 hot，保证 ts 整体单调递增。

    教训 L13：归档目录懒加载 + gzip 解压，单次调用代价随 archive 总量线性。
    一般用户场景（dashboard 默认查询）不应触发，仅显式 ?include_archive=1 才走全量。
    """
    if include_archive:
        archive_dir = _resolve_archive_dir()
        for evt in archival.iter_archived_events(archive_dir):
            yield evt
    if not EVENTS_PATH.exists():
        return
    with open(EVENTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _resolve_archive_dir() -> Path:
    """从 config 读 archive_dir（相对路径基于 PROJECT_ROOT），fallback 到 ARCHIVE_DIR 默认。"""
    try:
        from .config import load as _load_cfg
        cfg = _load_cfg()
        rel = (cfg.get("archival") or {}).get("archive_dir") or "data/archive"
        p = Path(rel)
        if not p.is_absolute():
            p = PROJECT_ROOT / rel
        return p
    except Exception:
        return ARCHIVE_DIR


def archive_if_needed() -> dict[str, Any]:
    """触发归档（启动时 + 周期调用）。

    读 config.archival 配置，调 archival.archive_if_needed()。
    enabled=False 时跳过。归档失败不抛异常，log warning 后返回 stats。
    """
    try:
        from .config import load as _load_cfg
        cfg = _load_cfg()
        ar_cfg = cfg.get("archival") or {}
        if not ar_cfg.get("enabled", True):
            return {"triggered": False, "reason": "disabled"}
        archive_dir = _resolve_archive_dir()
        return archival.archive_if_needed(
            events_path=EVENTS_PATH,
            archive_dir=archive_dir,
            max_hot_size_mb=int(ar_cfg.get("max_hot_size_mb", 50)),
            rotation_grace_days=int(ar_cfg.get("rotation_grace_days", 1)),
        )
    except Exception as e:
        # 归档失败绝不影响主流程（事件保留在 hot）
        return {"triggered": False, "reason": f"exception: {e}"}


def read_events_for(session_id: str | None = None, limit: int = 200,
                    include_archive: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for evt in iter_events(include_archive=include_archive):
        if session_id and evt.get("session_id") != session_id:
            continue
        out.append(evt)
    out = out[-limit:]
    # 叠加 LLM event summary（仅当 llm.enabled + event_summary_enabled 同时为 True）
    from .config import load as _load_cfg
    _cfg = _load_cfg()
    if _cfg.get("llm", {}).get("enabled") and _cfg.get("llm", {}).get("event_summary_enabled"):
        for evt in out:
            sid = evt.get("session_id") or ""
            ts = evt.get("ts") or ""
            et = evt.get("event") or ""
            if sid and ts and et:
                cached = enrichments.get(enrichments.event_key(sid, ts, et))
                if cached:
                    evt["llm_summary"] = cached
    return out


# ── 状态机：仅看最后一个事件类型；liveness/timeout 由 watcher 写入合成事件来表达
TERMINATING_EVENTS = {"SessionEnd", "SessionDead"}

# v3：这些事件不改变 status —— 心跳/会话开始/工具前置不应该把 idle 状态翻回 running
NON_STATUS_EVENTS = {"Heartbeat", "PreToolUse", "SessionStart"}

# L20/L21：dashboard status 派生时跳过 idle prompt Notification 副本。
# L21 升级：「严格 1 次任务 1 次状态变化」—— 只要看到过 Stop（last_stop_unix>0），
# 后续 idle prompt 一律不更新 last_event，直到 session 又触发新一次 Stop。
# 与 notify_filter._notification_decision 的 dedup 判定保持一致。
_IDLE_PROMPT_FILLER_PHRASES = {
    "claude is waiting for your input",
    "claude code needs your attention",
    "press to continue",
    "press enter",
}


def _is_idle_prompt_dup(evt: dict[str, Any], last_stop_unix: float) -> bool:
    """事件是否是 Stop 后副本的 idle prompt（不应改变 dashboard 状态）。

    L21：last_stop_unix > 0 + msg is filler → 一律视为副本（永久 dedup until next Stop）。
    每次新 Stop 来时 last_stop_unix 会被刷新到新 ts → 自动开启下一回合的 dedup 窗口。
    真权限请求带后缀的不命中 filler 整句相等 → False，正常更新 status。
    """
    if (evt.get("event") or "") != "Notification":
        return False
    if last_stop_unix <= 0:
        return False  # 没看到过 Stop → 第一条 idle prompt 视为真等输入，正常更新
    msg = (evt.get("message") or "").strip().lower()
    return msg in _IDLE_PROMPT_FILLER_PHRASES


def derive_status(last_event: str) -> str:
    if last_event == "SessionEnd":
        return "ended"
    if last_event == "SessionDead":
        return "dead"
    if last_event == "TimeoutSuspect":
        return "suspect"
    if last_event == "Stop":
        return "idle"
    if last_event == "Notification":
        return "waiting"
    return "running"


# ───────────── 派生字段（PM 设计 + QE 净化） ─────────────
DONE_WINDOW_SECONDS = 5 * 60   # idle 5 分钟内算"刚完成"，之外算"已歇"


def _derive_progress_state(status: str, age_seconds: int) -> str:
    """粗粒度状态机（口语化标签）。"""
    if status == "waiting":
        return "等你"
    if status == "suspect":
        return "可能卡住"
    if status == "ended" or status == "dead":
        return "已歇"
    if status == "idle":
        return "刚完成" if age_seconds < DONE_WINDOW_SECONDS else "已歇"
    return "工作中"


def _derive_last_action(pretool_raw: dict | None) -> str:
    """从最近一次 PreToolUse payload 抽 'tool_name + 简化 input' 一句话。"""
    if not isinstance(pretool_raw, dict):
        return ""
    tool = (pretool_raw.get("tool_name") or "").strip()
    if not tool:
        return ""
    inp = pretool_raw.get("tool_input") or {}
    hint = ""
    if isinstance(inp, dict):
        # 不同工具的关键字段：抽一个最有信息量的
        if tool == "Bash":
            cmd = (inp.get("command") or "").strip().split("\n", 1)[0]
            hint = cmd[:32] + ("…" if len(cmd) > 32 else "") if cmd else ""
        elif tool in {"Read", "Edit", "Write", "NotebookEdit"}:
            p = inp.get("file_path") or inp.get("path") or ""
            hint = Path(p).name if p else ""
        elif tool in {"Grep", "Glob"}:
            pattern = inp.get("pattern") or inp.get("query") or ""
            hint = pattern[:32]
        elif tool == "WebFetch":
            url = inp.get("url") or ""
            hint = url[:32] + ("…" if len(url) > 32 else "") if url else ""
        elif tool == "Agent" or tool == "Task":
            desc = inp.get("description") or inp.get("subagent_type") or ""
            hint = desc[:32]
    if hint:
        return f"{tool}: {hint}"
    return tool


def _derive_waiting_for(notif_msg: str | None) -> str:
    """最近一次 Notification message 截短作为'等什么'。"""
    if not notif_msg:
        return ""
    msg = " ".join(notif_msg.split())
    if len(msg) > 60:
        msg = msg[:59] + "…"
    return msg


def _derive_turn_summary(last_assistant: str | None) -> str:
    """最近一次 Stop 的 last_assistant_message 经 clean_summary 净化。"""
    from . import sources
    if not last_assistant:
        return ""
    return sources.clean_summary(last_assistant, max_len=80)


def _derive_last_milestone(
    sid: str,
    last_stop_ts: str,
    last_stop_event_type: str,
    fallback_turn_summary: str,
    use_llm_cache: bool = True,
) -> str:
    """v3 spec §1：上一个有实质内容的进展 = 最近 Stop/SubagentStop 的 LLM 摘要 → fallback 启发式 turn_summary。

    use_llm_cache=False（用户关闭 LLM）时跳过 cache 查询，直接用 fallback。
    """
    if not sid or not last_stop_ts or not last_stop_event_type:
        return fallback_turn_summary or ""
    if use_llm_cache:
        try:
            cached = enrichments.get(enrichments.event_key(sid, last_stop_ts, last_stop_event_type))
            if cached:
                return cached
        except Exception:
            pass
    return fallback_turn_summary or ""


# ───────────── task_topic：从最近 N 条 user prompt 启发式抽主题词 ─────────────
_RE_EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]+")  # ≥ 2 字符（让 d2/v2/CI 等都进来）
_RE_CN_PHRASE = re.compile(r"[一-鿿]{2,4}")
_RE_PATH = re.compile(r"\b[\w/.-]+\.(?:py|md|ts|tsx|js|jsx|json|yaml|yml|css|html|sql|sh|toml)\b")
_RE_QUESTION_TAIL = re.compile(r"(下一步|什么|吗|呢|怎么|如何|哪个|哪些|\?|？)")
_HEADLINE_MAX = 28


def _tokens_of(text: str) -> list[str]:
    """从一句文本抽 token 列表（保留出现顺序）：路径 > 英文/拼音 > 中文短语；过滤停用词。"""
    if not text:
        return []
    spans: list[tuple[int, str]] = []
    for m in _RE_PATH.finditer(text):
        spans.append((m.start(), m.group(0)))
    for m in _RE_EN_TOKEN.finditer(text):
        spans.append((m.start(), m.group(0)))
    for m in _RE_CN_PHRASE.finditer(text):
        spans.append((m.start(), m.group(0)))
    spans.sort(key=lambda x: x[0])
    out: list[str] = []
    seen_lower: set[str] = set()
    for _, t in spans:
        t = t.strip()
        if not t or stopwords.is_stopword(t):
            continue
        kl = t.lower()
        if kl in seen_lower:
            continue
        seen_lower.add(kl)
        out.append(t)
    return out


_RE_LEAD_PUNCT = re.compile(r"^[\s,，。！？!?:：;；·\-—'\"'\"\(\(\)\)（）「」]+")
_RE_TRAIL_PUNCT = re.compile(r"[\s,，。！？!?:：;；·\-—'\"'\"\(\(\)\)（）「」]+$")


def _topic_from_single_prompt(prompt: str) -> str:
    """从单句 prompt 提取 topic：去首尾标点 → 截前 _HEADLINE_MAX 字。

    设计：这是 LLM 关闭场景下的主力降级路径。直接保留用户原话前几字
    比硬切英文 token 更可读（实测 LLM 给的 "新建会话" 反而劣化）。
    """
    if not prompt:
        return ""
    s = " ".join(prompt.split())
    s = _RE_LEAD_PUNCT.sub("", s)
    s = _RE_TRAIL_PUNCT.sub("", s)
    if not s:
        return ""
    if len(s) > _HEADLINE_MAX:
        s = s[: _HEADLINE_MAX - 1] + "…"
    return s


def _derive_task_topic(prompts: list[str]) -> str:
    """启发式 headline。

    多句（≥2）→ 跨句命中英文/路径 token → 输出 "tokenA · tokenB"；命中失败仍走单句逻辑。
    单句 → 直接抽 prompt 前 28 字（去标点）作 topic。

    设计转变（教训 L07）：之前认为"乱切不如不切"返回空；实测 fallback 到
    recent_user_prompt[:28] 反而效果好。把这个 fallback 直接内化到 task_topic
    本身，让所有场景都有非空 topic_heuristic。
    """
    if not prompts:
        return ""

    # 多句路径：尝试跨句 token 提取（保留旧行为，命中即输出）
    if len(prompts) >= 2:
        per_sentence_tokens = [_tokens_of(p) for p in prompts]
        from collections import Counter
        counts: Counter = Counter()
        for tokens in per_sentence_tokens:
            for t in set(x.lower() for x in tokens):
                counts[t] += 1
        topic_lower = {k for k, c in counts.items() if c >= 2}

        selected: list[str] = []
        seen_lower: set[str] = set()
        for tokens in per_sentence_tokens:
            for t in tokens:
                if t.lower() in topic_lower and t.lower() not in seen_lower:
                    seen_lower.add(t.lower())
                    selected.append(t)

        # 至少 1 个 token 是"英文/拼音/路径"才用 token 拼接（中文 only 切错风险高）
        has_safe_token = any(
            _RE_EN_TOKEN.fullmatch(t) or _RE_PATH.fullmatch(t)
            for t in selected
        )
        if selected and has_safe_token:
            headline = " · ".join(selected[:3])
            last_prompt = prompts[0]
            if _RE_QUESTION_TAIL.search(last_prompt[-30:]):
                headline = headline + " → 决定下一步"
            if len(headline) > _HEADLINE_MAX:
                headline = headline[: _HEADLINE_MAX - 1] + "…"
            return headline
        # token 命中失败 → 落到单句路径

    # 单句路径（或多句但 token 提取失败）：用最新一条 prompt 的前 28 字
    return _topic_from_single_prompt(prompts[0])


# ───────────── next_action：状态触发的"下一步要做什么" ─────────────
_RE_TOOL_CALL = re.compile(r"(Bash|Edit|Write|Read|MCP|Task|WebFetch|Glob|Grep)\(([^)]*)\)")
_AUTH_KEYWORDS = ("允许", "授权", "approve", "permission", "approval")
_QA_KEYWORDS = ("?", "？", "请", "需要", "提供", "确认")


def _derive_next_action(status: str, waiting_for: str, last_action: str) -> str:
    if status == "waiting":
        wf = waiting_for or ""
        m = _RE_TOOL_CALL.search(wf)
        if m or any(k in wf.lower() for k in (k.lower() for k in _AUTH_KEYWORDS)):
            tool_call = m.group(0) if m else "工具调用"
            return f"授权 {tool_call}"
        if any(k in wf for k in _QA_KEYWORDS):
            return "回答补充"
        if "plan" in wf.lower() or "计划" in wf:
            return "确认计划"
        return wf or "确认"
    if status == "running":
        return f"等 {last_action} 结果" if last_action else "等结果"
    if status == "idle":
        return "提下一轮 / 或归档"
    if status == "suspect":
        return "进终端检查 / 或等长跑"
    if status in ("ended", "dead"):
        return "可归档 / 或 resume"
    return ""


def _derive_mute_state(cfg: dict[str, Any], sid: str, now: float) -> dict[str, Any]:
    """L14：计算单 session 的 mute_state，注入 list_sessions 输出。

    返回 { muted: bool, until: ts | None, scope: str, label: str | None,
           remaining_seconds: int | None, muted_at_iso: str | None }
    过期项视为 not muted，但不在此处清理（让 notify_filter 走标准清理路径）。
    """
    try:
        sm = (cfg.get("session_mutes") or {})
        entry = sm.get(sid)
        if not isinstance(entry, dict):
            return {"muted": False, "until": None, "scope": "all"}
        until = entry.get("until")
        # 过期判定
        if until is not None:
            try:
                if float(until) <= now:
                    return {"muted": False, "until": None, "scope": "all"}
            except Exception:
                return {"muted": False, "until": None, "scope": "all"}
        scope = (entry.get("scope") or "all").strip().lower()
        if scope not in ("all", "stop_only"):
            scope = "all"
        remaining = None
        if until is not None:
            try:
                remaining = max(0, int(float(until) - now))
            except Exception:
                remaining = None
        return {
            "muted": True,
            "until": (None if until is None else float(until)),
            "scope": scope,
            "label": entry.get("label") or "",
            "remaining_seconds": remaining,
            "muted_at_iso": entry.get("muted_at_iso") or "",
        }
    except Exception:
        return {"muted": False, "until": None, "scope": "all"}


def _color_token(session_id: str) -> int:
    if not session_id:
        return 0
    h = hashlib.md5(session_id.encode("utf-8")).digest()
    return h[0] % COLOR_TOKEN_COUNT


def _display_name(s: dict[str, Any]) -> str:
    """v3 spec §1.1 降级链：alias > task_topic(LLM/heuristic) > recent_user_prompt[:28] > cwd_short > sid。

    去掉 first_user_prompt（session 跑久后焦点早转移），改用最近一条 user prompt。
    """
    alias = (s.get("alias") or "").strip()
    if alias:
        return alias
    topic = (s.get("task_topic") or "").strip()
    if topic:
        return topic
    rp = (s.get("recent_user_prompt") or "").strip()
    if rp:
        return rp[: _HEADLINE_MAX] + ("…" if len(rp) > _HEADLINE_MAX else "")
    fp = (s.get("first_user_prompt") or "").strip()
    if fp:
        return fp[: _HEADLINE_MAX] + ("…" if len(fp) > _HEADLINE_MAX else "")
    cwd_short = (s.get("cwd_short") or "").strip()
    if cwd_short:
        return cwd_short
    sid = s.get("session_id") or ""
    return sid[:8] if sid else "(unknown)"


def list_sessions(active_window_minutes: int = 30,
                  include_archive: bool = False) -> list[dict[str, Any]]:
    sessions: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for evt in iter_events(include_archive=include_archive):
        sid = evt.get("session_id")
        if not sid:
            continue
        s = sessions.get(sid)
        if s is None:
            s = {
                "session_id": sid,
                "source": evt.get("source") or "claude_code",
                "project": evt.get("project") or "",
                "cwd": evt.get("cwd") or "",
                "cwd_short": evt.get("cwd_short") or "",
                "first_event_ts": evt.get("ts"),
                "first_event_unix": parse_iso(evt.get("ts", "")),
                "last_event": evt.get("event"),
                "last_event_kind": evt.get("event") or "",
                "last_event_ts": evt.get("ts"),
                "last_event_unix": parse_iso(evt.get("ts", "")),
                "last_message": evt.get("message", ""),
                "last_assistant_message": evt.get("last_assistant_message") or "",
                "last_heartbeat_ts": None,
                "last_heartbeat_unix": None,
                "permission_mode": evt.get("permission_mode"),
                "effort_level": evt.get("effort_level"),
                "transcript_path": evt.get("transcript_path") or "",
                "claude_pid": evt.get("claude_pid"),
                "tty": evt.get("tty") or "",
                "event_count": 0,
                "events_by_type": {},
                "suspect_pushed_for_unix": 0.0,
                "dead_pushed_for_unix": 0.0,
                # 派生字段的累积来源（最后聚合用）
                "_last_pretool_raw": None,
                "_last_notification_msg": None,
                "_last_notification_unix": 0.0,
                "_last_stop_assistant": None,
                "_last_stop_unix": 0.0,
                "_last_stop_ts": "",
                "_last_stop_event_type": "",  # Stop / SubagentStop
                "menu_detected": False,  # R11：最近一次 Notification 是否在菜单 prompt 上
            }
            sessions[sid] = s
        ev_name = evt.get("event") or ""
        # last_event 只跟踪「会改变 status 的事件」—— Heartbeat / PreToolUse / SessionStart 不算
        # last_event_ts 仍跟最新事件（用于"距今"显示）
        s["last_event_ts"] = evt.get("ts")
        s["last_event_unix"] = parse_iso(evt.get("ts", ""))
        # last_event_kind = 真实最后一条事件类型（含 PreToolUse / PostToolUse / Heartbeat），
        # 供 liveness_watcher 做分状态超时判定（L09 — 长 tool 不应误报 hang）
        if ev_name:
            s["last_event_kind"] = ev_name
        # L20：idle prompt Notification 副本不更新 last_event（dashboard status 保持回合结束）
        is_idle_dup = _is_idle_prompt_dup(evt, s.get("_last_stop_unix") or 0.0)
        # R11 hotfix：menu_detected=true 时强制不算 dup（菜单是真等输入，优先级高于 filler 拦截）
        _evt_raw = evt.get("raw") or {}
        if isinstance(_evt_raw, dict) and _evt_raw.get("menu_detected") is True:
            is_idle_dup = False
        if ev_name and ev_name not in NON_STATUS_EVENTS and not is_idle_dup:
            s["last_event"] = ev_name
        elif s.get("last_event") is None:
            # 第一条事件如果是 NON_STATUS_EVENTS 类型（如 SessionStart），也得占个位
            s["last_event"] = ev_name
        if evt.get("message"):
            s["last_message"] = evt.get("message")
        if evt.get("last_assistant_message"):
            s["last_assistant_message"] = evt.get("last_assistant_message")
        if evt.get("permission_mode"):
            s["permission_mode"] = evt.get("permission_mode")
        if evt.get("effort_level"):
            s["effort_level"] = evt.get("effort_level")
        if evt.get("claude_pid"):
            s["claude_pid"] = evt.get("claude_pid")
        if evt.get("tty"):
            s["tty"] = evt.get("tty")
        if evt.get("transcript_path"):
            s["transcript_path"] = evt.get("transcript_path")
        if not s.get("project") and evt.get("project"):
            s["project"] = evt.get("project")
        if not s.get("cwd_short") and evt.get("cwd_short"):
            s["cwd_short"] = evt.get("cwd_short")
        if not s.get("source") and evt.get("source"):
            s["source"] = evt.get("source")
        s["event_count"] += 1
        et = evt.get("event") or "unknown"
        s["events_by_type"][et] = s["events_by_type"].get(et, 0) + 1
        if evt.get("event") == "TimeoutSuspect":
            s["suspect_pushed_for_unix"] = s["last_event_unix"]
        if evt.get("event") == "SessionDead":
            s["dead_pushed_for_unix"] = s["last_event_unix"]
        if evt.get("event") == "Heartbeat":
            s["last_heartbeat_ts"] = evt.get("ts")
            s["last_heartbeat_unix"] = parse_iso(evt.get("ts", ""))
            # Heartbeat 携带的 raw 是 PreToolUse payload，含 tool_name
            raw = evt.get("raw") or {}
            if isinstance(raw, dict) and raw.get("tool_name"):
                s["_last_pretool_raw"] = raw
        if evt.get("event") == "Notification":
            s["_last_notification_msg"] = evt.get("message") or ""
            s["_last_notification_unix"] = parse_iso(evt.get("ts", ""))
            # R11：菜单 prompt 探测（backend hook 在 raw.menu_detected 打标），冒泡到 session level
            _raw = evt.get("raw") or {}
            s["menu_detected"] = bool(isinstance(_raw, dict) and _raw.get("menu_detected"))
        if evt.get("event") == "Stop":
            s["_last_stop_assistant"] = evt.get("last_assistant_message") or ""
            s["_last_stop_unix"] = parse_iso(evt.get("ts", ""))
            s["_last_stop_ts"] = evt.get("ts") or ""
            s["_last_stop_event_type"] = "Stop"
            s["menu_detected"] = False  # 回合结束 → 清菜单标记
        if evt.get("event") == "SubagentStop":
            # 子 agent 完成也算一次回合结束，可作为兜底来源（但只在没有 Stop 时填充）
            if evt.get("last_assistant_message") and not s.get("_last_stop_ts"):
                s["_last_stop_assistant"] = evt.get("last_assistant_message")
                s["_last_stop_unix"] = parse_iso(evt.get("ts", ""))
                s["_last_stop_ts"] = evt.get("ts") or ""
                s["_last_stop_event_type"] = "SubagentStop"

    aliases_all = aliases.load_all()
    # 教训 L07：用户关闭 LLM 时，忽略 LLM cache 以反映启发式真实效果（cache 保留，等启用后复用）
    from .config import load as _load_cfg
    _cfg = _load_cfg()
    _use_llm_topic_cache = bool(
        _cfg.get("llm", {}).get("enabled")
        and _cfg.get("llm", {}).get("topic_enabled")
    )
    _use_llm_event_cache = bool(
        _cfg.get("llm", {}).get("enabled")
        and _cfg.get("llm", {}).get("event_summary_enabled")
    )
    now = time.time()
    out = []
    for s in sessions.values():
        info = aliases_all.get(s["session_id"]) or {}
        s["alias"] = info.get("alias", "")
        s["note"] = info.get("note", "")
        if s.get("transcript_path"):
            s["first_user_prompt"] = transcript_reader.first_user_prompt(s["transcript_path"])
            recent = transcript_reader.recent_user_prompts(s["transcript_path"], n=3)
            # v3 §1：最近一条 user prompt 用作大标题降级链的第三档
            s["recent_user_prompt"] = recent[0] if recent else ""
            heuristic_topic = _derive_task_topic(recent)
            # LLM topic 优先（cache hit 才用），但仅在 llm.enabled + topic_enabled 同时为 True 时
            llm_topic = ""
            if _use_llm_topic_cache:
                try:
                    tx = Path(s["transcript_path"])
                    if tx.exists():
                        mtime_ns = tx.stat().st_mtime_ns
                        cached = enrichments.get(enrichments.topic_key(s["transcript_path"], mtime_ns))
                        if cached:
                            llm_topic = cached
                except Exception:
                    pass
            s["task_topic"] = llm_topic or heuristic_topic
            s["task_topic_heuristic"] = heuristic_topic
            s["task_topic_source"] = "llm" if llm_topic else "heuristic"
        else:
            s["first_user_prompt"] = ""
            s["recent_user_prompt"] = ""
            s["task_topic"] = ""
            s["task_topic_heuristic"] = ""
            s["task_topic_source"] = ""
        s["color_token"] = _color_token(s["session_id"])
        s["display_name"] = _display_name(s)
        s["status"] = derive_status(s["last_event"])
        s["age_seconds"] = max(0, int(now - s["last_event_unix"]))
        s["active"] = s["age_seconds"] < active_window_minutes * 60
        s["terminal"] = s["status"] in {"ended", "dead"}
        # 派生字段（PM/QE 联合方案）
        s["progress_state"] = _derive_progress_state(s["status"], s["age_seconds"])
        s["last_action"] = _derive_last_action(s.get("_last_pretool_raw"))
        s["waiting_for"] = _derive_waiting_for(s.get("_last_notification_msg"))
        s["turn_summary"] = _derive_turn_summary(s.get("_last_stop_assistant"))
        # v3 §1：last_milestone = 最近 Stop/SubagentStop 的 LLM 摘要（cache hit）→ fallback turn_summary
        s["last_milestone"] = _derive_last_milestone(
            s["session_id"],
            s.get("_last_stop_ts") or "",
            s.get("_last_stop_event_type") or "",
            s["turn_summary"],
            use_llm_cache=_use_llm_event_cache,
        )
        s["last_notification_unix"] = s.get("_last_notification_unix") or 0.0
        s["next_action"] = _derive_next_action(s["status"], s["waiting_for"], s["last_action"])
        # L12：注入推送决策 trace（最近 5 条），让用户在 dashboard 看"为什么这条推了/没推"
        try:
            s["recent_decisions"] = decision_log.recent_for(s["session_id"], limit=5)
        except Exception:
            s["recent_decisions"] = []
        # L14：注入 per-session mute_state（dashboard 卡片右上 🔕 按钮用）
        s["mute_state"] = _derive_mute_state(_cfg, s["session_id"], now)
        # 清理累积内部字段
        for k in ("_last_pretool_raw", "_last_notification_msg", "_last_notification_unix",
                  "_last_stop_assistant", "_last_stop_unix",
                  "_last_stop_ts", "_last_stop_event_type"):
            s.pop(k, None)
        out.append(s)
    out.sort(key=lambda x: x["last_event_unix"], reverse=True)
    return out


def get_session(session_id: str) -> dict[str, Any] | None:
    for s in list_sessions():
        if s["session_id"] == session_id:
            return s
    return None


def normalize_incoming(payload: dict[str, Any]) -> dict[str, Any]:
    """把 hook payload 规范化为内部事件（路由到对应 source normalizer）。"""
    raw = payload if isinstance(payload, dict) else {}
    base = sources.normalize(raw)
    base["ts"] = raw.get("ts") or now_iso()
    return base
