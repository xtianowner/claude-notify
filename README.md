# claude-notify

> 给 Claude Code 用户的本地通知系统：监控所有会话的 hook 事件，关键节点（等输入 / 等授权 / 任务结束 / 长任务疑挂）推飞书 + 本地 dashboard 实时展示。多会话并行不漏看。

## 它解决什么问题

你同时开了 5 个 Claude Code 终端跑各种长任务，切到别的事情上后，**没人提醒你**：
- 任务跑完了等你下一步指令
- Claude 卡在「请求授权 rm -rf …」等你确认
- 子 agent 派出去 30 分钟没动静（疑似 hang）
- 多会话之间分不清谁是谁

claude-notify 把所有 Claude Code 进程的 hook 事件聚合起来：
- 飞书机器人**只在你需要手动介入时**推一下（默认收紧策略）
- 本地 dashboard 一眼看清所有 session 状态
- 可选 LLM 摘要：把杂乱的 prompt/响应浓缩成一句话标题

## 截图

> （截图后补 — dashboard 卡片视图 / 飞书推送样例 / 配置抽屉）

## 快速开始

### 前置

- macOS（已测，Linux 应可，Windows 未测）
- Python 3.11+
- Claude Code CLI 已安装可用（`claude` 命令在 PATH）
- 飞书自建机器人 webhook URL（可选 secret 签名）

### 安装

```bash
git clone https://github.com/<你的用户名>/claude-notify.git
cd claude-notify
pip install -r backend/requirements.txt
```

### 启动后端

```bash
python -m backend.app
# 默认监听 127.0.0.1:8787
```

打开 http://127.0.0.1:8787 看 dashboard。

### 注册 hook（让 Claude Code 把事件投给我们）

```bash
python scripts/install-hooks.py
# 写入 ~/.claude/settings.json，注册 5 条 hook（Notification/Stop/SubagentStop/SessionStart/PreToolUse）
# 卸载：python scripts/install-hooks.py --uninstall
# 预览：python scripts/install-hooks.py --dry-run
```

任何 Claude Code 会话从此都会把事件投给本地 backend。**多终端并发完全 OK**（事件用 flock 串写）。

### 配置飞书 + LLM（dashboard 操作）

dashboard 右上角 ⋯ → **配置**：

1. **飞书 webhook URL**：必填。粘贴你机器人的 webhook 地址。
2. **推送策略**：默认已收紧 — 只在「等输入 / 等授权 / 任务回合结束 / 长任务疑挂」推送。子 agent 完成不打扰。
3. **LLM 智能摘要**：可选。打开后大标题/事件摘要走 LLM，关掉走启发式（机械但够用）。

LLM 三种 provider 可选：

| Provider | 适用 | 凭据 | 速度 |
|---|---|---|---|
| **本机 claude -p**（默认） | Claude Code 用户 | 走 OAuth（Claude Max 订阅免 key） | 7-15s（官方）/ 4-5s（reclaude） |
| **Anthropic 兼容格式** | 有 sk-ant-... key | `ANTHROPIC_API_KEY` 环境变量或填入 | 1-2s |
| **OpenAI 兼容格式** | DeepSeek / Qwen / Ollama / 自托管 vLLM / 第三方代理 | 任意 OpenAI 兼容 endpoint | 4-7s |

> **注意**：本机 claude -p 默认调官方 `claude`。如果你装了 reclaude/ctf 之类的 wrapper，在 dashboard 配置里把「二进制路径」填成 wrapper 绝对路径（比如 `/Users/xxx/.local/bin/reclaude`）即可加速到 4-5s（Go 启动 vs Node 启动）。

## 架构

```
   Claude Code (多 session 并行)
        │  hooks: Notification / Stop / SubagentStop / PreToolUse(心跳) / SessionStart
        ▼
   scripts/hook-notify.py  ──flock──>  data/events.jsonl  (兜底，不挂)
        │  (异步 POST, fire-and-forget)
        ▼
   backend (FastAPI :8787)
     ├── notify_policy（silence-then 合并模式 + 优先级过滤）
     │   └── feishu webhook 推送
     ├── liveness_watcher（PID + transcript mtime 双信号判活/疑挂/dead）
     ├── llm_enrich（异步 topic + event 摘要，写 enrichments.jsonl 缓存）
     └── WebSocket → frontend（事件实时推 + LLM 摘要 ready 后增量推）
        │
        ▼
   frontend (vanilla HTML/JS/CSS, 零构建)
     单页 dashboard：session 卡片 / 抽屉式事件流 / 别名编辑 / 配置
```

模块详细说明 → [docs/modules.md](docs/modules.md)
设计教训记录 → [LESSONS.md](LESSONS.md)

## 推送策略

每种事件可选三种行为：`immediate`（立即推） / `silence:N`（N 秒静默后推，期间相同 sid 事件合并发出）/ `off`（不推）。

| 事件 | 默认 | 含义 |
|---|---|---|
| `Notification` | immediate | 等输入 / 等授权 → 必推 |
| `TimeoutSuspect` | immediate | 长任务疑似 hang → 必推 |
| `Stop` | silence:12 | 主 agent 完成一回合（= 等下一步） → 12s 静默后推 |
| `SubagentStop` | off | 子 agent 完成 ≠ 主流程结束，不打扰 |
| `SessionDead/End/Heartbeat` | off | 不推 |

dashboard ⋯ → 配置可手动调整。

## 反馈循环防护

启用「本机 claude -p」provider 时，backend 会 spawn `claude/reclaude` 子进程跑 LLM。这些子进程**也是 Claude Code**，会触发 `~/.claude/settings.json` 里的 hook → 写 events → backend 又调 LLM → ♾️。

我们通过环境变量 `CLAUDE_NOTIFY_LLM_CHILD=1` 标记自家子进程，hook 顶部检测此标识立即退出。详细教训见 [LESSONS.md L02](LESSONS.md)。

## 数据 / 隐私

- `data/config.json`：含飞书 webhook + 可选 API key，**不进版本控制**（.gitignore）
- `data/events.jsonl`：所有 hook 事件，flock 串写，重启不丢
- `data/enrichments.jsonl`：LLM 摘要缓存（按 transcript path + mtime 复用）
- `data/aliases.json`：你给 session 起的别名 / 备注
- 凭据**不写入任何 .md / commit / 日志 / 文件名**

## 开发

```bash
# 跑测试套件
python scripts/0509-1620-test-silence-merge.py        # silence-then merge
python scripts/0509-1700-test-local-cli-provider.py   # 本机 claude -p
python scripts/0509-1645-test-enrich-e2e.py           # OpenAI 兼容 e2e
python scripts/0509-1710-test-local-cli-e2e.py        # local_cli e2e
```

详细见 [docs/modules.md](docs/modules.md) / [LESSONS.md](LESSONS.md)。

## License

MIT — 见 [LICENSE](LICENSE)。

## 鸣谢

- [Anthropic Claude Code](https://docs.claude.com/en/docs/claude-code) — hook 体系是这个项目存在的基础
