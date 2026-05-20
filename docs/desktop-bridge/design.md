<!-- purpose: claude-notify 扩展支持 Claude Desktop（macOS Electron app）会话监控的设计 -->

# Claude Desktop Bridge 设计

创建时间: 2026-05-17 18:30:00
更新时间: 2026-05-17 18:30:00（R29 baseline，未跟随后续实现更新）

> **⚠️ 时效声明**：本文档是 R29 立项时的**设计 baseline**。R30 之后实际实现已大幅演进（R33 "找窗口 Stop 按钮"被废弃 → 改读 sidebar Recents；R35-R38 增加 mode 板块识别；R40+ 增加 hidden_sessions / mark-dead 等）。
> **代码事实**以 `backend/desktop_bridge.py` 为准；**模块清单**见 [docs/modules.md `backend.desktop_bridge` 段](../modules.md#backenddesktop_bridger30-新增macos-only)；**用户视角的演进过程**见 [CHANGELOG.md R29-R49](../../CHANGELOG.md)。本文件保留作为方案选型背景。

## 1. 目标

让 claude-notify 不再只覆盖 Claude Code CLI，也能监控 **Claude Desktop**（`/Applications/Claude.app`，Electron 应用，对应 claude.ai SPA）的会话状态：
- 多会话窗口并行时，不漏看"等输入 / 等授权 / 任务完成"等关键节点
- 复用现有 backend 事件管线 + dashboard + 飞书 / 浏览器双推送

## 2. 为什么不能复用 CLI hook 链路

`scripts/hook-notify.py` 依赖 `~/.claude/settings.json` 的 hook 体系，由 Claude Code 主进程在事件时机 fork 出 hook 进程并把 JSON payload 喂给 stdin。

Claude Desktop 是 Electron + Chromium renderer + claude.ai 前端组合：
- 不会调用任何 `~/.claude/settings.json` 配置
- 所有会话事件留在 renderer 进程内（React state / IPC）
- 没有官方 event hook / webhook 出口

所以原通道在 Desktop 上**天然失效**，必须新增独立事件源。

## 3. 候选方案对比

| 方案 | 可观测面 | 可行性 | 主要风险 |
|---|---|---|---|
| **A. MCP server** | LLM 主动调用的 tool 触发；提供 `notify_progress` | ✅ 官方支持 | 只能由 Claude 主动调用，**无法感知"等用户输入"**（最关键状态） |
| **B. macOS Accessibility API** | 窗口 / 标题 / Stop 按钮 / 输入框 / 聊天 DOM 等价物 | ✅ **主推** | 需用户授辅助功能权限；UI 改版会破坏；不能拿原文 prompt |
| **C. Electron DevTools 注入** | DOM / MutationObserver / fetch hook | ⚠️ 限制大 | 当前 Claude.app 进程带 `--enable-sandbox` 拦截；需以 `--remote-debugging-port` 启动整个 app；升级即失效 |
| **D. LevelDB / IndexedDB 文件监听** | 会话历史落盘 | ❌ 不可行 | Chromium 持写锁，外部并发读触发损坏 |
| **E. 网络 MITM 抓 streaming response** | 完整事件流 | ❌ 不推荐 | Claude.app 已挂 `--proxy-server=127.0.0.1:57573` + 固定证书指纹；插队需重打 CA + 违反 ToS |

## 4. 决策

**主路径：B（Accessibility API）**
- 用 pyobjc 的 `ApplicationServices` / `HIServices` AXUIElement* API 枚举 Claude.app 窗口
- 1Hz 轮询每个窗口，通过 UI 元素（按钮 label / 输入框 enabled / 标题）推断三种状态：
  - **generating**：有 "Stop response" / "停止" 类按钮可见
  - **waiting_input**：上一次 generating 刚结束 / 输入框 enabled 且为空 / 无 spinner
  - **dialog**：有 modal 确认对话框（罕见，Desktop 自身极少弹）
- 状态翻转点合成事件，POST 到 `http://127.0.0.1:8787/api/event`

**辅助路径：A（MCP server）**
- 独立的 stdio MCP server `scripts/mcp_notify_server.py`
- 暴露 `notify_progress(title, summary, level)` 工具
- 用户在 Project Knowledge / system prompt 里写"完成关键节点时调用 notify_progress"
- 不替代主路径，只是给"Claude 自报家门式"的额外信号

## 5. 数据模型

复用现有 event 结构，新增 source = `desktop_app`。新增字段：
- `source = "desktop_app"`
- `session_id`：由窗口 `kAXWindowRole` 的 `AXIdentifier` + 进程 PID 派生 hash（稳定到窗口关闭）
- `window_title`：从 `kAXTitleAttribute` 取
- `conversation_id`：若能从窗口标题 / URL 提取（Desktop 内嵌 claude.ai 路由）则填入
- `event`：复用 `SessionStart` / `Notification`（等输入）/ `Stop`（回合结束）/ `SessionEnd`
- `cwd` / `tty` 不适用，置空；project 字段填"Claude Desktop"

新增 normalizer `_normalize_desktop_app` 注册到 `backend/sources.py:_REGISTRY`。

## 6. 风险与限制

- **辅助功能权限**：首次启动需用户去「系统设置 → 隐私与安全 → 辅助功能」勾选 python 解释器，否则 AX API 全返回 None。
- **UI 改版即坏**：Claude.app 是高频升级的，AX 启发式（按钮 label 中英文匹配）会被破。处理：把 label 模式抽 config，dashboard 加"诊断"按钮 dump 当前窗口 AX 树。
- **无法拿到 prompt 原文**：Accessibility 看到的是渲染后 DOM 文本，可拿但不可靠（emoji / 富文本会变形）。摘要走启发式 / LLM。
- **多窗口聚合**：同一 conversation_id 不同窗口要去重；先按 window_id 当 session，UX 测试后看是否需要合并。

## 7. 阶段计划

- **R29**：baseline + design + POC dump 工具（本轮）
- **R30**：状态机 + 事件合成 + 接入 `/api/event`
- **R31**：daemon 化 + 配置开关 + dashboard source 标记
- **R32**：MCP 辅助通道
- **R33+**：实测 QA → 修 UI label 漂移 / 误报

## 8. 回滚策略

每 round 一个 git commit，main branch 不阻塞 CLI 主链路。tag `pre-desktop-bridge-R29` 是 baseline，任何一轮翻车都能 `git reset --hard pre-desktop-bridge-R29` 回到 R28 状态。
