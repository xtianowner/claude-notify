<!-- purpose: claude-notify 模块清单（按 §6 模块化登记） -->

# modules

创建时间: 2026-05-08 20:29:45
更新时间: 2026-05-11 11:00:00

## backend.config
- 输入：磁盘 `data/config.json`，运行时 patch dict
- 输出：合并后的配置 dict / 脱敏 public 视图
- 依赖：标准库
- 复用入口：`from backend import config; config.load() / config.save(patch) / config.public_view(cfg)`
- L14 per-session 静音：`set_session_mute(sid, until, scope, label)` / `clear_session_mute(sid)` / `prune_expired_session_mutes()`
  - 内部用 `_write_session_mutes` 绕过 save() 的深合并（dict set/del 语义需要整体替换）

## backend.event_store
- 输入：`append_event(evt)` / 磁盘 `data/events.jsonl`（hot）+ `data/archive/*.jsonl.gz`（冷归档，L13）
- 输出：聚合后的 session 列表 / 事件流 / 规范化的事件 dict
- 依赖：标准库（fcntl）+ backend.archival（gzip 读写）
- 复用入口：
  - `event_store.append_event(evt)` 跨进程安全 append（hot 文件）
  - `event_store.list_sessions(active_window_minutes, include_archive=False)` 返回带 status 的 session 列表
    - **R11 派生字段 `menu_detected: bool`（L35）**：Notification + `raw.menu_detected=true` → true；Stop → 清零；SubagentStop → 不清。属"UI hint 字段"语义，不是 status 状态机字段。
  - `event_store.read_events_for(session_id, limit, include_archive=False)` 按 session 过滤事件
  - `event_store.normalize_incoming(payload)` 把 hook payload 规范化
  - `event_store.archive_if_needed()` 触发滚动归档（启动时调用；返回 stats）

## backend.archival（v3 新增，L13）
- 职责：events.jsonl 滚动归档（hot ≥ max_hot_size_mb 时把老事件 gzip 落到 data/archive/）
- 配置（config.archival）：
  - `enabled`：默认 True
  - `max_hot_size_mb`：默认 50（hot 阈值）
  - `rotation_grace_days`：默认 1（保留近期 N 天事件在 hot）
  - `archive_dir`：默认 "data/archive"（相对路径基于 PROJECT_ROOT）
- 文件命名：`events-YYYY-MM-DD.jsonl.gz`（按事件**最早 ts** 的本地日期分桶；同桶用 gzip 多 member 累积 append）
- 并发：归档时持 hot 文件 `LOCK_EX`，与 `append_event` 串行；归档失败 hot 不动（事件永不丢）
- 复用入口：
  - `archival.archive_if_needed(events_path, archive_dir, max_hot_size_mb, rotation_grace_days)` 返回 stats
  - `archival.iter_archived_events(archive_dir)` 惰性迭代（gzip 解压）
  - `archival.iter_archive_files(archive_dir)` 列出 .gz 文件（日期升序）

## backend.feishu
- 输入：事件 dict + `data/config.json` 中的 webhook/secret
- 输出：HTTP POST 飞书 webhook，返回 `{ok, reason, response}`
- 依赖：httpx
- 复用入口：`await feishu.send_event(evt)` / `await feishu.send_test()`
- 节流：SubagentStop 默认 30s 同 session 合并；Notification/Stop 不节流；Heartbeat 不推

## backend.liveness_watcher（v2 替代 timeout_watcher，L09 升级到分状态阈值，L32 修 active_window 边缘 bug）
- 输入：`watch_loop(callback, interval_seconds)` + 配置阈值
- 输出：
  - PID 失活 + transcript stale → append `SessionDead`
  - 仍活但事件/transcript 静默 ≥ 状态阈值 → append `TimeoutSuspect`（raw 带 last_event_kind/threshold_bucket/threshold_sec）
- 阈值按 `last_event_kind` 分桶（教训 L09，避免长 tool 误判）：
  - `Notification / Stop / SubagentStop` → 5 分钟（等用户 / 等下一回合）
  - `PreToolUse` → 15 分钟（长 tool 容忍）+ transcript mtime ≤ 60s 视为活，跳过报警
  - `PostToolUse / Heartbeat / SessionStart / 其它` → 10 分钟
  - `liveness_per_state_timeout.enabled=False` → 退化到旧 `timeout_minutes` 一刀切（kill switch）
- **active_window（L32 修复）**：`list_sessions` 过滤窗口动态默认 `max(60, dead_threshold * 2)`，避免与 dead_threshold 默认 30 重合导致 dead 判定永远不可达；用 `_read_raw()` 区分用户显式 vs DEFAULTS 合并值
- 不变量：`active_window_minutes ≥ dead_threshold_minutes + buffer`（buffer ≥ 30），否则 dead 判定路径死循环不可达
- 依赖：backend.config + backend.event_store（读取 `last_event_kind` 字段）
- 复用入口：`asyncio.create_task(liveness_watcher.watch_loop(on_event))`

## backend.sources（v2 新增）
- 职责：source 路由 stub，按事件 `source` 字段分派到对应 normalizer
- 已实现：claude_code；预留：codex / generic（暂不真实落地）
- 复用入口：`sources.normalize(raw_payload)`

## backend.transcript_reader（v2 新增）
- 职责：从 Claude Code transcript jsonl 读首条 user prompt（带 path+mtime LRU 缓存）
- 复用入口：`transcript_reader.first_user_prompt(path)`

## backend.aliases（v2 新增）
- 职责：用户给 session 起的别名 + 备注，存 `data/aliases.json`
- 复用入口：`aliases.load_all() / aliases.get(sid) / aliases.set_alias(sid, alias, note)`

## backend.notify_policy（v2 新增）
- 职责：silence-then 节流 + 立即推送 + 取消机制；活动事件（Heartbeat/PreToolUse）取消该 session pending
- 配置：`notify_policy: {EventName: "immediate" | "silence:N" | "off"}`
- 复用入口：`await notify_policy.get_dispatcher().submit(evt)`
- L12：每个推 / 不推决策点（`policy_off` / `silence-scheduled` / `merged` / `merged_all_filtered`）调 `_trace(...)` 写 `decision_log`

## backend.notify_filter
- 职责：should_notify 入口，做"哪些事件该推 / 该吞"的纯计算判定（不调 LLM）
- 调用方：notify_policy.submit / _wait_and_send 在 immediate / silence-then 路径前都调一次
- 前置过滤链（按顺序，命中即 return False + 写 decision_log）：
  1. **L14 `_session_mute_check`**：命中 `config.session_mutes[sid]` 且未过期 → drop
     - scope=all → `session_muted_all`；scope=stop_only → 仅 Stop/SubagentStop 算 `session_muted_stop_only`
     - 命中过期项时调 `cfg.clear_session_mute(sid)` best-effort 清理
  2. **L15 `_quiet_hours_check`**：命中全局静音时段 → drop（passthrough 白名单除外）
  3. 事件类型分发：always_true / always_false / Notification dedupe（L05/L08）/ Stop 复合判别（L11）
- **R11 阈值调整**：`notif_idle_reminder_minutes` 默认 `[5, 10]` → `[15, 45]`（L34）；用户可显式覆盖
- **R11 menu bypass（L33）**：`_idle_reminder_decision` 首位检查 `evt.raw.menu_detected`，命中 → `(True, "menu_detected_bypass_dup")` 直接放行，绕过 `notif_idle_dup` 窗口
- 配置入口：`notify_filter` 字段 + `session_mutes` 字段 + `quiet_hours` 字段
- 复用入口：`notify_filter.should_notify(evt, summary, cfg, log_trace=True) -> (ok, reason)`

## backend.decision_log（v3 新增，L12；前端 UI 已下线 L38）
- 职责：推送决策落盘，给开发者 debug 用（"为什么这条推了 / 没推"）
- 输入：`append(session_id, event_type, decision, reason, policy, extra)`
- 输出：环形 jsonl `data/push_decisions.jsonl`，按 sid trim 上限 50 条 / 文件累计 1000 行触发整体重写
- 字段：`{ts, session_id, event, decision: "push"|"drop"|"scheduled"|"merged", reason, policy}`
- 调用方：
  - `notify_filter.should_notify` 每个 return 处自动 append（policy="filter"）
  - `notify_policy.NotifyDispatcher._trace` helper（policy="off" / "silence:N" / "silence"）
- 复用入口：
  - `decision_log.append(sid, ev_type, decision, reason, policy)` 写
  - `decision_log.all_for(sid, limit=50)` 给 `/api/sessions/{sid}/decisions` endpoint 用（dashboard 入口已删，但 endpoint 保留，可 curl）
- L38（2026-05-11）：dashboard 卡片 trace 行 + modal 已删除（噪音）。`recent_for` 也不再被 list_sessions 注入，仅保留 `all_for` 给 endpoint。
- 依赖：backend.config（DATA_DIR）+ 标准库 fcntl
- 落盘开销：每条记录 ~120 字节；50 sessions × 50 条 = 25 KB 上限

## backend.notes（v10 新增，L28/L29/L31）
- 职责：全局单 markdown 笔记的持久化 + 大小校验 + 跨进程/跨线程并发安全
- 文件位置：`data/notes.md`（不存在则惰性创建，单文件 LFS-friendly，git 可直接追踪）
- 输入：
  - `read() -> {content: str, size: int, updated_at: float}`
  - `write(content: str, source: str = "api") -> {size, updated_at}`，超过 `MAX_SIZE_BYTES` 抛 `NotesOversizeError`
- 输出：
  - REST `GET /api/notes` → `{content, size, updated_at}`
  - REST `PUT /api/notes` body `{content: str}`（≤ 256KB，超限返回 413）
  - WS broadcast `{type: "notes_updated", source, size, updated_at}`（写入成功后由 app 层广播）
- 大小上限：`MAX_SIZE_BYTES = 256 * 1024`（约 6 万字 markdown）
- 并发：`fcntl.flock(LOCK_EX)`（跨进程）+ `threading.RLock`（同进程多请求）双锁；写采用 `O_WRONLY | O_CREAT | O_TRUNC` 整体覆盖；last-write-wins，无 version 字段（L31）
- 错误：
  - `NotesOversizeError`：超限抛出，app 层 catch 后返回 413 `{detail: "notes exceeds 256KB"}`
  - I/O 异常：app 层返回 500，前端状态行显示错误，本地编辑不丢
- 依赖：标准库 fcntl + threading + os.path + 模块顶 `NOTES_PATH` 解析自 `backend.config.DATA_DIR`
- 复用入口：
  - `from backend.notes import read, write, NOTES_PATH, MAX_SIZE_BYTES, NotesOversizeError`
- 前端配套模块：`frontend/notes.js`
  - 导出 `initNotes()` / `setCollapsed(bool)` / `onNotesEvent(payload)`（接收 WS notes_updated）
  - 800ms debounce autosave，textarea 未聚焦+未 dirty 时接收 WS 静默同步，聚焦/dirty 时静默忽略 WS（L31 接收侧自保护）
  - 折叠态用 localStorage（`notes.collapsed`）持久化，正文不进 localStorage（L28 内容 vs UI 偏好分层）

## backend.app
- 入口：`python -m backend.app`（uvicorn 127.0.0.1:8787）
- 路由：见 `docs/plan/0508-2029-design.md §3.2`；v10 新增 `GET /api/notes` / `PUT /api/notes`，PUT 成功后 `await ws_hub.broadcast({"type": "notes_updated", ...})`
- 依赖：fastapi + uvicorn + 上述 4 个模块

## scripts.hook-notify
- 入口：被 Claude Code 调起，stdin = JSON
- 行为：写本地 jsonl（兜底）+ 异步 POST 到后端
- **R11 菜单识别（L33）**：`_detect_menu_prompt(transcript_path)` 读 transcript 末 20 行，子串匹配 `❯\s*\d+\.|Type something`，命中 → 在 evt 上挂 `raw.menu_detected=true`。filter 侧据此走 bypass 通道（不重新读 transcript，职责分离）
- 依赖：纯标准库 + system `curl`（保证 Claude 启动期可用）
- 复用入口：`scripts/hook-notify.py [--heartbeat]`

## scripts.install-hooks
- 入口：`python scripts/install-hooks.py [--uninstall|--dry-run]`
- 行为：把 4 条 hook 注入 `~/.claude/settings.json`，备份原文件
- 依赖：标准库

## frontend (vanilla SPA)
- 入口：`frontend/index.html`，由后端 StaticFiles 挂载
- 模块：
  - `api.js` — REST + WebSocket 客户端
  - `format.js` — 时间相对化、状态徽章颜色映射
  - `app.js` — 主控制器（session 网格 / 抽屉 / 配置弹窗 / 测试按钮）
  - `styles.css` — 单文件样式
- L38（2026-05-11）：卡片底部 trace 行 + modal 已删除（开发者 debug 工具，对最终用户是噪音）。后端 `data/push_decisions.jsonl` 仍写，`/api/sessions/{sid}/decisions` 仍可 curl。
- 不引入任何 npm 依赖

## 复用判定
均尚未在第二个项目复用。**若将来在另一个项目复用**：按 §6 升级到 `~/Documents/00-Tian/AI/modules/`。
