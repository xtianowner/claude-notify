<!-- purpose: 设计教训记录 — 删除/否定一个设计前先写一段简要总结，避免后续重复犯错 -->

创建时间: 2026-05-09 16:25:00
更新时间: 2026-05-09 23:05:00

# 设计教训

## L01 — silence-then 单 task 模型：后续事件无条件 cancel 的副作用（已修）

**修改时间**：2026-05-09

**原设计**：`backend/notify_policy.py` 用 `_pending: dict[sid, asyncio.Task]`，新事件来时 `_schedule()` 内 `old.cancel()` 取消旧 pending、起新 task。

**为什么不行**：当主 agent 完成回合（Stop）后立刻派子 agent（SubagentStop），两事件间隔通常 < 12s 也 < 6s（silence 默认值），SubagentStop 会**永久吞掉**前面的 Stop。后果：dashboard 有事件，飞书没推送。
真实事故重现（13731877 session）：
```
15:11:02 Stop          → 排队等 12s
15:11:12 Stop          → cancel 上一个，重新排队
15:11:15 SubagentStop  → cancel 第二个，自己排队
                          ↑↑↑ 两个 Stop 永久消失
15:11:21 → 只飞了 SubagentStop
```

**替代方案（已实施）**：合并模式（方案 B）
- `_pending` 改为 `dict[sid, _Pending]`，`_Pending` 持有 `events: list` + `task` + `fire_at`
- 同 sid 后续事件 **append 到 events**、**不变 fire_at**、**不重新 schedule**
- 倒计时到点：1 条单发；> 1 条调 `feishu.send_events_combined()` 渲染最新事件主格式 + 「另含 N 个先发事件: …」一行
- ACTIVITY 事件 / immediate 事件 / Notification / TimeoutSuspect 行为不变（仍取消 pending）

**验证**：`scripts/0509-1620-test-silence-merge.py` 6/6 case pass。

**核心教训**：节流策略要区分「同语义事件覆盖」和「不同语义事件合并」。前者可 cancel，后者必须保留。设计 silence/throttle 时**默认是合并而非覆盖**。

---

## L02 — LocalCLIProvider 触发自家 hook 形成无限递归（已修）

**修改时间**：2026-05-09

**事故现象**：用户启用 `本机 claude -p` provider 后，dashboard 几十秒内被几十张「00-Tian-Project/claude-notify / 正在工作中」session 卡片刷屏，其中一条标题赫然是 LLM 自己的 system prompt 「你是一名简明的会话标题生成器...」。

**根因**：`LocalCLIProvider.complete()` 用 `asyncio.create_subprocess_exec` 启 reclaude/claude/ctf 子进程跑 LLM 推理。这些子进程也是 Claude Code，**会读 ~/.claude/settings.json 的全局 hook**。每次 LLM 调用：
```
backend 收到 hook event A
 → _llm_enrich(A)
 → 启 reclaude -p 子进程
 → 子进程触发 PreToolUse / Stop hook
 → hook-notify.py 写 events.jsonl + POST /api/event
 → backend 收到 hook event A'
 → _llm_enrich(A')
 → 启又一个 reclaude -p ...     ♾️
```
触发 prompt 内容（system prompt + user prompt）原文被 hook 读出 → 当成 first_user_prompt 写入 events.jsonl → 显示成卡片标题。

**替代方案（已实施）**：环境变量打标识 + hook 自检。
- `LocalCLIProvider` 启子进程时 `env["CLAUDE_NOTIFY_LLM_CHILD"] = "1"`
- `scripts/hook-notify.py` 顶部检测此 env，命中即 `return 0`（不写 events、不 POST）

**验证**：带 env 跑 `reclaude -p` → events.jsonl 0 行新增；不带 env → 3 行新增。

**核心教训**：
1. **任何在 backend 进程内调用 claude/reclaude 子进程的代码**都必须打这个标识，不仅 LLM provider —— 未来如果加 codex / 其它 CLI 调用同理（每个 wrapper 都要带 `CLAUDE_NOTIFY_LLM_CHILD=1`）。
2. **全局 hook 的 blast radius 很大**：~/.claude/settings.json 注册的 hook 对**所有** Claude Code 进程生效，包括你自己 spawn 出来的。设计任何 spawn claude 子进程的功能时，先想清楚是否会触发自己。
3. **hook 应该自带防递归 guard**：默认所有自家 spawn 的子进程都跳过 hook，不依赖 spawner 主动避让。

---

## L03 — osascript 的 `tell application "System Events"` 静默挂起（已修）

**修改时间**：2026-05-09

**事故现象**：dashboard 抽屉「→ 终端」按钮点击无反应。日志显示 `osascript_timeout`（3 秒）。但手动跑 `osascript -e 'tell application "Terminal" ...'` 60ms 即返回。

**根因**：脚本里 `tell application "System Events"` 检查进程是否存在（`exists process "iTerm2"`）。System Events 的 process 查询要求 macOS **Accessibility 权限**，Python 启动的 backend 进程（即 uvicorn）默认没获得该权限 → osascript 静默挂起在权限请求上 → 3 秒超时返回。

**替代方案（已实施）**：完全不用 System Events，对 iTerm2 / Terminal 直接 `try ... end try`：
```applescript
try
  tell application "iTerm2"
    repeat ...
  end tell
end try
try
  tell application "Terminal"
    repeat ...
  end tell
end try
return "not_found"
```
应用没装 → tell 抛错 → try 吃掉 → 继续下一个 / 返回 not_found。

**核心教训**：在没有 Accessibility 权限的进程里**不要碰 System Events**。如果一定要查应用是否在跑，用 try/catch 替代条件判断（按 macOS 习惯，应用未运行时 tell 会抛错，try 吞下即可）。

---

## L04 — claude -p 每次调用消耗 30K+ cache read tokens（已修，但 OAuth 模式有局限，见 L06）

**修改时间**：2026-05-09

**事故现象**：用户 dashboard 配 `本机 claude -p` provider 后，每次 LLM 摘要调用 token 用量 ~34K。

**根因 + 修法（最初版本，bare 模式有效，oauth 模式无效）**：
- `--bare` 强制 ANTHROPIC_API_KEY，跳过 CLAUDE.md / auto-memory / hooks / plugin sync
- `--system-prompt <text>` 替换默认 system prompt
- `--exclude-dynamic-system-prompt-sections` 移走 cwd/env/git 段

bare 模式实测 token 从 34K → < 1K，符合预期。

**OAuth 模式失败原因（教训 L06 才发现）**：
1. `--exclude-dynamic-system-prompt-sections` 在 `--system-prompt` 同存时**被静默忽略**（claude --help 写明）
2. CLAUDE.md / auto-memory **不在 system prompt 里**，是注入到 user message 的 → `--system-prompt` 替换无法跳过这些
3. OAuth 鉴权依赖 ~/.claude/* 文件存在，不能 `HOME=/tmp` 绕过

OAuth 模式下 token 仍在 28K 量级，本笔记的"OAuth 模式也能近似 bare 节省"结论错误。正确方向见 L06。

**核心教训（部分仍有效）**：
1. **最小必要上下文原则** —— bare 模式下成立。
2. **CLI flag 互斥关系要看清** —— `--exclude-dynamic-...` 与 `--system-prompt` 互斥，help 文档第三段写明，我之前漏看。改 flag 前先 grep 文档里的 "ignored with" / "only applies"。
3. **--bare 是唯一彻底跳上下文的开关** —— 之前误判可被 `--system-prompt` 替代。

---

## L06 — OAuth 模式 token 优化思路：从「减少」转向「最大化 cache hit」（已修）

**修改时间**：2026-05-09

**事故现象**：L04 修完后用户报告"还是 28.38k"。即便加了 `--system-prompt` + `--exclude-dynamic-system-prompt-sections`，OAuth 模式 token 量没降。

**根因（L04 的延伸认知）**：
- OAuth 模式下 `--bare` 不可用（强制 API key 与 OAuth 互斥）
- `--exclude-dynamic-system-prompt-sections` 与 `--system-prompt` 同存时被忽略
- CLAUDE.md / auto-memory / hooks / plugin sync 在 OAuth 流程中**必加载**（鉴权依赖）

**思路转换**：OAuth 模式下硬压 token 不可行，但 prompt cache 命中可大幅降低**计费**：
- 默认 system prompt 跨调用稳定 → cache 起来后 fresh tokens 极低
- cache read tokens ≈ 0.1× input tokens 的计费（几乎免费）
- 28K cache reads + 200 fresh ≈ "本质无成本"

**替代方案（已实施 + 实测验证）**：按模式分支策略
```python
if mode == "bare":
    # 老路：彻底跳过默认上下文
    argv += ["--bare", "--system-prompt", system]
else:  # oauth
    # 新路：最大化 cache 复用（仅 --append-system-prompt，不传 --exclude-dynamic-...）
    argv += ["--append-system-prompt", system]
# cwd 固定 /tmp：跳项目 CLAUDE.md + cwd 稳定提升 cache 复用
proc = create_subprocess_exec(*argv, cwd="/tmp", ...)
```

**实测对比（OAuth 模式 + reclaude 二进制）**：

| 配置 | 第 1 次 | 第 2 次 cache_creation | 第 2 次 cache_read | 第 2 次 cost | 第 3 次 cost |
|---|---|---|---|---|---|
| 带 `--exclude-dynamic-...` | $0.043 (33K creation) | 6018 | 61206 | $0.017 | (波动) |
| **不带** `--exclude-dynamic-...` | $0.043 (33K creation) | 8182 | 25106 | **$0.014** | **$0.014** ✅ |

不带 `--exclude-dynamic-...` 反而稳定 hit。原因：`--exclude-dynamic-...` 把 cwd/git/env 移到 user message，而 **user message 每次内容不同（每次 user prompt 是不同的事件摘要）**，这部分 cache 不复用，每次 6K 新 creation。留在 system prompt 里 + cwd=/tmp 固定 → system prompt 跨调用完全稳定 → cache hit 稳。

Anthropic prompt cache 1 小时 TTL（`ephemeral_1h_input_tokens`），不是 5 分钟。连续摘要调用 1 小时内都受益。

OAuth 模式下：
- 不替换 default system prompt → 跨调用 cache hit 稳定
- `--append-system-prompt` 把摘要指令追加到尾部 → 第一次后稳定
- `--exclude-dynamic-...` 不再被忽略（无 `--system-prompt`）→ cwd/git/env 进 user msg → 跨 cwd 调用也命中
- `cwd="/tmp"` → 跳过项目 CLAUDE.md auto-discovery，且 cwd 稳定提升 cache 复用

**核心教训**：
1. **OAuth ≠ bare**：OAuth 用户的优化目标不是减少 token 总量，而是最大化 prompt cache 命中。token 数字大但**计费小** —— 看 cache_read_input_tokens vs input_tokens 区分。
2. **flag 互斥关系决定哪些能共存** —— `--exclude-dynamic-...` 只在不传 `--system-prompt` 时生效。要走 cache-friendly 路径就得改用 `--append-system-prompt`。但实测发现不传 `--exclude-dynamic-...` 反而 cache 复用更好（避免 user msg 每次新 creation）。
3. **cwd 是被忽视的杠杆** —— claude.md auto-discovery 从 cwd 一路爬到 ~/，把子进程 cwd 固定到 /tmp 是简单粗暴但有效的隔离手段。
4. **"修了"≠"测了"** —— L04 当时只验证了 bare 模式 6.27s 延迟，没测 OAuth 模式实际 token。下次涉及 token / 成本类修改，必须按用户实际模式实测。

---

## L07 — LLM 摘要价值被高估，启发式应作为 Default（已修）

**修改时间**：2026-05-09

**事故现象**：用户报"token 损耗有点大"，要求测试关 LLM 后效果，并优化启发式逻辑。

**关 LLM 实测结果**（4 个真实 session）：

| session | LLM topic | 启发式 (新) | 哪个更好？ |
|---|---|---|---|
| `f39866f5` (hello) | "新建会话" | "hello" | 启发式 ✅ LLM 反而劣化 |
| `9eb39d2b` | (没 cache) | "查看当前项目，加入setting页面..." | 启发式（信息量大）|
| `cfaf4bc2` | "1+1=" | "用一句话回答：1+1=" | 启发式 ✅ |
| `be17b8c7` | (没 cache) | "感觉损耗有点大, 测试不开启llm..." | 启发式 |

**根因**：启发式的弱点不是质量，是覆盖率。`_derive_task_topic` 旧版要求"≥2 句 + 跨句命中 + 至少 1 个英文 token"，门槛太严，3/3 测试 session 全部空命中，fallback 链才生效。LLM 只是补了这个空白，但补出的内容（"新建会话"）质量经常比 fallback `recent_user_prompt[:28]` 还差。

**替代方案（已实施）**：M1 + M2 + M3 三件套。

**M1 放宽 task_topic 启发式**：
- 多句 + 命中英文 token → 输出 `"tokenA · tokenB"`（保留旧行为）
- 多句但命中失败 OR 单句 → 直接 `_topic_from_single_prompt(prompt[0])` 截前 28 字
- 实测：4/4 session topic_heuristic 非空（之前 0/3）

**M2 LLM 默认配置调整**：
- `DEFAULTS.llm.enabled = False`（已经是）
- `topic_enabled` 默认 False（启发式已够用）
- `event_summary_enabled` 默认 True（last_milestone 是 LLM 真正有价值的场景）
- `list_sessions()` / `read_events_for()` 在 `llm.enabled=False` 时**忽略 LLM cache**，反映启发式真实效果（cache 保留以便重启）

**M3 clean_summary 增强**：
- 切句子（按句末标点）→ 优先选含锚词（"完成/修复/已/跑通/done/fixed"）的句子
- 跳过寒暄首句（"好的/嗯/明白/ok"）取下一句
- 选中后再剥离寒暄前缀（"好的，<实质>" → "<实质>"）
- 实测：'好的，我已经完成了端到端验证。' → '我已经完成了端到端验证。'；'你好！👋 有什么需要帮助的吗？' → '👋 有什么需要帮助的吗？'

**核心教训**：
1. **LLM 不是 free improvement** —— LLM 摘要在简单场景（单句对话、短答复）反而劣化。质量评估要看分位（P50/P90），不是"开比关好"。
2. **覆盖率优先于精度** —— 启发式之前为追求"不乱切"返回空，让 LLM 当救火队员。**正确做法是降低启发式精度门槛 + 一定比例可读输出 > 偶尔精彩但常空白**。
3. **接受用户原话作 topic** —— 用户最近一条 prompt 截前 28 字，往往比任何"提炼"都更准确反映焦点（焦点本来就来自用户）。
4. **配置开关要 gate cache 读取** —— 用户关 LLM 后旧 cache 还在用，违反预期。`enabled=False` 应该全链路屏蔽，cache 保留但不读。
5. **"性能优化"和"功能优化"是两件事** —— L04-L06 试图通过 cache 命中降低 LLM 成本（性能侧），L07 反过来质问"是否真需要 LLM"（功能侧）。当性能优化触底但用户仍嫌贵时，回到功能层重审"必要性"。

---

## L05 — 同回合 Stop + Notification 双推送（已修）

**修改时间**：2026-05-09

**事故现象**：用户对一个简单 hello 问候收到两条飞书：
```
[19:08:29] ✅ [Claude] 任务完成（Stop, 含 milestone "你好！有什么需要帮忙的？"）
[19:09:29] 🔔 [Claude] 等你确认（Notification, message="Claude is waiting for your input"）
```
间隔正好 60 秒，session id 相同。用户原话："一个问题 没必要提醒两次"。

**根因**：Claude Code 在主回合结束时发 Stop hook，约 60s 后还会派一条 Notification 提示"还在等输入"——对 Claude Code 是两个独立 hook 触发点；对用户是同一交互回合的两面（已完成 → 等输入）。
- silence 节流窗 ~12s，60s 已过，silence 合并管不到
- Notification 在 `notify_filter._ALWAYS_TRUE` 直通无门槛
- Notification message "waiting for your input" 是空话，无新增信息

**替代方案（已实施）**：跨事件 dedupe（filter 层 + dispatcher 内存表）
1. `NotifyDispatcher` 加 `_last_stop_pushed_at: dict[sid, float]`，Stop 推送成功后写入
2. `_summary_with_dedupe()` 在调 `should_notify` 前把 ts 注入 summary
3. `notify_filter` 把 Notification 拆出 `_ALWAYS_TRUE`，加 `_notification_decision`：
   - `last_stop_pushed_unix` 距今 < 3 分钟 + message 命中 filler 短语黑名单 → 吞
   - 黑名单："waiting for your input"、"press to continue"、"press enter"
4. config 加 `notif_suppress_after_stop_min: 3` + `notif_filler_phrases` 可调

**为什么不扩大 silence 窗**：silence 是同窗事件合并机制，把它扩到 60-90s 会让所有事件都延迟，破坏即时性。dedupe 是显式跨语义事件去重，只对 Stop → Notification 这一对生效。

**核心教训**：
1. **节流（silence）和去重（dedupe）是不同机制**。节流处理"短窗内连发"；去重处理"跨窗的语义重复"。需求是后者就别拉长前者。
2. **Stop 成功推送的 ts 是新 dedupe 信号**，必须从 dispatcher 流到 filter。最简方案：dispatcher 加内存表，summary lookup 时 merge 进去（不污染 event_store schema、不持久化、进程重启自动清空）。
3. **filler 内容兜底**：纯时间窗判断在边界（2:59 vs 3:01）会有抖动，配合"内容是空话"再确认一层，更稳健。
4. **用户直觉 "时间放最前" 反而是错的** —— 飞书消息列表自带时间戳渲染，正文第一行该让 BLUF。要敢和用户讨论用户预期不一定最优，给搜索结论 + 替代 mockup 让用户做出判断。

---

## L08 — 子 agent Notification 错推（已修，启发式）

**修改时间**：2026-05-09

**场景**：用户在跑主任务，主 agent 派子 agent（Task tool）；子 agent 内部触发权限确认 / idle prompt → Claude Code 打 Notification hook → backend 推送飞书 → 用户切到终端却看不到任何确认提示（因为子 agent 自己处理了或父 session 已恢复）→ 产生「虚假打扰」破坏信任。

**问题**：Claude Code 的 Notification hook payload **不带任何 sidechain / parent_session / is_subagent 标记**。实测父 / 子 agent 触发的 Notification payload 几乎完全一致：
- `session_id` 永远是父 session 的（子 agent 继承）
- `transcript_path` 永远指向父 transcript（`<sid>.jsonl`，不指向 `<sid>/subagents/agent-*.jsonl`）
- `cwd` / `permission_mode` / `hook_event_name` 都一样
- 唯一携带 sub-agent 标识的 hook 是 SubagentStop（带 `agent_id` / `agent_type` / `agent_transcript_path`），其它事件没有

对比：Claude Code 的 transcript jsonl **本身**有 `isSidechain: true` 字段（每条 user/assistant message 都带），但是写到独立目录 `<parent_dir>/<sid>/subagents/agent-<aid>.jsonl`，**不混进父 transcript**。父 transcript 永远 `isSidechain=false`。

**解决（启发式）**：`detect_sidechain_active(transcript_path)` —— Notification 触发瞬间检查 `<parent_transcript_dir>/<sid>/subagents/` 目录是否有任何 `agent-*.jsonl` 在最近 5 秒内被写过。是 → 视为 sidechain active → `notify_filter` 吞这条 Notification（reason=`sidechain_active`）。
- `backend/sources.py` `_normalize_claude_code()` 仅对 Notification 跑检测，写入 `is_sidechain` 字段
- `backend/notify_filter.py` `_notification_decision()` 读 `is_sidechain` + `cfg.notify_filter.filter_sidechain_notifications`（默认 True，用户可关）
- `backend/config.py` `DEFAULT_NOTIFY_FILTER` 加默认开关
- 启发式精度：mtime 窗 5 秒，覆盖 hook 触发前后子 agent 任意 jsonl 写入；O(目录 entry 数) IO，无远程调用

**验证**：`scripts/0509-2030-test-sidechain-filter.py` 6/6 case pass（主 session 推送 / 子 agent 过滤 / 配置关闭恢复推送 / 老 jsonl 不算 active / 空路径降级 / Stop 跳过检测）。

**核心教训**：
1. **Claude Code hook payload 没有原生 sidechain 标记** —— 不要凭空假设字段名。先 grep 现有代码 + 实测 events.jsonl payload 找出真正可用字段，再设计。
2. **transcript 文件系统布局是隐藏信号源** —— 父 / 子 transcript 在不同路径（`<sid>.jsonl` vs `<sid>/subagents/agent-*.jsonl`），mtime 是几乎免费的活性指标。Claude Code 没暴露的状态，文件系统暴露了。
3. **启发式有边界，必须可关** —— 5 秒窗会误吞「父 agent 在子 agent 刚结束 5s 内的真实 Notification」（罕见），用户必须能 `filter_sidechain_notifications=false` 退回原行为。默认 True 是因为「子 agent 错推」比「父 agent 5s 内边界误吞」频率高 1-2 个数量级。
4. **保守 fallback** —— 任何检测失败（路径不存在 / 权限拒绝 / payload 没 transcript_path）一律返 False = 不过滤 = 不冤推空。少推不如错推，但虚假打扰比少推更伤信任，所以这里的兜底是 `is_sidechain=False = 推送`。
5. **检测只在必要事件上跑** —— Notification 才需要，Stop / SessionEnd 等不跑（性能 + 清晰边界，避免未来 SubagentStop 也走错误分支）。

---

## L09 — TimeoutSuspect 单一阈值误判长 tool 执行为 hang（已修）

**修改时间**：2026-05-09

**事故现象**：用户跑长 tool（curl 大文件 / 4 分钟 build / 等 LLM 慢响应），3-8 分钟无新事件，飞书收到「⚠️ 疑似 hang」误推。任务实际正常跑。

**根因**：旧版 `liveness_watcher.watch_loop` 只看「最后事件的时间戳」，不看「最后事件的类型」：
```python
if effective_age >= timeout_min * 60:  # 5 分钟一刀切
    push TimeoutSuspect
```
但事件类型其实已经携带状态信息：
- `Notification` / `Stop` 之后无新事件 = **真的等用户输入** → 5 分钟报 hang 合理
- `PreToolUse` 之后无 `PostToolUse` = **正在执行 tool**（curl/build/LLM）→ tool 自己慢，不该报
- `PostToolUse` 之后无新事件 = Claude 在思考下一步 → 短窗 OK，长窗可疑

**替代方案（已实施）**：分状态阈值 + transcript 活性兜底
1. `event_store.list_sessions` 暴露新字段 `last_event_kind` —— 真实最后一条事件的 `event` 字段（含 PreToolUse/PostToolUse/Heartbeat），与 status 派生用的 `last_event` 字段独立（后者过滤掉 NON_STATUS_EVENTS，扩它语义会牵动 dashboard / status）
2. `config.py` 加 `liveness_per_state_timeout` dict（默认值见 `DEFAULT_LIVENESS_PER_STATE_TIMEOUT`）：
   - `Notification_minutes / Stop_minutes / SubagentStop_minutes`：5（保持原 timeout 5 分钟）
   - `PreToolUse_minutes`：15（长 tool 容忍）
   - `PostToolUse_minutes`：10（思考下一步）
   - `default_minutes`：10
   - `pretool_transcript_alive_seconds`：60（PreToolUse 状态 transcript 活性兜底）
   - `enabled`：True（关闭即退化为旧 timeout_minutes 一刀切，保留 kill switch）
3. `liveness_watcher.watch_loop` 调 `_pick_timeout_seconds(last_kind, per_state_cfg, fallback)` 取阈值；PreToolUse 状态再加一层 transcript mtime 兜底（mtime 在最近 60s 内 → 视为活，跳过本轮报警）
4. TimeoutSuspect 事件 raw 里多带 `last_event_kind` / `threshold_bucket` / `threshold_sec` 三字段，dashboard / 排障可追溯"为什么这次报 / 不报"

**验证**：`scripts/0509-2030-test-liveness-states.py` 8/8 case pass。覆盖 Notification / PreToolUse(短·长) / PreToolUse 活性兜底 / PostToolUse(短·长) / legacy 退化。

**核心教训**：
1. **"距今多久"是指标，不是判定** —— 同样 8 分钟无事件，状态不同含义完全不同。指标设计要带"上下文标签"（这里是 `last_event_kind`）。
2. **新增字段而不改既有字段语义** —— `last_event` 已被定义为「会改 status 的事件」（NON_STATUS_EVENTS 过滤），扩它的语义会牵动 dashboard / status。直接加 `last_event_kind` 是隔离的最小改动，与 L08 的 `is_sidechain` 字段思路一致。
3. **保留可关闭开关** —— 默认开新逻辑（修复用户痛点），但 `enabled=False` 退化到旧逻辑作为安全网。任何 watcher 类核心判定的修改都该有 kill switch。
4. **transcript mtime 是廉价的旁路信号（与 L08 同源）** —— PID 还活说明进程在跑、transcript mtime 还新说明 Claude 在写文件。两者结合比单看事件流稳得多（tool 执行期间 Claude Code 不发 PostToolUse hook 的话，transcript mtime 仍能识别活性）。

---

## L11 — Stop 短回合漏推：字数阈值过严 + 黑词子串吞合法回合（已修）

**修改时间**：2026-05-09

**事故现象**：用户问"查一下 X"，Claude 一行回"已完成"。Stop 触发后 `notify_filter._stop_decision` 拒推 → 飞书收不到 → 用户只能手动刷 dashboard 才知道任务结束。短回合 + 短回复在用户实际使用中占很大比例，这违反「Stop 立即可见」的核心承诺。

**根因（双重判负）**：
1. 字数阈值过严：`stop_min_milestone_chars=12` / `stop_min_summary_chars=15`。"已完成"3 字 / "看一下啊"4 字、"我已经处理完毕"7 字均不到 15 → 拒。
2. 黑词表用 `in` 子串匹配："已完成端到端验证" 这种**带任务说明**的回合，因为含子串"已完成"被吞。
3. 没有时间窗 fallback：长任务跑了 10 分钟回一句"完成"，旧逻辑同样按字数拒，丢的是最重要的"任务结束时刻"。

**替代方案（已实施）**：把 Stop 通过判定改为「以下任一」OR 关系，旧逻辑只是其中一支。

**新条件**：
1. milestone / summary 字数过阈值（保留旧逻辑，但阈值大幅放宽：12/15 → 6/8）
2. **锚词放过**：`last_milestone` / `turn_summary` / `last_assistant` 任一含 `sources._ANCHOR_KEYWORDS`（"完成 / done / passed / 失败 / 报错 …"）→ 直接放过不计字数
3. **时间窗 fallback**：距离同 sid 上一次推送（Stop 推 ts 与 Notification 事件 ts 取 max）> `stop_short_summary_grace_min`（默认 8 分钟）→ 即使 summary 极短也推。从未推过 → 视为首推也放过
4. 旧的"距上次 Notification > min_gap" 兜底保留

**新 config**：
- `stop_min_summary_chars` 默认 12 → **8**（normal 档）
- `stop_min_milestone_chars` 默认 12 → **6**（normal 档）
- `stop_short_summary_grace_min` **新增**，默认 8 分钟
- `stop_sensitivity` **新增**，"fast" | "normal" | "strict"，默认 "normal"
  - fast：milestone=4 / summary=4（几乎全推）
  - normal：6 / 8（默认；尊重用户自定义 stop_min_*_chars 字段）
  - strict：20 / 20（仅有内容回合）
  - 非 normal 时 sensitivity 覆盖 chars 字段值

**黑词整句相等**：
- 旧 `_is_blacklisted` 实际就是 `norm in blacklist`，已经是整句相等。但用户曾抱怨"已完成端到端验证"被吞，原因不在 blacklist 本身（黑词只判 last_assistant），而是字数条件不够 + 没有锚词逃逸。L11 通过加锚词条件、加时间窗 fallback，让"已完成端到端验证"无论走 summary_len（8 字过 normal 阈值）还是 anchor 都能通过。
- 黑词的"长度 ≤ 4 字 + 完全无标点空格"短回复保护**保留**，避免把"两字"这种没语义的内容也推。

**验证**：`scripts/0509-2100-test-stop-threshold.py` 11/11 case pass，覆盖三档 sensitivity 实际阈值表 + grace 窗 fallback + 锚词放过 + 黑词整句 + 长任务首推。

**核心教训**：
1. **字数阈值是最粗暴的过滤器** —— 短回合本质上是真实使用模式（一问一答），用字数当唯一阀门会系统性丢失这部分。要么加多元信号（锚词、时间窗），要么默认放过。L05 / L08 / L09 的方向都是"加上下文标签 + 多元判定"，L11 同源。
2. **过滤层的 OR 关系比 AND 关系安全** —— 任一条件通过即放过 = 用户少吃漏推。AND 关系（所有条件都过）= 用户多吃漏推。Stop 这种"用户已经发了请求等回执"的事件，**漏推比错推致命**。
3. **sensitivity 预设比单独调字数有用** —— 用户不知道字数阈值的合适数字，但能直观说"我希望短回合也推（fast）"或"只推有内容的（strict）"。三档预设把决策抽象到用户语言层。
4. **跨事件 ts 的 max 是廉价的"上一次推送"代理** —— 不需要新加 dispatcher 字段，复用已有的 `last_stop_pushed_unix` + `last_notification_unix` 取 max 即可。最少改动原则（L08 / L09 同源）。
5. **配置默认值改动需要在 LESSONS 标注前后值** —— 这次 12/15 → 6/8 是显著行为变更，老用户会感知。文档里写清"为什么改"，未来回头看时不会再误以为"6/8 一直是默认"。

---

## L12 — 推送决策 reason 只在 logger，dashboard 看不到"为什么没推"（已修）

**修改时间**：2026-05-09

**用户痛点**：`notify_filter._stop_decision` / `_notification_decision` / `notify_policy.submit` 已经返回了大量精确的 reason 字符串（`sidechain_active` / `notif_dedup_after_stop(1.5min)` / `stop_grace_first_push` / `stop_anchor:last_assistant` / `policy_off` / `merged_into_pending_3_events` …），但这些 reason **只在 logger 输出**，不落盘、不返回前端。用户怀疑漏推时只能 `tail -f` backend log 配合自己解析时间戳排查。dashboard 上的卡片完全没暴露"这条事件为什么被吞 / 为什么延迟到 12s 后再推"。

**替代方案（已实施）**：单独的环形决策日志 + 注入到 list_sessions + 卡片底部 trace + modal。
1. 新模块 `backend/decision_log.py`：
   - 写入 `data/push_decisions.jsonl`，每行 `{ts, session_id, event, decision, reason, policy}`
   - `recent_for(sid, limit=5)` / `all_for(sid)` 读路径，倒序最新在前
   - `PER_SESSION_MAX=50`：trim 时按 sid 分组，保留每组最近 50 条
   - `TRIM_THRESHOLD=1000`：累计文件行数 ≥ 此值才整体重写一次（避免每次写都全量）
   - 文件锁 `fcntl.flock`，跨进程安全
2. 接入点：
   - `notify_filter.should_notify` 每个 return 处自动 `decision_log.append(...)`，policy="filter"
   - `notify_policy.submit` 在 `policy_off` / `silence-scheduled` / `merged` / `merged_all_filtered` 路径调 `_trace(...)` helper
   - 故意**不**记 `activity_cancels_pending`（活动事件本就用来取消推送窗口，不算独立决策）
   - `should_notify` 加 `log_trace=True` 形参，policy 层在"合并发送前 dry-run"等不算独立决策的路径可以传 False（暂未启用，留扩展点）
3. 暴露：
   - `event_store.list_sessions` 每个 session 注入 `recent_decisions`（最近 5 条）
   - 新 endpoint `GET /api/sessions/{sid}/decisions` 返回完整 50 条
4. UI（前端）：
   - 卡片新增第 4 行 `.push-trace`：`最近：12:30 ✓ stop_anchor · 12:31 ✗ notif_dedup_after_stop`
   - 11px 灰字，dashed 边框，hover 高亮，点击展开 modal
   - modal 里按 ts 倒序列 50 条决策，push 标绿、drop 标灰

**验证**：
- `scripts/0509-2200-test-decision-log.py` 5/5 case pass：
  - case 1：notify_filter 各路径（anchor / blacklist / sidechain / TestNotify）写决策
  - case 2：recent_for / all_for 倒序 + 字段完整 + sid 隔离
  - case 3：list_sessions 注入字段
  - case 4：trim 严格上限 PER_SESSION_MAX=50
  - case 5：notify_policy.off path 写 drop trace
- 真实重启 backend 后访问 dashboard：
  - 触发 `POST /api/event` 一条 Stop（policy=silence:12）→ 卡片底部立即出现 `最近：21:03 ⌛ scheduled_silence_12s`
  - 触发 TestNotify → 卡片底部显示 `最近：21:04 ✓ always_true:TestNotify`
  - 点击 trace 按钮 → modal 展开完整 50 条历史
  - `/api/sessions` 接口已返回 `recent_decisions` 数组

**核心教训**：
1. **`reason` 字符串只生成不落盘 = 浪费**。日志层的 reason 是低成本观测，落盘成本几乎为零（轻量 jsonl + per-sid trim），不落盘就等于让用户必须 `tail -f` 才能 debug。任何"用户看不见 = 用户怀疑系统坏了"的场景，都该把内部状态升级成第一类 UI 元素。
2. **新增独立 jsonl 比给 events.jsonl 加字段更稳**。events.jsonl 是事件日志（"发生了什么"），决策日志是策略观测（"我们怎么判的"），两者生命周期不同（决策只对最近 50 条有意义，事件要长期保留）。**强行嵌套在 raw 里会让 list_sessions 读路径每次都加载完整 raw，性能下滑**。这与 L09 加 `last_event_kind`、L08 加 `is_sidechain` 同思路：新增隔离字段 / 文件，不改既有字段语义。
3. **Per-session trim + 估算行数触发** 是廉价的环形缓冲实现。每次都精确计数要全文件读，但 trim 容忍误差，按文件大小 / 平均行长估算就够用了。这种"懒清理"策略适用于"写多读少 + 总量有上限"的场景。
4. **API 新字段 + UI 新行 + 后端新文件 = 三处协同改动一次 commit**。这种端到端 P1 改进必须三层一起改才有用：单只改后端用户看不到、单只改前端没数据来源。教训：跨层改动尽量在同一轮内完成，避免半成品状态。
5. **不记录"取消活动"类事件**避免 trace 噪声。`activity_cancels_pending`（PreToolUse / Heartbeat）每秒都触发，不写 trace 是有意的：决策日志要看"决定推不推"，活动事件只是 cancel pending 不是独立决策。这条边界要在 helper 注释里写清楚，避免下次重复辩论。

---

## L10 — 飞书消息看不出"完成了什么"：单行 milestone 被末段日志/代码污染（已修）

**修改时间**：2026-05-09

**用户痛点**：飞书推送原模板的第二行：
```
✅ hello · 任务完成
<single-line milestone>             ← 这一行常是末段日志、代码尾、"执行环境"声明
→ <next_action>
```
milestone 来源 = `clean_summary(last_assistant_message)`：先把整段 markdown 拼成一行，再切句、选首句。当 LLM 答复的"流式段落"里第一句就是结论时（如"全部 5 项任务完成。"）这种"先合再切"还能用；但实测样本里大量答复是"段落首行 = 结论 / 中段 = 详情列表 / 末段 = 执行环境 footer"的"分块"风格——把整段拼成一行后，要么截断到很短（OLD：`端到端验证通过：` 后面接了无关延迟句），要么把 markdown heading 残留拼进来（`改动总结 核心认知错误已修正 之前 L04 ...`）。**用户看不出"对应哪个任务诉求 + 实际做了什么"**。

**实地诊断（采自 data/events.jsonl 最近 28 条 Stop）**：
- `last_assistant_message` 抽取本身是对的（永远是 assistant 的最终文本，不是工具响应）
- 段落首行常是结论："全部 N 项任务完成。" / "**端到端验证通过**：" / "全部跑通。" / "改完。"
- 短回合答复整段就是结论："你好！有什么需要帮忙的？" / "1+1=2。"
- 结尾常带 footer："**执行环境**：local mac" / "**本轮修改文件**：..."（Tian 全局规则要求标注 → 内化进 LLM 输出）
- 大量答复以 markdown heading 开头："## 改动总结" → 接 "### 核心认知错误已修正"

**替代方案（已实施）**：分两块（任务线 + 完成了什么）+ 行级抽取
1. `backend/sources.py` 新增 `extract_conclusion(raw, max_len)`：与 `clean_summary` 互补，按"行"看而不是"先拼后切"
   - 切原文 → 跳过空行 / 代码块内部 / footer 行（`执行环境` / `本轮修改` 等前缀）/ 纯结构行（`---` 分隔、空 heading、表格行）
   - 优先级 1：前 6 行内含锚词（"完成 / 修复 / 修完 / 改完 / 跑通 / 通过 / 已修正 / fixed / done / passed"）的那一行 → 即用
   - 优先级 2：前 5 行第一条非寒暄的实质行；但若是"短小标题"（≤ 8 字 + 原文是 # 开头，例 "改动总结"）→ 视为占位，跳到下一行
   - 优先级 3：lines[0] 兜底；最后再 fallback 到 `clean_summary`
   - 输出经 `_polish` 去 markdown 残留（粗体/斜体/链接/列表前缀），剥寒暄前缀，去尾冒号
2. 同模块新增 `topic_overlaps_conclusion(topic, conclusion)`：判断 task_topic 与 conclusion 前缀字符重合 ≥ 50% 或包含关系，用于飞书去重
3. `backend/feishu.py` 改 `_format_text()` Stop / SubagentStop 分支为两块：
   - `📌 task_line`：优先 task_topic → 与 L1 focus 重复（无 alias 时）就降级到 first_user_prompt → 仍重复就降级到 recent_user_prompt → 都重复就略掉这行
   - `✓ conclusion`：调 `extract_conclusion(last_assistant_message)`，fallback 用旧 last_milestone
   - 重叠去重：conclusion 与 task_line 重合 ≥ 50% → 只保留 conclusion
4. 总行数 ≤ 7 行（emoji + 任务线 + 完成 + 下一步 + 分隔 + meta + tail），保持紧凑

**实测对比**（5 个真实 last_assistant_message 样本，OLD = clean_summary，NEW = extract_conclusion）：
| 场景 | OLD | NEW |
|---|---|---|
| 长 markdown 答复（heading + 列表 + 表 + footer） | `改动总结 核心认知错误已修正 之前 L04 的修法在...` | `核心认知错误已修正` |
| 锚词在首行 + 长尾详情 | `端到端验证通过： 延迟从 17.76s 降到 6.94s 验证 cache 在工作。` | `端到端验证通过` |
| 列表前置 + 多段 | `改完。一处微调： **renderDrawerEvents()** —— 抽屉事件流改为...` | `改完。一处微调` |
| 短答复 | `有什么需要帮忙的？`（丢"你好"） | `你好！有什么需要帮忙的？` |
| 寒暄 + 实质 | `我已修复 X。` | `我已修复 X。重启后生效。` |

**验证**：`scripts/0509-2100-test-feishu-content.py` 全部 case pass（8 个抽取样本 + 5 个端到端飞书结构 + 重叠 / 非重叠去重）。

**核心教训**：
1. **"先拼后切" vs "先切后选" 适用场景不同** —— `clean_summary` 把整段当流式段落拼成一行再切句，适合自然语言连续叙述；但 LLM 实测输出是"分块"（首行结论 / 中段详情 / 末段 footer），"按行看"再用锚词 / heading 启发式选才对路。两个函数共存，不互相吞。
2. **锚词命中是最可靠的单信号** —— 中文/英文锚词列表（"完成 / 修复 / 跑通 / fixed / passed"）覆盖了 80%+ 的"任务结束"模式。用锚词当首选 + 第一条实质行兜底，比纯位置（首句）或纯长度（取最长）稳得多。
3. **footer 前缀黑名单是廉价过滤** —— Tian 全局规则要求 LLM 答复尾部带"执行环境 / 本轮修改文件" footer，但飞书是"快速浏览"不是"完整记录"，footer 应明确剥掉而不是让它参选首句。这种"语义噪声"用 startswith 黑名单一刀干净。
4. **降级链要看上下文重复，不光看是否非空** —— L1 focus 已经是 `task_topic or display_name`，L2 再写 task_topic 就是冗余。降级链应该看"与上一行是否重复"再决定显不显。这是 BLUF 原则的延伸：每行只承载一个新信号。
5. **"分两块"比"塞一行更密"信息密度高** —— 用户视线扫第二行/第三行比扫一行 80 字快。把"任务线（用户问的什么）"和"完成线（agent 干的什么）"分开，在屏幕高度成本可接受时（≤ 7 行）总能用。L05 飞书模板从 9 行压到 6 行是对的方向，但极端紧凑反而牺牲了关键信号；L10 是反向微调，关键信号补回来。

---

## L13 — events.jsonl 单文件无界增长（已修）

**修改时间**：2026-05-09

**用户痛点**：`backend/data/events.jsonl` 是单个 append-only 文件。重度用户 100+ session/天 × 几周 → 100MB+。后果：
- 启动期 `list_sessions()` 全量读 → 内存峰值 + 启动慢
- `iter_events()` 是 list_sessions 主路径，每次 dashboard 刷新都 O(全量)
- 调用频率高（每 hook、每 ws 推送都触发 `_session_summary`）→ 用户感知卡顿

**替代方案（已实施）**：滚动归档（hot + archive 两层）。

设计 spec：
- 阈值：`max_hot_size_mb=50`（hot 文件 ≥ 此值触发检查）
- 保留窗：`rotation_grace_days=1`（只归档 ts 早于 24h 前的事件，保留近期热）
- 归档目录：`data/archive/events-YYYY-MM-DD.jsonl.gz`（按事件**最早 ts** 的本地日期分桶）
- gzip 多 member 串联：同 date 桶用 `'ab'` 模式追加，读端 `gzip.open('rt')` 自动跨 member 顺序读
- 触发点：app 启动 lifespan 时调一次 `event_store.archive_if_needed()`
- 默认行为：`list_sessions()` / `read_events_for()` / `iter_events()` 只读 hot；显式 `?include_archive=1` 才合并归档（懒加载 + 解压）

并发安全：
- `archive_if_needed()` 持 `LOCK_EX` 整个归档过程（与 `append_event()` 的 flock 同 path）
- 归档先写 archive（gzip append），再 `os.replace(tmp, hot)` 原子替换
- 任何环节失败 → 删 .tmp → hot 不动 → events 保留（永远不丢事件）

实测（mock 5 万条 ~64MB hot，30MB threshold）：
| 指标 | 归档前 | 归档后 |
|---|---|---|
| hot 大小 | 63.9 MB | 19.2 MB |
| hot 行数 | 50,000 | 15,000（保留近期） |
| archive 文件数 | 0 | 9 个 .gz（按日期分桶） |
| 归档总条数 | 0 | 35,000 |
| 总数守恒 | 50,000 | 15,000 hot + 35,000 archive = 50,000 ✓ |
| `iter_events` 全量读 | 118.9ms | hot-only 36.2ms（**3.3× 加速**） |
| `list_sessions` | — | hot-only 248ms / full 1003ms |
| 触发 archive 耗时 | — | 630ms（一次性，启动时） |

**核心教训**：
1. **append-only 日志要带滚动机制，但不要用"按行数 / 按时间"** —— 「按大小阈值 + grace 窗」更鲁棒：用户突发产生大量事件时，按 size 触发；用户安静期不动；grace 窗保证近期事件永远在 hot（list_sessions 命中率 100%）。
2. **gzip multi-member 是 append-friendly 的归档格式** —— 不需要"先解压、合并、重压"，每次 `'ab'` 直接拼一个新 gzip member，读端透明。代价：压缩率略降（每 member 各自 header），但相对于"必须读完整压缩文件再 append"的开销，胜算明显。
3. **flock 必须跨"读+写"全过程** —— 归档要读 hot → 决定哪些归档 → 重写 hot 三个步骤同临界区，否则中间 append_event 写入的事件会被 os.replace 覆盖丢失。`open(path, 'r+')` + LOCK_EX 一把锁罩住整段。
4. **失败兜底是"事件保留 hot"，不是"事件丢失"** —— 归档失败的语义边界：宁可 hot 不缩，也不能丢任何事件。读 hot 出错 → 跳过本轮；写 archive 出错 → 不动 hot；写 .tmp 出错 → unlink .tmp。每个阶段都有 rollback。
5. **新增 API param 而不是新增 endpoint** —— `?include_archive=1` 是 query param，复用 `/api/sessions` `/api/events` 既有路由 + 既有 schema，前端零改动。新 endpoint 会带来路由命名 / 鉴权 / 文档同步 N 倍工作量。
6. **path 解析两层 fallback** —— archive_dir 优先从 config（用户可改），fallback 到 `cfg_mod.ARCHIVE_DIR` 默认。相对路径基于 `PROJECT_ROOT` 解析（绝对路径直接用）—— 用户配 `data/archive` 或 `/var/log/claude-notify` 都生效。

**未来留白（可做但本轮不做）**：
- archive 老于 N 天的文件清理（避免 archive 自己也无限大）
- dashboard 显示"已归档 X 个事件 / Y 个文件"meta（前端字段已具备查询能力，UI 待加）
- 周期归档（不只启动）：当前是"启动时一次"，长跑实例需要再加一个 watcher loop 调用，但实测启动时跑过一次后 hot < threshold 概率高，按需再加

---

## L15 — quiet_hours 夜间不打扰：分钟空间判定 + 关键事件穿透白名单（已修）

**修改时间**：2026-05-09

**用户痛点**：晚上 23:00-08:00 用户在睡觉，被 SubagentStop / Stop / Heartbeat 推醒次数多到要把整个推送关掉。但「真等输入」（Notification）和「疑似 hang」（TimeoutSuspect）夜里也要叫醒 —— 这种事件错过损失更大。一刀切 mute 把婴儿和洗澡水一起倒掉。

**替代方案（已实施）**：全局 `quiet_hours` 配置 + 事件类型白名单穿透。

设计 spec：
- 数据模型挂在 `cfg.quiet_hours`：`{enabled, start, end, tz, passthrough_events, weekdays_only}`
- 判定层在 `notify_filter.should_notify()`，**位置：session_mute → quiet_hours → 既有 _stop/_notification_decision**（用户 per-session mute 优先于全局 quiet_hours，匹配「显式 > 隐式」）
- 跨午夜采用「分钟空间」判定：把 (h, m) 映射到 cur=h*60+m、s/e 同理；`s < e` 走同日窗 `[s, e)`，`s > e` 走跨午夜窗 `cur >= s OR cur < e`，`s == e` 视为不静音（fail-safe）
- tz 用 `zoneinfo.ZoneInfo(IANA)`；解析失败回退本地 `datetime.now()`，绝不抛异常
- passthrough：默认 `["Notification","TimeoutSuspect"]`；用户可勾 `TestNotify` 测试
- weekdays_only：用 `datetime.weekday() >= 5` 判周末跳过
- drop reason 写 `quiet_hours_HH:MM`，dashboard trace modal 一眼能看出「夜里 02:30 被吞」
- 配置不完整 / start 非法 / end 非法 / start==end → 安全默认**不静音**（避免静默吞事件让用户找不到推送）

**验证**：`scripts/0509-2300-test-quiet-hours.py` 22 case pass：
- 跨午夜（23:00-08:00）：02:30 / 23:30 / 07:00 在内，08:30 / 14:30 / 22:30 在外
- 同日窗（午休 13:00-17:00）：14:30 在内，12:30 / 17:30 在外
- 边界：23:00 入、08:00 出（半开区间 [s, e)）
- passthrough：Notification / TimeoutSuspect 即使在 quiet 时段也通过
- weekdays_only：周一静音，周六周日跳过
- 配置不完整 / start==end → 不静音
- 端到端 `should_notify(Stop @ 02:30)` → drop, reason=quiet_hours_02:30
- 端到端实测：POST `/api/event` Stop → silence:12 → 12s 后过滤命中 `quiet_hours_21:17`（**真在 trace 里看到了**）

**核心教训**：
1. **「真等输入」和「疑似 hang」是不可降级事件** —— 设计 mute / 静音 / quiet_hours 时永远要留穿透白名单。一律 drop 等同于把可观测性整个关掉，用户会失去对 Claude 状态的感知。
2. **跨午夜判定用「分钟空间」而非 hour 比较** —— `cur >= s OR cur < e` 一行处理跨午夜，比「if start.hour > end.hour 然后 if-else」分支干净一倍，且单元测试只需覆盖 cur 的多个时刻就够。
3. **fail-safe 默认是「不静音」而非「按 default 静音」** —— 配置缺字段、字段非法、start==end，所有边界都返回 `(False, "")`。原则：**安静失败 > 静音吞事件**，因为用户对「没收到推送」的发现路径比「收到无用推送」长得多（数小时 vs 数秒）。
4. **L15 reason 字段把当前时分写进去** —— `quiet_hours_02:30` 比 `quiet_hours_active` 信息量多一倍，trace UI 不需要额外字段就能让用户秒懂「哦凌晨被吞了」。
5. **session_mute 与 quiet_hours 的顺序：显式 > 隐式** —— 用户对单个 session 显式 mute 是「我不要看这个」，quiet_hours 是「夜里别打扰我」。前者更确定 → 顺序在前；过期用户 mute 自动清理后再走 quiet_hours，不会漏判。
6. **不新增 endpoint，复用 `/api/config`** —— 配置类字段一律走 `POST /api/config`，dashboard 配置 modal 直接读写整体 config 对象。新增 endpoint 会污染 API 表面，且无收益（quiet_hours 不是高频操作，无需独立路由 / 独立鉴权）。

**未来留白（可做但本轮不做）**：
- 多时段（如 12:30-13:30 午休 + 23:00-08:00 夜间）：当前只支持单时段，需要时把 quiet_hours 改成 list of windows 即可。
- 假日日历：weekdays_only 仅按 weekday()，没接 PRC 节假日；接入 chinese_calendar 库可解决，但 99% 用户不需要。
- per-session quiet_hours：当前是全局的；未来若用户想"项目 A 夜里也打扰、项目 B 才静"，需要把 quiet_hours 配置下沉到 session_mutes 旁边。当前设计够用。


---

## L14 — per-session 静音（已实施）

**修改时间**：2026-05-09

**用户痛点**：全局 mute（`muted=true` / `snooze_until`）粒度太粗。用户在调试 / 开会 / 专注时只想静音**某一个**会话（"这个 session 太吵了静 30 分钟，其它正常推"），但保留其它推送。

**设计**：在 `config.session_mutes: dict[sid, entry]` 加 per-sid 静音表。
```json
"session_mutes": {
  "<sid>": {
    "until": 1778335000,    // unix ts；null = 永久
    "scope": "all",          // "all" | "stop_only"
    "muted_at": 1778334000,
    "muted_at_iso": "2026-05-09 21:30:00",
    "label": "user-set"
  }
}
```

**过滤规则**（在 `notify_filter.should_notify` 最前置，先于 quiet_hours / Stop / Notification 决策）：
- 命中 session_mutes[sid] 且 (until is None OR now < until)：
  - scope=all → drop（reason='session_muted_all'）
  - scope=stop_only → 仅 Stop / SubagentStop drop；Notification / TimeoutSuspect 通过
- until 已过期 → 视为未静音 + 命中时自动清理 entry
- app 启动时调 `prune_expired_session_mutes()` 一次清理残留

**API**：
- `POST /api/sessions/{sid}/mute` body `{minutes: number|null, scope: "all"|"stop_only", label?}`
  - minutes=null → 永久；minutes<=0 → 等价 unmute
- `DELETE /api/sessions/{sid}/mute` 解除
- 静音状态在 `/api/sessions` 每个 session 加 `mute_state: {muted, until, scope, remaining_seconds, muted_at_iso, label}`

**Dashboard**：
- 卡片右上 🔔 / 🔕 按钮（已静音 → 🔕 + 剩余分钟数）
- 点击弹小菜单：5min / 30min / 60min / 永久 / 仅 Stop · 30min / 解除（已静音时显示）
- 静音中卡片整体淡灰（opacity 0.7 + 角标 🔕）
- 30s 整体重渲染时基于 mute_state.until 实时算剩余分钟（不依赖后端固定 remaining_seconds）

**为什么不复用全局 snooze_until**：
1. snooze 是全局开关，session 级需求是"互不影响"——必须用按 sid 索引的 dict 结构
2. snooze_until 是 ISO 字符串（前端传值），session_mutes.until 是 unix ts（后端比较高频，避免每次 parse_iso）
3. scope=stop_only 是 session 级独有需求（全局 snooze 没有"只静某类事件"的语义）

**为什么 set/clear/prune 绕过 save() 走 _write_session_mutes**：
- `save(patch)` 用 `_merge` 深合并 dict，对 `notify_policy / display / llm` 这种"部分更新"是对的
- 但 session_mutes 是 set/del 语义：删一个 key 后 save 会把 disk 上旧 entry 又 merge 回来
- 所以单独走 `_read_raw + 整体替换 session_mutes 字段 + 写回` 路径

**核心教训**：
1. **粒度从粗到细要新增字段，别 overload 旧字段**——`muted` (bool) → `snooze_until` (ts) → `session_mutes` (dict)，每一档语义不同。把 session 静音强行塞进 snooze_until 会污染语义（"这是全局还是某 sid 的？"），以及前端老逻辑（snooze badge）会失灵。
2. **过滤检查的顺序是设计成本最低的扩展点**——`_session_mute_check` 加在 should_notify 最前置，`_quiet_hours_check` 紧跟其后，互不耦合。每加一个静音维度（用户级 / session 级 / 时段级 / 项目级 / ...）都是一个 helper + 一个 cfg key + 在 should_notify 前置插一行，零回归。
3. **timer-based 过期清理"懒清理 + 启动一次"够用**——不需要后台 task / 调度器。命中过期项时清理（写错忽略不影响过滤），启动时全表扫一次清掉残留。session_mutes 写入频率 << session 数（单 session 一天最多手动几次），lazy strategy 永远不会堆积。
4. **silence:N 路径要确保过滤前置点也起作用**——Stop 走 silence:12，12s 后 _wait_and_send 内部仍调 should_notify → 触发 mute_check → drop。`scheduled` trace 是中间态（policy 层），`drop session_muted_all` 是最终态（filter 层），两条都写 decision_log，dashboard 能看到完整链路。

**未来留白**：
- 多 sid 批量静音（"静音所有 idle session"）：当前需循环 N 次 API；接 `POST /api/sessions/mute-batch` 解决
- 静音时段策略（"工作日 23:00 后自动静音此 sid"）：需把 session_mutes.entry 升级到含 schedule 字段；目前不做
- 静音模板（"标记此 sid 为日常调试，永远静音 Stop"）：alias.tags 字段可承载，本轮不做
