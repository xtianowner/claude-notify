<!-- purpose: 设计教训记录 — 删除/否定一个设计前先写一段简要总结，避免后续重复犯错 -->

创建时间: 2026-05-09 16:25:00
更新时间: 2026-05-09 20:38:20

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
