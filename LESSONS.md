<!-- purpose: 设计教训记录 — 删除/否定一个设计前先写一段简要总结，避免后续重复犯错 -->

创建时间: 2026-05-09 16:25:00
更新时间: 2026-05-13 12:30:00

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

---

## L25 — Dashboard 按项目（cwd_short）分组视图（已实施）

**修改时间**：2026-05-10

**用户痛点**：同时跑 5+ Claude Code 终端在不同项目下（claude-notify / ragSystem / etl / 临时 tmp 测试），dashboard 平坦列表把所有 session 卡片混在一起。直接结果：
- "项目 A 现在 2 个 running / 1 个等输入" → 要肉眼数
- "哪个项目活动最多 / 最久没动" → 没有总览
- 切到关心的项目要 Ctrl+F 搜 cwd → 多此一举

**设计**：在 topbar 加一个 segmented control（pill toggle，"列表 / 按项目"），选择 grouped 模式后按 `cwd_short` 分桶，每组一个可折叠的 group header。

**布局**：
```
▼ 00-Tian-Project/claude-notify   5 · 🟡1 🟢2 ⚪2
   ├── [现有卡片 a]
   ├── [现有卡片 b]
   ...

▶ 00-Tian-Project/ragSystem       1 · 🟢1
   └── (折叠中，0 卡片可见)
```

**组渲染要素**：
- 折叠箭头（▶ collapsed / ▼ expanded）
- cwd_short（mono font, 中间截断 60 字符内）
- 总数 `5`（带左右竖线分隔，tabular-nums）
- 状态汇总 chip（仅显示 count > 0 的状态）：waiting=🟡 / suspect=🟠 / running=🟢 / idle=⚪ / ended=⚫ / dead=🔴

**排序**：组之间 = 组内最紧急 status weight（与卡片排序权重一致），并列按组内最新 last_event_ts 倒序。组内 = 沿用现有平坦排序（不重写 — 与 L23 二级排序联动）。

**持久化**：localStorage 记两个 key：
- `cn.viewMode`: "list" | "grouped"，默认 list（保持向后兼容 — 老用户刷新后体验不变）
- `cn.collapsedGroups`: JSON array of cwd_short，per-cwd 折叠态独立记忆

**实现核心**：
1. `renderSessions()` 不动既有 sort + filter 链路；只在 innerHTML 那一句分支：`viewMode === "grouped" ? renderGroupedHTML(visible) : visible.map(sessionCardHTML)`
2. 卡片本身的 HTML / 事件绑定 / 抽屉打开等 0 改动 — 卡片仍由 sessionCardHTML 渲染，header 只是包了一层 section。后续 click handler 通过 `$sessions.querySelectorAll(".session-item")` 仍能拿到所有卡片（descendants）。
3. 组 header 的 click handler 通过独立的 `bindGroupHeaderEvents()` 绑定，它不与卡片 click 冲突（不同 selector），事件 stopPropagation 避免冒泡到 document outside-click。

**为什么不复用 status filter 而非新加 viewMode**：
1. status filter 和 cwd 分组语义不同 —— 一个是"过滤"、一个是"展示"。多 cwd 用户关心的是"看清各项目分布"（不是要隐藏其它），不能用过滤代替。
2. localStorage 偏好是"我喜欢哪种视图"，不是"我现在想看什么" —— 跨刷新粘性自然不同。

**为什么默认 list 不默认 grouped**：
1. 老用户预期不变（避免 surprise change）
2. 单项目用户用 list 信息密度更高（grouped 多一行 header 浪费）
3. 5+ 项目用户主动切到 grouped + localStorage 记住，第一次切换后零成本

**核心教训**：
1. **视图模式扩展点要在 render 层而非 data 层** —— sort 是数据层、filter 是数据层、分组是展示层。把分组写到 sort/filter 里会污染原有逻辑（`if grouped...` 散落各处）；写在 render 入口的一个分支，只额外加一个 helper 文件量级，零回归到原 list view。
2. **共享原子（卡片）跨视图复用** —— group view 的核心是"外面包一层 group header + body"，不是"重新渲染卡片"。坚持调 `sessionCardHTML` 既保证两种视图卡片视觉一致，又让后续卡片改动（mute / trace / alias）零额外维护。
3. **localStorage 的 try/catch 包裹 + 默认 fallback 是必须的** —— 私密浏览模式 / 配额满 / iframe sandbox 都会让 localStorage 抛异常。每个 read/write 都要包，并提供"读失败 → 用默认值"的兜底，不能让一个偏好读失败就阻塞 UI 启动。
4. **状态 emoji 在前端是装饰、在源码是 const map** —— 不要每次组装时硬编码 emoji 字面量，用一个 STATUS_EMOJI map 集中维护。后续状态扩展 / 改图标只改一处。

**未来留白**：
- 多级分组（先按 cwd 再按 status）：当前一级；如用户反馈 5+ 项目 + 每项目又 5+ session 时会需要
- 组内排序独立选项："按等待时长 / 按最近活动 / 按 alias"：当前组内沿用全局 sort 一致；够用
- 拖拽重排序 / 自定义组顺序：复杂度暴涨，且 sort by status weight 对"哪个最紧急"已经够直观，不做
- 组的批量操作（"展开所有 / 折叠所有 / 静音整组"）：手动逐个折叠在 5-10 项目数量下不是 pain；20+ 项目时再加

---

## L23 — 多 session 飞书堆叠场景：消息列表预览看不出归属（已修）

**修改时间**：2026-05-10

**痛点**：
用户同时开 5+ 个 Claude Code 终端跑不同项目（claude-notify / ragSystem / etl / ...）。多 session 推送堆积时，飞书消息列表所有头部都长得一样：

```
✅ hello, 回复收到 · 任务完成
✅ 修 loader · 任务完成
🔔 等你确认 · 等你确认
```

只看预览根本分不清哪条对应哪个项目，必须点开每条才能看 L7 的 `cwd_short`。多 session 越多，定位成本越高。

**为什么 L7 cwd 行不够**：
飞书 / 钉钉 / Slack 这类消息列表预览**只截取首行**做缩略显示。L7 在第 7 行，永远不会出现在预览里。要让用户从消息列表"扫一眼就识别"，关键信息必须挤进 L1。

**为什么不在 L1 末尾加 cwd**：
L1 已有 `emoji + focus + status` 三段（视觉权重最高）。在末尾追加项目名会被截断（飞书 PC 端单行约 30 字符）。**前置**才稳定可见——预览总是从头展示。

**实现**：
`backend/feishu.py:_extract_project_label(evt, summary)` 取首个非空：
1. `summary.alias` —— 用户显式 dashboard 起的别名（最权威）
2. `cwd` 末段（path.split('/')[-1]）—— 最贴近"项目仓库名"
3. `cwd_short` 全文 fallback
4. 都没有 → 空串，**不**渲染空 `[]`

L1 模板：`[claude-notify] ✅ hello · 任务完成`（项目名加在 emoji 之前，用 `[]` 包裹，与正文视觉切分）。

**核心教训**：
1. **信息架构要看通讯渠道的"渲染边界"**——飞书消息列表预览只展示首行。L1 是"全局唯一展示位"，L2-L7 是"详情展开位"。多 session 区分是预览级需求，必须挤进 L1，不能放详情。
2. **加前缀不加后缀**——预览从头截，前置 100% 可见，后置可能被吃掉。
3. **聚合标识符要分层**：alias > cwd-tail > cwd-short > 空。alias 是用户主动表达的语义（最高优先），cwd-tail 是机器派生的稳定标识（次高），cwd-short 是 fallback。空时不要硬塞占位符（不要出现 `[]` 空括号——那是噪音）。

**未来留白**：
- 项目级颜色 emoji（`🟦 [claude-notify]`）：飞书消息列表 emoji 比纯文本更显眼，但需要 alias → emoji 的映射表，本轮不做
- 多事件 combined 卡片要不要每个事件都标项目：当前 combined 只标主事件项目，earlier 列表不区分。多 session combined 场景较稀有（同一 silence 窗口内不同 session 的事件合并很少触发），暂不做。

---

## L24 — dashboard 同 status 内排序"等最久的 vs 最近活动的"语义冲突（已修）

**修改时间**：2026-05-10

**痛点**：
dashboard 卡片排序原来是 `status_weight 一级 + last_event_ts 倒序二级`。**所有 status 都用同一个二级**——倒序（最近事件往前）。问题：

```
waiting (等了 12 分钟) ←—— 用户最该看到的
waiting (刚 30 秒前 push)
waiting (刚 1 分钟前 push)
waiting (5 分钟前)
```

二级是倒序的，所以"刚 push 的"挤掉"等了 12 分钟没人理"的卡片到最下面。用户痛点是"我有个 session 等了很久没人理"，但视觉上反而看到的是"刚刚才 push 的"。

**为什么 running 还是要倒序**：
running 状态用户关心"刚刚 Claude 干了啥"——最新事件是 fresh signal。运行中的 session 不存在"等最久"的语义。

**实现**：
`frontend/app.js:renderSessions()` 排序二级 key 按 status 切换：
- `waiting / suspect / idle` → `last_event_ts` **升序**（老的往前）
- `running / ended / dead` → `last_event_ts` **降序**（新的往前）

10 行代码改动：定义 `STALE_FIRST = new Set(["waiting","suspect","idle"])`，比较函数末尾按集合判定方向。

**核心教训**：
1. **"按时间排"在不同 status 下语义不同**——idle/waiting 的时间是"等了多久"（越大越紧急），running 的时间是"最近活跃度"（越大越值得关注）。同一字段、同一比较方向，覆盖不了两种语义。
2. **二级 key 不要写死方向**——按 status 分支选择升/降序是最低成本的扩展点。后面如果新增"按 token 用量"等三级 key，只需在比较函数末尾追加。
3. **测试要覆盖混合 status**——单测只测同 status 内的方向不够，必须有"waiting 老 + running 新 + idle 老"混合排序的断言，确保一级权重 dominate 二级方向。

**未来留白**：
- ended / dead 是否也按"退出时刻倒序"足够：用户对已退出 session 的关注度本就低，目前用 last_event_ts 降序 OK
- 多 status 内是否要再加"按 alias 字典序"三级 key：当前用户没反馈，暂不做

---

## L26 — 紧急度视觉徽 + 飞书 reminder 消息差异化（已实施）

**修改时间**：2026-05-10

**用户痛点**（Round 6 后剩的"最后一公里"）：
1. dashboard 多张 idle 卡片视觉权重相同——5 张都是"⚪ 回合结束 · 30 分钟前"，看不出哪张"等了 30 秒"哪张"等了 8 分钟"。"距今 X 分钟前"埋在第 3 行 meta，扫读时不进眼。
2. 飞书 reminder 第 1 次 / 第 2 次内容看起来完全一样：都是 `🔔 hello · 等你确认`。用户从消息列表预览看不出"这是首次提醒还是已经第 N 次了"，无法判断紧迫度，也没法把"已等多久"作为信号决定要不要立刻处理。

**为什么不在 progress_state 里加"X 分钟"**：
progress_state（"刚完成" / "已歇" / ...）是状态分类，"等了多久"是程度连续值。混在一个字段会丢掉其中一个维度。徽章独立呈现"程度"，不污染既有状态语义。

**为什么不直接放大 status badge**：
status badge（🟡 等输入 / ⚪ 回合结束）是分类徽，不能因为时长不同就变颜色——会扭曲"状态"本身的视觉编码。用独立的紧急度徽以"⏱ 颜色 + 时长文字"传达"程度"维度，与状态维度正交。

**改动 1：dashboard 紧急度徽章**

`frontend/app.js:urgencyBadge(status, ageSeconds)` + `frontend/styles.css:.urgency-badge`：

| age | idle 档位 | waiting/suspect 档位（升一档） |
|---|---|---|
| < 2min | 不显示（避免噪音） | 不显示 |
| 2-5min | hint 浅黄 | warn 橙色 |
| 5-15min | warn 橙色 | critical 红色 |
| ≥ 15min | critical 红色 | critical 红色（饱和） |

仅 idle / waiting / suspect 三档 status 显示。running / ended / dead 不显示——running 时间是"刚活跃"不是"等了多久"，ended/dead 用户已经不关心。

徽章位置：`item-actions` 容器内、🔔 静音按钮左侧。`formatAge(s)` 输出 `<1min` / `Xmin` / `Xh Ym`。

**改动 2：飞书 Notification reminder 模板差异化**

按 reminder_index 分支：

```
ridx == 0（真权限请求 / notification_kept）  → [proj] 🔔 task · 等你确认       ← 保持现状
ridx == 1（首次 reminder, 5min 阈值）          → [proj] 🔔 task · 还在等你（5 分钟）
ridx >= 2（第 2 次及之后, 10min/15min/...）   → [proj] 🚨 task · 已等 10 分钟
```

emoji 从 🔔 → 🚨 升级是**视觉优先级跳档**——飞书消息列表预览里 🚨 比 🔔 更扎眼，让"第 2 次还没回应"的紧迫感不需要点开就传达到位。

**reminder_index 怎么从 should_notify reason 流到 evt**：

`notify_filter._idle_reminder_decision` 已经返回形如 `"notif_idle_reminder_1_at_5.0min"` 的 reason 字符串（含数字 + gap 分钟数）。
`notify_policy.submit` 的 Notification 分支在 `should_notify` 返回 ok 后、调 `feishu.send_event` 前，用正则 `_REMINDER_REASON_RE` 解析：

```python
_REMINDER_REASON_RE = re.compile(r"^notif_idle_reminder_(\d+)_at_([\d.]+)min$")
```

解析成功 → 把 `reminder_index` 与 `reminder_gap_min` 注入 evt 副本（不改原 evt）。
`feishu._format_text` 在 ev == "Notification" 时优先读 evt 里这两个字段；缺失/0 → 走原模板。

**为什么用 evt 注入而不是新参数**：
1. 顺路兼容 `send_events_combined`（合并卡片复用 `_format_text`，不用改第 2 个调用点）
2. evt dict 是本来就在数据流里的载体，新增字段不破坏 send_event 接口签名
3. 原 evt 不被修改（dict spread 复制），其它消费者不受影响

**核心教训**：
1. **状态徽 vs 紧急度徽的维度正交**——分类（什么状态）与程度（多紧迫）是两个独立维度，必须分两个视觉元素表达。把"等了多久"挤进 status badge 颜色会丢掉"状态"的清晰分类。
2. **决策 reason 字符串本身就是结构化数据源**——`should_notify` 返回的 reason 不只是给 trace 看的字符串，而是带语义的标签。policy 层用正则解析它来驱动下游模板分支，避免改 should_notify 签名加新返回字段，也让 reminder_index 这个语义只在一个地方派生（filter 内部计数 → reason 字符串），其它消费者解析即可。
3. **emoji 跳档是低成本传达"严重性升级"的手段**——同消息渠道内重复推送的"第 2 次和后续"必须视觉上区别于"首次"，否则用户会盲化。🔔 → 🚨 是 IM 通用语义。颜色 / 字号 / 文本结构都加上，多通道冗余增强识别。
4. **age_seconds 已是 session.age_seconds 的现成字段**——前端徽章 0 后端改动；30s 自动刷新已存在的 `setInterval(renderSessions, 30 * 1000)` 自然带动徽档位变化，无需独立 timer。

**未来留白**：
- 紧急度徽是否影响 dashboard 排序：当前不影响（排序只看 status_weight + last_event_ts）。如用户反馈"老 idle 应该比新 idle 排前"，那是 L24 同 status 二级排序的扩展，不属于徽章本身职能。
- reminder 第 3 次之后用户配 `[5, 10, 15]` 时第 3 次也用 🚨：当前 ridx >= 2 都用 🚨。如果用户希望第 3 次更醒目（如 ⛔），改 emoji 表即可，本轮不做。
- 徽章是否带"距今超时多少"语义（当前是"已等"，待提取的是 timeout_seconds 还差多久）：复杂度暴涨，且既有 TimeoutSuspect 事件已经覆盖临界场景，不做。
- waiting 状态本身就有 status badge 高亮（粗体 + 黄边 + dot），紧急度徽再叠加是否过载：不过载——状态 badge 是"分类标签"，紧急度徽是"程度数字"，且 waiting 卡片用户看的就是它们，多 1 个 22px 高 pill 不构成视觉负担。

## L27 — 首次接入空状态：用户看不到该做什么（已修）

**修改时间**：2026-05-10

**原设计**：dashboard 在没有 session 时只显示一行灰字 `暂无会话。等待第一个 hook 事件…`。

**为什么不行（PM/用户视角）**：
1. 用户首次启动 backend、打开 dashboard → 看到这行字，不知道接下来该做什么。
2. 关键步骤其实有 3 个：① `python3 scripts/install-hooks.py` 把 hook 写进 `~/.claude/settings.json` ② 在配置面板填飞书 webhook ③ 在任意 Claude Code 终端发起一句话触发事件。
3. 三步都漏了不会有任何反馈——用户最终会以为工具坏了。
4. 已配置 webhook 但还没装 hook 是最隐蔽的失败模式：用户以为"按钮亮了就是 ok"，但实际事件根本不会到 backend。

**替代方案（已实施）**：首次接入引导卡 + 后端检查 endpoint
- 后端 `GET /api/health/setup`：
  - `hooks_registered`：扫 `~/.claude/settings.json`，看 4 个事件 hook 中有 ≥3 个含 `hook-notify.py` 字符串
  - `webhook_configured`：cfg.feishu_webhook 非空
  - `session_count`：当前活跃 session 数（避免有 session 时还显示引导卡）
- 前端 `renderSetupChecklist()`：
  - 仅在 `state.sessions.length === 0` 且无 filter 时显示
  - 3 项 ✓/✗ 列表 + 缺啥提示啥（hook 未装提示具体命令；webhook 未配提示按钮 → 直开配置）
  - "重新检查"按钮调 `loadSetupHealth()` 即时刷新（避免等 60s 定时刷）
- 配置保存后 invalidate `state.setupHealth = null`，下次空状态出现重新拉
- P2-4 联动：`$btnConfig.disabled = true` 直到 `loadConfig()` 完成；防"配置加载中…"卡顿感
- 配置面板 LLM 区块加 1 行 muted 文案：「可选。关闭时走启发式摘要（基于关键词），仍能用」——避免用户误以为"必填"

**核心教训**：
1. **空状态是新用户的第一面**——空状态不是"暂时无数据"的占位，是首次接入的引导界面。一行灰字 = 0 信息密度，浪费首次成功率最大杠杆。
2. **检查清单 ≠ 教程**——不要塞长说明，只列 3 项必要步骤 + 每项的"是否完成"信号 + 未完成时具体下一步。用户能扫读、能照做。
3. **hooks_registered 用阈值而非全等**——用户可能只装了部分 hook（比如不要 PreToolUse 心跳）。`>= 3/4` 的容错比 `== 4` 更现实。
4. **invalidate 不删除**——`state.setupHealth = null` 让下次空状态重新拉，比"立即调用 loadSetupHealth + 渲染"更省：用户可能保存配置后不会回到空状态（例如 session 立刻进来），那次拉是浪费。
5. **后端检测优于前端推断**——webhook 是否配好后端有，但 hook 是否装在 `~/.claude/settings.json` 前端无权限读。把检测逻辑放后端 endpoint 是必要的，不是过度工程。

**未来留白**：
- 探测到 settings.json JSON 解析失败时是否更激烈提示：当前 catch + log + 走 false 分支。**不做**——用户看到 `hook 未注册` + 装 hook 命令已经够引导了，深度诊断属运维。
- 是否在 dashboard 顶 banner 持续显示"webhook 未配置"警示：**不做**——空状态已覆盖首次场景；非首次（已有 session）但 webhook 删了，更可能是用户主动操作，不应再唠叨。
- session_count > 0 但 hook 实际丢了（用户误删 settings）：当前不触发引导卡。**不做主动检测**——session_count > 0 就说明事件还在进，相当于功能在工作。
- "等待第一个 hook 事件"项永远显示 ✗（除非 session_count > 0，那时整个卡都不显示）。这个 UX 上看着像永远第三步未完成，但只要前两步完成 + 用户去触发事件，下一次刷新就整张卡消失，体验上不构成"任务永远做不完"。

## L28 — notes 用后端单 markdown 文件而非 localStorage（已修）

**修改时间**：2026-05-11

**否定方案**：notes 正文存浏览器 localStorage（按 sid 或全局 key 都试过设想），后端不参与持久化。

**为什么不行**：
1. **换浏览器丢**——用户在 MBP Chrome 写的 notes，切到 Safari / 切到台式机 / 切到 incognito 全部没有，跟「跨 session 沉淀」的诉求直接冲突。
2. **多窗口/隐身不同步**——同浏览器开两个 dashboard 标签，localStorage 写入不会跨标签实时通知（要听 `storage` 事件，且 incognito 完全隔离）；用户在 A 标签写完切到 B 标签看到旧数据，会以为"丢了"。
3. **无法外部编辑**——markdown 文件天然适合 vim / Obsidian / VS Code 直接打开编辑，localStorage 串只能 devtools 里搬。
4. **备份目录与 config/events 分裂**——`data/` 目录下已经统一存 `config.json` / `events.jsonl` / `aliases.json` 等，git 追踪 / rsync 备份一份就够；localStorage 要求用户额外导出，体验割裂。

**替代方案（已实施）**：后端单 markdown 文件 `data/notes.md` + 2 个 REST endpoint + WS 广播。
- `backend/notes.py`（119 行）：`read()` / `write(content, source)` / `NotesOversizeError`，fcntl.flock + threading.RLock 双锁
- `backend/app.py` 新增 `GET /api/notes` / `PUT /api/notes`，写入成功后 `await ws_hub.broadcast({"type": "notes_updated", "source", "size", "updated_at"})`
- 前端 `frontend/notes.js`（170 行）：textarea + 800ms debounce autosave + 状态行 + 折叠按钮 + 接收 WS 同步

**保留的部分**：localStorage 仍承担「UI 偏好」语义——折叠态 / 滚动位置 / 未保存的 crash 草稿。这些是"显示侧瞬时状态"，不是"内容"，丢了不痛。

**核心教训**：
1. **持久化层选型按"内容 vs UI"分离**——内容（笔记正文 / 配置 / 事件）放后端文件；UI 偏好（折叠态 / 排序 / 滚动）放 localStorage。语义错位是大多数前端持久化 bug 的源头。
2. **markdown 文件单文件优于数据库**——本工具的 notes 是单用户单条全局笔记，加 SQLite / 拆多文件都是过度工程；外部可直接编辑是 markdown 文件相对结构化存储的最大优势。

---

## L29 — 不做"每个 session 一份 note"（已确认）

**修改时间**：2026-05-11

**否定方案**：notes 按 session_id 分文件存 `data/notes/<sid>.md`，每张 session 卡有独立笔记区。

**为什么不行**：
1. **session 生命周期短**——一个 Claude Code session 通常 30 分钟到几小时就结束，但用户的笔记需求是「跨 session 沉淀」（待办、思路、调试线索、长期 TODO）；按 session 分文件会让笔记跟着 session 死。
2. **大量空文件**——backend 在跑几天就有几百个 session id，每个都建一个 .md 文件大半是空；扫描 + 备份 + 列表都吃成本。
3. **诉求错位**——用户原话是"主控板"，要的是全局视角；按 session 分笔记 = 又一个"按上下文找笔记"的检索负担，跟"主控板能一眼看到所有要紧事"反。
4. **跨 session 链接成本**——想从 session A 的笔记跳到 session B 的笔记，要建跨文件链接 / 索引，单文件下只是滚动几行。

**替代方案（已实施）**：全局单 markdown 笔记。

**保留的部分**：后续 round 加的"卡片 📝 加引用"按钮（task #82）= **轻量反向链**——点 session 卡片的 📝 按钮，把该 session 的 `first_user_prompt` / `last_assistant_message` / cwd_short 摘要追加（append）到全局笔记末尾，作为「我当时是从这个 session 想到这点的」上下文。这保留了"按 session 触发笔记记录"的便捷，但落地到全局笔记里，不分裂。

**核心教训**：
1. **存储粒度按"读出频率"决定**——读多写少的全局视图，用单文件；读少写多的局部细节，可以拆。notes 是典型读多写少（用户每天看 N 次，但不会高频改动），单文件最优。
2. **"按上下文分组"在工具内常常是反直觉的**——用户的工作记忆本来就是全局的，把工具强行按其内部结构（session_id）切片，加重了用户的认知负担。

---

## L30 — dashboard 不用三列固定 grid 布局（已确认）

**修改时间**：2026-05-11

**否定方案**：左 sessions（grid）+ 中 session drawer（详情）+ 右 notes 三列固定网格布局。

**为什么不行**：
1. **drawer 是瞬时态**——drawer 只在用户点开某 session 时出现、点关闭就消失。给一个"用户多数时间看不到"的元素留出永久列空间 = 大量空白。
2. **13" 屏幕崩**——13" MBP 视口 1440×900；Chrome chrome 自身约 60px → 实际可用 ~1380。三列若各占 360/520/360，加 gap + padding 后 60-100px 留白也撑不下；开 devtools 横向再砍 ~400px 直接溢出。
3. **drawer 现有的 overlay + box-shadow 是"detail-on-demand"模式**——改 column 意味着 drawer 不再悬浮、所有 overlay 动效（淡入淡出 / shadow scrim / ESC 关闭）都要重写为列宽变化或 slide-in；R6 的卡片排序 / R9 的引导卡 / R7 的紧急度徽都要重新对齐三列空间。

**替代方案（已实施）**：sessions + notes 两列布局 + drawer 维持 overlay。
- `.workspace` 用 `grid-template-columns: 1fr 360px`，gap 16px
- drawer 维持 `position: fixed` + 右侧滑入 + `box-shadow` scrim，与新加的 notes panel 在不同 z-index 层
- 中段断点 960-1199px：右列收窄到 300px；< 960px：notes 折叠到 sessions 下方（移动端 / drawer 80vw）

**保留的部分**：sessions 列响应式可伸（1fr），notes 列固定（360px / 300px / 折叠）。"主内容自适应、次要内容固定"是 dashboard 布局通用模式。

**核心教训**：
1. **瞬时元素不占永久网格槽**——drawer / modal / toast 这类"按需出现"的元素，永远走 overlay 层；给它们留固定列空间 = 浪费 + 视觉空洞。
2. **响应式约束从最窄屏开始算**——13" MBP 是国内开发常见配置；如果 1380px 撑不下三列，那 1440px 标称的"宽屏"也只是边缘可用，必然要在断点处妥协，不如开始就两列。
3. **既有动效是布局决策的硬约束**——R6/R7/R9 的细节都基于"drawer 是 overlay"的前提；任何"改成 column"的方案隐式承担"重写 N 个既有交互"的成本，必须显式权衡。

---

## L31 — 单用户场景下 last-write-wins 可接受，不引入 version + conflict banner（已确认）

**修改时间**：2026-05-11

**否定方案**：进程内 version 计数器 + 前端 PUT 携带 base_version + 后端 if-match 不上则返回 409 + 前端 conflict banner UI（"远程有更新，重新加载？" / "保留本地" / "合并"）。

**为什么不行**：
1. **单用户多窗口编辑频率极低**——本工具是个人本机的 dashboard，能同时编辑同一份 notes 的场景只有"同一台机器开两个浏览器标签"或"两台机器都连同一个 backend"，且要"几乎同时键入"。日常占比 < 1%。
2. **6 处协调成本**——version 锁要前端 baseVersion 状态 + dirty flag + 接收 WS 时判断是否 reload 还是 banner + 提交时携带 base_version + 后端 in-memory counter + 409 分支 + banner 三选项 UI。每处都是潜在 bug 点。
3. **用户已明确"直接静默覆盖"**——R10 决策里用户原话是"直接覆盖，不要弹框"，工具应该尊重用户偏好而不是替用户决定"你不知道这会丢数据"。
4. **冲突 UI 本身是打扰**——textarea 正在敲字时弹 banner 抢焦点是糟糕的 UX；如果 banner 不抢焦点，用户也大概率忽略，等于白做。

**替代方案（已实施）**：last-write-wins + 前端接收侧自保护。
- 后端写入是简单 `O_WRONLY | O_TRUNC`，无 version 字段；返回 `{size, updated_at}` 只是给前端展示用，不是 lock token
- 前端收到 WS `notes_updated` 时：
  - 如果 textarea **未聚焦** + **未 dirty** → 静默拉新内容覆盖（"对方写完了我同步显示"）
  - 如果 textarea **聚焦中** 或 **有 dirty** → 静默忽略 WS（"我在敲字，不打扰我"），等下一次 autosave 自然覆盖远程

**保留的部分**：
- WS 广播 `source` 字段（区分"是不是我自己发的"，避免回环）
- 状态行的"已保存 · X KB · HH:MM"显示，给用户可见反馈，间接帮助 catch 异常情况
- 256KB 上限 + 413 错误体面降级（PUT 失败时本地内容不丢，状态行显示错误）

**核心教训**：
1. **冲突解决的成本要按"实际触发频率 × 触发后用户损失"算**——单用户工具的实际多窗口编辑碰撞概率 < 1%，且即使碰撞最多丢几秒键入；与之相对的实施成本是 6 个组件协调 + UI 闪烁。ROI 明显负。
2. **"接收侧自保护"≠"冲突解决"**——前者只要"我在敲字时别覆盖我"就够了，是单方面的；后者要"两边都满意"，是双向协商。本场景只需要前者。
3. **静默策略好过弹框策略**——文字编辑场景最贵的是"焦点 + 上下文"，任何抢焦点的 banner 都不该出现；用 toast / 状态行 muted 字提示足以。
4. **version 是数据库语义不是文件语义**——markdown 文件本身没有 version 概念，强加 in-memory counter 会有"backend 重启 version 归零 / 前端 baseVersion 失效 → 误判冲突"的边角 bug。如果未来真的要做版本，应该走 git（文件天然支持），而不是自建 counter。

**未来留白**：
- 真出现多用户场景时再回头补乐观锁——届时 backend 不再是 single-process，需要的不只是 version 还有 etag / locking server，跟当前架构差距大，不预先抽象。
- 是否给状态行加"远程刚被改过"的灰字提示（用户在 dirty 时收到 WS 时）：**留白**，先看真实使用反馈是否需要。

---

## L32 — active_window ≤ dead_threshold 边缘 bug：dead 判定永远触发不了（已修）

**修改时间**：2026-05-11

**否定方案**：`liveness_watcher` 用一组**互相独立、默认值碰巧重合**的阈值：
- `active_window_minutes` 默认 30：`event_store.list_sessions` 用它过滤"窗口内有事件的 session"
- `dead_threshold_minutes` 默认 30：session transcript 静默超过此阈值才升 `dead`

两个默认值同为 30，看起来语义独立，但在判定链路上**直接重叠**——一旦 session 静默到 30 分钟，先被 `list_sessions` 过滤掉，根本进不了 `liveness_watcher` 的 dead 判定循环。

**为什么不行**：
1. **死循环不可达**——dead 判定路径形如 `for s in list_sessions(active_window=30): if silence(s) > dead_threshold(30): mark_dead(s)`。`silence(s) > 30` 的 session 必然不在 `list_sessions` 返回里，逻辑层面 `mark_dead` 永远走不到。
2. **症状隐蔽**——从 trace 上看 session 卡在 `status=suspect` + 心跳 31min+，UI 显示"已 31 分钟无活动"但永远不升 dead。日志里也看不到"dead 判定被跳过"，因为根本没进循环。Round 10 用户体感是"dead 这个状态我从来没见过"。
3. **默认值耦合是隐式合约**——两个配置项分别有合理默认值，但合在一起就坏。代码里没有任何注释 / 断言提醒它们必须满足 `active_window > dead_threshold + buffer`。

**替代方案（已实施）**：active_window 动态默认 = `max(60, dead_threshold * 2)`。
- `liveness_watcher.py` 加 `_read_raw()` 区分"用户在 config.json 显式写了 active_window_minutes"还是"走 DEFAULTS 合并出来的 30"。
- 用户显式写了 → 尊重用户设定（即使会触发同样的 bug，也是用户自己的选择，但 dashboard 仍能看到）。
- 用户没写 → 默认 `max(60, dead*2)`：dead=30 时 active_window=60，保证至少有 30 分钟的"判定窗口"。

**保留的部分**：`dead_threshold_minutes` 配置项不动；`active_window_minutes` 仍允许显式覆盖。语义"两个独立配置项"对外保持不变，只是默认值算法改了。

**核心教训**：
1. **语义相关的阈值需要显式不变量声明**——`active_window` 是"我会去看这个 session 多久前的事件"，`dead_threshold` 是"我多久判它死"。不变量是 `active_window ≥ dead_threshold + buffer`，否则 dead 判定不可达。代码层面应该有 assert 或 startup 检查；docs 层面应该写明这个约束。未来加新窗口 / 新阈值时，先把所有"窗口对阈值"的不变量列出来再编默认值。
2. **DEFAULTS 合并破坏"用户显式 vs 系统默认"区分**——`config.load()` 把 DEFAULTS 和用户 config.json 合并成一个 dict 后，下游代码 `cfg.get("k") or fallback` 永远拿不到 None，无法识别"用户没设这个项"。需要的话必须直接读 `_read_raw()` 原始 dict。这是配置层的通用陷阱。
3. **"默认值碰巧相等"是最难发现的 bug**——两个配置项独立写时各自合理，组合起来就坏。code review 时无法靠"读一处看不出问题"识别；必须做"所有相关阈值的相互关系矩阵"才能发现。

**未来留白**：
- 是否给 `_read_raw()` 增加更通用的"未显式配置"探测（用 sentinel `_UNSET` 替代 dict 合并），让所有"想知道用户设没设"的代码点都用同一套接口：**留白**，目前只有 active_window 一处需要，等第二处出现再统一抽象。
- 是否在 backend 启动时做"阈值不变量检查"（如 `active_window < dead_threshold` 时打 warning）：**留白**，先看是否还有别的阈值对暴露类似问题再统一加。

---

## L33 — 阈值不能脱离信号源识别：菜单 prompt 要先识别再 bypass（已实施）

**修改时间**：2026-05-11

**否定方案**：用户希望"Claude 列菜单等用户选（`❯ 1. xxx`）→ 立即飞书提醒"。第一反应是**直接缩 `notif_idle_dup` 阈值**：把"距上一次 Stop 5 分钟内的 idle prompt 副本被吞"改成 2 分钟，让菜单提示能更快推送出去。

**为什么不行**：
1. **菜单 prompt 不是唯一的 idle Notification 源**——Claude Code 的 `Notification` hook 在多种场景触发：菜单选择、回合心跳、tool 提示、空回合 idle 等。`notif_idle_dup` 之所以默认 5 分钟，就是因为 idle Notification 在一次任务里会被反复触发（每次 Claude 写一段就发一条），不去重的话 5 分钟内能收到 10+ 条几乎一样的提示。
2. **缩阈值是"群体改动"**——把 5min → 2min 意味着**所有** Notification 都更快放行，包括"心跳通知"。结果是：菜单场景如愿快了，但日常 idle 心跳也开始 2 分钟一弹，整体噪声反而上升。用户的诉求是"菜单要快推"，不是"所有 idle 都要快推"。
3. **失去 dup 过滤价值**——把窗口压到 2 分钟接近无过滤；当初引入 dup 过滤就是为了避免同一回合内重复打扰，缩到 2min 等于半废这个机制。

**替代方案（已实施）**：先识别"真等用户输入"的菜单信号，再让它走 bypass 通道。
- `hook-notify.py` 加 `_detect_menu_prompt(transcript_path)`：读 transcript 末 20 行，子串匹配 `❯\s*\d+\.|Type something`，命中 → `evt.raw.menu_detected=true`
- `notify_filter._idle_reminder_decision` 首位加 menu bypass：`if evt.raw.get("menu_detected"): return True, "menu_detected_bypass_dup"`
- 普通 Notification 仍按 `notif_idle_dup` 5min 走 dup（实际本轮调成与 reminder 阈值同步），噪声不变

**保留的部分**：
- `notif_idle_dup` 阈值机制不动（语义"普通 idle 心跳的去重窗口"）
- `notif_idle_reminder_minutes` 数组机制不动（语义"按累积等待时长升级"）
- 菜单识别只在 hook 侧打 flag，filter 侧只读 flag 不重新检测——职责分离

**核心教训**：
1. **当一个通道既有"真等用户"又有"心跳"时，先做信号源分类再调阈值**——所有"调阈值缓解某场景"的提案，先问"这个场景的事件是不是和其它场景同源"。如果同源，就要先做识别再做差异化处理，不能直接缩阈值。
2. **bypass 优于阈值压缩**——阈值是统计学口径（"大多数情况下 X 分钟够了"），bypass 是确定性口径（"这条事件就是我要的，不参与去重"）。识别准确时 bypass 永远优于阈值；识别不准时阈值兜底。
3. **识别成本应该花在 hook 侧而非 filter 侧**——hook 拿到原始 transcript，做一次子串匹配几乎零成本；filter 侧拿到的是规范化后的 evt，重新去读 transcript 是耦合污染。把"识别"作为 evt 的元数据字段是更干净的分层。

**未来留白**：
- transcript JSONL 的菜单文本是 escape 字符串（`\n` 是字面 `\n` 不是真换行），`re.MULTILINE` 锚定不可用。如果未来要识别更复杂的 prompt 模式（如"按 y 确认"、"Choose an option:"），仍优先子串匹配，不要走多行正则。
- 当前菜单识别只在 hook 触发时做一次（事件驱动）；若未来 Claude Code 改成"菜单显示后但不发 Notification"的模式，需要补 transcript watcher 主动扫描。目前没必要。

---

## L34 — 阈值 [15, 45] 节奏选定：避免"刚走开冲杯咖啡就被叫回"（已确认）

**修改时间**：2026-05-11

**否定方案候选**：
- A) `[5, 10]`（R7 旧节奏）—— 5min 二次提醒 + 10min 最终提醒
- B) `[10, 30]` —— 中庸方案
- C) `[10, 60]` —— 长尾覆盖 AFK
- D) `[15, 45]` —— 用户最终选定

**为什么旧 `[5, 10]` 不行**：
1. **微中断成本高**——R7 节奏下用户反馈"我刚去倒杯咖啡，5 分钟后第二次提醒就来了，10 分钟后第三次又来了，连续被三条飞书追着"。短任务的"已完成"提示和"还在等你"提醒之间间隔过短，体感是"刚回完一句话还没消化呢"。
2. **不匹配真实 AFK 场景**——典型离桌行为是"接个水/上个厕所/拿快递"（2-10 分钟）或"开会/午饭/小睡"（30-60 分钟）。`[5, 10]` 落在第一档的尾巴，正好覆盖"我准备回去看"的窗口，刚要回来就被提醒，等于"白叫"。
3. **不匹配菜单 bypass 机制**——L33 的菜单识别已经让"真要立即处理"的 prompt 走 bypass 通道，普通 idle 提醒不再承担"快速响应"的职责，可以放慢。

**B / C 为什么也不选**：
- B `[10, 30]` 一档勉强够离桌缓冲，但 30min 最终提醒卡在"dead 判定也是 30min"的边界（参考 L32），容易让用户分不清"是 reminder 还是 dead"。
- C `[10, 60]` 第一档 10min 仍偏短，60min 太长——超过 30min 大多数情况已经升 dead 或被用户主动关掉 session，最终提醒就推不到了。

**替代方案（已实施）**：`notif_idle_reminder_minutes = [15, 45]`
- 15min 二次提醒：覆盖"短任务后离桌一会"，避免微中断
- 45min 最终提醒：覆盖"开会/午饭/拿快递"的典型 AFK 场景，且远离 dead 边界（30min）
- 配合 L33 菜单 bypass：真"等用户输入"的菜单场景从第 0 分钟就立即推，不受 reminder 节奏影响

**保留的部分**：
- 3 次硬上限不变（L22）：第 1 次 = 立即（Stop 完成）、第 2 次 = 15min、第 3 次 = 45min，之后永远不再推
- 反向兜底机制不变：用户可在 `config.json` 显式改回 `[5, 10]` 或自定义任意数组

**核心教训**：
1. **提醒节奏要按"用户离桌典型时长"而非"我担心他错过"设定**——担心错过是工具方视角；用户视角是"打扰我多少次合理"。15/45 的间隔承认了"用户有权离桌 45 分钟"，工具不该每 5 分钟刷一次存在感。
2. **配对系统的阈值要协调**——L32 已经让 dead=30min 是个稳定的状态切换边界；reminder 阈值不应跨过 dead 边界（30→45 才是合理顺序）。否则会出现"已推 reminder #2 / 此时 session 也判 dead"的语义重叠。
3. **节奏调整不能孤立做，必须和信号源识别一起做**——本轮 [5,10]→[15,45] 之所以可行，前提是 L33 已经让"真急的事"走 bypass，普通 reminder 才有资格放慢。若没有 bypass，单纯放慢 reminder 会真错过菜单场景。

**未来留白**：
- 若用户反馈"菜单出来后 15min 才二次提醒太慢"：单独给菜单场景做"加速 reminder"——`menu_detected=true` 的 session 用独立的 `[5, 15]` reminder 数组，与 idle_dup bypass 配合实现"菜单从第 0 分钟就紧急"。当前不预先抽象，等用户反馈触发。
- 是否给 reminder 阈值加"自适应"（首次 [5,10] 试推几次，用户响应慢就拉长到 [15,45]）：**留白**，自适应需要"用户响应"信号源，目前 dashboard 没记录"用户什么时候点开 session"。

---

## L35 — UI 紧急徽不引入新 status enum：派生 UI hint 字段 vs 状态机字段（已确认）

**修改时间**：2026-05-11

**否定方案候选**：
- A) 新增 status enum `awaiting_select`（菜单等待中）—— 走完整状态机
- B) 仅加 UI 紧急徽（`menu_detected: bool` 派生字段）—— 不动状态机
- C) 不改 UI，只动飞书推送（菜单时改文案）—— 缺 dashboard 反馈

**为什么 A 不可行**：
1. **status enum 是状态机核心字段**——`status: running | idle | waiting | suspect | ended | dead` 涉及：
   - `derive_status()`：根据事件序列计算（7+ 处分支）
   - `list_sessions` 排序权重：waiting > suspect > idle > running > ended > dead（每次新加 enum 要重排权重）
   - 飞书消息文案分支：每个 status 有独立的 emoji / 模板
   - dashboard 卡片颜色 / 排序 / 过滤 chips：每个 status 对应一组 UI 元素
   - 紧急度徽（L26）按 status 决定阈值表
2. **awaiting_select 不是新状态而是 idle 的子类**——菜单出来时 session 本质上还是 idle（Claude 回完一段、等用户）。强行加一个并列 enum 会让 `idle vs awaiting_select` 的边界模糊（菜单出来但用户也可能不点选直接发新消息，那它该转回 idle 还是停在 awaiting_select？又要加一组转换规则）。
3. **新增 enum 是滚雪球改动**——加完 status 还要补：派生函数、排序权重、飞书模板分支、徽章配色、过滤 chip、状态语义文档（user-guide §一）、状态转换图。每处都是潜在 bug 点，且没一个是用户真要的。

**为什么 C 不够**：
- 缺 dashboard 反馈：用户开 dashboard 想"扫一眼看哪个 session 在等我"，没视觉差异就看不出
- 飞书消息文案变化是事后的（要等推送），不是实时的（菜单一出就该看到）

**替代方案（已实施）**：B - 加派生 UI hint 字段 `menu_detected: bool`，不动状态机。
- `event_store.list_sessions` 在派生 session 摘要时加 `menu_detected` 字段：
  - 见到 `Notification` 且 `raw.menu_detected=true` → 置 `true`
  - 见到 `Stop` → 清零（菜单已被新事件取代）
  - 见到 `SubagentStop` → 不清零（子 agent 完成不代表主菜单消失）
- 前端 `app.js` `renderSessionRow` 加紧急徽 HTML：`menu_detected=true` 时在状态徽旁加 🔥 "指令选择·待响应"
- 前端 `styles.css` 加 `.urgency-badge.urgency-menu` + 1.8s pulse 动画 + `prefers-reduced-motion` 守约（无障碍）
- 复用既有 `--c-suspect-soft/solid` token，不引入新色板

**保留的部分**：
- status 状态机不动，所有既有逻辑（排序权重、消息模板、过滤）零影响
- 紧急徽（L26）继续按 status + age 算法生效，菜单徽与紧急徽**并存**（菜单徽显眼，紧急徽显时长）
- `menu_detected` 字段是派生的，不存盘——重启 backend 后从 events 重新派生

**核心教训**：
1. **新增 UI 视觉差异时先评估是不是状态机层面的事**——判断标准：
   - 影响排序 / 过滤 / 持久化逻辑 → 状态机字段（status enum）
   - 仅影响视觉表现（颜色/图标/动画） → 派生 UI hint 字段（bool / enum）
   - 当前菜单场景属于后者：菜单出来 = "提醒用户看一眼"，但 session 本质生命周期没变。
2. **派生字段比 enum 扩展便宜一个数量级**——加 `menu_detected: bool` 涉及 1 个派生函数 + 1 处 UI 渲染 + 1 处 CSS；加 `awaiting_select` enum 涉及 7+ 处状态机协调。同样的用户价值，成本差 7×。
3. **派生 = 单一职责**——`menu_detected` 只回答"现在 session 卡片要不要显示菜单徽"，不参与排序、不参与推送决策、不进飞书消息。语义边界极小，未来想砍也容易（删 3 行代码）。

**未来留白**：
- 若未来发现"菜单徽 + 紧急徽并存视觉太挤"，可以让菜单徽**替换**紧急徽（同位置渲染，菜单优先）。当前两个徽是不同语义不重叠，先并存看用户反馈。
- 若未来要在飞书消息也加菜单标识（如"📋 task · 等你选"），那时是消息模板分支的事，不需要回头改 status enum——继续走"派生字段进消息"的路径。
- 是否给菜单徽加"已等 X 分钟"细节（类比紧急徽）：**留白**，先看用户实际使用反馈是否需要时长信息。

---

## L36 — liveness_watcher 对 inactive session 也必须跑 dead 判定（已修）

**创建时间**：2026-05-11 13:10:00
**触发场景**：dashboard 上看到一张"00-Tian-Project/claude-notify · ✎ 运行中 · 11:48 · 1 小时前 · 最近：11:48 ✗ policy_off"的卡片，但实际进程早死了，"→ 终端"按钮 disabled（tty 空）。无法清除。

**根因（两层 bug 叠加）**：
1. `derive_status("SessionStart")` 走 fall-through 默认 `return "running"`（event_store.py:176-187）——状态机里没有"刚启动还没干活"这个语义档位。
2. `liveness_watcher.watch_loop` 在 `if not s.get("active"): continue`（旧 line 120）直接跳过所有 inactive session。R11 把 active_window 默认拉宽到 max(60, dead*2)=60min，但**没修对称的另一头**：超过 active_window 的 inactive ghost session（典型：SessionStart-only，age=69min）永远进不来 dead 判定。
3. 结果：status 卡 "running"、active=False、watcher 不收，dashboard 上显示"运行中"且无法清除。

**修复**：
- `liveness_watcher.py:117-122` 重构：移除 `if not s.get("active"): continue` 顶层 skip。
- **dead 判定提到 active 检查之前**：只要 `pid_dead_fast`（pid 死 + tx≥60s）或 `tx_only_slow`（tx ≥ dead_threshold）命中，无论 active 与否都标 dead。
- **suspect 判定仍按 active skip**：避免对 inactive session 反复刷 TimeoutSuspect 噪音（这种 session 本来就 dormant，再报 suspect 没意义）。
- `evt.raw.active` 记录原 active 标志，便于排查"为什么这次报"。
- 单测：`scripts/0511-1310-test-r12-ghost-session.py`（4 PASS）：
  - inactive + pid 死 + tx 不存在 → dead ✓
  - inactive + pid 活 + tx 在 → 不发任何事件 ✓
  - 已 push 过的 dead 幂等 ✓
  - active + pid 死 → dead（R11 fast path 回归）✓

**核心教训**：
1. **状态机的 skip 条件要对称**——R11 修了"卡在 31min 的 active session 进不来 dead 判定"，但漏掉"超过 active_window 的 inactive session 也卡在 running 进不来 dead 判定"。状态机的每一个 skip 条件都要问"这个分支会不会让某类 session 永远到不了 terminal 状态"。
2. **default 分支的语义要清楚**——`derive_status` 用 default `return "running"` 处理所有未匹配事件，看似稳健，但和 SessionStart 这种 "刚启动还啥都没干" 的场景叠加就成了 ghost。每个未匹配 case 都要主动选语义，而不是让"running"做 catch-all。
3. **dead 判定 vs suspect 判定的优先级**——dead 是"事实结论"（pid 没了/transcript 没了），suspect 是"启发式猜测"（静默了一会）。事实判定不应被启发式判定的前置条件（active）所阻挡。把强信号（dead）排在弱信号（suspect）前面，是状态机的一般原则。

**为什么不在 derive_status 里给 SessionStart 单独分支**：
- 已考虑过，但状态机派生字段（`derive_status`）应该是**纯函数**，只看 `last_event` 字符串就出结果。要"SessionStart age > active_window → dead"必须看 age，已超出纯派生的边界。
- 让 watcher 把 ghost 收为 dead，反而是把"时间敏感"的判断留给了对的层（watcher 本来就是按时间扫的）。

**连带发现的二级 bug（同次修掉）**：
- `_dead_msg(tx_age=float("inf"), ...)` 在 `int(tx_age // 60)` 抛 `ValueError: cannot convert float NaN to integer`。
- 这个 bug 之前从未暴露——因为 R12 之前 inactive session 永远跑不到 dead 判定，自然走不到 `_dead_msg` 处理 inf 的分支。R12 一开后第一次重启 backend，log 里全是 ValueError 被 `except Exception` 吞掉，SessionDead 一条都发不出来。
- 修法：`_dead_msg` 显式分支处理 `tx_age == float("inf")`（transcript 文件不存在）。返回"transcript 文件不存在"语义而不是数字分钟。
- 教训：**`except Exception: log.exception` 是隐性 bug 沉淀池**——业务路径没跑通，但 traceback 只进了日志没冒泡到测试。靠 mock 跑测试时没炸（因为顶层 try 把 ValueError 吞了），靠真实重启 backend 才暴露。
- 防回归：单独加 `TestDeadMsgInfHandling` 三个用例直接测 `_dead_msg(tx_age=inf)`，下次任何人改这个函数破坏 inf 处理会立刻被抓。

**用户层面留白**：
- 修复仅在 backend 重启后生效（live watcher 用新代码）。
- 历史遗留的 ghost session（如 04910788）会在 backend 重启后 30s 内被批量标 dead。dashboard 卡片状态会从"运行中"变为"已歇"。已验证 04910788 在 13:08:07 被 watcher 标 dead，dashboard 列表中 `status=running & active=False` 的 ghost 数 0。


## L37 — CSS container query：两条路都翻车，回退到 @media viewport（已修）

**创建时间**：2026-05-11 14:00:00
**更新时间**：2026-05-11 14:15:00（R13b 回滚 sessions-list 方案 + 改 @media）
**触发场景**：用户给截图，窄视口下 session 卡片宽度异常窄（被压到 ~30% 宽，左半边空白）；卡片头部的 dot/name/✎/status-badge 和 actions 区的 → 终端按钮挤在同一行严重重叠（"→等输入"、"→运行中"、"② 2min 回合结束 → 终端" 互相叠字）。

**根因**：
- `.session-item { container-type: inline-size; }` 把卡片自己声明为 inline-size container，本意是让卡片"按自身可用宽切布局"。
- 然后写了 `@container (max-width: 540px) { .session-item { grid-template-columns: minmax(0, 1fr); grid-template-areas: "head" "summary" "actions" "meta" "trace"; } }`，期望卡片宽 ≤ 540px 时变单列。
- **但 `@container` 查的是 *ancestor* container，元素不是自己的 container**（CSS Containment 规范明文）。`.session-item` 的祖先（`.sessions-list` / `.workspace` / `main`）都没声明 container-type → query 找不到匹配的 container → 单列规则**永远不命中**。
- 后果：窄视口下卡片依旧用桌面双列布局 `minmax(0, 1fr) auto`，head 列和 actions 列在 ~300px 宽空间里挤在同一行，子元素重叠出错。

**修复尝试一（R13，失败）**：把 `container-type: inline-size` 从 `.session-item` 挪到 `.sessions-list`，期望 `@container` 查到 sessions-list 作为祖先 container。

- **挪后新 bug**：sessions-list 是 workspace 的 grid item，而 CSS 规范规定 **"The auto inline-size of an inline-size containment box behaves as 0px"**。sessions-list 没有显式 width，作为 grid item `justify-self: stretch` 时其 used inline-size 仍可能被 containment 算法视作 0，max-width 1100 clamp 后等于 0+min(0, 1100)≈0。
- 实测翻车：sessions-list 在视口 720-959 段实际渲染宽 ~50px，子卡片被压成窄柱，actions/meta/trace 飘到卡片外，比修前更糟。

**修复尝试二（R13b，已修）**：放弃 container query，回退到 viewport-based `@media`：
- 删 `.sessions-list` 上的 `container-type: inline-size`。
- `@container (max-width: 540px)` 改为 `@media (max-width: 720px)`，触发条件用视口宽度而非容器宽度。
- 阈值从 540 提到 720：实测 head-l 内 dot + name + status badge + menu badge 一行至少需 ~450px，actions 再加 70px → 双列布局需要卡片可用宽 ≥ 520px，对应视口 ≥ 720（main padding 扣 32-48px 后 ~ 670+）。

**核心教训**：
1. **container query 是 ancestor query，不是 self query**——元素自己设 `container-type` 只服务 descendants，不服务自己。要查"我自己的宽"必须由父容器声明 container-type。规范第一段"An element with a containment context is considered to be a containment context for all of its descendants, but not for itself"是关键。
2. **inline-size containment 不能加在 layout-sensitive 容器上**——`contain: inline-size` 让 auto inline-size 视为 0；如果该元素是 grid/flex item 且依赖 stretch 取得自然宽度，containment 会和 stretch 算法打架，导致 width 塌缩。换句话说：container-type 适合加在"width 已经被显式决定（block + 父定宽）"的元素上，不适合加在"width 靠 stretch / fr / fit-content 间接决定"的元素上。
3. **container query 的"按容器宽切布局"愿景，在常见 layout（grid/flex 嵌套）下经常无法干净实现**。没有"安全容器"可以放 container-type 时，承认局限，老老实实用 `@media` viewport。少一点酷炫但能 work 的方案胜过精致但坑爹的方案。

**用户层面**：
- 硬刷页面（cmd+shift+R）即生效，不需要重启 backend。
- 视口 ≤ 720px 时 session 卡片单列（head / summary / actions / meta 各一行）。
- 视口 > 720px 时双列（head 与 actions 同行）。

**响应式手测的盲点**：
- task #80 标"响应式 6 档手测通过"但漏掉这个 bug。原因之一：当时 container query 写错（self-target），手测在 320/375/...视口都试过但 @container 静默失败，肉眼看不出"窄屏没切单列"——卡片在桌面布局下虽然挤但勉强能读，没人意识到那是"规则没生效"。
- 教训：CSS silent failure 比 JS 难抓得多。响应式回归不能只靠肉眼，要在 devtools 把 @media/@container 触发的 class 写到一个明显的 indicator（如 body 加个标签 "narrow-mode"），手测时直接看 indicator 是否切换，而不是看视觉效果对不对。

## L38 — trace UI 是开发者工具，不该长在最终用户视野里（已删）

**背景**：v3 引入 `decision_log`（L12）记录每条飞书推送的"推/吞/scheduled" 决策与 reason，原本是为了 debug "为什么这次没推"。当时同时做了两件事：
1. **后端**：`data/push_decisions.jsonl` 写 + `/api/sessions/{sid}/decisions` endpoint
2. **前端**：卡片底部一行虚线灰字 + 点击展开 50 条 modal

**问题**：用户开源后看到真实环境的卡片，反馈"内容很杂乱"。具体几个槽点：
- 卡片底部 `permission_mode · effort_level`（"bypassPermissions · xhigh"）是 Claude Code 内部状态名，普通用户不懂
- `心跳 N 分钟前` 与相对时间重复，也是开发者监控视角
- **trace 行 + modal 最严重**：内容像 `最近：14:02 ✗ policy_off · 14:00 ✗ notif_idle_dup(0.8min<15min)`，普通用户根本不知道 "policy_off / notif_idle_dup" 是什么。点开是 50 条这种行的表格，纯 debug 面板。

**核心教训**：**面向开发者的可观测性工具 ≠ 面向最终用户的 UI**。哪怕同样的数据，呈现给谁、什么时候呈现，是两个完全不同的设计问题。给开发者的 jsonl + curl endpoint 是合理的；把它"顺手"渲染进最终用户的主视野，就违反了"默认隐藏专家功能"原则。

**修复（L38）**：
- 前端：删 `modal-trace` HTML、`renderTraceLine` JS、`openTraceModal/closeTraceModal/renderTraceModalBody/traceFullTime`、CSS `.push-trace/.trace-row/.trace-time/.trace-event/.trace-mark/.trace-reason/.trace-policy/.trace-body` 整组、`api.listDecisions` 调用、`grid-template-areas` 里的 `trace` 行、`recent_decisions` 字段消费。
- 前端同时清理：删 `permission_mode · effort_level` 副元行、删"心跳 N 分钟前"、绝对+相对时间合并成"相对时间 + hover 完整"一处。
- 后端：**保留** `decision_log.append` 写盘、`/api/sessions/{sid}/decisions` endpoint。开发者要 debug 仍能 `tail -f data/push_decisions.jsonl` 或 curl endpoint。
- 文档：user-guide.md §五 "push trace reason 字典" 删除（reason 速查并入 §五"场景速查"的"我怀疑某条没推"段，给开发者看）。modules.md `backend.decision_log` 模块说明加 L38 备注"前端 UI 已下线"。

**判断准则（给未来）**：要不要把一个数据放在卡片首屏？三问：
1. **新用户看到能理解吗**？不能 → 默认隐藏
2. **看到能 actionable 吗**（看到后能/需要做什么）？看不出能做啥 → 默认隐藏
3. **不看会错过关键信息吗**？不会 → 默认隐藏

trace 三问全 No。permission_mode / effort_level 同样三问全 No。心跳时间同样 No（relTime 已够）。**默认隐藏不等于删除**——后端数据照写，开发者另有路径（jsonl + endpoint）。这是"两个用户群"思维：最终用户的 UI 极简、开发者的可观测性走旁路。

**反例（不该这么改）**：
- ~~在配置里加 `display.show_trace` 开关默认 off~~：增加配置复杂度，YAGNI。开发者要看直接 curl。
- ~~把 trace 行折进"详情抽屉"~~：drawer 里塞这种 debug 内容仍然误导（用户点开是想看"这个 session 在干啥"，不是"为什么这条没推"）。

**用户层面**：
- 硬刷页面（cmd+shift+R）即生效。
- 卡片现在只剩：状态 / 紧急徽 / 静音按钮 / 终端按钮 / 摘要 / cwd + 一处时间。
- 开发者 debug 推送：`tail -f data/push_decisions.jsonl` 或 `curl /api/sessions/<sid>/decisions`。

## L39 — 卡片摘要里的 markdown 控制符要 strip（已修）

**背景**：用户报告 dashboard 卡片摘要显示：

```
刚完成 **硬刷 cmd+shift+R 看实际效果**。 · 下一步 提下一轮 / 或归档
```

裸 `**` 控制符直接暴露在视野里。

**根因**：backend 派生卡片摘要的多个字段（`last_assistant_message` / `last_action` / `next_action` / `waiting_for` / `last_milestone` / `last_message` / `note`）时，直接抄 transcript 原文。`backend/sources.py:260 clean_summary` 函数本来就有 markdown strip 能力（去 `**bold**` / `*italic*` / `` `code` `` / `[link](url)` / `# heading` / `>` quote / list bullets），但它只在生成 `turn_summary` 一处调用，其它字段未走 clean_summary。

**修法权衡**：
- A. 在 backend 所有字段派生处统一过 clean_summary —— 风险：clean_summary 还做了 sentence selection + 锚词优先 + max_len 截断，把 `last_action`（应保完整）也跑一遍会改语义
- B. 抽一个**轻量 strip_markdown** 公用函数，只去控制符不动语义 —— 干净，但要改 backend 多处
- C. **前端 strip**：渲染前过一遍 `stripMd()`，只改显示不改数据 —— 隔离最好

**选 C**：前端 `frontend/format.js` 新增 `stripMd(s)` helper，`app.js` import 后在 `sessionCardHTML` 里把所有摘要字段（waitingFor / lastAction / lastMilestone / turnSummary / nextAction / lastMsg + summaryRaw 用的 `s.last_assistant_message` / `s.note`）渲染前都过一遍。

**stripMd 覆盖的格式**：
- `` ```fenced``` `` → ` [代码] `（围栏代码块保意图）
- `` `inline code` `` → `inline code`
- `**bold**` / `__bold__` → `bold`
- `*italic*` / `_italic_` → `italic`（用负 lookbehind 避免 `**bold**` 被双重处理）
- `# heading` → `heading`（仅去 # 标记）
- `[text](url)` → `text`
- `> blockquote` → `blockquote`
- `- list item` / `1. list item` → `list item`
- 多空白折叠为单空格 + trim

**测试 9 个 case 全过**（含中文 + 混排）：
```
"**修改前**（你的真实卡片）："  →  "修改前（你的真实卡片）："
"运行 `git push origin main` 完成"  →  "运行 git push origin main 完成"
"见 [README](docs/user-guide.md) 章节 3"  →  "见 README 章节 3"
```

**核心教训**：
1. **显示层 strip vs 数据层 strip**：先选显示层。数据层 strip 会破坏 LLM prompt 上下文、影响 ranking、影响外部 API 消费者。显示层 strip 只服务"人眼看的那一刻"。
2. **regex 顺序敏感**：必须先 fenced code（多行）→ inline code（单行）→ bold（双星）→ italic（单星）。颠倒会让 `**foo**` 在 italic 阶段被错误吃掉一个 `*`。
3. **下次再加摘要字段**：所有 transcript-derived 字符串字段在 sessionCardHTML 里使用前一律 stripMd。这条放进 modules.md 的"前端约定"段。

**反例（不该这么改）**：
- ~~在卡片里渲染 markdown~~：line-clamp 2 里粗体反而难读，且需引入 markdown 库；本来卡片就是简洁信息流。
- ~~在 hook-notify.py 写 transcript 时 strip~~：transcript 是真实记录，不该被改。
- ~~把 stripMd 强加到 backend 所有字段 setter~~：会破坏 LLM 摘要 prompt 输入（拿到 strip 后的 last_assistant_message 上下文丢失）。

**用户层面**：硬刷 cmd+shift+R 即生效，无需重启 backend。卡片摘要现在只显示纯文。

## L40 — Claude Code Notification.raw.notification_type 是更可靠的"等用户响应"信号（已修）

**背景**：用户在另一个项目用 Claude Code 跑任务，Claude 调用了 `AskUserQuestion` 工具向用户问问题（结构化菜单 4 选项 + Type something）。dashboard **完全没推飞书**，等了几分钟才发现没提醒。

**根因排查**：

1. `data/events.jsonl` 14:28:33 有 sid=8d8d6fb0 的 `Notification` 事件，message="Claude Code needs your attention"
2. 但 `raw.menu_detected` 是 **未设** —— R11 菜单识别（`hook-notify.py:_detect_menu_prompt`）读 transcript 末 20 行匹配 `❯ 数字.` / `Type something`，**只识别 Claude 在 assistant text 里直接写的菜单文本**
3. AskUserQuestion 工具产生的菜单是**结构化 tool_input.questions**（JSON 数组），不在 assistant text 里 → R11 menu 识别完全错过
4. `data/push_decisions.jsonl` 决策：`drop, reason=notif_idle_dup(6.5min<15min)` —— 距上次 Stop 才 6.5 分钟，没到 idle reminder 第 1 次 15 分钟阈值，被 dup 过滤吞了

**关键发现**：Notification 事件 `raw` 里有 Claude Code 官方信号：
```json
"raw": {
  "hook_event_name": "Notification",
  "message": "Claude Code needs your attention",
  "notification_type": "permission_prompt"
}
```

`notification_type: "permission_prompt"` 是 Claude Code 内部明确告诉你"这是等用户响应的提示"——包括：
- `AskUserQuestion` 工具调用产生的问询
- 工具授权请求（Bash / Edit 等 needs approval）
- 任何 SDK 未来引入的新问询 tool

它**比文本菜单识别更早、更可靠**（无需读 transcript、无需 regex 匹配中文菜单符号）。

**修复（L40 / Round 15）**：

1. `backend/notify_filter.py:_idle_reminder_decision` 在 `menu_detected_bypass_dup` 之前加 `permission_prompt_bypass_dup` 分支：
   ```python
   raw = evt.get("raw") or {}
   if raw.get("notification_type") == "permission_prompt":
       return True, "permission_prompt_bypass_dup"
   if raw.get("menu_detected") is True:
       return True, "menu_detected_bypass_dup"
   ```

2. `backend/event_store.py:list_sessions` Notification 处理也加 OR 条件：notification_type==permission_prompt 同样让 `session.menu_detected=True`（dashboard 🔥 指令选择徽亮起）+ status 翻 waiting + is_idle_dup=False。

**核心教训**：

1. **优先用上游官方信号，少做下游 heuristic**：菜单识别是"我从 transcript 倒查出来"的间接信号；`notification_type` 是 Claude Code "我现在告诉你这是 permission"的直接信号。前者依赖文本格式（容易因 SDK 升级 / 中英文混排 / ANSI 转义而失效），后者是结构化字段（稳定）。**两者都保留**（heuristic 兜底文本菜单场景），但 official 优先。
2. **每次飞书"莫名其妙没推"先看 `data/push_decisions.jsonl`**：reason 列直接告诉你被谁拦了（`notif_idle_dup` / `sidechain_active` / `session_muted_all` / `quiet_hours_HH:MM` / `policy_off`）。本次 root cause 5 秒锁定就靠这。
3. **R11 设计是"菜单文本识别"，不是"等用户响应识别"**——前者是后者的子集。下次再加这类信号识别，命名应从行为出发（"need_user_response"）而非从机制（"menu_detected"），否则 SDK 引入新的"等用户响应"机制（AskUserQuestion）时会有同样盲点。

**回归保护**：新增 `scripts/0511-1438-test-r15-permission-prompt-bypass.py` 4 个 case 全过（permission_prompt bypass / menu_detected 仍 work / 普通 idle 仍被吞 / event_store 派生 session.menu_detected）；R11 hotfix 12/12 pass、R12 7/7 pass，无回归。

**用户层面**：
- 改动在 backend，需要重启 backend 生效（`python3 -m backend.app` ctrl+c 后重跑）。
- 重启后下一次 AskUserQuestion / 任何 permission_prompt 类 Notification 都会立即推飞书 + dashboard 显示 🔥 指令选择徽。
- push 决策 reason 会显示 `permission_prompt_bypass_dup`。

---

## L41 — 推送渠道解耦：飞书 + 浏览器桌面通知并存（已修）

**触发**：用户提出"不愿意使用飞书的用户怎么办？能否关闭飞书，只用 web 显示？"。

**根因 / 历史包袱**：
- 全项目从设计起死认飞书一条渠道 —— `feishu.send_event` 是唯一推送出口，没有渠道概念。
- Dashboard 只是"状态可视化窗口"，不发任何浏览器通知。tab 切到后台用户就完全没感知。
- 想关飞书唯一方法是清空 webhook（reason=no_webhook），但没有显式"用户主动关闭"的语义。

**实现**：

1. `backend/config.py` 新增 `DEFAULT_PUSH_CHANNELS = {"feishu": True, "browser": True}`。
2. `feishu.send_event` / `send_events_combined` 入口加渠道守卫：`channels.feishu=False → reason="channel_off"`。
3. `NotifyDispatcher.set_push_listener(cb)`：5 个调 feishu 的位置前都先 `_emit_browser_push(evt, reason)`；listener 由 `app.py:lifespan` 注入，包装 `hub.broadcast({"type":"push_event",...})`。
4. `_globally_silenced` 内置 muted / snooze_until 判断 → browser 跟随全局静音，但**不**跟随 feishu channel。即便 feishu 关掉，browser 仍发。
5. 新增 `frontend/notify.js`（~100 行）：顶部条带 "启用桌面通知"、`Notification.permission` 管理、`onPushEvent(env)` 弹 toast。
6. `Settings → 推送渠道` 两个 checkbox（飞书 / 浏览器）。
7. `/api/test-notify` 双发：先广播 push_event 给 WS（任何开关），再发飞书（按开关）。关掉飞书也能用"测试推送"验证浏览器通知。

**核心教训**：

1. **不要把"传输层"和"决策层"绑死**：原代码 `feishu.send_event` 既做"决定发送"又做"决定走哪条管道"。解耦后，决策层（notify_policy + notify_filter）只输出"这条要推"，渠道层（feishu / browser / 未来 Bark / Telegram）各自按开关决定要不要落地。新增第 3 条渠道只需注一个 listener。
2. **全局静音 vs 渠道开关分清楚**：
   - `muted` / `snooze_until` = 全局静音（用户："现在别烦我"）→ browser + feishu **都**不发
   - `push_channels.feishu=false` = 渠道偏好（用户："不想用飞书但要 web 看"）→ browser 仍发
   - 在 `_globally_silenced` 做前置判断，避免 browser 在用户已 snooze 期间还弹
3. ~~**tab visibility + hasFocus 双重判断**：visible+focused 时不弹 OS toast。~~  
   **L41-HotFix（同回合修正）已删此抑制**。实测用户反馈"飞书有，浏览器没"产生混乱 —— 用户开着 dashboard tab 不代表正盯着，飞书不会因为 dashboard 开着就不推手机，浏览器也应一样无条件推。**渠道对齐 > 重复打扰避免**。教训本身：UX 决策不要替用户太精细；和并存渠道行为一致比"少打扰一次"更重要。
4. **首次授权 UX**：**不要**在页面加载时直接调 `Notification.requestPermission()`（浏览器视为骚扰，可能直接 deny block）。改用"用户点击触发"模式 —— 顶部条带带"启用"按钮，用户主动点才弹权限请求。dismiss 持久化到 localStorage 防止反复出现。
5. **不响声音是用户偏好驱动**：本项目用户明确说"不要声音，有弹提示就行"——`silent: true` 是默认。如果未来要响，加 sub-toggle `push_channels.browser_sound` 而不是全局改默认。

**回归保护**：R15 permission_prompt 测试（6/6）通过，无回归。`config.DEFAULTS["push_channels"]` 通过 import sanity 验证。WS push_event 端到端 collected 1 message 确认链路。

**用户层面**：
- 想完全脱离飞书：Settings → 推送渠道 → 取消勾选"飞书 webhook 推送"。其它推送逻辑（合并 / 静默 / 过滤）不变。
- 启用浏览器通知：dashboard 顶部"启用桌面通知"条带 → 点 "启用" → 浏览器权限弹窗 → 允许。
- 浏览器通知无视前/后台都弹（和飞书对齐，避免"飞书有但浏览器没"的不一致）。
- 测试推送：广播给 dashboard（任何开关下）+ 试图发飞书（按开关）。
- 配置字段详见 `docs/configuration.md#推送渠道`。

---

## L42 — OS Notification "有时弹有时不弹"的三因 + 各自的修法（已修）

R16 上线浏览器桌面通知后，用户反馈"有时候只有飞书推送，有时候也有浏览器推送" —— 行为不稳让用户怀疑功能没做好。OS Notification 通道本身可靠性低于飞书 webhook，根因可以归到三个独立因素，**单独看每个都能解释一部分"漏"，叠加起来就是用户感受到的随机不稳**：

### 三因 + 修法

1. **WS 不在线时 push_event 凭空消失**（最常见，已修）
   - `hub.broadcast` 对 0 个 WS 客户端是 no-op；飞书走自己的 webhook 不依赖前端 → "dashboard tab 关了 / 刷新中 / Chrome Memory Saver 把后台 tab 休眠"时，飞书照推浏览器空响
   - 重连窗口内丢的事件不会自动补发
   - **修法**：后端加 `PushBuffer`（环形缓冲 / retention 60s / cap 200），每条 `_on_push_for_browser` 同步写一份；WS endpoint 接受 `?since_ts=<ISO>` query param，握手后从 buffer 取严格大于 since_ts 的条目回放；前端 `state._lastPushTs` 每次收到 push_event 更新，`connectWS` 重连时把它写进 URL
   - **buffer 容量假设**：60s 足以覆盖 Chrome 后台休眠 / 短网络抖动；过窗丢失（很少见）由飞书兜底
   - **安全守卫**：`parse_iso` 解析失败返 0.0；如果不 guard 会把整个 buffer 回放给伪造 since_ts 的客户端 → 加 `if since_unix <= 0: return []`

2. **`tag=session_id` 把同 session 多事件压成一条**（已修）
   - Notification API 的 `tag` 相同时，新通知**替换**上一条（不堆叠）。同 session 短时间内多事件，前几条被静默覆盖，用户视觉上"飞书堆 3 张卡片，浏览器只看到最新 1 条"
   - **修法**：tag 改成 `sid|ev_type|ts`，让同 session 不同事件 / 不同时刻能各自堆栈

3. **macOS 系统层吞通知**（不可代码修，只能用户检查）
   - 勿扰 / Focus 模式 / 系统设置里 Chrome/Safari 通知被关 / 自动焦点模式 —— 系统层吞掉前端无感
   - **应对**：README + docs/user-guide 把"3 层检查清单"写清楚（浏览器 perm / 系统通知 / 勿扰）

### 配套：双端诊断日志（已加）
- backend `_on_push_for_browser` 每次广播 info 日志：`push_event broadcast clients=N sid=XX ev=Y reason=Z`，**看 clients 数**就能立刻判断"是 WS 没接上"还是"接上了但没弹"
- frontend `onPushEvent` 入口 `console.info("[notify] push_event recv", {...})`，包含 sid / ev / reason / ts / perm；不弹时打印 skip reason（`channel off` / `permission != granted` / `dup`）
- 前端 `(sid,ev_type,ts)` dedupe Set（200 条 LRU），WS 重连后端补发同一条时不重弹

### 教训本质
1. **依赖单向 broadcast 的"实时性"通道，必须配 buffer + replay**：消息不持久化 + 客户端不在线 = 消息消失。这个反模式之前在 hub.broadcast 没暴露是因为消息更新 sessions UI 不要紧（前端会从 `/api/sessions` 重拉），但 push_event 是**一次性事件**，丢了就没了。R17 加 60s ring buffer 是最小修正；想彻底解决要么消息持久化（不值得）要么用 SW + Push API（复杂度爆炸）。
2. **`Notification.tag` 是替换语义不是分组语义**：写默认值时认知错了。任何需要"多条堆栈"的场景，tag 必须带时间戳或 nonce。
3. **`parse_iso` 失败回退 0.0 是个静默陷阱**：用户传 `?since_ts=garbage` → 解析返 0 → "大于 0 的全部"= 整个 buffer 回放。任何用 `parse_iso` 比较的地方都要先判 `<= 0`。

**回归保护**：PushBuffer 单元测试 5 since case + GC（手动跑过）；WS resume e2e 3 case（baseline / past since / future since / garbage since 共 4 个连接场景）通过。`backend.app` import 干净，前端 JS syntax 检查通过。

**用户层面**：
- "飞书有，浏览器没"复现时：打开 dashboard tab 的 console，看 `[notify] push_event recv` 日志：
  - **没有**这条 → 前端没收到 → 看 backend log `push_event broadcast clients=N`：如果 N=0 → 你的 dashboard tab 没连上 / 被 Chrome 休眠 / 刚刷新中；如果 N>0 → WS 客户端连了但消息没到（罕见）
  - **有**这条 → 看后面的 skip 原因：`skip: dup`（重复，正常）/ `skip: channel off`（开关关了）/ `skip: permission = denied`（系统层吞）
- 重连补发：dashboard tab 后台休眠醒来后 60s 内的事件会自动补；超过 60s 的丢了，靠飞书兜底。
- 想完全可靠：保留飞书通道。浏览器通道是"前台用 dashboard 时少切到飞书 App"的便利，不是替代品。


---

## L43 — Dashboard 状态从等输入翻不回工作中的根因 = 缺 UserPromptSubmit hook（已修）

**触发症状**：Claude 弹了 Notification（dashboard 显示等你 / 🔥 请你 →），用户在终端回了消息、Claude 已经开始干活几十秒乃至几分钟，dashboard 还是等输入。直到 Claude 这一回合 Stop，状态才翻成刚完成。

**根因**：
- `scripts/install-hooks.py` 的 `EVENTS_NORMAL` 只注册了 `[Notification, Stop, SubagentStop, SessionStart, SessionEnd]` + 一条心跳 `PreToolUse`。**没注册 UserPromptSubmit**。
- 用户回车的瞬间，Claude Code 触发 `UserPromptSubmit`（**没 hook，丢了**）+ 后续 `PreToolUse`（**hook 了但 backend `NON_STATUS_EVENTS` 屏蔽，不写 `last_event`**）。
- 后端 `derive_status` 只看 `last_event`：`Notification → waiting`，`Stop → idle`，其他 → `running`。从 `Notification` 翻回 `running` 唯一的事件源就是 "既不是 NON_STATUS、又不是 Notification` 副本" 的事件 —— 而这正是 UserPromptSubmit 该担当的角色。
- 在用户终端回复 → Claude 这一回合 Stop 之间这段时间（Claude 跑长任务时可达数分钟），dashboard 状态完全错位，并且 R11 的 🔥 紧急徽继续显示，让用户以为还在等自己。

**修法**：
- 一行：`EVENTS_NORMAL` 追加 `"UserPromptSubmit"`。
- 后端不用改：`sources._normalize_claude_code` 是泛化的，事件名原样透传；`event_store.append` 里 UserPromptSubmit 既不在 NON_STATUS 也不是 Notification → 自动写入 `last_event` → `derive_status` 走 default `running` 分支。
- `notify_policy` 未在 config 显式列出的事件类型默认 off（`backend/notify_policy.py:114`），不会带来飞书/浏览器推送骚扰。

**教训本质**：
1. **状态机的事件白名单要和 hook 注册同源**：后端状态机定义了 "哪些事件改 status"，但 hook 注册侧没保证 "这些事件都注册"。两份配置漂移的代价就是状态翻不回来这种安静 bug。修正后将哪些事件需要注册明确写到 install-hooks 注释里。
2. **"心跳事件不改 status" 这条规则在缺位 UserPromptSubmit 时坏掉**：`PreToolUse` 被列 NON_STATUS 是对的（避免 Heartbeat 把 idle 翻回 running 的副作用），但前提是用户开始回复有更早的事件源（UserPromptSubmit）。一旦缺位，`PreToolUse` 反成唯一可用信号，被屏蔽就没了下家。**任何过滤式白名单都必须有兜底事件**。
3. **dashboard 状态的语义本质是用户对屏幕的认知期望**：飞书走事件即推送管道不需要状态推断，所以这个 bug 在飞书侧不可见。dashboard 是状态可视化窗口，状态机必须覆盖所有可能的语义跃迁。

**修法只改一行（`scripts/install-hooks.py`），但要求用户重装 hook**：`python scripts/install-hooks.py`。重装幂等，已有 5 个事件不会被破坏。重装后用户回复 Claude 的瞬间 dashboard 应该立刻翻工作中。

**验证**：
- 重装后看 `~/.claude/settings.json` 的 `hooks` 段有 6 个 event（5 + UserPromptSubmit + PreToolUse heartbeat）。
- 在 Claude 回话时观察 dashboard：Notification 弹 → 终端回车 → dashboard 应在 ~1s 内从等你翻工作中。
- 后端 log: `tail -f data/events.jsonl` 应看到 `UserPromptSubmit` 事件被持久化。


---

## L44 — 状态机静态字符串 vs 活动证据：AskUserQuestion 答题后 dashboard 翻不回（已修）

**触发症状**（两个面向）：
1. Notification（AskUserQuestion / 权限请求 y/n）弹出 → 用户做完决策 → Claude 继续跑（一连串 Heartbeat）→ dashboard 永远停在 "等输入 / 🔥 请你 →"。终端早已在工作，但 web 不更新。
2. waiting/idle 卡超时被 watcher 报 `TimeoutSuspect` → status=suspect → 之后 Claude 又开始跑（Heartbeat 涌入）→ dashboard 永远停在 "疑似挂起 N 分钟前"。

**根因**：
- `derive_status` 只看 `last_event` 字符串：`Notification → waiting`、`TimeoutSuspect → suspect`、`Stop → idle`。
- `NON_STATUS_EVENTS = {Heartbeat, PreToolUse, SessionStart}` —— 这些事件不更新 `last_event`。
- 设计意图（L20 时期）是"心跳不应把 idle 翻 running"；但**前提是 idle 之后没有用户输入**。一旦用户回话后 Claude 真在跑，Heartbeat 是**最早能感知**的事件源（也是大多数情况下**唯一的事件源**，因为 AskUserQuestion 答题不走 UserPromptSubmit、权限请求 y/n 也不走）。
- R18 加 `UserPromptSubmit` hook 只能覆盖"用户在终端打字回复"那一类场景；**菜单选择 / y/n 授权 / AskUserQuestion 这些结构化问询，答案走 tool_result 通道，hook 层完全收不到任何事件**。

**修法**：把"活动证据胜过静态等待状态"原则内化到 `derive_status`：
1. `derive_status(arg)` 改签名接受 session dict 或 last_event 字符串（兼容旧测试）
2. session dict 维护 `_last_status_event_unix`：每次 `last_event` 被赋值时同步刷新
3. 当 `last_event ∈ {Notification, TimeoutSuspect}` 时多判一步：
   - `last_event_unix > _last_status_event_unix + 1s`（1s 容差防止同秒抖动误翻）
   - 即 status 事件之后还有 NON_STATUS 活动 → 翻 `running`
4. 旧行为完全保留：last_event 是 Stop/SessionEnd/SessionDead 等仍按 string 派生；旧字符串签名调用 fallback 到无活动证据的简化逻辑

**为什么不动 `NON_STATUS_EVENTS`**：把 Heartbeat / PreToolUse 加入 status 事件会破坏 L20 防御（Stop 后 PreToolUse 不应翻 idle→running，因为那时确实没有用户输入）。**修法的关键洞察**：是不是该翻 running，**取决于翻之前的 last_event 语义**，不是"事件类型本身能不能改状态"。所以应该在 `derive_status` 派生时根据 last_event 决定是否纳入活动证据，而不是 append 时一刀切。

**教训本质**：
1. **状态机不能只看"最后一个 status 事件"**：静态等待状态（waiting / suspect）必须看后续活动证据。否则形成"单向锁"——进得去出不来。
2. **"事件类型白名单"是个错的抽象**：`NON_STATUS_EVENTS` 试图把"哪些事件能改状态"做成全局规则，实际上**同一个事件类型在不同 last_event 下应该有不同语义**。Heartbeat 接在 Stop 之后 = 噪音（不该翻 running），接在 Notification 之后 = 真活动信号（必须翻 running）。
3. **hook 不可达的状态转换必须有内部兜底**：AskUserQuestion / 权限请求等 Claude Code 内部结构化交互**永远收不到 hook**（Anthropic 设计如此）。任何只靠"等下一个 hook 事件"的状态机都会在这类场景下死锁。修法必须是"从已有事件流推断"（活动证据），不能依赖加 hook。

**验证**：
- 单测 7 case 全过（dict / string 兼容 / Notification+HB / TimeoutSuspect+HB / 仅 Notification 不翻 / 1s 容差 / 旧字符串签名）
- E2E 临时 jsonl 重放 3 case（Notification+Heartbeat → running / Notification only → waiting / TimeoutSuspect+Heartbeat → running）

**用户层面**：
- 修完后 dashboard 状态翻转链路：
  - **AskUserQuestion 弹 → 答完后 Claude 跑 → ~1s 内翻"工作中"**（靠第一条 Heartbeat 触发）
  - **权限请求 y/n 同理**
  - **TimeoutSuspect 之后 Claude 继续跑也会自动翻 running**（不再死锁在"疑似挂起"）
- **必须重启 backend** 才生效：`pkill -f 'uvicorn backend.app' && python -m uvicorn backend.app:app --host 127.0.0.1 --port 8787 &`
- 状态翻得快 = ~30s（取决于 Heartbeat 频率，即下一个 PreToolUse 触发）。极端场景：Claude 一直在 LLM 推理不调工具，那就要等到调工具或 Stop。

---

## L45 — 飞书链接 tab 复用：BroadcastChannel 仲裁 + 遮罩兜底（已实现）

**触发**：飞书消息每条带 `↗ http://127.0.0.1:8787/#s=<sid>`，用户频繁点链接看详情 → 浏览器每次开新 tab → 几小时后 dashboard tab 爆炸 + WS 连接数膨胀 + Memory Saver 杀掉旧 tab。

**根因**：浏览器对"点链接是否复用 tab"的默认行为依赖飞书链接的 `target` 属性 —— 飞书消息卡片渲染由飞书自家 webview 控制，**无法注入自定义 target**。所以"已开 dashboard 复用 tab"必须在前端代码层自己仲裁，浏览器层 / 操作系统层都没钩子。

**实现**：BroadcastChannel + 250ms 仲裁窗口
1. 飞书链接保持 `#s=<sid>`（不动 backend / feishu.py）
2. 前端 main 启动时检查 URL 是否含 `#s=<sid>`：
   - 是 → 创建 `BroadcastChannel("claude-notify-tab")` + 广播 `{type:"PING_FOCUS", sid}` + 等 250ms
   - 否 → 跳过仲裁，正常加载（普通访问 dashboard 不应触发任何竞争）
3. 已存在的主 tab 监听 channel，收到 PING_FOCUS：
   - `window.focus()` → 浏览器尝试把后台 tab 切前台
   - 写 hash `#s=<sid>` → 触发已有 `hashchange` listener → 打开 drawer + 滚动高亮
   - 回 `{type:"PONG", sid}`
4. 新 tab 250ms 内收 PONG → 已被接管 → 显示遮罩"已切换到现有 dashboard 窗口" + 「关闭此 tab」/「仍在此 tab 加载」两个按钮
5. 没收 PONG → 自己当主 tab → 注册 channel 监听 + 正常加载（覆盖"第一次开 dashboard"和"主 tab 被 Memory Saver 杀掉后"两个场景）

**关键设计决策**：
- **只在有 hash 时仲裁**：普通用户打开 dashboard 不该触发竞争（否则多 tab 并存被强行收编反而错）。仅"带定向 sid"的访问视为飞书点击。
- **遮罩 stay 按钮兜底**：用户故意要"在此 tab 加载"（比如想分屏看两个 session）→ stay 按钮 → 调 `_resumeAsMainTab()` 重跑一遍 loadSessions / WS / notify bar 等初始化。设计成"非破坏性"：即使误判，1 次点击就能恢复。
- **window.close() 不抱期望**：浏览器只允许关闭脚本 `window.open()` 打开的窗口，用户点链接打开的不算。试一下，失败则把遮罩文案换成"请手动 ⌘W"。**这是浏览器层硬限制，跨 OS 一致，没解。**
- **250ms 选型**：BroadcastChannel 同源同浏览器通信是同步级别（< 5ms RTT），250ms 给足慢机器 / debug 工具拖慢的余量；同时短到用户感知不到等待。

**已知限制（全是浏览器层，OS 完全无关）**：
1. **跨浏览器不工作**：Chrome 开了 dashboard，Safari 点飞书链接 → 各自当主 tab。同源同浏览器同 profile 才共享 BroadcastChannel。
2. **`window.focus()` 不保证切到前台**：Chrome / Firefox 对脚本调用的 `focus()` 在某些时机会忽略（通常是非用户事件链）。但消息流"用户点链接 → 浏览器开 tab → 新 tab 脚本广播 → 主 tab 接收 + focus"在 Chrome 实测稳定。若不切，至少 hash 已改、drawer 已开，用户切回去就能看到。
3. **Chrome Memory Saver 把后台 tab 休眠 → BroadcastChannel 也休眠**：新 tab 收不到 PONG → 自己当主 tab。结果是两个活跃 dashboard tab 并存，但每个都独立工作。这是 Memory Saver 预期，无法绕过。
4. **多 dashboard tab 同时开（用户故意分屏）**：哪个先回 PONG 哪个赢，其它保持现状。可接受。

**教训本质**：
1. **"已存在则复用"是个看似简单实则需要 application 层协调的特性**。`<a target="_self">` 不够（target 名固定才能复用，飞书链接无法注入）；浏览器历史栈复用也不行（不同源 / 不同时间）。BroadcastChannel 是同源 tab 间协调的标准答案，但它只是**通信通道**，仲裁协议（谁先谁主、超时回退、stay 兜底）必须自己设计。
2. **设计带超时仲裁时，永远要有"超时后降级"路径**：250ms 没收 PONG 不等于"一定没主 tab"（可能主 tab 暂时无响应 / Memory Saver 短暂唤醒延迟），但**当作没有处理**比"无限等"更稳。新 tab 自己当主 tab 是 graceful degradation，最坏结果就是双 tab 并存 —— 用户体验比"卡在加载页等永久 PONG"好太多。
3. **不要假设浏览器允许关 tab**：`window.close()` 限制极严。所有"自动关闭"逻辑都得有"用户手动关"的退路 UI。

**回归保护**：
- puppeteer e2e 11/11（A 主 tab 接管 / B 无主 tab 单独 / C stay 按钮恢复 / D 无 hash 不触发；含 4 项核心断言 + 状态字段检查）
- 浏览器兼容：Chrome 64+ / Firefox 38+ / Safari 15.4+ / Edge 79+（BroadcastChannel 普遍支持）

**用户层面**：
- 点飞书消息 ↗ 链接：若已开 dashboard tab → 该 tab 自动 focus + 跳到对应 session；新 tab 显示遮罩可手动关
- 普通输入 URL 打开 dashboard：完全不触发仲裁，行为不变
- 想强制在新 tab 加载（比如分屏看不同 session）：点遮罩里"仍在此 tab 加载"按钮

---

## L46 — Dashboard 显示旧 summary：tab 后台节流冻结 WS + 定时器（已修）

**触发**：用户切回 dashboard tab，看到 session 卡片显示"卡 21 分钟 · 上次 Bash: grep ..." —— 那是 21 分钟前的工具调用，新任务都执行完了。后端 `/api/sessions` 返回的 status 已经是 running、last_action 是最新命令，但前端 state 卡在旧值。

**根因**：
- dashboard summary 数据流是**事件驱动**：后端收到 hook event → `_dispatch` → `hub.broadcast({type:"event", session: summary})` → 前端 `upsertSessionFromEvent` 替换 `state.sessions[idx]` → render
- 兜底：每 60s 调一次 `loadSessions()` 重拉 `/api/sessions`，覆盖错过的更新
- **当 tab 切到后台**：
  - Chrome / Firefox 后台 tab 节流：`setInterval` 最低 1s 间隔（轻度），Memory Saver 重度休眠时**完全暂停 JS 定时器 + WS 接收**
  - 60s 兜底定时器没跑、WS 缓冲的 broadcast 没派给 handler
  - tab 切回前台，定时器恢复，但**下一次 60s 兜底要等到下一周期才触发**，期间用户看的是 stale 数据
- 与 L42 / R17 的 push_event ring buffer 不重叠：R17 只补 `push_event`（推送通知用），不补 `type:event`（dashboard summary 用）。补全 `type:event` 也不现实（高频，且通常用户已能从 `/api/sessions` 拿到正确状态）

**修法**：监听 `visibilitychange`，tab 切回 `visible` 时立刻 `loadSessions()`：
```javascript
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !_tabTakenOver) {
    loadSessions().catch(err => console.warn("[app] visibility loadSessions failed", err));
  }
});
```
`_tabTakenOver` 是 R20 / L45 的遮罩状态字段 —— 遮罩态不应再发 API 请求（已被其它 tab 接管）。

**教训本质**：
1. **"事件驱动 + 定时兜底"在浏览器后台不可靠**：JS 定时器和 WS 都受后台节流影响。任何依赖周期性 polling 的设计都要补一个"用户回来"的钩子：`visibilitychange` / `focus` / `pageshow` 视场景挑。
2. **WS resume 不等于 state 同步**：R17 加的 since_ts 补发只覆盖 push_event 一种 envelope；session summary 类的 envelope 没补就靠下一次 event 触发或 visibilitychange 拉取。
3. **后端正确不等于前端正确**：debug 时 `curl /api/sessions` 看到的是后端实时计算的；用户 dashboard 看到的是前端 cached state。两端不一致时多半是同步缝隙问题，不是后端逻辑 bug。

**回归保护**：puppeteer e2e — visibility 'visible' dispatch 触发 1 次 `/api/sessions` 拉取 ✓

---

## L47 — Tab 复用模式：从"强制让用户选"改"自动 focus + Settings 兜底"（R22，L45 迭代）

**R20 初版反馈**：用户嫌"两按钮遮罩"增加操作复杂度且无实际价值。要求改成"识别并跳转到旧 tab"，备选"识别并关闭旧 tab + 启动新 tab"。

**浏览器层硬限制（跨 OS 一致）**：
1. **新 tab 一定会出现**：用户点链接是用户行为，浏览器必开新 tab，脚本不可阻止
2. **脚本不能关用户输入 URL / 点链接打开的 tab**：`window.close()` 只能关脚本自己 `window.open()` 出来的窗口

这两条意味着用户的两个理想方案**都不能做到 100% "无新 tab" 或 "自动关旧 tab"**。最接近的折中：

- **跳到旧 tab（focus_old）**：旧 tab `window.focus()` 切前台（Chrome / Edge 多数情况下生效，user activation 通过 BroadcastChannel 短链传递）+ 接管 hash/drawer；新 tab 显示 2.5s 自动消失的提示，淡出后变白页（最接近"用户无感"）
- **关旧 tab + 新 tab 接手**：旧 tab 不能被脚本 close，只能"自废"显示提示让用户手动关 —— 用户实际体验比"focus_old"更糟（旧 tab 还在，且写明已废，用户被迫做关 tab 操作），**所以这个模式直接砍掉**

**R22 实现**：两档模式（focus_old / user_choice），Settings 切换，默认 focus_old：
- `backend/config.py`：`DEFAULT_TAB_REUSE_MODE = "focus_old"` + 白名单 `TAB_REUSE_MODE_VALUES = {"focus_old", "user_choice"}`
- `backend/app.py:update_config`：PUT `/api/config` 校验 `tab_reuse_mode` 白名单，非法值 400
- `frontend/app.js`：仲裁后按 `loadTabReuseMode()` 决定遮罩；loadConfig 完成后回写 localStorage cache（首启没 cache 默认 focus_old）
- `frontend/index.html`：Settings 弹窗加 `<fieldset id="tab-reuse-fieldset">` 两个 radio
- `frontend/styles.css`：`.tab-auto` 全屏白页 + `.tab-auto-toast` 自动消失（fade-out 600ms）

**关键设计决策**：
1. **mode 缓存在 localStorage**：仲裁要在 main 函数最开头跑（loadConfig 之前），不能等后端 config。本地缓存兜底 + loadConfig 完成时同步真值。首启无 cache 走默认 focus_old。
2. **focus_old 模式仍试 `window.close()`**：浏览器拒绝时 console.warn，但白页 + toast 已能让用户理解"此 tab 可关"，不会困惑
3. **2.5s 显示 + 600ms 淡出**：足够用户瞄一眼"已跳到旧窗口"，不会久到烦人
4. **不删 user_choice 模式**：用户偏好不同（开发场景可能就是要分屏看），保留作为 Settings 选项

**教训本质**：
1. **当浏览器/平台限制让"理想方案"做不到时，需要清楚分辨"做不到的本质"和"次优替代"**。R20 直接上"两按钮"看似稳妥，但本质上是把开发者的折中思考成本转嫁给用户。R22 改"自动 focus + 自动消失提示"才真正贴近用户原意（哪怕没法 100% "无新 tab"）
2. **"用户选择"的 UI 模式应是兜底而非默认**：除非选项各有不可调和的 trade-off 且用户偏好真实分布是双峰的，否则强制选择 = UX 负担。R20 默认 user_choice 是错的，R22 默认 focus_old 才对
3. **物理约束坦白比绕路"假装做到"重要**：跟用户讲清"新 tab 一定会出现 + 旧 tab 不能被关，能做的是最大化 focus 效果"，比硬上一个看似能"关 tab"实则用户被迫手动关的方案更诚实

**回归保护**：puppeteer e2e 13/13
- A（focus_old）：新 tab `.tab-auto` 显示、`.tab-takeover` 隐藏；主 tab hash + drawer 接管；toast 2.5s 后 fade
- B（user_choice）：新 tab `.tab-takeover` 显示、`.tab-auto` 隐藏；stay 按钮恢复加载
- C（backend API）：focus_old / user_choice 互切 OK，bogus 值返 400

---

## L48 — Hash 跳转打开 drawer 时强制刷新 sessions（已修）

**触发**：用户点飞书 ↗ 链接或浏览器桌面通知，dashboard 打开/接管时 drawer 头部立刻显示 status badge / alias / cwd —— 这些**全部来自前端 `state.sessions.find(sid)`**。如果主 tab 后台休眠 / 节流，state.sessions 可能是几十秒到几分钟前的快照，drawer 显示就是旧值。

**根因**：
- `onHashChange` / `_onTabMessage`（主 tab 收 PING_FOCUS）原本只调 `openDrawer(sid)` + `applyHashHighlight()`，**不主动刷新 sessions**
- `openDrawer` 内 `state.sessions.find(x => x.session_id === sid)` 用现存 state，无 fallback 拉取
- L46 的 `visibilitychange` 监听虽然能补刷一次，但仅在 tab 真切前台时 fire；`window.focus()` 跨浏览器不保证生效；即便 fire 也是异步，drawer 已经先用旧 state 渲染完头部

**修法**：把 loadSessions 内化到 hash 跳转入口，确保任何"通过通知/链接打开 drawer"的路径都先刷新：
```javascript
// onHashChange 改 async
async function onHashChange() {
  const m = (window.location.hash || "").match(/s=([^&]+)/);
  if (!m) return;
  const sid = decodeURIComponent(m[1]);
  try { await loadSessions(); } catch (_) {}
  openDrawer(sid);
  applyHashHighlight();
}
// _onTabMessage 同 hash 路径（hashchange 不会再 fire）也补一次
if (window.location.hash === `#s=...`) {
  await loadSessions();
  openDrawer(sid);
}
```

**为什么不只靠 visibilitychange（L46）**：
- `visibilitychange → visible` 是 tab 真切前台才 fire；`window.focus()` 跨浏览器不可靠
- 即便 fire 也是异步：onHashChange 同步路径已经用旧 state 渲染完 drawer 头部
- L48 是"用户主动跳转入口"的强约束（"我点了通知，我要看最新"），不依赖浏览器调度

**教训本质**：
1. **"用户主动行为"路径必须用最新数据**：60s 兜底 polling 适合后台监控，但用户点通知/链接的瞬间是"我要立刻知道最新状态"，绝不能用 stale state 应付。
2. **每个"打开 drawer / 显示详情"的入口都要保证数据新鲜**：未来如果加新入口（比如键盘快捷键），统一在 openDrawer 之前 await loadSessions —— 或者更彻底，把 loadSessions 收编进 openDrawer 内（但这会让普通点卡片也多一次拉取，权衡）。
3. **多层兜底设计要分清"主动入口"和"被动 fallback"**：visibilitychange / 60s polling 是被动 fallback，hash 跳转是主动入口。主动入口的兜底必须比被动更激进。

**回归保护**：puppeteer e2e 3/3
- A: hashchange → /api/sessions 触发 ✓
- B: 仲裁主 tab 接管（hash 变化）→ /api/sessions 触发 ✓
- C: 同 hash 二次 PING_FOCUS（hash 不变路径）→ /api/sessions 触发 ✓
