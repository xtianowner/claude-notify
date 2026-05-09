<!-- purpose: claude-notify 模块清单（按 §6 模块化登记） -->

# modules

创建时间: 2026-05-08 20:29:45
更新时间: 2026-05-09 22:15:00

## backend.config
- 输入：磁盘 `data/config.json`，运行时 patch dict
- 输出：合并后的配置 dict / 脱敏 public 视图
- 依赖：标准库
- 复用入口：`from backend import config; config.load() / config.save(patch) / config.public_view(cfg)`

## backend.event_store
- 输入：`append_event(evt)` / 磁盘 `data/events.jsonl`（hot）+ `data/archive/*.jsonl.gz`（冷归档，L13）
- 输出：聚合后的 session 列表 / 事件流 / 规范化的事件 dict
- 依赖：标准库（fcntl）+ backend.archival（gzip 读写）
- 复用入口：
  - `event_store.append_event(evt)` 跨进程安全 append（hot 文件）
  - `event_store.list_sessions(active_window_minutes, include_archive=False)` 返回带 status 的 session 列表
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

## backend.liveness_watcher（v2 替代 timeout_watcher，L09 升级到分状态阈值）
- 输入：`watch_loop(callback, interval_seconds)` + 配置阈值
- 输出：
  - PID 失活 + transcript stale → append `SessionDead`
  - 仍活但事件/transcript 静默 ≥ 状态阈值 → append `TimeoutSuspect`（raw 带 last_event_kind/threshold_bucket/threshold_sec）
- 阈值按 `last_event_kind` 分桶（教训 L09，避免长 tool 误判）：
  - `Notification / Stop / SubagentStop` → 5 分钟（等用户 / 等下一回合）
  - `PreToolUse` → 15 分钟（长 tool 容忍）+ transcript mtime ≤ 60s 视为活，跳过报警
  - `PostToolUse / Heartbeat / SessionStart / 其它` → 10 分钟
  - `liveness_per_state_timeout.enabled=False` → 退化到旧 `timeout_minutes` 一刀切（kill switch）
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

## backend.decision_log（v3 新增，L12）
- 职责：推送决策 trace 落盘，让 dashboard 能看到"为什么这条推了 / 没推"
- 输入：`append(session_id, event_type, decision, reason, policy, extra)`
- 输出：环形 jsonl `data/push_decisions.jsonl`，按 sid trim 上限 50 条 / 文件累计 1000 行触发整体重写
- 字段：`{ts, session_id, event, decision: "push"|"drop"|"scheduled"|"merged", reason, policy}`
- 调用方：
  - `notify_filter.should_notify` 每个 return 处自动 append（policy="filter"）
  - `notify_policy.NotifyDispatcher._trace` helper（policy="off" / "silence:N" / "silence"）
- 复用入口：
  - `decision_log.append(sid, ev_type, decision, reason, policy)` 写
  - `decision_log.recent_for(sid, limit=5)` 给 list_sessions 注入用（最新在前）
  - `decision_log.all_for(sid, limit=50)` 给 `/api/sessions/{sid}/decisions` endpoint 用
- 依赖：backend.config（DATA_DIR）+ 标准库 fcntl
- 落盘开销：每条记录 ~120 字节；50 sessions × 50 条 = 25 KB 上限

## backend.app
- 入口：`python -m backend.app`（uvicorn 127.0.0.1:8787）
- 路由：见 `docs/plan/0508-2029-design.md §3.2`
- 依赖：fastapi + uvicorn + 上述 4 个模块

## scripts.hook-notify
- 入口：被 Claude Code 调起，stdin = JSON
- 行为：写本地 jsonl（兜底）+ 异步 POST 到后端
- 依赖：纯标准库 + system `curl`（保证 Claude 启动期可用）
- 复用入口：`scripts/hook-notify.py [--heartbeat]`

## scripts.install-hooks
- 入口：`python scripts/install-hooks.py [--uninstall|--dry-run]`
- 行为：把 4 条 hook 注入 `~/.claude/settings.json`，备份原文件
- 依赖：标准库

## frontend (vanilla SPA)
- 入口：`frontend/index.html`，由后端 StaticFiles 挂载
- 模块：
  - `api.js` — REST + WebSocket 客户端（含 L12 `listDecisions(sid)`）
  - `format.js` — 时间相对化、状态徽章颜色映射
  - `app.js` — 主控制器（session 网格 / 抽屉 / 配置弹窗 / 测试按钮 / L12 trace 行 + modal）
  - `styles.css` — 单文件样式（`.push-trace` / `.trace-row` 等）
- 卡片底部 L12 trace 行：灰字 11px 显示最近 3 条决策，点击展开 modal 看 50 条
- 不引入任何 npm 依赖

## 复用判定
均尚未在第二个项目复用。**若将来在另一个项目复用**：按 §6 升级到 `~/Documents/00-Tian/AI/modules/`。
