<!-- purpose: claude-notify 用户级使用文档：状态语义 + 推送规则 + 操作 + 配置 -->

# claude-notify 用户使用指南

创建时间: 2026-05-09 22:53:31
更新时间: 2026-05-10 10:55:00

claude-notify 监控你本机的 Claude Code 所有 session，把"任务完成 / 等输入 / 疑似挂起"等关键时刻同步到飞书和 dashboard。本文写给使用者，不讲实现细节。

---

## 〇、首次启动 30 秒上手

启动 backend、打开 dashboard 后，如果 dashboard **还没有任何 session**，你会看到一张"首次接入检查"卡片。它会列三件事：

1. **Hook 已注册到 Claude Code** —— 没装就在终端跑：`python3 scripts/install-hooks.py`
2. **飞书 webhook 已配置** —— 没配就点卡片上的"去配置"按钮，粘贴你的飞书机器人 webhook URL
3. **等待第一个 hook 事件** —— 切到任意 Claude Code 终端发一句话，dashboard 会自动出现该 session

三步都做完后，卡片自动消失，session 列表开始填充。

> 小贴士：如果你刚装完 hook，但卡片还显示 ✗，点卡片上的"重新检查"按钮即可。

---

## 一、6 种 session 状态

dashboard 每张卡片右上角都有一个状态标签。它告诉你：**这个 session 现在到底在干啥，要不要关心。**

| 状态 | 标签 | 颜色 | 你看到时意味着 | 你该做什么 |
|---|---|---|---|---|
| running | 运行中 | 🟢 | Claude 正在用工具 / 调子 agent / 处理你刚发的内容 | 等着，可以去做别的 |
| idle | 回合结束 | ⚪ | Claude 回完了一个回合，等你下一步 | **该看终端了**（或飞书会推送提醒，见第二节） |
| waiting | 等输入 | 🟡 | Claude 真的在阻塞等你（权限请求 / 多次提醒后） | **必须立即看**（很可能在等你按 y 授权） |
| suspect | 疑挂起 | 🟠 | 长时间没有任何活动，可能挂了 | 切到该终端确认；不一定真挂，也可能 tool 跑很慢 |
| ended | 已退出 | ⚫ | 你正常关掉了那个 Claude Code 窗口 | 不用管 |
| dead | 已死亡 | 🔴 | 进程没了 / transcript 30 分钟不动 | 看下是不是 crash 了 |

**重点理解**：`idle`（回合结束）和 `waiting`（等输入）的区别——
- **idle**：Claude 回完一句话，技术上没在等什么；你也许走开了，也许在思考下一句。
- **waiting**：Claude 真的卡在那里需要你确认（最常见是工具权限：要执行 Bash / 写文件 / 调 MCP 等）。

---

## 二、飞书推送规则（一个任务最多 3 次）

### 消息模板（多 session 一眼识别归属）

每条飞书推送 L1 行（也是消息列表预览首行）格式：

```
[<项目名>] <emoji> <focus> · <状态>
```

例：

```
[claude-notify] ✅ hello, 回复收到 · 任务完成
[ragSystem]     🔔 调 ETL 管道 · 等你确认
[etl-pipeline]  ⚠️ 卡 8 分钟 · 疑似 hang
```

`<项目名>` 取首个非空：
1. **alias**（你在 dashboard 起的别名，最高优先）
2. **cwd 末段**（如 `claude-notify`）
3. **cwd_short** fallback（如 `~/foo/bar`）
4. 都没有 → 不显示前缀

项目名长度 ≤ 18 字符，超长截断为 `…`。多 session 同时推送时，飞书消息列表预览靠前置项目名一眼分辨归属。

### 推送次数上限

**核心约束**：每个任务（你发一句话 → Claude 回完）**最多推送 3 次**，不会无限打扰。

```
你发任务 → Claude 回完
   ┃
   ┣━ 第 1 次推送（立即）：[proj] ✅ task · 任务完成
   ┃
   ┃   ⏱ 5 分钟你没回应...
   ┣━ 第 2 次推送：[proj] 🔔 task · 还在等你（5 分钟）   ← reminder #1
   ┃
   ┃   ⏱ 又 5 分钟过去（共 10 分钟）...
   ┣━ 第 3 次推送：[proj] 🚨 task · 已等 10 分钟          ← reminder #2，emoji 升级 🚨
   ┃
   ┃   ⏱ 之后永远不再推（即使你离开 1 小时）
   ┃
   ┗━ 你又发新消息 → 新任务 → 计数重置 → 又允许 3 次
```

**reminder 模板差异化（multi-session 速读紧迫度）**：

| 场景 | L1 模板 | 视觉信号 |
|---|---|---|
| 真权限请求（"needs approval" 带后缀） | `[proj] 🔔 task · 等你确认` | 🔔 中性 |
| 第 1 次 reminder（5min 后） | `[proj] 🔔 task · 还在等你（5 分钟）` | 🔔 + 时长提示 |
| 第 2 次及之后 reminder | `[proj] 🚨 task · 已等 10 分钟` | 🚨 紧急 + 累计时长 |

emoji 从 🔔 升级到 🚨 是为了让"第 2 次还没回应"在飞书消息列表预览里更扎眼——一眼分清"刚到的提醒"和"已经晾了一会儿的提醒"。括号 / "已等"里的具体分钟数是从 idle prompt 距 Stop 推送的真实间隔派生的（四舍五入到整分钟），不是配置阈值。

**例外**：Claude **真在等权限**（飞书消息含 "needs approval" 这种带后缀的）属于权限请求，不在 3 次限制内，每次都会推。这种你必须看到。

### 多 session 并发

每个 session **独立**走 3 次配额。同时跑 3 个 Claude Code 终端，互不影响——A 任务用满 3 次不会影响 B 任务的提醒。

### dashboard 卡片状态变化次数 ≠ 飞书推送次数

- 飞书：1 任务最多 3 次推送
- dashboard：1 任务**通常只 1 次状态变化**（running → idle）。后续 idle prompt 副本被吞，卡片保持稳定不抖动。

---

## 三、dashboard 操作

### 3.0 视图模式：列表 / 按项目分组

topbar 右侧有个分段控件（pill toggle）：

```
┌──────┬──────────┐
│ 列表 │ 按项目   │
└──────┴──────────┘
```

- **列表**（默认）：所有 session 平铺一列，按状态紧急度排序。适合 1-3 个项目同时跑。
- **按项目**：按 cwd_short 分桶，每组一个可折叠 header，显示总数 + 状态分布徽。适合 5+ 项目同时跑。

每组 header 长这样：

```
▼ 00-Tian-Project/claude-notify   5 · 🟡1 🟢2 ⚪2
```

- 折叠箭头（▶ 折叠 / ▼ 展开），点击切换
- cwd_short（中间过长会用 `…` 截断）
- 总 session 数 `5`
- 状态汇总：🟡 waiting / 🟠 suspect / 🟢 running / ⚪ idle / ⚫ ended / 🔴 dead，仅显示有的

**记忆**：选哪种视图、哪些组被折叠 —— 都自动存浏览器（localStorage），刷新 / 重启都不丢。

**组内排序**：和列表视图一致 —— waiting/suspect/idle 等最久的往前（方便定位"等了 10 分钟没人理"）；running/ended/dead 按最近活动往前。**组与组之间**按"组内最紧急 status"排，让最危险的项目浮到最上。

### 3.0a 紧急度徽章（"⏱ 5min" / "⏱ 12min"）

idle / waiting / suspect 三种 status 的卡片，右上角（🔔 静音按钮左侧）会出现一个紧急度徽。三档颜色随等待时长加深：

| 等待时长（age） | idle | waiting / suspect |
|---|---|---|
| < 2min | 不显示 | 不显示 |
| 2-5min | ⏱ 浅黄 hint | ⏱ 橙色 warn |
| 5-15min | ⏱ 橙色 warn | ⏱ 红色 critical |
| ≥ 15min | ⏱ 红色 critical | ⏱ 红色 critical |

waiting / suspect 比 idle 更紧迫——所以同样时长档上"再升一档"。多 session 场景下，扫一眼颜色就能定位"哪张卡片等最久"，不用逐个读 `X 分钟前`。

dashboard 每 30s 自动刷新会重算徽档，无需手动操作。卡片处于 running / ended / dead 时不显示（"运行中"和"已结束"看时长无意义）。

### 3.1 静音单个 session

正在专注调试 / 开会，不希望某个 session 打扰你：

1. dashboard 卡片右上角点 🔔 图标
2. 弹小菜单选时长：5min / 30min / 60min / 永久 / 仅 Stop·30min
3. 静音中的卡片整体淡灰，🔕 角标显示剩余分钟数
4. 点 🔕 → 解除

**两种范围**：
- **all**：所有事件都不推（推送总开关）
- **stop_only**：只吞 Stop / SubagentStop。真权限请求 / TimeoutSuspect 仍会穿透 → 不会错过关键事件

### 3.2 全局夜间不打扰（quiet_hours）

晚上不想被推醒：

1. dashboard 右上角设置（齿轮）→ 「免打扰时段」
2. 配置：
   - 开关 / 开始时间（如 23:00）/ 结束时间（如 08:00）
   - 时区（默认 Asia/Shanghai，跨午夜支持）
   - 仅工作日 ✓ → 周末不静音
   - 穿透事件 → 默认 `Notification` 和 `TimeoutSuspect` 仍会推（关键事件不该被睡眠掩盖）

实际效果：在 quiet_hours 时段内，Stop / SubagentStop 静默，但权限请求 / 疑似挂起仍叫醒你。

### 3.3 查 push 决策 trace（debug 漏推 / 错推）

每张卡片底部有一行虚线灰字：

```
最近：22:50 ✓ stop_milestone_len=29 · 22:50 ⌛ scheduled_silence_12s · 22:49 ✗ policy_off
```

读法：
- `✓ push` → 真的推到飞书了
- `✗ drop` → 被吞了（reason 解释为啥）
- `⌛ scheduled` → 进入 silence 等待中

点这行展开 modal 看完整 50 条历史。常见 reason 含义见第五节。

### 3.4 重命名 session（写个 alias）

dashboard 卡片标题旁边有个 ✎ 铅笔图标，点击编辑别名。给会话起个有意义的名字（"调 ETL 管道" / "review PR-123"）后，飞书推送和 dashboard 都用这个 alias。

---

## 四、配置面板

`/api/config` 或 dashboard 设置面板：

| 配置 | 默认 | 含义 |
|---|---|---|
| `feishu_webhook` | 空 | 飞书机器人 webhook URL |
| `feishu_secret` | 空 | 飞书签名密钥（可选） |
| `notify_filter.notif_idle_reminder_minutes` | `[5, 10]` | idle reminder 阈值数组：第 N 次提醒在 N 分钟后。`[]` = 关闭提醒（严格 1 次）。`[3, 6, 15]` = 3 次提醒，分别在 3/6/15min |
| `notify_filter.stop_sensitivity` | `normal` | Stop 推送敏感度：`fast`(短回合也推) / `normal` / `strict`(只推有内容的回合) |
| `notify_filter.filter_sidechain_notifications` | `true` | 过滤子 agent 内部 Notification（避免虚假打扰） |
| `quiet_hours.enabled` | `false` | 全局免打扰开关 |
| `liveness_per_state_timeout.enabled` | `true` | 智能 timeout 检测：等输入 5min / tool 执行 15min / 思考 10min |

`data/config.json` 直接编辑也可（不用懂前端）。重启或 dashboard 操作即生效。

---

## 五、push trace reason 字典

| Reason | 推/吞 | 含义 |
|---|---|---|
| `stop_milestone_len=N` | ✓ push | Stop 触发，Claude 回了 N 字内容（够长，推） |
| `stop_anchor:summary` | ✓ push | Stop 触发，回复含锚词（"完成 / fixed / passed" 等），无视字数 |
| `stop_grace_first_push` | ✓ push | session 第一次推，绕开字数阈值 |
| `stop_low_signal` | ✗ drop | Stop 但回复太短无信息（如"嗯"、"OK"） |
| `notif_idle_reminder_1_at_X.Xmin` | ✓ push | 第 1 次 idle 提醒（5min 阈值后） |
| `notif_idle_reminder_2_at_X.Xmin` | ✓ push | 第 2 次 idle 提醒（10min 阈值后，最后一次） |
| `notif_idle_dup(X.Xmin<Ymin)` | ✗ drop | idle prompt 副本，距 Stop 时间没到下一阈值 |
| `notif_idle_max_reminders(2)` | ✗ drop | 已达 3 次上限，永远不再推 |
| `notification_kept` | ✓ push | 真权限请求或长间隔后的 Notification |
| `sidechain_active` | ✗ drop | 子 agent 内的 Notification，避免虚假打扰 |
| `session_muted_all` | ✗ drop | 该 session 被你手动静音 |
| `quiet_hours_HH:MM` | ✗ drop | 命中夜间不打扰时段 |
| `policy_off` | ✗ drop | 该事件类型默认不推（如 SubagentStop / SessionStart） |
| `scheduled_silence_12s` | ⌛ wait | Stop 进入 12 秒静默期，等是否被新事件取消（合并连续动作） |

---

## 六、场景速查

### 「我在重要会议中，不想被打扰」
→ dashboard 设置面板开 quiet_hours，或临时把所有 active session 都静音。

### 「Claude 跑长 build / 大文件下载，不要因为 5min 没事件就报 hang」
→ 已经处理。`liveness_per_state_timeout` 区分"等输入"和"在干活"——tool 执行中默认容忍 15 分钟，且如果 transcript 文件还在被写就一直不报 hang。

### 「子 agent 内的权限请求被推过来打扰我，我什么都看不见」
→ 已经默认过滤。检测到子 agent transcript 在 5 秒内被写过 → 该 Notification 被识别为子 agent 内部，drop。

### 「我希望某个项目永远静音」
→ 给该 session 设永久静音（点 🔔 → 永久）。

### 「我希望长任务结束就立即推一次，但不要后续提醒」
→ 改 `notif_idle_reminder_minutes: []` → 严格 1 次模式。

### 「我希望短回合也推（哪怕回复就一个字）」
→ 改 `stop_sensitivity: fast`。

### 「我怀疑某条没推，想 debug」
→ dashboard 卡片底部 trace 行展开看完整决策历史，对照第五节的 reason 字典。

---

## 七、还想了解什么

- 安装 / 启动：见 [README.md](../README.md)
- 设计教训和决策来源：见 [LESSONS.md](../LESSONS.md)
- 模块结构：见 [docs/modules.md](modules.md)
