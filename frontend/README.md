<!-- purpose: claude-notify 前端 Phase 1 说明（技术栈 / 启动 / 后端 mount 目录） -->

# frontend (Phase 1)

创建时间: 2026-05-08 20:33:00
更新时间: 2026-05-08 20:33:00

## 技术栈

- **vanilla HTML + ES module JS + 单文件 CSS**
- 零构建、零依赖、零 npm
- 选这套是为了 Phase 1 节约 token、加速主线；Phase 2 美化时如需切到 Vue/React 也可以独立替换（API 层 `api.js` 已与业务隔离）

## 文件结构

```
frontend/
├── index.html      入口页（顶部栏 / sessions 网格 / 抽屉 / 配置弹窗）
├── styles.css      所有视觉常量（颜色 token / 布局）—— Phase 2 美化主战场
├── app.js          业务入口：状态、渲染、事件绑定
├── api.js          REST + WebSocket 客户端（与业务隔离，便于替换/mock）
├── format.js       展示工具：相对时间、状态/事件徽章 class 映射（视觉常量）
└── README.md       本文件
```

## 启动方式

前端是纯静态文件，靠后端 FastAPI 用 `StaticFiles` 挂在 `/` 下：

```bash
cd <repo>
python -m backend.app
# 浏览器打开 http://127.0.0.1:8787
```

不需要单独跑前端 dev server。

## 后端 mount 目录

**直接 mount `frontend/`**，不需要 build：

```python
# backend/app.py 里大致这样：
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
```

注意：`/api/*` 和 `/ws` 路由要先注册，再 mount `/`，否则 StaticFiles 会吃掉所有路径。

## 接口契约（与 docs/plan/0508-2029-design.md §3.2 一致）

前端依赖：
- `GET  /api/sessions`            → `[{session_id, project, cwd, status, last_event, last_event_ts, event_count}, ...]`
- `GET  /api/events?session_id=&limit=50` → `[event, ...]`，时间正序
- `GET  /api/config`              → 配置对象（含 `feishu_webhook` 当前值，前端用密码框展示）
- `POST /api/config`              → 同 schema，返回保存后对象
- `POST /api/test-notify`         → `{ok: true, message: "..."}`
- `WS   /ws`                      → 服务端推单条 event JSON

WebSocket 断线 5 秒重连。

## Phase 2 待美化点（明确预留）

- `styles.css` 里 `:root` 颜色 token 是单一改造入口（可一次切暗色 / 玻璃拟态 / 品牌色）
- `format.js` 里徽章 class 映射 + 相对时间格式可整体替换（如换成 dayjs / 卡片配色重设计）
- `index.html` 结构已分区（topbar / sessions-grid / drawer / modal），Phase 2 可不动 JS 业务、只改 HTML 结构 + CSS
- 卡片信息密度、抽屉折叠交互、空态插画、按钮 hover/active 微交互均未投入 —— 留给 ui-ux-pro-max
- 配置弹窗的密码型回显策略（目前直接 password 输入框承载真实 webhook）Phase 2 可改为"已配置 ●●●● [更换]"模式

## Phase 1 已闭环的功能

- session 卡片网格（项目名 / 状态徽章 / 最后事件 / 相对时间 / event_count）
- 点击卡片打开抽屉看最近 50 条事件
- WebSocket 实时推送：自动 upsert session 卡片 + 抽屉自动追加
- WS 断线 5s 重连 + 状态徽章
- 飞书 webhook 配置弹窗（webhook URL / 超时阈值 / 节流秒数 / 4 个事件开关）
- 测试推送按钮
- 顶部 webhook 状态徽章（已配置 / 未配置）
- 没有 webhook 时整个页面照常工作（仅顶部徽章变红）
