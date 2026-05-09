<!-- purpose: claude-notify 信息架构 v3 spec —— 卡片 / 抽屉 / 飞书三面统一 -->
创建时间: 2026-05-09 17:22:29
更新时间: 2026-05-09 17:22:29

# claude-notify 信息架构 v3 spec

## 0. 问题诊断（一句话）

当前系统抽事件层（PreToolUse 工具名 / Stop 类型）当主信息，把语义层（LLM 摘要 / 最近 user prompt / 真完成 vs 应答完成）当装饰位，造成卡片标题机械、副标题只看到工具调用、抽屉行重复、飞书把无意义对答也推出去。

v3 把信息架构倒过来：**语义层是主信息，工具层是辅助 / hover 透出**，并且推送过滤从「有事件就推」改成「有用户介入需求才推」。

---

## 1. 信息层级模型（核心原子）

| 原子 | 含义 | 来源（优先级从上到下） | 缓存层 | 刷新时机 |
|---|---|---|---|---|
| `task_topic` | 这个 session 在解决什么（大标题） | LLM `summarize_session_topic`（最近 5 条 user prompt + last_assistant 200 字）→ 启发式 `_derive_task_topic`（跨句频率 ≥2 且含英文/路径 token） | `enrichments.topic_key(transcript_path, mtime_ns)` | transcript mtime 变更后异步 enqueue；list_sessions 只 cache hit 才用 |
| `recent_user_prompt` | 用户最近一句话（标题第三档兜底） | `transcript_reader.recent_user_prompts(path, n=1)[0]`，截 28 字 | 复用 `transcript_reader._cached_recent_prompts` lru | transcript mtime 触发失效 |
| `current_activity` | 当前在跑什么工具 / 哪一步 | 最近 `Heartbeat` 携带的 PreToolUse `tool_name + tool_input`，经 `_derive_last_action` 净化 | 内存（聚合时） | 每次 list_sessions 重算 |
| `last_milestone` | 上一个**有实质内容**的进展 | 最近 Stop / SubagentStop 事件的 `llm_summary`（来自 `summarize_event`）→ 否则 `_derive_turn_summary(last_assistant)` | `enrichments.event_key(sid, ts, event_type)` | 事件落库后异步 enqueue；list_sessions 时按 `_last_stop_ts` 反查 cache |
| `waiting_for` | 等什么 / 卡哪 | 最近 `Notification` message，截 60 字 | 内存 | list_sessions 重算 |
| `next_action` | 下一步要做啥（提示用户/自己） | `_derive_next_action(status, waiting_for, last_action)` 状态触发 | 内存 | list_sessions 重算 |
| `progress_state` | 粗状态标签 | `_derive_progress_state(status, age_seconds)` | 内存 | list_sessions 重算 |

### 1.1 关键决策

- **大标题**降级链改为 `alias > task_topic(LLM) > task_topic(heuristic) > recent_user_prompt[:28] > cwd_short > sid`（去掉 `first_user_prompt`，session 跑久后焦点早转移）。
- **副标题主信息**优先 `last_milestone`（语义层），不再优先 `last_action`（工具层）。
- **抽屉事件主文本**优先 `llm_summary`（已有此字段，event_store.py:84 就在叠加），原 `message` 降为 hover/title，不再单独占第二行。
- list_sessions 必须新增 `last_milestone` 字段（区别于 `turn_summary`），后端按 `_last_stop_ts` 反查 enrichments cache 取 LLM 摘要，cache miss 才回退到 `_derive_turn_summary`。

---

## 2. 卡片信息架构（dashboard）

### 2.1 三层结构

```
[●] [大标题]               [✎]   [状态徽]                  [→ 终端]
    [副标题：1 行 line-clamp，状态触发模板]
    [meta: cwd · mode · effort]                    [time · 心跳]
```

副标题**保持 1 行**（line-clamp:1），不展开第二行。多余信息走 hover title + 抽屉。

### 2.2 副标题模板（按 status × progress_state）

| status | 模板 | 关键字段 |
|---|---|---|
| `waiting`（等你） | `请你 → ${next_action || waiting_for || '确认'}` | next_action |
| `running`（工作中） | `${last_milestone ? '上次 ' + last_milestone : '正在 ' + last_action} · ${last_action ? '当前 ' + last_action : '等结果'}` | last_milestone（主） + last_action（辅） |
| `idle / 刚完成` | `刚完成 · ${last_milestone || turn_summary || '...'} · 下一步 ${next_action}` | last_milestone |
| `idle / 已歇` | `上次 · ${last_milestone || '...'} · 等你 resume` | last_milestone |
| `suspect`（可能卡住） | `卡 ${ageMin} 分钟 · 上次 ${last_action || '...'}` | last_action |
| `ended / dead` | `已结束 · ${last_milestone || '可归档'}` | last_milestone |
| 空 session | `刚启动 · 等你输入第一条 prompt` | — |

**改动相对现状**：`running` 与 `idle/刚完成` 把主语从工具层（last_action）换成里程碑（last_milestone）；`waiting` 取消单独 `waitingFor` 文本，直接吃 `next_action`。

### 2.3 抽屉事件流（单行）

```
[chip: 事件类型] [相对时间]   [主文本]
```

主文本优先级：`llm_summary > 启发式 message > 事件类型默认文案`。

**禁止规则**：
- 不再渲染「任务一回合结束」这种事件类型 default message（`feishu.py:EVENT_HEAD` 这类常量只用于飞书标题，不进抽屉行）。
- 主文本与 chip 文本重复时（如 `Stop` chip + 主文本「Stop」）只保留 chip。
- LLM summary 与 message 实质相同时（trim+lower 等价或一方是另一方前缀）只渲染一条。
- Heartbeat / SessionStart 默认隐藏（已在 `drawerHiddenTypes` 默认值里）。

---

## 3. 飞书消息架构

### 3.1 原则

1. 飞书**只发该用户介入的事件**（见 §4 should_notify）。
2. 发出时用**和卡片副标题同一套状态触发模板**，但允许 2-3 行（飞书没有 line-clamp）。
3. 字段术语与 dashboard 一致（见 §5 一致性表）。

### 3.2 模板（按事件 × 状态）

| 触发 | 行 1（标题） | 行 2（任务焦点） | 行 3（核心信息） | 行 4（下一步） |
|---|---|---|---|---|
| `Notification` | 🔔 [Claude] 等你确认 + 色点 | 任务焦点: ${task_topic 或大标题降级链} | 等: ${waiting_for} | 请你 → ${next_action} |
| `TimeoutSuspect` | ⚠️ [Claude] 长任务疑似 hang | 任务焦点: ${task_topic} | 卡 ${ageMin} 分钟 · 上次 ${last_action} | 建议 → 进终端检查 |
| `Stop`（满足 should_notify） | ✅ [Claude] 任务完成 | 任务焦点: ${task_topic} | 完成: ${last_milestone}（必有，否则别推） | 下一步 → ${next_action} |
| 合并消息 | 用最新事件的标题 | 同上 | 同上 | 另含 N 个先发事件: ... |

固定尾两行：`session: ${sid[:8]}  路径: ${cwd}` / `Dashboard: ...`。

### 3.3 砍掉的废话

- 「名称: 待命状态」→ 改 `任务焦点: ${task_topic}`，无 topic 时不发这行（不是发占位符）。
- 「刚完成 · 任务一回合结束」→ should_notify 已挡掉，根本不会发。
- 「下一步 → 可归档 / 或 resume」→ 仅在 `ended / dead` 状态发，普通 Stop 用 `next_action` 计算结果（`提下一轮 / 或归档`）。
- `任务: ${first_user_prompt}` 这一行（feishu.py:81）删除，不再用 first_prompt。

---

## 4. 推送过滤规则（新增 should_notify）

### 4.1 函数签名

`should_notify(evt: dict, summary: dict | None) -> tuple[bool, str]` — 返回 `(是否推, 拒推原因)`。**纯计算，不调 LLM，不读 transcript**（cache 已有的 summary 字段可用）。

### 4.2 判别原则（按事件类型）

| 事件 | 规则 |
|---|---|
| `Notification` | 永远 True（必有用户介入） |
| `TimeoutSuspect` | 永远 True |
| `SessionStart` / `Heartbeat` / `PreToolUse` / `PostToolUse` | 永远 False |
| `SessionEnd` / `SessionDead` | 永远 False（已在用户预期内，不构成介入） |
| `SubagentStop` | 永远 False（不是主流程结束） |
| `Stop` | 见 §4.3 复合判别 |
| `TestNotify` | 永远 True |

### 4.3 Stop 复合判别（核心）

记 `M = last_milestone`（cache hit 才有）、`A = last_assistant_message`、`Δ = now - last_notification_unix`、`turn_len = len(turn_summary)`。

满足**任一**条件 → True：

1. `M` 非空且 `len(M) ≥ 12`（LLM 已提取出有内容的里程碑）。
2. `turn_len ≥ N`（默认 N=15）且 `A` 不命中黑词表（见 §4.4）。
3. `Δ > M_minutes`（默认 M=5 分钟，说明是真任务结束而不是闲聊回答；用于 cache miss 时兜底）。

**否则 False**（拒推原因：`stop_low_signal`）。

### 4.4 黑词表（默认）

短纯应答：`OK / Yes / No / 好 / 好的 / 已完成 / 完成 / 收到 / 嗯 / 是 / 不 / 对 / 行 / 可以`。判定：`A.strip().lower()` 完全等于黑词，或长度 ≤ 4 字且不含标点/空格。

### 4.5 配置点

`config.notify_policy` 不变。新增 `config.notify_filter`：

```
notify_filter:
  stop_min_milestone_chars: 12        # §4.3 条件 1 阈值
  stop_min_summary_chars: 15          # §4.3 条件 2 阈值（候选 N）
  stop_min_gap_after_notification_min: 5  # §4.3 条件 3 阈值（候选 M）
  blacklist_words: [...]              # §4.4 用户可补
```

dashboard 提供 4 个输入点（小弹层即可，不需要独立页）。

### 4.6 接入点

`notify_policy.py:NotifyDispatcher.submit()` 在 `policy != "off"` 之后、`feishu.send_event` 之前调 `should_notify`。silence 路径在 `_wait_and_send` 真正发送前再调一次（这段时间 enrichments 可能新写入了 LLM milestone，二次判别能升真为推 / 降假为不推）。

---

## 5. 飞书 vs dashboard 一致性核查

| 字段语义 | dashboard 字段名 | 飞书 _format_text 文案 | 一致？ | 统一建议 |
|---|---|---|---|---|
| 大标题 / session 名 | `display_name` | `名称: ${name_line}` | ❌ | 飞书改 `任务焦点: ${task_topic 或 display_name}`；dashboard / API 字段名保持 `display_name` 不动（内部字段），但 UI 文案显示用「任务焦点」 |
| 工作目录短名 | `cwd_short` | 拼在 `名称:` 后或 `路径:` 行 | ⚠️ | 统一只在底部 `路径:` 行用全 cwd；name_line 不再附 `cwd_short` |
| 当前活动 | `last_action` | `正在 ${last_action} · 等结果` | ✅ | 文案保留 `正在 / 当前` 一致词 |
| 等什么 | `waiting_for` | `请你 → ${waiting_for}` | ✅ | 飞书改 `等: ${waiting_for}` 单列一行（更清晰） |
| 完成摘要（语义层） | `last_milestone`（新增）/ 当前 `turn_summary` | `刚完成 · ${turn_summary}` | ❌ | 两边统一字段名 `last_milestone`（API 返回新增），文案统一用「完成:」前缀；`turn_summary` 降级为内部 fallback |
| 下一步建议 | `next_action` | `下一步 → ${next_action}` / `请你 → ` / `建议 → ` | ⚠️ | 三个前缀按状态固定：waiting → `请你`、suspect → `建议`、idle → `下一步`；dashboard 与飞书共用 |
| 状态标签 | `status` + `progress_state`（"等你/工作中/刚完成/已歇/可能卡住"） | 标题 emoji 隐含 | ⚠️ | 飞书在标题色点旁加状态文字（如 `🔔 等你确认 · 等你 ●`），与卡片的 status badge 文案一致 |
| 第一条 prompt | `first_user_prompt` | `任务: ${first_user_prompt}` | ❌ | dashboard 已不用，飞书也删掉这一行 |
| 模式 | `permission_mode` / `effort_level` | `模式: ${permission_mode · effort_level}` | ✅ | 卡片 meta-left 已用同样的 `·` 分隔，保留 |
| 心跳时间 | `last_heartbeat_ts` | 不展示 | ⚠️ | 飞书可选展示 `心跳: ${rel_time}`（对长任务有价值），放可配置 |

**字段重命名（API 层）**：`turn_summary` 保留作为启发式 fallback；新增 `last_milestone`（优先 LLM，fallback 到 turn_summary），后端 `list_sessions` 派生时填充。前端读 `last_milestone`，不再直接读 `turn_summary`。

---

## 6. 实施切片（建议拆 4 个 PR）

| # | 切片 | 范围 | 行数估 | 验证方式 |
|---|---|---|---|---|
| **A** | **推送过滤（破冰）** | 后端：新增 `notify_filter.py`（should_notify + 黑词表）；`notify_policy.py:submit` 与 `_wait_and_send` 接入；`config.py` 加默认值 | 后端 ~120 行 / config ~15 行 | 跑 `pytest` + 手工触发「OK」Stop 看是否被拦；现有 Notification / TimeoutSuspect 路径不能回归 |
| **B** | **last_milestone 字段贯通** | 后端：`event_store.py:list_sessions` 派生 `last_milestone`（cache hit 优先）+ API 字段；前端：`sessionCardHTML` 把 `turnSummary` 替换为 `lastMilestone`；抽屉 `eventRowHTML` 已经在用 `llm_summary`，去掉 `subText` 行只留主行 | 后端 ~30 行 / 前端 ~25 行 | 看 dashboard：跑久 session 副标题是否变成「刚完成 · 主任务完成: 正则引擎模块及测试已同步远端」 |
| **C** | **大标题降级链 + 飞书模板对齐** | 后端：`_display_name` 加 `recent_user_prompt[:28]` 档；`feishu.py:_format_text` 按 §3.2 重写；删除 `任务:` 行 / 删除 `名称:` 拼 cwd_short | 后端 ~70 行 | 卡片大标题是「最近一句话」而非首句；飞书 4 行结构 |
| **D** | **配置 UI** | dashboard 新增 notify_filter 4 个输入；`config` API 透出 / 持久化 | 前端 ~50 行 / 后端 ~10 行 | 改阈值后立刻生效 |

### 6.1 破冰建议

**先做 A（推送过滤）**：

- 用户痛点 4（OK 还推飞书）是当前最骚扰的体验问题，A 直接解决；
- A 完全在后端，不动前端，不影响 dashboard 当前呈现，回滚成本低；
- A 完成后，B/C 的副标题 / 标题改造即使没上，飞书已经"安静"了，给 B/C 留出从容打磨时间；
- A 的判别函数纯计算，依赖 enrichments cache 已有结果，不引入新依赖。

### 6.2 不做 / 显式拒绝

- 不在 should_notify 里调 LLM（会让 silence:12 倒计时变成数十秒）。
- 不引入"用户撤销推送"的反向链路（成本高，先用阈值过滤足矣）。
- 不改 `notify_policy` 的 silence 机制本身（合并 / 取消逻辑不需要动）。
- 不在卡片副标题改 2 行（信息密度该靠词序优化，不是堆行数）。
