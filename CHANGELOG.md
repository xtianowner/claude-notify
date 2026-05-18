# Changelog

> 用户视角的功能演进。每个 round 对应一个 git commit，可在 `git log` 找到完整 diff。

## R29-R47 · Claude Desktop 桥接（macOS）

把 claude-notify 从"只监控 Claude Code CLI"扩展到"也能监控 Claude Desktop"。详细方案见 [docs/desktop-bridge/design.md](docs/desktop-bridge/design.md)。

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
