<!-- purpose: claude-notify 前端 UI 阶段记录（Phase 1 已交付 / Phase 2 已实施） -->

# UI 阶段记录

创建时间: 2026-05-08 20:38:44
更新时间: 2026-05-09 16:04:29

## 当前阶段：**Phase 2（极简美化）已实施**

> Phase 1 闭环：用户在 2026-05-09 确认；Phase 2 spec：[`0509-1140-phase2-spec.md`](./0509-1140-phase2-spec.md)；ui-ux-pro-max 校准结论见 spec §"实施备注"段（无重大改动，仅细节微调）。

### Phase 2 polish 记录

| 时间 | 范围 | 实施 |
|---|---|---|
| 2026-05-09 15:06 | 配置 modal 嵌套 fieldset 文字溢出 | 仅 `frontend/styles.css`：label 换行 / 嵌套层 subtle bg + hairline / 内层 legend 字重做层级 / input 防御性 ellipsis；详见 [`00TEM/AgentBus/artifacts/0509-1506-phase2-impl.md`](../../00TEM/AgentBus/artifacts/0509-1506-phase2-impl.md) |
| 2026-05-09 16:04 | LLM fieldset 内 `.muted.small` 排版 + 对比度 | 仅 `frontend/styles.css`：fieldset 内 `p.muted.small` 提升对比度（`--c-fg-soft` 替代 `--c-fg-muted`，在 `--c-subtle` 浅底达 WCAG 4.5:1）、reset `<p>` 默认 margin 给 fieldset 节奏、与下方 label 留 `--sp-2` 间距、line-height tight；select 撑满并对齐内宽；详见 [`00TEM/AgentBus/artifacts/0509-1604-phase2-impl.md`](../../00TEM/AgentBus/artifacts/0509-1604-phase2-impl.md) |

#### 2026-05-09 16:04 决策（ui-ux-pro-max 校准）

- **pattern**：表单内嵌套配置块的 helper text（per-fieldset 一句话说明），不是 form-level 顶部说明
- **style**：极简 / 灰底层级（沿用上一轮的 `--c-subtle` 嵌套层）
- **color**：fieldset 内 helper text 走 `--c-fg-soft`（#5a5a55）而非 `--c-fg-muted`（#8a8a82），在浅底上对比度 4.5+:1（命中 ux Result 6 color-contrast / Result 7 contrast-readability）
- **typography**：保留 `.small` (12px) 字号 + `--lh-tight`（紧凑行高，避免一句话占两行带来的视觉膨胀）
- **motion**：无（静态文字）
- **stack**：原生 CSS + 现有 token 体系，零新依赖

## 技术栈（Phase 1）

- vanilla HTML + ES module JS + 单文件 CSS
- 零构建（无 Vite / npm）
- 无 UI 库（不引 antd / element / mui）

选型理由：节约 token、加速主线、最小化 context 膨胀；后端 FastAPI 直接 `StaticFiles(directory="frontend")` 挂载，无构建产物。

## 文件结构 / 分层

```
frontend/
├── index.html      DOM 骨架（topbar / sessions-grid / drawer / modal-config）
├── styles.css      所有视觉常量（CSS 变量 token + 布局）
├── format.js       展示工具：相对时间 / 状态&事件徽章 class 映射
├── api.js          REST + WebSocket 客户端（断线 5s 重连）
├── app.js          业务逻辑：state、渲染、事件绑定、WS upsert
└── README.md       前端启动 + 后端 mount 说明
```

分层原则：业务逻辑（app.js）/ 网络层（api.js）/ 视觉常量（styles.css + format.js）解耦，Phase 2 美化时只动后两者。

## Phase 1 已闭环功能

- 顶部栏：品牌名、webhook 状态徽章、ws 状态徽章、测试推送按钮、配置按钮
- session 卡片网格：项目名、状态徽章（颜色按 running/waiting/idle/suspect 区分）、最后事件类型、相对时间、event_count
- 点击卡片 → 右侧抽屉展开最近 50 条事件，事件徽章按事件类型上色
- WebSocket 实时推送：upsert session 卡片 + 当前抽屉同 session 时自动追加
- WS 断线 5s 重连，状态徽章可视化
- 配置弹窗：飞书 webhook（密码型）、超时阈值（分钟）、节流秒数、4 个事件类型开关、保存/取消
- 测试推送按钮 → POST /api/test-notify
- 没有 webhook 配置时页面正常工作，仅顶部徽章变红
- 兜底：30s 刷新相对时间、60s 拉一次 /api/sessions 刷新权威 status

## Phase 2 待美化点（明确预留，避免 Phase 1 视觉硬编码进业务）

| 范围 | 入口 | 待办 |
|---|---|---|
| 颜色 / 主题 | `styles.css` `:root` token | 暗色 / 玻璃拟态 / 品牌色定制 |
| 徽章配色 | `format.js` `statusBadgeClass` / `eventBadgeClass` | 配色重新设计、可加图标 |
| 相对时间格式 | `format.js` `relTime` | 切 dayjs / 国际化 |
| 卡片信息密度 | `index.html` `.session-card` 结构 + `styles.css` | 紧凑/详细两种密度切换、空态插画 |
| 抽屉交互 | `index.html` `#drawer` + `app.js` `openDrawer` | 折叠分组、按事件类型筛选、滚动加载更早 |
| 配置弹窗回显 | `app.js` `fillConfigForm` | webhook 改"已配置 ●●●● [更换]"模式 |
| hover / active 微交互 | `styles.css` | 按钮、卡片、抽屉切换动效 |
| 响应式 | `styles.css` grid + 抽屉 | 窄屏抽屉切换为全屏 / 卡片单列 |

## 闸门规则

- **进入 Phase 2 前置**：用户在对话或 `00TEM/NowTodo/todo.md` 显式确认 Phase 1 闭环
- **Phase 2 工作流**：先走 ui-ux-pro-max 决策，再写 UI 代码
- 用户中途要求"做漂亮一点"时不得直接做，需先回到上述前置
