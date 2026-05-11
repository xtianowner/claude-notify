<!-- purpose: claude-notify 配置项参考（每个 data/config.json 字段的含义/默认/取值/影响） -->

# 配置参考

创建时间: 2026-05-11 13:55:00
更新时间: 2026-05-11 17:55:00

> 所有配置存在 `data/config.json`。可直接编辑文件，也可在 dashboard 右上角 ⋯ → 配置面板里改（推荐）。改完后立即生效，不用重启 backend（绝大多数字段是热加载，少数标注「需要重启」）。

---

## 0. 推送渠道 `push_channels`（L41 / R16）

两条独立渠道。可只开 browser 完全脱离飞书使用。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `push_channels.feishu` | `true` | bool | 飞书 webhook 推送。`false` 时 `feishu.send_event` 直接 return `reason="channel_off"`，不发起 HTTP 请求。webhook URL 字段可保留（不会用）。 |
| `push_channels.browser` | `true` | bool | 浏览器桌面通知（dashboard tab 打开 + 已授权时）。`false` 时前端忽略 push_event。 |

**使用模式**：

- **只飞书**：browser=false，feishu=true + 配 webhook。和原始行为一致。
- **只浏览器**：feishu=false，browser=true。无需 webhook，dashboard 必须打开（tab 后台也行，OS toast 仍弹）。首次需点顶部"启用桌面通知"条带授权。
- **两者并存**（默认）：飞书覆盖手机场景，浏览器覆盖坐在电脑前但 dashboard tab 在后台的场景。

**何时不弹浏览器**：

- `Notification.permission !== "granted"`：用户未授权 / 已 deny。
- tab `visibilityState==="visible"` 且 `document.hasFocus()`：用户正盯着 dashboard 看，OS toast 是重复打扰。
- 全局静音命中（`muted=true` / `snooze_until` 未到期）：browser **跟随**全局静音；这与 channel 开关是两个维度。

**测试推送行为**：dashboard ⋯ → 测试推送 = 同时广播 push_event 给 WS（任何开关下）+ 试发飞书（按 channel.feishu 开关）。关掉飞书也能用这个验证浏览器通知。

---

## 1. 飞书推送

最基础的两个字段：webhook 地址 + 可选签名密钥。webhook 没配 = backend 起得来、能收事件、能写卡片，但所有推送都会以 `webhook_empty` 失败。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `feishu_webhook` | `""` | string | 飞书自定义机器人 webhook 完整 URL。空值 = 不推送（dashboard 仍可看事件）。改完立即生效。 |
| `feishu_secret` | `""` | string | 飞书机器人启用「签名校验」时填入的 secret；机器人未开签名校验则留空。 |

> 想完全关闭飞书：建议改 `push_channels.feishu=false`（语义清晰，保留 webhook 备用）；清空 `feishu_webhook` 也能达到相同效果但语义弱（reason 会是 `no_webhook`）。

---

## 2. 推送策略 `notify_policy`

每个事件类型映射到一个推送策略字符串：`immediate`（立即推） / `silence:N`（N 秒静默期；期间同 session 有任意活动事件 Heartbeat/PreToolUse/PostToolUse 则取消推送；同 session 多个事件命中静默期会合并成 1 条卡片）/ `off`（不推）。未列出的事件类型按 `off` 处理。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `notify_policy.Notification` | `immediate` | enum: `immediate` \| `silence:N` \| `off` | Claude Code 等待用户输入 / 等授权时的事件。默认必推。 |
| `notify_policy.TimeoutSuspect` | `immediate` | 同上 | 会话疑似 hang（按 liveness 阈值判定）。默认必推。 |
| `notify_policy.Stop` | `silence:12` | 同上 | 主 agent 一回合结束。默认 12 秒静默后推 —— 这 12 秒内若用户立刻又下指令（产生 PreToolUse），就会取消推送。 |
| `notify_policy.SubagentStop` | `off` | 同上 | 子 agent 结束。默认不推（主 agent 还在跑，没必要打扰）。 |
| `notify_policy.SessionStart` | `off` | 同上 | 新会话开启。默认不推。 |
| `notify_policy.SessionEnd` | `off` | 同上 | 会话结束。默认不推。 |
| `notify_policy.SessionDead` | `off` | 同上 | liveness watcher 判定会话已死。默认不推。 |
| `notify_policy.Heartbeat` | `off` | 同上 | 心跳事件（活动信号，仅用于取消 silence 倒计时）。 |
| `notify_policy.PreToolUse` | `off` | 同上 | 工具调用前。活动事件，会取消 silence 倒计时。 |
| `notify_policy.PostToolUse` | `off` | 同上 | 工具调用后。活动事件。 |
| `notify_policy.TestNotify` | `immediate` | 同上 | dashboard 「发送测试通知」按钮触发的事件。 |

**何时改 `Stop`**：嫌 12 秒太长 → `silence:5`；想 Stop 立刻推不要任何延迟 → `immediate`；完全不想要 Stop 推送（只要 Notification）→ `off`。

**何时改 `SubagentStop`**：当你跑大量长任务并发的 agent 编排、想知道每个子 agent 收尾 → 设 `silence:30`。

---

## 3. 精细过滤 `notify_filter`

在 policy 层"决定推不推"之后，filter 层"决定这条卡片内容够不够格"。这一组是用户最容易迷惑的，建议先弄懂 `stop_sensitivity` 再调其它。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `notify_filter.stop_sensitivity` | `"normal"` | enum: `fast` \| `normal` \| `strict` | Stop 推送的字数阈值预设。`fast` = milestone/summary 都 4 字（几乎全推）；`normal` = 6/8（默认）；`strict` = 都 20 字（仅推有内容回合）。非 normal 会**覆盖** 下面两个 `stop_min_*_chars` 字段。 |
| `notify_filter.stop_min_milestone_chars` | `6` | int (≥0) | 当 `stop_sensitivity=normal` 时生效：last_milestone 字段达到此长度即放过过滤。 |
| `notify_filter.stop_min_summary_chars` | `8` | int (≥0) | 当 `stop_sensitivity=normal` 时生效：turn_summary 字段达到此长度即放过（前提是 last_assistant 不命中黑词）。 |
| `notify_filter.stop_min_gap_after_notification_min` | `5` | int (分钟) | 距离上一次 Notification 超过此分钟 → 即使 summary 极短也放过 Stop（cache miss 时仍能救）。 |
| `notify_filter.stop_short_summary_grace_min` | `8` | int (分钟) | **长任务收尾兜底窗**。距离同 sid 上一次任意推送超过此分钟 → 即使一字也放过 Stop。设 `0` 关闭。 |
| `notify_filter.notif_idle_reminder_minutes` | `[15, 45]` | list[int]，分钟 | Stop 之后用户没反应、Claude Code 又派 idle prompt 时的提醒节奏。`[]` = 严格 1 次（只推完成那次）；`[15, 45]` = Stop 后 15min 推第 1 次 reminder、再过 30min（即 Stop 后 45min）推第 2 次，之后不再推。下一次新 Stop 推送 → 计数重置。 |
| `notify_filter.notif_filler_phrases` | 见下方 | list[string] | 「空话」白名单。Claude Code 的 idle prompt 文本命中此列表的整句、且距离 Stop 推送未超时 → 走 idle reminder 节流判定。匹配规则：strip + lower 后**整句严格相等**。默认值：`["claude is waiting for your input", "claude code needs your attention", "press to continue", "press enter"]`。 |
| `notify_filter.blacklist_words` | 见下方 | list[string] | last_assistant_message 若 strip + lower 后**整句严格相等**命中此列表（或长度 ≤4 字且无标点/空格） → 视作短纯应答、不构成有效任务回合。默认值：`["ok","yes","no","好","好的","已完成","完成","收到","嗯","是","不","对","行","可以","thanks","thank you","ack"]`。匹配是**整句相等**，不做子串匹配，所以"已完成端到端验证"不会被"已完成"吞掉。 |
| `notify_filter.filter_sidechain_notifications` | `true` | bool | 子 agent 内触发的 Notification 是否过滤。默认 true（推荐）：父 session 已恢复，子 agent 内的"等输入"对用户而言是虚假打扰。设 false 恢复全量 Notification 推送。 |
| `notify_filter.notif_dedup_until_next_stop` | `true` | bool | **已废弃**字段，保留为读兼容，新代码不读。无实际行为。 |
| `notify_filter.notif_suppress_after_stop_min` | `3` | int (分钟) | **已废弃**字段，保留为读兼容。新逻辑由 `notif_idle_reminder_minutes` 控制。 |

**何时改 `stop_sensitivity`**：飞书推送太骚扰 → 调 `strict`；觉得短回合漏推 → 调 `fast`。这一个字段比下面两个 `stop_min_*_chars` 更值得先调。

**何时改 `notif_idle_reminder_minutes`**：彻底不想要 idle 提醒 → `[]`；想要更密集提醒（如 5min / 15min） → `[5, 15]`；嫌默认 [15,45] 太啰嗦 → `[30]`（只在 Stop 后 30min 来一次最终提醒）。

**何时改 `blacklist_words`**：发现某条"短肯定回答"还是被推上来 → 加入这个列表；发现合法回合被吞 → 从这个列表移除。注意只匹配**整句相等**。

**何时改 `filter_sidechain_notifications`**：当 Tian 项目骨架里有大量子 agent 长任务（如 Explore 子 agent）、希望知道每个子 agent 等输入 → 设 false。

**关于「silence-then-merge」与「menu_detected bypass dup」**：这两个不是单独配置字段，是固有行为。silence-then-merge 由 `notify_policy.*` 中 `silence:N` 的语义自动启用；menu_detected（菜单 prompt 被识别为真等输入）会硬性 bypass idle reminder 节流，无开关。

---

## 4. 空闲提醒 idle reminder

idle reminder 是 §3 里 `notif_idle_reminder_minutes` 的行为，没有独立顶层字段。补充说明此处。

**3-cap 行为**：每个任务回合（一个 Stop push 起）最多推 3 次：
1. Stop 推送（第 1 次，由 `_stop_decision` 控制）
2. Stop 后 `notif_idle_reminder_minutes[0]` 分钟（默认 15）+ Claude Code 又派来 idle prompt → 推第 2 次（reminder #1）
3. Stop 后 `notif_idle_reminder_minutes[1]` 分钟（默认 45）+ 又一次 idle prompt → 推第 3 次（reminder #2）
4. 之后再来 idle prompt 也不推；直到下一次新 Stop push 来 → 计数重置 → 进入下一回合的 3 次循环

计数器在 backend 进程内（`backend/idle_reminder.py` 的 `_counts`），**重启 backend 会清零**（用户预期 reminder 是临时状态，重启后从 0 开始可接受）。最多保留 1024 个 sid 的计数，LRU 淘汰。

---

## 5. 会话存活判定 liveness

liveness watcher 每 `liveness_interval_seconds` 秒扫一次活跃 session，按 last_event_kind 选阈值，判定 `TimeoutSuspect`（疑挂）或 `SessionDead`（死亡）。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `timeout_minutes` | `5` | int (分钟) | **legacy 单一阈值**。当 `liveness_per_state_timeout.enabled=false` 时一刀切用此值；启用 per-state 时仅作 fallback。 |
| `dead_threshold_minutes` | `30` | int (分钟) | transcript 文件静默超此分钟 → 标 `SessionDead`。也是 PID 仍存在的"慢路径"死亡阈值。 |
| `active_window_minutes` | `30` | int (分钟) | dashboard 与 list_sessions 的"近期活跃窗"长度。**未显式配置**时 watcher 会自动用 `max(60, dead_threshold_minutes * 2)` 保证 dead 判定窗口覆盖；显式配置 → 完全按用户值。 |
| `liveness_interval_seconds` | `30` | int (秒) | liveness watcher 巡检周期。改完**需要重启 backend** 才生效（仅启动时读一次）。 |
| `liveness_per_state_timeout.enabled` | `true` | bool | 是否按 last_event_kind 分状态用不同阈值。false → 退化到 `timeout_minutes` 一刀切。 |
| `liveness_per_state_timeout.Notification_minutes` | `5` | int (分钟) | "等用户输入"状态的疑挂阈值。 |
| `liveness_per_state_timeout.Stop_minutes` | `5` | int (分钟) | "主回合刚结束 / 等下一步"状态的阈值。 |
| `liveness_per_state_timeout.SubagentStop_minutes` | `5` | int (分钟) | 同上，子 agent 结束时。 |
| `liveness_per_state_timeout.PreToolUse_minutes` | `15` | int (分钟) | "长 tool 执行中"状态的阈值（curl 大文件 / build / LLM 慢响应都吃这个）。 |
| `liveness_per_state_timeout.PostToolUse_minutes` | `10` | int (分钟) | "工具刚跑完、思考下一步"的阈值。 |
| `liveness_per_state_timeout.Heartbeat_minutes` | `10` | int (分钟) | 心跳后静默的阈值。 |
| `liveness_per_state_timeout.SessionStart_minutes` | `10` | int (分钟) | 会话刚开但没动作时的阈值。 |
| `liveness_per_state_timeout.default_minutes` | `10` | int (分钟) | 上面没列名的事件类型走这个。 |
| `liveness_per_state_timeout.pretool_transcript_alive_seconds` | `60` | int (秒) | PreToolUse 状态额外保护：transcript 文件在最近此秒内有 mtime 更新 → 视为活、跳过本轮疑挂判定。 |

**状态机简述**：每个 session 在 watcher 视角下处于 `running` / `ended` / `dead` 之一。`ended`/`dead` 跳过判定；`running` 时按 last_event_kind 选阈值。死亡判定有快路径（PID gone + transcript ≥60s 静默 → 立即 dead）和慢路径（transcript ≥ `dead_threshold_minutes` 静默 → dead，无视 PID）。每个 session 在每个状态下只 push 一次。

**何时改 `dead_threshold_minutes`**：如果长任务（大批量 LLM 调用、build 整个项目）经常被误标 dead，从默认 30 拉高到 60 甚至 120。

**何时改 `PreToolUse_minutes`**：经常跑 30 分钟以上的单工具（如 `npm install` 大仓 / 大模型 batch 调用）→ 拉高到 30 或 60。

**何时改 `timeout_minutes`**：只有关掉 per-state（`liveness_per_state_timeout.enabled=false`）后才有意义。一般保持 per-state 即可。

---

## 6. 免打扰 `quiet_hours`

时段静音。命中时段时除穿透列表外的所有事件被 drop。**事件不会丢失** —— 仍写入 `events.jsonl` 与 dashboard，只是不推飞书。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `quiet_hours.enabled` | `false` | bool | 总开关。false → 整段逻辑短路、永不静音。 |
| `quiet_hours.start` | `"23:00"` | string `HH:MM` | 静音时段起点（含）。`start > end` 时自动按跨午夜处理（如 23:00→08:00）。 |
| `quiet_hours.end` | `"08:00"` | string `HH:MM` | 静音时段终点（不含）。 |
| `quiet_hours.tz` | `"Asia/Shanghai"` | string (IANA tz) | 解析 start/end 用的时区。非法或 zoneinfo 不可用 → 回退本地时区。 |
| `quiet_hours.passthrough_events` | `["Notification","TimeoutSuspect"]` | list[string] | 即使时段命中也照推的事件类型（关键事件穿透）。建议保留 `Notification`/`TimeoutSuspect`，否则真等输入时也叫不醒。`TestNotify` 始终穿透（硬编码）。 |
| `quiet_hours.weekdays_only` | `false` | bool | true → 仅周一~周五（Mon=0..Fri=4）生效，周末不静音。 |

**关于「事件不丢失，恢复后会补送」**：当前实现里 quiet hours 静音的事件**不会被自动补送**到飞书；它们正常写入 events.jsonl，所以在 dashboard 上仍可见。如果你需要"夜里发生、早上补推"的语义，请保持 `Notification`/`TimeoutSuspect` 在 passthrough，让真正关键的事件即使夜里也叫醒你。

**何时改**：默认 `enabled=false`。希望夜里不被打扰 → 开 true，调整 start/end；如果只想工作日生效 → `weekdays_only=true`。

---

## 7. LLM 摘要 `llm`

可选的 LLM 摘要功能（用于把长 assistant 答复抽成 last_milestone）。默认关闭；启发式已经覆盖 90% 场景，只在用户主动启用时调用。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `llm.enabled` | `false` | bool | 总开关。false → 完全不走 LLM。 |
| `llm.provider` | `"local_cli"` | enum: `local_cli` \| `anthropic` \| `openai` | 后端选择。`local_cli` 调本机 `claude` CLI（Claude Code 用户零配置首选）。 |
| `llm.topic_enabled` | `false` | bool | 是否让 LLM 生成会话 topic（标题）。实测 LLM 对"新建会话"这种空泛 prompt 反而劣化，默认 false。 |
| `llm.event_summary_enabled` | `true` | bool | 是否让 LLM 生成 last_milestone（从长 assistant 答复抽核心动作）。仅当 `llm.enabled=true` 时才真正调用。 |

### 7.1 `llm.local_cli`（默认 provider）

调本机 `claude` 二进制（Claude Code CLI）。Claude Max 订阅用户零成本可用。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `llm.local_cli.binary` | `""` | string (path) | claude CLI 路径。留空 → 自动从 PATH 与 nvm 探测。 |
| `llm.local_cli.model` | `"claude-haiku-4-5"` | string (model id) | 调用的模型 ID。 |
| `llm.local_cli.timeout_s` | `30` | int (秒) | 单次调用超时。OAuth 冷启动通常 11-15 秒，30 秒预留余量。 |
| `llm.local_cli.mode` | `"auto"` | enum: `auto` \| `bare` \| `oauth` | `bare` = 走 `ANTHROPIC_API_KEY` 直连 API（快）；`oauth` = 走 Claude Max 订阅（慢）；`auto` = 优先 bare、fallback oauth。 |
| `llm.local_cli.extra_args` | `[]` | list[string] | 透传给 `claude` CLI 的额外参数（如 `["--allow-dangerously-skip-permissions"]`）。 |

### 7.2 `llm.anthropic`

直连 Anthropic API。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `llm.anthropic.api_key` | `""` | string | Anthropic API key。留空 → 读环境变量 `ANTHROPIC_API_KEY`。 |
| `llm.anthropic.model` | `"claude-haiku-4-5"` | string (model id) | 模型 ID。 |
| `llm.anthropic.base_url` | `""` | string (URL) | 自定义 endpoint（公司代理 / Bedrock proxy）。留空走官方。 |
| `llm.anthropic.timeout_s` | `8` | int (秒) | 单次调用超时。 |

### 7.3 `llm.openai`

OpenAI 兼容协议；可切到 DeepSeek / Qwen / Ollama / 自托管。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `llm.openai.api_key` | `""` | string | API key。留空 → 读环境变量 `OPENAI_API_KEY`。 |
| `llm.openai.model` | `"gpt-4o-mini"` | string (model id) | 模型 ID。可写 `deepseek-chat` / `qwen-turbo` / 任意 Ollama 本地模型名。 |
| `llm.openai.base_url` | `"https://api.openai.com"` | string (URL) | API base URL。DeepSeek：`https://api.deepseek.com`；Ollama：`http://localhost:11434`。 |
| `llm.openai.timeout_s` | `8` | int (秒) | 单次调用超时。 |

**何时改**：默认 `llm.enabled=false` 即可（启发式已够用）。如果你发现 last_milestone 列里很多是"完成 / 已修复"这种短词、想要更具体的描述 → `llm.enabled=true`，provider 用 `local_cli`（零配置）或 `openai` + DeepSeek（成本最低）。

---

## 8. 归档 `archival`

events.jsonl 长期 append 会无界增长。开启归档后，老事件会 gzip 落到 `data/archive/`，热文件只保留近期事件。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `archival.enabled` | `true` | bool | 是否启用归档。false → events.jsonl 一直增长（重度用户慎关）。 |
| `archival.max_hot_size_mb` | `50` | int (MB) | 热文件大小达到此 MB 才触发归档检查。 |
| `archival.rotation_grace_days` | `1` | int (天) | 只归档 ts 早于"now - 此天数"的事件；近期事件保留在热文件。 |
| `archival.archive_dir` | `"data/archive"` | string (path) | 归档目录相对路径（相对项目根）。改完**需要重启 backend** 才生效。 |

**何时改 `max_hot_size_mb`**：默认 50MB 通常够用。希望更早归档（如低内存机器）→ 调到 20；几乎不归档 → 调到 200。

---

## 9. 别名 / 备注 / Session 静音

这三组通常在 dashboard UI 操作，不手改 config.json。但底层数据落盘位置如下。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `session_mutes` | `{}` | dict[sid → entry] | 单 session 静音表。结构：`{"<sid>": {"until": <unix\|null>, "scope": "all"\|"stop_only", "muted_at": <unix>, "muted_at_iso": "...", "label": "..."}}`。`until=null` 表示永久；`scope=all` 拒推该 sid 全部事件；`scope=stop_only` 仅静 Stop/SubagentStop，Notification/TimeoutSuspect 仍推。过期项会被 watcher 命中时清理。在 dashboard 卡片右上 🔕 按钮维护。 |
| （aliases） | 不在 config | 见 `data/aliases.json` | 别名 + 备注独立文件 `data/aliases.json`，schema：`{"<sid>": {"alias": "...", "note": "...", "updated_at": "iso8601"}}`。在 dashboard 卡片"重命名"操作维护。 |
| （notes） | 不在 config | 见 `data/notes.md` | 全局记事本独立文件 `data/notes.md`，纯文本 markdown，最大 256 KB。在 dashboard 记事本面板维护。 |

**注意**：aliases 和 notes 不在 `config.json` 内，它们是独立文件，避免和 config 的 deep-merge 语义冲突。

---

## 10. 显示 `display`

控制 dashboard 卡片显示哪些字段。

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `display.show_first_prompt` | `true` | bool | 卡片是否显示用户首条 prompt（任务起源信息）。 |
| `display.show_last_assistant` | `true` | bool | 卡片是否显示 Claude 最后一条 assistant 答复。 |

---

## 11. 进阶 / 全局开关

| 字段 | 默认 | 取值/类型 | 含义 |
|---|---|---|---|
| `muted` | `false` | bool | 全局静音开关。true → 所有飞书推送在最终发送前被 drop（dashboard 仍记录事件）。dashboard 顶部"全局静音"按钮维护。 |
| `snooze_until` | `""` | string (ISO8601) | 临时贪睡截止时间。非空且未过期 → 推送被 drop。过期后字段值仍在但不再生效。dashboard "贪睡 N 分钟" 按钮维护。 |

---

## 写入约定与热加载

- `data/config.json` 是热加载的；改完保存后下一次事件来时即应用。极少数字段（如 `archival.archive_dir`、`liveness_interval_seconds`）需要重启 backend 才生效，已在表格的「含义」列标注。
- `config.json` 只持久化"与 DEFAULTS 不同"的字段：改回默认值后会从文件里消失，未来 DEFAULTS 升级老用户自动跟上。所以你的 `config.json` 看起来很瘦是正常的。
- 直接编辑 `config.json` 时请保持 JSON 合法（用 `python -m json.tool data/config.json` 验证）；UI 修改走 backend 接口，会原子写入（tmp + rename），不会半截损坏。
