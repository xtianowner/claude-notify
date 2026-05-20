<!-- purpose: claude-notify 模块清单（按 §6 模块化登记） -->

# modules

创建时间: 2026-05-08 20:29:45
更新时间: 2026-05-20 11:30:00

## backend.config
- 输入：磁盘 `data/config.json`，运行时 patch dict
- 输出：合并后的配置 dict / 脱敏 public 视图
- 依赖：标准库
- 复用入口：`from backend import config; config.load() / config.save(patch) / config.public_view(cfg)`
- L14 per-session 静音：`set_session_mute(sid, until, scope, label)` / `clear_session_mute(sid)` / `prune_expired_session_mutes()`
  - 内部用 `_write_session_mutes` 绕过 save() 的深合并（dict set/del 语义需要整体替换）
- L45 / R22 `tab_reuse_mode`：`DEFAULT_TAB_REUSE_MODE = "focus_old"`，白名单 `TAB_REUSE_MODE_VALUES = {"focus_old", "user_choice"}`。`update_config` 校验非法值返 400
- **R50 / F6 `public_url` + `get_public_url(cfg, default=...)`**：DEFAULTS 新增 `"public_url": ""` 字段；helper 三级 fallback —— `cfg.public_url`（用户显式覆盖，rstrip `/`）> caller default（通常是 `app.state.default_public_url`）> 兜底 `http://127.0.0.1:8787`。供 feishu ↗ 链接和 osascript Chrome 匹配统一读。
- **R51 / F6 补 `_RUNTIME_DEFAULT_PUBLIC_URL` + `set_runtime_default_public_url(url)`**：R50 的 helper 默认参数固定值，feishu.py（plain 模块，拿不到 `app.state`）调用时实际 fallback 到 `127.0.0.1:8787` → `PORT=9000` 后飞书链接死。R51 改为 module-level 运行时变量，`app.py:main()` 启动后注入实际 `HOST:PORT`。feishu.py 代码不动，自动受益。
- **R52 / F6 启动姿势兜底**：R51 注入只在 `main()`，但 `python -m uvicorn backend.app:app` 直接拉 ASGI app 绕过 `main()` → README/update.py 推荐的就是这条路径。R52 把注入抽成 `backend/app.py:_inject_runtime_public_url(source)`，`lifespan` startup 也调一次（幂等）。无论 `python -m backend.app` 还是 uvicorn 直起都覆盖。
- **R52 补 `liveness_interval_seconds` 进 DEFAULTS**：`docs/configuration.md §5` 提到该字段但 DEFAULTS 漏，R52 补默认值 30。

## backend.event_store
- 输入：`append_event(evt)` / 磁盘 `data/events.jsonl`（hot）+ `data/archive/*.jsonl.gz`（冷归档，L13）
- 输出：聚合后的 session 列表 / 事件流 / 规范化的事件 dict
- 依赖：标准库（fcntl）+ backend.archival（gzip 读写）
- 复用入口：
  - `event_store.append_event(evt)` 跨进程安全 append（hot 文件）
  - `event_store.list_sessions(active_window_minutes, include_archive=False)` 返回带 status 的 session 列表
    - **R11 派生字段 `menu_detected: bool`（L35）**：Notification + `raw.menu_detected=true` → true；属"UI hint 字段"语义，不是 status 状态机字段。
    - **R51 / F8 清零分支**：`Stop / UserPromptSubmit / SessionEnd / SessionDead` 都清。原仅 Stop → 用户答完菜单后 🔥 红徽要等到下一次 Stop 才消，"已响应了还在催"。`PostToolUse` / `SubagentStop` 故意不清（≠ 用户响应）。
  - `event_store.derive_status(s)` 派生 status。L44：接受 session dict 或旧 last_event 字符串；dict 模式应用"活动证据胜过静态等待"规则——`last_event ∈ {Notification, TimeoutSuspect}` 且 `last_event_unix > _last_status_event_unix + 1s` → 翻 `running`（覆盖 AskUserQuestion 答题 / 权限 y/n / 长任务 suspect 后继续跑等 hook 不可达场景）。`_last_status_event_unix` 在 append 写入 last_event 时同步刷新
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
- **L41 / R16 渠道开关**：入口检查 `cfg.push_channels.feishu`，False 时直接 return `reason="channel_off"`（不发起 HTTP）。与浏览器渠道独立。
- **R50 / F6 ↗ 链接读 `get_public_url`**：`_format_text` 拼 `↗ {base}/o/{sid}` 时不再硬编码 `127.0.0.1:8787`，而是 `cfg_mod.get_public_url(cfg)` —— 跟随用户 `HOST` / `PORT` env 或 `cfg.public_url` 显式覆盖（反向代理 / Tailscale 场景）。
- **L51 / R26 外链格式**：尾行 ↗ dashboard 链接用 `http://127.0.0.1:8787/?s={sid}` 而非 `/#s={sid}`。`#` 在飞书 PC 客户端→ OS URL handler 链路上易被吞（shell 注释符 / link parser 截断），导致点击后 URL 整段丢失，Chrome 落到最近活跃 tab。query string 在所有链路稳定。前端 main() 入口把 `?s=` 转写为 `#s=` 走既有 hash 导航。
- **L52 / R27 外链精准跳转**：链接进一步改成 `http://127.0.0.1:8787/o/{sid}` 走 backend 专用 endpoint。R26 改 query 解决了 `#` 被吞但留下另一痛：Chrome 接收外部 URL 时只看最右匹配 host 的 tab，不向前扫描 → 前面位置的 dashboard tab 拿不到焦点。R27 让 backend 接到 `/o/{sid}` 时调 osascript 主动控制 Chrome（扫所有 window/tab 找 dashboard URL → activate + 切 window 前台），同时 WS 广播 `open_intent` 让 dashboard 自动 openDrawer(sid)。需要 Chrome Automation 权限（第一次弹"Python wants to control Chrome"），仅 macOS 有效；osascript 失败时退化为 HTML meta refresh 到 `/?s={sid}` 走原路径。

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
- **L41 / R16 push listener**：`set_push_listener(cb)` 由 `app.py:lifespan` 注入；dispatcher 5 个调 feishu 的位置前都先 `_emit_browser_push(evt, reason)` 给 WS 广播 `push_event`。listener 自带 `_globally_silenced(cfg)` 判断（muted / snooze_until 时不广播）。渠道解耦：feishu channel off 时仍广播给 browser，反之亦然。
- **R50 / F1 任一渠道 emit 就记 ts**：`_emit_browser_push` 改成返回 `bool`（True=真正派给了 listener）；`_record_stop_push(ev_type, sid, *, feishu_ok, browser_emitted)` 判定改为 `feishu_ok or browser_emitted`。修 browser-only 用户的 idle reminder 计数永远不刷新 + filler 反复弹的链路断裂。

## backend.app — PushBuffer + WS resume（L42 / R17 新增）
- 类 `PushBuffer`：环形缓冲存最近 60s 内的 `push_event` payload，cap 200；GC 按 `_unix` 字段（time.time() 写入）滚出；`since_iso(iso) → list` 返回严格大于 since_unix 的条目；`parse_iso` 失败回退 0.0 时直接返 `[]`（防伪 since_ts 灌历史）
- `_on_push_for_browser`：每次广播前同步 `push_buffer.add(payload_with_unix)` + 写 `clients=N` 诊断日志，再 strip `_unix` 后 `hub.broadcast`
- WS endpoint `/ws`：接受 `?since_ts=<ISO>` query；握手后从 buffer replay missed；客户端不带 since_ts 时不灌历史（保持原行为）
- **R50 / F5 `open_intent` 也入 buffer**：`/o/{sid}` 路径接到飞书 ↗ 请求后，除 `hub.broadcast` 还先 `push_buffer.add({"type":"open_intent","sid":sid,"_unix":time.time()})`。覆盖 Chrome Memory Saver 把 dashboard tab 休眠的场景 —— osascript 唤前台 → tab 重连 WS 走 `since_ts` replay → buffer 里有 open_intent → drawer 正常打开。
- 测试：`PushBuffer` 单元 5 case + GC 通过；e2e 4 连接场景（无 since / past since / future since / garbage since）

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

## backend.desktop_bridge（R30+ 新增，macOS only）
- 职责：macOS Accessibility API 轮询 `/Applications/Claude.app`，把侧栏 Recents 列表的状态字（`Running` / `Idle` / `Awaiting input` / ...）合成为 SessionStart / Stop / Notification / SessionEnd 事件，复用 `/api/event` 链路
- 关键演进：
  - R30 首版（"找窗口内 Stop 按钮"方案）
  - R33 重大重写 → 改读 sidebar Recents（Electron 默认不暴露 web DOM 到 AX 树，需 `AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)` 唤醒 Chromium a11y）
  - R34 `→ Desktop` 按钮：dashboard 卡片点击调 `AXUIElementPerformAction(button, "AXPress")` 模拟点击 Claude.app 侧栏对应 session + `open -a Claude` 拉前台
  - R35-R38 顶部 Chat/Cowork/Code 三 tab 通过 `AXARIACurrent='page'` 判定激活，bridge **只采 Code 和 Cowork**，Chat tab 完全不采（用户私聊不污染 dashboard）
  - R39 mark-dead 跨重启持久化：backend 重启时扫 `events.jsonl` 把"最后一条 status-changing event = SessionDead + reason=user_marked"的 desktop_app sid 注入 `bridge._forgotten`
  - R40-R42 `WINDOW_DEAD_TIMEOUT_SEC` 60s→5s（Claude.app 内删 session 后 dashboard 5s 内消失）+ 与 hidden_sessions 集成
  - R43-R47 desktop_embedded 字段 + AX/hook 双视角合并 + 段去重
  - R48-R49 desktop_embedded latest-wins，修 `--resume` 跨环境后误标（见 `backend.event_store.list_sessions`）
- 配置（cfg.desktop_bridge）：`enabled` (默认 false) / `poll_interval_seconds` (默认 1.0) / `waiting_debounce_seconds` (默认 1.5)
- 启用要求：① `pip install -r backend/requirements-desktop.txt`（pyobjc 的 ApplicationServices / HIServices）② 系统设置 → 隐私与安全 → 辅助功能 加 backend 运行的 python 解释器
- 复用入口：`desktop_bridge.DesktopBridge(post_event, cfg).start()`；CLI 测试入口 `python3 -m backend.desktop_bridge`
- 诊断 endpoint：`GET /api/desktop-bridge/status`（R31）；离线 dump 工具 `scripts/desktop_ax_dump.py`
- 重要限制（详见 `docs/desktop-bridge/design.md` §6）：UI 改版即可能 break；启发式 keyword 在 `RUNNING_KEYWORDS` / `BLOCKED_KEYWORDS` 常量里，需对 Claude.app 升级保持响应

## backend.idle_reminder（v2 雏形，R50 / F2 加持久化）
- 职责：每个 sid 在每回合（Stop push 起）的 idle reminder 计数（默认 3 次配额，节奏 `notify_filter.notif_idle_reminder_minutes=[15,45]`）
- 旧实现：模块级 `_counts: OrderedDict` 纯内存，重启 backend 即清零 → 用户已收到 reminder #1 后重启，下条 filler 又被当 #1 推一次
- **R50 / F2 持久化**：落盘 `data/idle_reminder.json`，schema `{"<sid>": {"count": int, "last_at": iso8601}}`；lazy load + `threading.RLock` + tmp+rename 原子写（与 `backend.notes` / `backend.hidden_sessions` 同模式）
- 接口签名保持不变（向下兼容 `notify_filter` 调用方）：
  - `get_count(sid) -> int`
  - `mark_sent(sid) -> int`（触发落盘）
  - `reset(sid)`（触发落盘，由 `notify_policy._record_stop_push` 在新 Stop push 时调用）
  - `snapshot() -> dict`
  - `_reload_for_tests()`（内部钩子，回归测试用）
- LRU 容量：`_MAX_ENTRIES = 512`（从 1024 降，落盘后控文件体积；512 × ~80 byte ≈ 40 KB）

## backend.hidden_sessions（R40 新增）
- 职责：`data/hidden_sessions.json` 持久化用户主动删除的 session_id 集合
- 与 mark-dead 的区别：mark-dead 仅改 `status=dead`（卡片仍在 dashboard）；hidden = `list_sessions` 完全跳过（dashboard 看不到），但 `events.jsonl` 内事件保留作审计/debug
- schema：`{"<sid>": {"deleted_at": iso8601, "name": "...", "source": "..."}}`
- 复用入口：`hidden_sessions.is_hidden(sid) / hidden_ids() / add(sid, name, source) / remove(sid)`；cache 失效用 `clear_cache()`
- REST endpoint（在 backend.app）：`DELETE /api/sessions/{sid}` → `add`；`POST /api/sessions/{sid}/restore` → `remove`；WS 广播 `session_deleted` / `session_restored` 多端实时同步
- 并发：`threading.RLock` + tmp file + rename 原子写

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
- L49 / R24 入参校验加固：
  - `POST /api/event`：必须是 JSON dict 且带 `session_id`（B1），非法 JSON 返 400 而非 500（B2）
  - `POST /api/config`：非法 JSON 返 400；`tab_reuse_mode` 走白名单
  - `GET /api/health/setup` `hooks_expected` 列表 7 项（与 `scripts/install-hooks.py:EVENTS_NORMAL` 同步，B3）
- **L52 / R27 `/o/{sid}` Chrome tab 精准切换 endpoint**：飞书 ↗ 链接入口。`_osascript_focus_chrome_dashboard()` 调 `tell application "Google Chrome"` 扫所有 window/tab 找匹配 dashboard URL 的 tab → `set active tab index + set index of w to 1 + activate`（不走 System Events，避开 L02 Accessibility 坑），2s 超时；同时 `await hub.broadcast({type:"open_intent", sid})` 让 dashboard WS handler 自动 openDrawer(sid)。成功返回轻量"已切到 dashboard"HTML（含 `setTimeout window.close()` 尝试，多半被 Chrome 拒绝），失败返回 `<meta refresh url=/?s={sid}>` 兜底走原仲裁路径。必须在 `_mount_frontend()` 之前注册（StaticFiles mount 在 `/` 是 catch-all）。需要 Chrome Automation 权限（首次弹 macOS 权限框）。
- **L53 / R28 osascript 排除 `/o/` 自指 bug**：原匹配条件 `URL starts with "http://127.0.0.1:8787/"` 会把"飞书链接刚打开的 `/o/<sid>` 那个 tab 自身"误当成 dashboard tab activate，导致空状态（无任何 dashboard tab）时 fallback 不触发、dashboard 永不加载。修法：匹配条件加 `and (not (theURL starts with "http://127.0.0.1:8787/o/"))`，让自身永远不匹配，空状态下正确返回 NOT_FOUND → 走 meta refresh fallback。
- 依赖：fastapi + uvicorn + 上述 4 个模块

## scripts.hook-notify
- 入口：被 Claude Code 调起，stdin = JSON
- 行为：写本地 jsonl（兜底）+ 异步 POST 到后端
- **R11 菜单识别（L33）**：`_detect_menu_prompt(transcript_path)` 读 transcript 末 20 行，子串匹配 `❯\s*\d+\.|Type something`，命中 → 在 evt 上挂 `raw.menu_detected=true`。filter 侧据此走 bypass 通道（不重新读 transcript，职责分离）
- 依赖：纯标准库 + system `curl`（保证 Claude 启动期可用）
- 复用入口：`scripts/hook-notify.py [--heartbeat]`

## scripts.install-hooks
- 入口：`python scripts/install-hooks.py [--uninstall|--dry-run]`
- 行为：把 7 条 hook 注入 `~/.claude/settings.json`，备份原文件
- `EVENTS_NORMAL = [Notification, Stop, SubagentStop, SessionStart, SessionEnd, UserPromptSubmit]`（事件型）+ `PreToolUse`（heartbeat）。L43：UserPromptSubmit 覆盖"终端打字回复"的状态翻转；L44：AskUserQuestion / 权限请求 y/n 等 hook 不可达场景靠 `derive_status` 内"活动证据胜过静态等待"规则兜底翻 running
- 幂等：按 `hook-notify.py` 关键字识别自家 hook → 已存在则更新 command，不破坏用户其它 hook
- 依赖：标准库

## frontend (vanilla SPA)
- 入口：`frontend/index.html`，由后端 StaticFiles 挂载
- 模块：
  - `api.js` — REST + WebSocket 客户端。L42 / R17：`connectWS` 接受 `getLastPushTs` 回调，重连时把 `lastPushTs` 编码进 `?since_ts=` query 让后端补发漏的 push_event
  - `format.js` — 时间相对化、状态徽章颜色映射；`stripMd` 卡片摘要清理 markdown 控制符
  - `app.js` — 主控制器（session 网格 / 抽屉 / 配置弹窗 / 测试按钮）。L42 / R17：`state._lastPushTs` 在 `onWsEnvelope` 收到 push_event 时单调更新；`connectWS` 调用注入 `getLastPushTs`。L45 / R20+R22：BroadcastChannel `claude-notify-tab` + 250ms PING_FOCUS/PONG 仲裁，飞书 `#s=<sid>` 链接复用已有 dashboard tab；按 `config.tab_reuse_mode` 分支：`focus_old`（默认，新 tab `#tab-auto` 自动消失提示）/ `user_choice`（新 tab `#tab-takeover` 两按钮）；mode 缓存在 `localStorage.cn.tabReuseMode`，loadConfig 完成时回写真值。L46 / R21：visibilitychange→visible 时立即 `loadSessions()` 修后台节流导致的 stale state；L48 / R23：`onHashChange` + 仲裁同 hash 路径都 await `loadSessions()` 再 openDrawer，确保通知/链接跳转打开 drawer 用最新状态；L49 / R24·B6：`loadSessions` 加 module-level `_loadSessionsInflight` promise，多触发源并发时复用同一 fetch（visibility + hashchange + 兜底定时 + WS upsert fallback 叠加时只发 1 次）；L51 / R26：`main()` 入口把入站 `?s=<sid>` 用 `history.replaceState` 转写为 `#s=<sid>`，配合 backend 把飞书外链改成 query string（避免 `#` 在 OS URL handler 链路被吞）；L52 / R27：`onWsEnvelope` 加 `open_intent` 处理 —— backend `/o/{sid}` 接到飞书链接请求后通过 WS 推 `{type:"open_intent",sid}` 给所有 dashboard tab，触发 hash 跳转 + openDrawer；解 Chrome 不向前扫匹配 tab 的痛点（osascript 在 backend 侧切 Chrome window/tab）。
    - **R45 / R51 合并视角实现细节**：`mergeDesktopViews(items)` 现在直接 mutate `state.sessions[ax]` 对象（写 `merged_hook` 字段 + 补 hook 的 cwd/tty/transcript 等到 ax 上）。R45 首版只构造临时 merged 副本不写回 state.sessions → R50 写的 drawer 镜像逻辑实际拿不到 merged_hook，删除/mark-dead 仅删 ax → 幽灵卡复活（R50 假动作）。R51 mutate + 合并条件不满足时 `delete s.merged_hook` 清旧引用。
    - **R51 / F3-F7 `collectMergedSids(sid)` helper**：合并卡的两个 sid 凡是按 sid 持久化的操作都必须镜像。drawer 删除 / mark-dead / alias 编辑 / 静音 / 解除静音 5 处 handler 统一调 `for (s of collectMergedSids(sid)) await api.xxx(s, ...)`。非合并卡返 `[sid]`，无副作用。
    - **R52 / F3-F7 grouped 视图修补**：R51 的 mutate 只在 list 视图 `renderListWithSections` 跑，grouped 视图 `renderGroupedHTML(visible)` 不跑 → `collectMergedSids` 在 grouped 视图下拿不到 `merged_hook` → 删除等操作只镜像一面。R52 新增 `applyMergedHookToState(sessions)` 提前到 `renderSessions()` 顶部跑，覆盖 list / grouped 两个视图入口；新增 `filterGroupedVisible(visible)` 给 grouped 视图也剔 hook 卡（R47 的 grouped 等价补丁）。
  - `notes.js` — 记事本面板（L28/L29/L31）
  - **`notify.js`（L41 / R16，L42 / R17 加固，L50 / R25 SW 化）— 浏览器桌面通知**：监听 WS `push_event` → 调 Notification API；顶部"启用桌面通知"条带管理 permission 授权；channel 开关跟随 `state.config.push_channels.browser`。L42 / R17：tag 改 `sid|ev_type|ts` 让多事件能堆栈（而非互相覆盖）；`(sid,ev_type,ts)` LRU dedupe Set（200 条）防 WS 补发重弹；入口 `console.info("[notify] push_event recv", {...})` + skip 原因日志（用户复现"飞书有浏览器没"时打开 console 立即可定位）。L50 / R25：`bindPermBar` 时尝试 `navigator.serviceWorker.register("./sw.js")`，注册成功后 `onPushEvent` 优先 `registration.showNotification(title, {data:{sid,ev_type,ts}, ...})`；SW 不支持/注册失败 fallback 到 `new Notification(...)` 旧路径
  - **`sw.js`（L50 / R25）— Service Worker，OS Notification 跨 window 切前台**：root scope，`notificationclick` 里 `clients.matchAll({type:'window',includeUncontrolled:true})` 扫同源 window client（origin = dashboard `127.0.0.1:<port>`，天生过滤其它 localhost 服务），选 visible 优先，`postMessage({type:'NOTIFICATION_CLICK',sid})` 后 `client.focus()`（SW 上下文可跨 window）；没现成 client → `clients.openWindow(scope+'#s='+sid)`。`install` skipWaiting + `activate` claim()，新版立即接管。前端在 main 启动注册 `navigator.serviceWorker.addEventListener('message', ...)` 收 NOTIFICATION_CLICK 后走 hash 跳转复用 `onHashChange`（带 loadSessions + openDrawer）
  - `styles.css` — 单文件样式
- L38（2026-05-11）：卡片底部 trace 行 + modal 已删除（开发者 debug 工具，对最终用户是噪音）。后端 `data/push_decisions.jsonl` 仍写，`/api/sessions/{sid}/decisions` 仍可 curl。
- 不引入任何 npm 依赖

## 复用判定
均尚未在第二个项目复用。**若将来在另一个项目复用**：按 §6 升级到 `~/Documents/00-Tian/AI/modules/`。
