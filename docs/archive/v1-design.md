<!-- purpose: claude-notify 最小原型设计与决策记录 -->

# claude-notify 设计

创建时间: 2026-05-08 20:29:45
更新时间: 2026-05-08 20:29:45

## 1. 目标与非目标

### 目标
- 任务结束（Stop）/ 等待用户确认（Notification）/ 子 agent 完成（SubagentStop）/ 长任务疑似 hang，4 类事件能从飞书第一时间感知。
- 本地 dashboard 一眼看清"现在所有 Claude Code 会话各处于什么状态、最近发生了什么、有谁在等我"。
- 不破坏 Claude Code 主流程：hook 必须 < 几百 ms 返回（异步 fire-and-forget）。

### 非目标
- 不接管 Claude Code 内部机制（不 hook 模型 token 流、不读 transcript 内容）。
- 不做云端存储 / 多人协作。
- 不做手机端独立 app（依赖飞书 app 已经够）。

## 2. 事件模型

### 2.1 来源 hooks
| Claude Code Hook | 触发时机 | 处置 |
|---|---|---|
| `Notification` | Claude 等用户授权/输入 | **强推**（必须立刻看到） |
| `Stop` | 主任务一回合结束 | 推（含项目名 + 摘要） |
| `SubagentStop` | 子 agent 完成 | 节流推（同一 session 30s 内合并） |
| `PreToolUse` | 工具调用前 | **不推**，仅写心跳 mtime（用于 hang 检测） |

### 2.2 事件 schema（jsonl 一行）
```json
{
  "ts": "2026-05-08T20:30:00+08:00",
  "session_id": "<claude session id>",
  "event": "Notification|Stop|SubagentStop|Heartbeat|TimeoutSuspect",
  "cwd": "/abs/path",
  "project": "<basename(cwd)>",
  "message": "需要授权: Bash(rm -rf …)",
  "transcript_path": "...",
  "raw": { /* hook stdin 原样 */ }
}
```

### 2.3 长任务超时（TimeoutSuspect）
- 后端 asyncio 任务每 60s 扫一次 `data/events.jsonl` 末尾。
- 对每个 active session（最近事件 < 30 分钟），若 `now - last_event_ts > threshold` 且最后事件不是 `Stop` → 生成 `TimeoutSuspect`。
- threshold 可在 dashboard 调整，默认 **10 分钟**。同一 session 同一次怀疑只推一次（写入 `suspected_at` 字段去重）。

## 3. 组件

### 3.1 hook 脚本（`scripts/hook-notify.sh`）
- 入口：被 Claude Code 以 stdin 传 JSON 调起。
- 行为（顺序）：
  1. `flock` 把 stdin 原样合并基础字段写入 `data/events.jsonl`（< 5ms）
  2. `curl --max-time 0.3 -s` 异步 POST 到 `127.0.0.1:8787/api/event`
  3. 立刻 exit 0（即使后端没起也不阻塞 Claude Code）
- 必须 zero 依赖（只用 bash + jq + curl + flock）

### 3.2 backend（FastAPI，`backend/`）
| 模块 | 职责 |
|---|---|
| `app.py` | FastAPI 实例、路由、WebSocket、生命周期管理 |
| `event_store.py` | jsonl 读写、按 session 聚合、计算 session 状态 |
| `feishu.py` | 飞书 webhook 推送（text + 卡片二选一）、节流 |
| `timeout_watcher.py` | asyncio 后台任务，长任务超时检测 |
| `config.py` | data/config.json 读写（webhook URL、阈值、过滤规则） |

#### API
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/event` | 接收 hook 事件（也支持手动注入） |
| GET | `/api/sessions` | 当前所有 session 列表 + 状态 |
| GET | `/api/events` | 查事件流（query: session_id, since, limit） |
| GET | `/api/config` | 读配置 |
| POST | `/api/config` | 改配置（飞书 URL / 阈值 / 是否启用各事件） |
| POST | `/api/test-notify` | 手动测试推送 |
| WS | `/ws` | 推新事件给前端 |
| GET | `/` | 前端单页（serve `frontend/`） |

#### Session 状态机（仅展示用，不强约束）
- `running`: 最近事件不是 Stop，且 < threshold
- `waiting`: 最近事件是 Notification（等用户）
- `idle`: 最近事件是 Stop
- `suspect`: TimeoutSuspect 触发后

### 3.3 frontend（Phase 1 极简）
- 单页 SPA（vanilla 或最薄的 React/Vue 都行，由 phase1 agent 选择）
- 视觉保持最简，**不在 Phase 1 投入精细 style**
- 内容：
  - 顶部：飞书 webhook 状态徽章 + "测试推送"按钮 + 阈值配置入口
  - 主区：session 卡片网格（每张卡：项目名 / 状态徽章 / 最后事件 / mtime / "查看事件流"按钮）
  - 抽屉：点卡片展开详细事件流（最近 50 条）
- 实时性：WebSocket 推；断线 5s 重连。

## 4. 节流与去重策略

| 场景 | 策略 |
|---|---|
| 同一 session 频繁 SubagentStop | 30s 滑动窗口，期内合并为一条 |
| 同一 TimeoutSuspect 反复触发 | 每个 session 同一可疑期只推 1 次，收到新事件后重置 |
| Notification | **不节流**（最重要，必须立刻送达） |
| Stop | 不节流（按回合自然就少） |
| 静默时段 | 暂不做（可后续加） |

## 5. 配置文件 `data/config.json`
```json
{
  "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx",
  "feishu_secret": null,
  "timeout_minutes": 10,
  "events_enabled": {
    "Notification": true,
    "Stop": true,
    "SubagentStop": true,
    "TimeoutSuspect": true
  },
  "throttle_subagent_seconds": 30,
  "active_window_minutes": 30
}
```

## 6. settings.json 接入
hook 注册（项目维度优先；本工具是 host 全局工具，所以注册在 user 级别 `~/.claude/settings.json`）：

```json
{
  "hooks": {
    "Notification": [{"hooks": [{"type": "command", "command": "/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/scripts/hook-notify.sh"}]}],
    "Stop":         [{"hooks": [{"type": "command", "command": "/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/scripts/hook-notify.sh"}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/scripts/hook-notify.sh"}]}],
    "PreToolUse":   [{"hooks": [{"type": "command", "command": "/Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify/scripts/hook-notify.sh --heartbeat"}]}]
  }
}
```

## 7. 启动方式（开发期）
```bash
cd /Users/tian/Documents/00-Tian/00-Tian-Project/claude-notify
conda activate <你的 py3.11+ 环境>
pip install -r backend/requirements.txt
python -m backend.app
# 浏览器打开 http://127.0.0.1:8787
```

后续可用 launchd 做开机自启（Phase 2 处理）。

## 8. 风险 & 备忘
- ❗ hook 脚本必须容错（后端没起时也要 exit 0），否则 Claude Code 会被卡。
- ❗ 飞书自建机器人 webhook URL = 凭据，**绝不写入仓库 / 文件名 / 日志**，落到 `data/config.json` 即可（项目内 `.gitignore` 排除整个 `data/`）。
- ❗ 多 Claude Code 实例并发写 `events.jsonl` → flock 保护。
- ❓ heartbeat 频率高（每次 PreToolUse），事件文件会涨。后续考虑：心跳事件 30s 节流后写盘。

## 9. 后续 Phase 2 候选
- dashboard 美化（走 ui-ux-pro-max 决策）
- 飞书 interactive card（按钮直跳 dashboard）
- 移动端 PWA
- 历史 session 归档查询
- 多机器（VPS）汇聚
