<!-- purpose: claude-notify 模块清单（按 §6 模块化登记） -->

# modules

创建时间: 2026-05-08 20:29:45
更新时间: 2026-05-09 11:20:00

## backend.config
- 输入：磁盘 `data/config.json`，运行时 patch dict
- 输出：合并后的配置 dict / 脱敏 public 视图
- 依赖：标准库
- 复用入口：`from backend import config; config.load() / config.save(patch) / config.public_view(cfg)`

## backend.event_store
- 输入：`append_event(evt)` / 磁盘 `data/events.jsonl`
- 输出：聚合后的 session 列表 / 事件流 / 规范化的事件 dict
- 依赖：标准库（fcntl）
- 复用入口：
  - `event_store.append_event(evt)` 跨进程安全 append
  - `event_store.list_sessions(active_window_minutes)` 返回带 status 的 session 列表
  - `event_store.read_events_for(session_id, limit)` 按 session 过滤事件
  - `event_store.normalize_incoming(payload)` 把 hook payload 规范化

## backend.feishu
- 输入：事件 dict + `data/config.json` 中的 webhook/secret
- 输出：HTTP POST 飞书 webhook，返回 `{ok, reason, response}`
- 依赖：httpx
- 复用入口：`await feishu.send_event(evt)` / `await feishu.send_test()`
- 节流：SubagentStop 默认 30s 同 session 合并；Notification/Stop 不节流；Heartbeat 不推

## backend.liveness_watcher（v2 替代 timeout_watcher）
- 输入：`watch_loop(callback, interval_seconds)` + 配置阈值
- 输出：
  - PID 失活 + transcript stale → append `SessionDead`
  - 仍活但事件/transcript 静默 → append `TimeoutSuspect`
- 依赖：backend.config + backend.event_store
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
  - `api.js` — REST + WebSocket 客户端
  - `format.js` — 时间相对化、状态徽章颜色映射
  - `app.js` — 主控制器（session 网格 / 抽屉 / 配置弹窗 / 测试按钮）
  - `styles.css` — 单文件样式
- 不引入任何 npm 依赖

## 复用判定
均尚未在第二个项目复用。**若将来在另一个项目复用**：按 §6 升级到 `~/Documents/00-Tian/AI/modules/`。
