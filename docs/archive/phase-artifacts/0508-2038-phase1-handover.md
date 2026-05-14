<!-- purpose: claude-notify frontend Phase 1 交付 handover（给主 agent） -->

# Phase 1 Frontend Handover

创建时间: 2026-05-08 20:38:44
更新时间: 2026-05-08 20:38:44

## Outcome

claude-notify 单页 dashboard Phase 1 闭环：vanilla HTML + ES module JS + 单文件 CSS，零构建零依赖，所有功能链路（session 卡片网格 / 抽屉事件流 / WS 实时推送 + 5s 重连 / 配置弹窗 / 测试推送 / webhook 状态徽章）打通。

## 后端 mount 目录

**直接 mount `frontend/`**（不需要 build / 不需要 dist）：

```python
from fastapi.staticfiles import StaticFiles
# 注意先注册 /api/* 和 /ws，再 mount "/"
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
```

## 交付文件

- `/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/frontend/index.html`
- `/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/frontend/styles.css`
- `/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/frontend/api.js`
- `/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/frontend/format.js`
- `/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/frontend/app.js`
- `/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/frontend/README.md`
- `/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/docs/ui/0508-2038-ui.md`（Phase 1 标记 + Phase 2 待美化清单）

## API 依赖（与 design.md §3.2 一致）

前端调用：
- GET  /api/sessions
- GET  /api/events?session_id=&limit=50
- GET  /api/config
- POST /api/config
- POST /api/test-notify
- WS   /ws

后端实现这些路由后即可联调。

## Phase 2 启动条件

用户显式确认 Phase 1 闭环 → 主 agent 调用 ui-ux-pro-max 决策 → 才能开始精细 UI。
