<!-- purpose: claude-notify 运行环境说明 -->

# env

创建时间: 2026-05-08 20:29:45
更新时间: 2026-05-20 11:30:00

## 运行面
- 执行面：local mac（darwin 24.5+）；Linux 原则上可（CLI 主链路），但 `hook-notify.py` 用了 `fcntl.flock` + `ps -p $PID -o tty=`，**Windows 不支持**
- 后端端口：默认 `127.0.0.1:8787`；`HOST` / `PORT` 环境变量可覆盖（R47）
- 数据落盘：本项目 `data/events.jsonl` + `data/config.json` + `data/aliases.json` + `data/notes.md` + `data/hidden_sessions.json` + `data/push_decisions.jsonl` + `data/enrichments.jsonl` + `data/idle_reminder.json` + `data/archive/`
- hook 脚本：`scripts/hook-notify.py`，由 Claude Code 经 `~/.claude/settings.json` 调起
- MCP 辅助 server（可选，Claude Desktop）：`scripts/mcp_notify_server.py`

## Python 环境
- 推荐 conda：python 3.11+
- 主依赖：`backend/requirements.txt`（fastapi + uvicorn + httpx + pydantic）
- macOS Claude Desktop 桥接附加依赖（R29+）：`backend/requirements-desktop.txt`（pyobjc 的 ApplicationServices / HIServices），仅在 `cfg.desktop_bridge.enabled=true` 时需要
- hook 脚本本身**不依赖任何第三方库**（只用标准库 + system curl），保证 Claude Code 启动期可用

## 系统依赖
- `curl`（macOS 自带）
- `python3`（macOS 自带 3.9+，能跑 hook 脚本；后端建议另起 conda env 跑 3.11+）
- macOS 辅助功能权限（仅 desktop_bridge）：系统设置 → 隐私与安全 → 辅助功能 → 加入实际运行 backend 的 python 解释器路径
- Chrome Automation 权限（仅 R27 飞书 ↗ 链接 osascript 路径）：首次访问 `/o/<sid>` 时 macOS 会弹"Python wants to control Chrome"

## 前端
- 零构建，vanilla HTML + ES module JS + 单文件 CSS
- 由后端以 StaticFiles 挂载 `frontend/`，访问 http://127.0.0.1:8787

## 配置 / 凭据
- 飞书 webhook 与可选 secret 存于 `data/config.json`
- `data/` 目录全部排除在版本控制之外（含 events.jsonl）
- 凭据**不写入任何 md 文件、commit、日志、文件名**
