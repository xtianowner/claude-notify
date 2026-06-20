# Changelog

> 用户视角的功能演进。每个 round 对应一个 git commit，可在 `git log` 找到完整 diff。

## R55 · 修「→ 终端」跨 Space 激活跳错（误切到顶层别的 app） (2026-06-20)

`→ 终端` focus：当多个终端窗口分布在不同 macOS Space（桌面）时，旧逻辑 `set frontmost of w to true` → `activate` 只把"当前 Space 已有的那个终端窗口"拉前，**不切到目标窗口所在 Space** → 用户停在当前桌面、看到的是该桌面顶层的别的 app（如 Telegram），误以为"跳转跳错了"。
**修法**（`backend/app.py` `_build_focus_script`）：先把目标窗口提到该 app 窗口栈最前（Terminal `set index of w to 1`；iTerm2 补 `select theWindow`）→ 再 `activate`（触发跨 Space 切到目标窗口所在桌面）→ activate 后再 re-assert 一次目标窗口在最前（防落到当前 Space 的另一个终端窗口）。
**诊断**：iTerm2 未装（该分支 `-1728` 被 `try` 吃掉）、tty 唯一命中、前端无乱跳 fallback —— 三者均排除后定位到激活逻辑。用户实测确认修复。

## R54 · 采纳 StopFailure 推送 + events.jsonl 周期归档 (2026-06-20)

接 R53 适配排查的两个 follow-up：采纳 R53 deferred 的一个 opportunity + 修一个归档 gap。

### StopFailure：失败回合也即时推送（多文件全链路）

CC 2.1.x 的 `StopFailure`（回合因 API 错误结束）**不触发 `Stop`**，dashboard 会静默卡在"运行中"直到 liveness 超时才标 dead。本轮全链路接入：
- `install-hooks.py` 注册第 8 条 hook `StopFailure`（**需重跑 `install-hooks.py` 生效**）；
- `hook-notify.py` / `sources.py` 摘要成「任务失败：<error>」，透传 `error`/`error_details`；
- `event_store.derive_status` → `idle`（回合终结，不再卡 running）+ 按 Stop 清菜单红徽；
- `config.notify_policy` 默认 `immediate`；`notify_filter._ALWAYS_TRUE` 收录（否则落 `unknown_event` 被吞）—— 仍尊重 session 静音 / quiet hours，不进穿透白名单；
- `feishu` 渲染 `🔴 任务失败`。
全链路 offline 验证通过（normalize→status→policy→filter→feishu）。不想要可在配置面板把 `StopFailure` 调 `off`。

### events.jsonl 周期归档 — `backend/app.py` / `backend/config.py`

此前归档只在 backend 启动时跑一次（`lifespan`），长驻不重启的 backend events.jsonl 会无界增长（实测涨到 185MB）→ `list_sessions` 每次全量读 hot 文件变慢。新增 `_archive_loop` 周期任务（`archival.check_interval_seconds`，默认 600s），到点调同一个 `archive_if_needed()`（持 LOCK_EX 安全重写，未超阈值只是一次 stat）。`enabled=False` 不起任务；lifespan 退出时 cancel。验证：0.05s 间隔 spy 测试确认按时 tick + cancel 干净。

## R53 · 适配 Claude Code 2.1.183：修 `source` 字段语义冲突 + hook 实机漂移 (2026-06-20)

随本机 Claude Code 升到 **2.1.183** 做的一轮适配排查（对二进制 Zod schema 取证 + 多 agent 对抗式验证）。**结论：无 BREAKING** —— 项目 hook 的全部事件在 2.1.183 仍存在，settings.json 三级格式、`effort{level}`、`last_assistant_message`、`notification_type==permission_prompt` 全部仍匹配当前 schema。本轮修掉 1 个真 bug + 1 个配置漂移。

### `source` 字段语义冲突（真 bug）— `scripts/hook-notify.py` / `backend/sources.py` / `backend/event_store.py`

CC 的 `SessionStart` 负载带顶层 `source` ∈ `{startup,resume,clear,compact}`（= 会话**启动原因**），`hook-notify.py` 之前 `payload.get("source") or "claude_code"` 把它当成 claude-notify 自己的**渠道** `source`，污染了 session 的渠道归属 → 前端「终端 / Desktop」分段错乱、`desktop_embedded` 兜底被跳过（实测 2.1.183 下 **10/14** session 中招）。
**修法**：① 渠道按事件名硬钉 `claude_code`，启动原因另存 `session_start_source`（按事件名 gate，未来枚举扩张也不漏）；② 新增单一真相函数 `sources.coerce_channel_source()`，在入口 `normalize`（新事件）+ `list_sessions` 建卡（旧事件 replay）两处把被污染的历史 `source` 纠回 `claude_code`。详见 [LESSONS.md L54](LESSONS.md)。
**验证**：实机重启 replay 后 15/15 session 全归 `claude_code`，污染清零；合成 `source=compact` 事件归段正确。

### hook 实机漂移 — `~/.claude/settings.json`（重跑 install-hooks 收敛，代码无改）

- 实机 settings.json 缺 `UserPromptSubmit`（install-hooks.py 早已注册但实机漂移；缺它会导致回话后 dashboard 状态翻不回、🔥 红徽不清，见 L43）→ 重跑 `python3 scripts/install-hooks.py` 补回（幂等 + 自动备份）。
- settings.json 与 settings.local.json **双份注册**同 6 条 hook → 每个事件 POST 双触发。去重 settings.local.json（仅删自家 hook，保留 `permissions`），统一由 settings.json 单一持有 7 条。

> 后续 opportunity（CC 2.1.x 二进制确认存在）：`StopFailure`（API 错误结束的回合也推送）**已于 R54 采纳**；仍未采纳：`PreCompact`+`PostCompact`（压缩态显示 + `compact_summary` 上下文）/ `Notification.notification_type=idle_prompt`（结构化等输入，替代脆弱的 transcript 正则）/ `SubagentStart`+`agent_id`/`background_tasks[]`（细粒度子 agent 追踪）。

## R52 · 修 R51 残余的"启动姿势依赖"与"视图依赖"半失效 (2026-05-20)

R51 落地后第三轮独立 audit 发现两个新隐蔽 bug：F6 在 `uvicorn backend.app:app` 启动姿势下失效（README/update.py 推荐的就是这条路径，绕过 `main()`）；F3/F4/F7 在「按项目」分组视图下退化（合并卡 mutate 只在 list 视图入口跑）。本轮全部修透 + 配套清理 3 处文档错信息。所有修复主 agent 独立端到端验证（2+7 case 全过，不复用 subagent 留下的脚本）。

### F6 启动姿势注入兜底 — `backend/app.py`

**症状**：R51 把 `set_runtime_default_public_url(...)` 注入放在 `main()`，但 README:235 / `scripts/update.py:88,161,166` / `docs/user-guide.md:81` 全部推荐 `python -m uvicorn backend.app:app` 启动 — 直接拉 ASGI app **绕过 main()**。`_RUNTIME_DEFAULT_PUBLIC_URL` 维持初值 `http://127.0.0.1:8787` → 用户 `PORT=9000` 后飞书 ↗ 链接仍指向 8787。
**修法**：抽 helper `_inject_runtime_public_url(source)`，`lifespan` startup 也调一次（两条启动路径都覆盖；幂等）。`main()` 路径保留显式调用便于诊断启动姿势。HOST=`0.0.0.0`/`::` 退化 `127.0.0.1`（浏览器视角）。
**主 agent 独立验证**：模拟 `PORT=9000 HOST=127.0.0.1` lifespan-only 启动 + `PORT=7000 HOST=0.0.0.0` 退化两个 case，全过。

### F3/F4/F7 提前到 `loadSessions` 路径 — `frontend/app.js`

**症状**：R51 让 `mergeDesktopViews` mutate `state.sessions[ax]`，但只在 `renderListWithSections`（list 视图）入口跑。grouped 视图走 `renderGroupedHTML(visible)` 不跑 mergeDesktopViews → `collectMergedSids` 拿不到 `merged_hook` → drawer 5 处镜像操作（删除/mark-dead/alias/mute/unmute）只删一面 → 幽灵卡复活。
**修法**：
- 新增 `applyMergedHookToState(sessions)` — 按段（desktop-code / desktop-cowork）跑 `mergeDesktopViews` 的 mutate 副作用。
- `renderSessions()` 顶部统一调一次，覆盖所有进入渲染的路径（loadSessions / WS upsert / 30s 定时刷新等）。
- 新增 `filterGroupedVisible(visible)` — grouped 视图段内剔 hook 卡（R47 在 grouped 路径的等价补丁）。
- list 视图行为完全不变（既有 `mergeDesktopViews` 调用 + filterEmbeddedWhenAxPresent 全保留）。

**主 agent 独立验证**：从 frontend/app.js 抠出真实函数跑 7 case — grouped 入口 ax.merged_hook === hook / cwd 补齐 / collectMergedSids 双 sid / grouped 渲染剔 hook / hook 翻 ended 清旧引用 / 退化单 sid，全过。

### install-hooks docstring 数字错 — `scripts/install-hooks.py`

docstring 写"4 条 hook"，实际 7 条（事件型 6 + 心跳型 1）。文案对齐 EVENTS_NORMAL + 心跳型拆解。

### `dead_threshold_minutes` 默认值文档对齐 — `docs/user-guide.md:70`

R38 已把默认值从 30 升到 180min，但 user-guide §一 6 种 status 表格里仍写"transcript 30 分钟不动 → dead"。改为"transcript 静默超过 `dead_threshold_minutes`（默认 180min，R38 从 30 调大）"。

### 飞书链接格式文档对齐 — `docs/user-guide.md:113`

R27/L52 已把链接改成 `{public_url}/o/{sid}`（backend 专用 endpoint，osascript 切 Chrome tab），但 user-guide §二.飞书 ↗ 链接段仍写 `↗ http://127.0.0.1:8787/#s=<sid>`。改为 `↗ {public_url}/o/{sid}` + 补 R27/L52 原理引用。

### dashboard trace UI 自相矛盾段对齐 — `README.md:293,339`

L38 已删 dashboard 内的 trace UI（开发者 debug 工具，对最终用户是噪音），但 README 故障排查段和"看历史事件 / debug"段都还写"卡片底部 trace 行点开"。改为 `tail data/push_decisions.jsonl` 或 `curl /api/sessions/<sid>/decisions`。

### `liveness_interval_seconds` 补进 DEFAULTS — `backend/config.py:166`

`docs/configuration.md:161` 把 `liveness_interval_seconds=30` 列为字段，标"需要重启 backend 才生效"。但 `backend/config.py:DEFAULTS` 没收 → dashboard config UI 没法把它当合法字段处理，用户被迫改 JSON。R52 补进 DEFAULTS。

---

## R51 · 真修 R50 假动作 + 残余功能缺口 (2026-05-20)

R50 上线后第二轮 subagent 审计发现 **F3/F4 假动作**：我以为修了合并卡幽灵卡，实际由于 `mergeDesktopViews` 不 mutate `state.sessions`、`merged_hook` 字段只在临时 merged 副本上，**drawer handler 通过 `state.sessions.find()` 拿到的 ax 对象永远没有 merged_hook → 镜像循环只跑 1 次 → 删除/mark-dead 仍只删 1 个 sid**。同时 F6 半修：osascript 路径自适应了 HOST/PORT，但 feishu.py 拼链接没拿到真实 default，飞书消息里的 ↗ 仍是 127.0.0.1:8787。

本轮全部修复都由主 agent **独立端到端验证**（不只信 subagent 报告），三次 7+4+7 case 测试全过。

### F3/F4/F7 · `mergeDesktopViews` mutate ax 对象 — `frontend/app.js`

**根因**：R45 的 `mergeDesktopViews(items)` 接收渲染期临时数组，返回 `items.filter(...).concat([merged])`，merged 是 `{...ax}` 浅拷贝，不写回 `state.sessions`。
**修法**：
- `mergeDesktopViews` 改为直接 `ax.merged_hook = hook` + 字段补齐到 ax 对象（与 state.sessions[i] 同对象引用）。渲染层只 `items.filter(s => s !== hook)`。
- 合并条件不满足时显式 `delete s.merged_hook` 清旧引用，防止 hook 翻 dead 后 drawer 镜像到一个无关 sid。
- 新增 `collectMergedSids(sid)` helper：从 `state.sessions.find()` 拿 `merged_hook.session_id`，返回 `[ax_sid, hook_sid]` 或 `[sid]`。
- **F7**：drawer 删除 / mark-dead（R50 已写但失效）/ alias 编辑 / 静音 / 解除静音 5 处 handler 全部改为 `for (s of collectMergedSids(sid)) await api.xxx(s, ...)`。

**主 agent 独立验证** 7 case：mutate 真生效 + cwd 等字段补齐 + 合并条件不满足时清掉 + collectMergedSids 双 sid / 单 sid / 非合并卡 三种返回都对。

### F6 补齐 · feishu 拼链接拿到真实 HOST/PORT — `backend/{config,app}.py`

**根因**：R50 改了 osascript Chrome 匹配的 `_build_chrome_focus_script(base_url)`（拿 `app.state.default_public_url` 作 default），但 `feishu.py:271` 调 `get_public_url(cfg)` 没传 default → fallback 到 helper 内置的 `127.0.0.1:8787`。用户 `PORT=9000` 后飞书消息里链接死。
**修法**：
- `backend/config.py` 加 module-level `_RUNTIME_DEFAULT_PUBLIC_URL` + setter `set_runtime_default_public_url(url)`。
- `get_public_url(cfg, default=None)` 三级 fallback：`cfg.public_url` > caller default > runtime default > 兜底常量。
- `backend/app.py:main()` 在算出 `default_public_url` 之后调 `cfg_mod.set_runtime_default_public_url(...)` 注入。
- `feishu.py` **不改**（继续 `get_public_url(cfg)`），自动从 module-level 拿到真实端口。

**主 agent 独立验证** 4 case：默认 / `set_runtime_default_public_url("...9000")` 之后 / `cfg.public_url` 覆盖 / caller default 覆盖，全部按优先级正确返回。

### F8 · `menu_detected` 紧急徽多分支清零 — `backend/event_store.py:651`

**根因**：只在 `Stop` 事件清 `menu_detected = False`。用户答完菜单（hook 推 `UserPromptSubmit`）后，🔥 持续亮到 Claude 下一次 Stop。
**修法**：清零分支扩展到 `Stop / UserPromptSubmit / SessionEnd / SessionDead`。`PostToolUse` / `SubagentStop` 故意不清（≠ 用户响应）。

**主 agent 独立验证** 7 case：通过 `list_sessions` 真实事件流 `Notification(menu)+X` 7 个 X，每个 case 的 `menu_detected` 翻转都按预期。

---

## R50 · 核心功能可靠性修复 — 消息提醒 + 跳转链路补齐 (2026-05-20)

针对"如果连消息提醒/session 跳转都不可靠，再做安全也没意义"的核心反馈，集中修了 6 个端到端链路真痛点。功能审计结论与每个修复点的来由见 `00TEM/NowTodo/review.md`。

### F1 · browser-only 用户的 idle dedup 不再失效 — `backend/notify_policy.py`

旧逻辑：`_record_stop_push` 只在 `feishu.send_event` 返回 `{ok:True}` 时记 `last_stop_pushed_at` + reset idle reminder 计数。
**症状**：用户只配浏览器通知（不配飞书 webhook）→ feishu 返 `{ok:False, reason:"no_webhook"}` → 时间戳不更新 → 之后 Claude 的每条 "waiting for your input" filler Notification 都被当独立事件推 → 浏览器反复弹同一句通知 + reminder 3 次配额完全失效。
**修法**：`_emit_browser_push` 改成返回 `bool`（True=实际派给了 listener），`_record_stop_push` 签名加 `feishu_ok` / `browser_emitted` 两个 kw-only 参数，"任一渠道实际 emit 就记 ts"。policy_off / quiet_hours / session_muted 在 `should_notify` 阶段早就被拦截，走不到这里，所以"任一 emit"是安全的。

### F2 · idle reminder 计数持久化 — `backend/idle_reminder.py`

旧实现：`_counts` 是模块级 `OrderedDict` 纯内存。
**症状**：backend 重启 → 计数清零 → 用户已收到 reminder #1 (15min) 后重启 backend，下一条 filler 进来 count=0 → 又被当 reminder #1 推一次。
**修法**：落盘 `data/idle_reminder.json`，schema `{"<sid>": {"count": int, "last_at": iso8601}}`，与 `hidden_sessions.py` 同模式（lazy load + RLock + tmp 写 + os.replace）。`get_count / mark_sent / reset / snapshot` 函数签名不变；`_MAX_ENTRIES` 从 1024 降到 512（落盘后控文件体积）。新增 `_reload_for_tests()` 内部钩子用于回归测试。

### F3 · 合并卡删除镜像第二个 sid — `frontend/app.js`

R45 合并视角卡 = AX sid (`dsk-*`) + `merged_hook.session_id` (uuid) 两个 sid。`delete_session` 旧路径只删一个 → 下次 `loadSessions` 时 R47 `filterEmbeddedWhenAxPresent` 见同段无 dsk-\* → 兜底返回原 items → hook uuid 卡重新冒出来（cwd 一样的"幽灵卡"）。
**修法**：`$drawerDelete` handler 在调 `api.deleteSession(sid)` 前从 `state.sessions` 查目标卡，若 `merged_hook.session_id` 存在就串行调两次。单 toast、单 closeDrawer、state 同时移除两个 sid。

### F4 · 合并卡 mark-dead 同步镜像 — `frontend/app.js`

与 F3 对称问题。旧路径只 mark 一个 sid → hook 视角继续 running → dashboard 分裂出"一张 dead + 一张活跃"。
**修法**：`$drawerMarkDead` handler 同样镜像调两次 `api.markDead`（mark-dead 对已 dead 的 sid 返 `{ok:True, already:True}`，幂等安全）。

### F5 · `open_intent` 入 push_buffer，覆盖 Chrome 休眠场景 — `backend/app.py:973`

旧路径：`/o/{sid}` 接到飞书 ↗ 请求后 osascript 唤前台 + `hub.broadcast({"type":"open_intent",...})`，但**不入 push_buffer**。
**症状**：Chrome Memory Saver 把 dashboard tab 休眠时 WS 已断；osascript 唤前台 → tab 重连 WS 走 `since_ts` replay → buffer 里没有 open_intent → drawer 不打开。
**修法**：`intent_payload` 打 `_unix` 字段先 `await push_buffer.add(...)` 再 `hub.broadcast(...)`（前端 buffer replay 时 strip 下划线开头字段后走原 WS handler，complete reuse 既有 envelope 处理）。

### F6 · 飞书 ↗ 链接 + osascript 自适应 HOST/PORT — `backend/{config,feishu,app}.py`

旧链路：`feishu.py:269` 和 `app.py:880-897` 硬编码 `http://127.0.0.1:8787/`。用户改 `PORT=9000` 或 `HOST=0.0.0.0` 或上反向代理 / Tailscale → ↗ 链接全断、osascript 扫不到 dashboard tab。
**修法**：
- `backend/config.py` DEFAULTS 加 `"public_url": ""`（空 = 自动推导）+ helper `get_public_url(cfg, default)` 三级 fallback（cfg.public_url > caller default > `http://127.0.0.1:8787`），统一 rstrip `/`。
- `backend/app.py:main()` 推导 `default_public_url`（HOST=0.0.0.0/:: 退化 127.0.0.1）写 `app.state`，供 osascript 路径每次 cfg.load 时读取。
- `backend/feishu.py:_format_text` ↗ 链接拼接读 `cfg_mod.get_public_url(cfg)`。
- `backend/app.py:_build_chrome_focus_script` 接 `base_url` 参数动态拼 AppleScript starts-with 前缀（含 `\` / `"` 转义防注入），自指排除前缀 `{base}/o/` 同步跟随。
- `docs/configuration.md` §11 加 `public_url` 字段行 + §11.1 何时该改；README.md §远程多机使用 段尾补段落。

### D1 · README 数据文件列表对齐 — `README.md` §数据/隐私

旧列表：`aliases.json / notes.json / decisions.jsonl`。
实际文件：`aliases.json / notes.md / push_decisions.jsonl / hidden_sessions.json / idle_reminder.json`。
同步修正 `docs/env.md` 数据落盘列表也加 `idle_reminder.json`。

---

## R29-R49 · Claude Desktop 桥接（macOS）

把 claude-notify 从"只监控 Claude Code CLI"扩展到"也能监控 Claude Desktop"。详细方案见 [docs/desktop-bridge/design.md](docs/desktop-bridge/design.md)（R29 baseline 设计，R30+ 实现细节已演进）。

### R49 · 撤回 R48 的 TTY 兜底，靠后端 latest-wins 准确归段 (2026-05-20)

R48 之后前端 `categorizeSession` 里临时加的「TTY 非空 → 强制归终端段」兜底有反作用：
session 4098acf8 原本在终端 `ttys000` 开过、之后用 `--resume` 在 Claude Desktop 接着跑，sticky 老 TTY 字段还在 → 兜底硬塞回终端段，但 `desktop_embedded=true` 让徽章仍是 Desktop · Code → 卡片出现"终端段 + Desktop · Code 徽章"的自相矛盾。

撤回 `frontend/app.js:categorizeSession` 的 TTY 检查。完全靠 R48 latest-wins 出来的 `desktop_embedded` 字段归段：

- 真终端 CLI（`desktop_embedded=false`）→ 终端段
- Desktop 嵌入 CLI / `dsk-*`（`desktop_embedded=true` 或 `source=desktop_app`）→ Desktop · Code 段
- 嵌入式 UUID 卡被 R47 段去重 filter 隐去，用户看到的是 N chat = N 张卡

### R48 · `desktop_embedded` latest-wins，修 `--resume` 跨环境后误标 (2026-05-18)

问题：用户在 Claude Desktop 起 session（events 写 `desktop_embedded=True`），后用 `claude --resume <sid>` 在真终端接着跑（hook 写 `False`）。旧逻辑「任何一条 event 标 True → 整张卡永远算嵌入」锁死 True，dashboard 一直挂 Desktop · Code 徽章。

实例：session `604890df` 历史 232 条 True / 133 条 False / 2 条 None，但用户最近一直在终端 `tty=/dev/ttys002` 跑（`claude_pid` 指向 nvm/claude），错把它当 Desktop 嵌入卡。

修：`backend/event_store.py:list_sessions` 改 latest-wins —— 取最新事件的 `desktop_embedded` 值（True 或 False），显式 `None`（老事件没该字段）不改变现状。

### R47 · Desktop 段去重 — N 个 chat = N 张卡 (2026-05-18)

之前 R45 只在"AX 1 张 + hook 1 张"恰好各 1 的情况合并；用户开 ≥2 chat 时，每个 chat 仍被算 2 张卡（AX 视角 + hook 嵌入式 CLI 视角），3 chat → dashboard 6 张卡，污染面板。

新规则（纯前端，`frontend/app.js`）：在 `desktop-code` / `desktop-cowork` 段，**只要有任何 dsk-\* 卡（AX 视角），就隐去所有同段的 embedded UUID 卡**。AX 桥拿到的 chat title 信息更对用户友好，hook 数据通过 R45 已经塞到 AX 卡的副字段里。

兜底：若该段完全没 dsk-\*（罕见，AX 桥失活），原样不过滤，避免段空掉。

### 配套修复（2026-05-18）

- **backend HOST/PORT env 支持**：`backend/app.py main()` 现在读 `HOST` / `PORT` 环境变量，对齐 README §高级用法（`PORT=9000 python -m backend.app`）和 §LAN 访问（`HOST=0.0.0.0`）承诺。先前 README 写了但代码没实现。
- **README desktop_bridge 启用说明修正**：dashboard config UI 并未暴露 `desktop_bridge.enabled` 控件，README 行 131 之前误导。改为明确指引"编辑 `data/config.json` 加该字段"，附 oneliner 命令。

### R45 · 同一会话两个视角自动合并 (2026-05-17)

同一个 Claude Desktop · Code 会话之前会显示两张卡（hook 视角 + AX bridge 视角），现合并为一张：以 AX 为主，hook 的 cwd / 工具调用 / transcript 等元数据塞副字段；徽章 `🖥 Desktop · Code ✦`（深蓝色，✦ 标识融合）；drawer 打开时同时拉两个 sid 的 events 按 ts 合并。

### R44 · Desktop 嵌入 CLI 卡片徽章去 (CLI)

`source=claude_code + desktop_embedded` 的卡片徽章统一显示 `🖥 Desktop · Code`（去掉误导的 "(CLI)" 后缀），按钮统一为 `→ Desktop`。

### R43 · Claude Desktop Code mode 嵌入 CLI 正确归类

bug：用户在 Claude Desktop · Code 跑的对话被错分到"终端 (Claude Code CLI)"段。原因是 Claude Desktop 在 `~/Library/Application Support/Claude/claude-code/` 启动 CLI 子进程跑 session，hook 链路看 `source=claude_code` 自然归"终端"。修：hook 端用 `ps -p` 探测 CLI binary 路径，标 `desktop_embedded=True`，前端 categorize 归 Desktop·Code 段。

### R40-R42 · 删除 session + reap 提速

- 新增 `DELETE /api/sessions/{sid}`：彻底从 dashboard 移除（events.jsonl 保留供审计），`data/hidden_sessions.json` 持久化
- drawer 加红色"删除"按钮（与"标记已结束"区分；后者改 status，前者完全隐藏）
- `WINDOW_DEAD_TIMEOUT_SEC` 60s→5s：在 Claude.app 内删 session 后 dashboard 5s 内消失
- 点 `→ Desktop` 报 `button_not_found` / `session_not_tracked` 时弹 confirm 提议直接删除
- WS 广播 `session_deleted` / `session_restored` 多端实时同步

### R39 · mark-dead 跨重启持久化 + status 准确

backend 重启时扫 `events.jsonl` 把"最后一条 status-changing event = SessionDead + reason=user_marked"的 desktop_app sid 注入 `bridge._forgotten`，让 mark-dead 跨重启稳定。bridge 撤掉 Heartbeat（避免 L44 derive_status 的"活动证据胜过等待"规则把 Notification 错翻成 running）。

### R35-R38 · Mode 板块识别 + 列表分段 + dead 阈值 180min + 手动标记

- 顶部 Chat/Cowork/Code 三 tab 通过 AX 属性 `AXARIACurrent='page'` 判定激活
- bridge 只采集 **Code 和 Cowork** 的 Recents 列表，Chat tab 完全不采（用户聊天不污染 dashboard）
- 列表视图按 source/mode 分三段：**终端 (Claude Code CLI) / Claude Desktop · Code / Claude Desktop · Cowork**
- `dead_threshold_minutes` 默认 30→180min（长任务不再被误判挂起）
- 新增 `POST /api/sessions/{sid}/mark-dead`：用户主动把 session 标 dead

### R34 · 点击跳转 → Desktop 按钮

`source=desktop_app` 的卡片右上多一个 `→ Desktop` 按钮，点击 backend 用 `AXUIElementPerformAction(button, "AXPress")` 模拟点击 Claude.app 侧栏 Recents 对应 session，并 `open -a Claude` 把 app 拉前台。同时修复 StaticFiles 浏览器缓存问题（加 `Cache-Control: no-cache`），dev 改动浏览器立即生效。

### R33 · 桥接重大重写 — sidebar Recents 状态解析

原计划"在每个会话窗口内找 Stop 按钮"在 Electron + Chromium 上不可行（默认不暴露 web DOM 到 AX 树）。改用 sidebar Recents 列表作为信号源：每个会话项是 `AXButton`，子树含 `AXApplicationStatus` 节点，`AXDescription` 给状态字（实测 "Running" / "Idle" / "Awaiting input" / "Done" / "Error"）。`AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)` 唤醒 Chromium a11y 子系统。

### R32 · MCP 辅助通道

`scripts/mcp_notify_server.py` 暴露 `notify_progress(title, summary, level)` 工具，Claude Desktop 通过 MCP 主动汇报里程碑（level=`info|milestone|blocked`）。事件 source=`desktop_app`、session_id=`mcp-<conversation_id>`，多轮汇报聚合到一张卡。

### R31 · dashboard 标识 + 诊断 API

- session card 右上加 `🖥 Desktop` 蓝色徽章（仅 source=desktop_app 时显示）
- 新增 `GET /api/desktop-bridge/status`：报告 configured / running / ax_trusted / claude_pid / 当前追踪 sessions / 观察到的所有 raw 状态字
- 新增 `docs/configuration.md §A desktop_bridge` 配置段

### R30 · Claude Desktop AX 桥接（macOS）

- `backend/desktop_bridge.py` 用 pyobjc AXUIElement* API 枚举 Claude.app 窗口
- 1Hz 轮询、状态机映射（generating / idle / waiting_input / dialog → SessionStart / Stop / Notification / SessionEnd）
- 与 backend lifespan 集成，按 `cfg.desktop_bridge.enabled` 启停
- 默认 `enabled=false`，对原有 CLI 用户零影响

### R29 · baseline + design + POC

- `docs/desktop-bridge/design.md` 方案对比：选 Accessibility API 作为主路径，MCP 作为辅助
- `scripts/desktop_ax_dump.py` 离线 dump Claude.app AX 树（label 漂移排查用）
- baseline git tag `pre-desktop-bridge-R29`（一键回滚锚点）

## R0-R28 · CLI hook 主链路

不在此 changelog 范围（详见 git log 早期 commits）：飞书 webhook、浏览器桌面通知、liveness watcher、推送策略（silence / quiet hours）、per-session 静音、记事本、LLM 摘要（local_cli / anthropic / openai 三 provider）、菜单 prompt 检测、归档等。
