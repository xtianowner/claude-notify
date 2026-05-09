<!-- purpose: claude-notify 运行环境说明 -->

# env

创建时间: 2026-05-08 20:29:45
更新时间: 2026-05-08 20:42:00

## 运行面
- 执行面：local mac（darwin 24.5+）
- 后端端口：127.0.0.1:8787
- 数据落盘：本项目 `data/events.jsonl` + `data/config.json`
- hook 脚本：`scripts/hook-notify.py`，由 Claude Code 经 `~/.claude/settings.json` 调起

## Python 环境
- 推荐 conda：python 3.11+
- 依赖：`backend/requirements.txt`（fastapi + uvicorn + httpx + pydantic）
- hook 脚本本身**不依赖任何第三方库**（只用标准库 + system curl），保证 Claude Code 启动期可用

## 系统依赖
- `curl`（macOS 自带）
- `python3`（macOS 自带 3.9+，能跑 hook 脚本；后端建议另起 conda env 跑 3.11+）

## 前端
- 零构建，vanilla HTML + ES module JS + 单文件 CSS
- 由后端以 StaticFiles 挂载 `frontend/`，访问 http://127.0.0.1:8787

## 配置 / 凭据
- 飞书 webhook 与可选 secret 存于 `data/config.json`
- `data/` 目录全部排除在版本控制之外（含 events.jsonl）
- 凭据**不写入任何 md 文件、commit、日志、文件名**
