<!-- purpose: claude-notify R1-R16（2026-05-08 ~ 2026-05-11）历史 round 详细实施小结归档 -->

# Round 历史归档 R1-R16

创建时间: 2026-05-08 20:29:45
归档时间: 2026-05-13 17:12:11

> 这份是 `00TEM/NowTodo/review.md` 在 R16 闭环时的完整快照。
> R17 之后的最新调度日志和摘要见 `00TEM/NowTodo/review.md`。
> 各 round 对应的 LESSON 编号见 `LESSONS.md`。

## 调度日志

2026-05-08 20:30  task=frontend-dashboard  agent=frontend-phase1-builder  reason=前端 Phase 1 单页 dashboard，按 §8 委托给 phase1-builder  user_correction=none
2026-05-09 12:00  task=ws-realtime-rootcause  agent=Plan  reason=WS 实时推送失效，独立视角做根因+架构改进  user_correction=none
2026-05-09 12:00  task=session-identity-design  agent=Plan  reason=会话可识别性架构设计（信息源评估+分层方案）  user_correction=none
2026-05-09 12:00  task=session-deadness-design  agent=Plan  reason=session 真实生命周期探测架构设计（含关 terminal 场景）  user_correction=none

## v2 实施小结（2026-05-09）

3 份 Plan agent 方案 → 一致性检查（正交无冲突）→ 4 批次实施：
- B1 修了 WS schema 不对齐的静默吞掉 bug（前端按 envelope.type 分派 + 后端 broadcast 带 session 摘要 + hook stdin.close）
- B2 数据基座：source 路由（预留 codex 兼容）+ transcript_reader（首 prompt）+ aliases.json + liveness_watcher（PID + transcript mtime 双信号）+ event_store 重写聚合
- B3 前端卡片重写：色点 + display_name + first_user_prompt + last_assistant_message + 搜索/过滤 chips + 铅笔起 alias + 终态折叠
- B4 silence-then 节流策略：Notification/TimeoutSuspect 立即推；Stop/SubagentStop 静默 12/6 秒后推；Heartbeat/PreToolUse 取消 pending

新增模块（按 §6 登记于 docs/modules.md）：
- backend.sources / transcript_reader / aliases / notify_policy / liveness_watcher（替换 timeout_watcher）

已知细节：发现旧 backend 进程占着 8787 端口跑 v1 代码，导致 v2 代码不生效；用户重启 backend 后 v2 才会真正可用。

2026-05-09 11:40  task=card-ia-design       agent=general-purpose  reason=Phase 2 美化前置：信息架构/卡片简化（用户反馈卡片杂乱看不出是什么）  user_correction=none
2026-05-09 11:40  task=visual-minimal-design  agent=general-purpose  reason=Phase 2 美化前置：简约视觉设计语言 tokens + 组件规范  user_correction=none
2026-05-09 11:50  task=phase2-implementation  agent=frontend-phase2-polisher  reason=按整合 spec 落地，强制走 ui-ux-pro-max 决策  user_correction=none
2026-05-09 15:07  task=config-modal-overflow  agent=frontend-phase2-polisher  reason=配置 modal LLM fieldset 文字溢出 fieldset 边框，做 word-wrap/min-width/嵌套层级  user_correction=none
2026-05-09 17:15  task=llm-modal-polish       agent=frontend-phase2-polisher  reason=精简文字后 3 个 provider-block + 新加 p.muted.small 元素的视觉对齐审视  user_correction=none
2026-05-09 13:30  task=phase2-rework         agent=frontend-phase2-polisher  reason=用户反馈"文字与边框冲突 / 内容不完整 / 美化度不够"，重新走 ui-ux-pro-max 全方位架构  user_correction=上轮 polisher 实施质量不够，重做
2026-05-09 14:30  task=content-pm-design     agent=general-purpose          reason=用户反馈"卡片内容杂乱"，PM 视角拆"任务进展"字段 + 信息层级  user_correction=none
2026-05-09 14:30  task=content-qe-audit      agent=general-purpose          reason=QE 视角实测真实样本下当前渲染翻车点 + 显示边界  user_correction=none

## 长循环优化模式（2026-05-09 20:30 启动）

用户：「loop 开启长期循环检测开发，不断创建子 agent 并监督，从使用者角度优化」
约束：长轮（每轮 1-2h），git 管理（每轮 commit 一个版本），用户中断 OR 连续 3 轮无新发现自动停。
用户强调：**主要目的是实现提醒功能，不要搞错重点**。

### Round 1 (2026-05-09 20:30, commit b6bee38)
2026-05-09 20:30  task=r1-audit              agent=Explore                  reason=长循环 Round 1 用户视角审查提醒功能历史遗留问题  user_correction=none
2026-05-09 20:35  task=r1a-sidechain         agent=general-purpose          reason=Round 1·A 修子 agent Notification 错推（P1）  user_correction=none
2026-05-09 20:35  task=r1b-timeout-smart     agent=general-purpose          reason=Round 1·B 修 TimeoutSuspect 长 tool 误判（P1）  user_correction=none

成果：
- L08 sidechain 启发式（Claude Code hook 不带标记，用 transcript subagents/ 目录 mtime 5s 窗探测）
- L09 per-state timeout（Notification/Stop=5min, PreToolUse=15min+transcript 旁路, PostToolUse=10min）
- 两个修复都加 kill switch 在 config.json 里
- 测试 6/6 + 8/8 PASS
- v0 baseline 4eb6863, Round 1 v1 b6bee38

### Round 2 (2026-05-09 20:47, commit 3d71a65)
2026-05-09 20:47  task=r2a-feishu-content     agent=general-purpose          reason=飞书消息内容质量重构（看不出完成了什么）  user_correction=none
2026-05-09 20:47  task=r2b-stop-threshold     agent=general-purpose          reason=Stop 阈值放宽 + 时间窗 fallback（短回合漏推）  user_correction=none

成果：
- L10 飞书两块结构 📌 task_line + ✓ conclusion，行级抽取 + 锚词优先 + footer 跳过
- L11 阈值 12/15 → 6/8 + 三档 sensitivity (fast/normal/strict) + 8min grace 窗 fallback + 黑词整句严格相等
- 真实飞书推送 StatusCode:0 成功；Round 1 回归 14/14 全绿
- Round 2 v2 3d71a65

### Round 3 (2026-05-09 21:00, commit 待补)
2026-05-09 20:55  task=r3-audit               agent=Explore                  reason=Round 3 副作用审查 + 下一痛点定位  user_correction=none
2026-05-09 21:00  task=r3a-decision-trace     agent=general-purpose          reason=推送决策 trace 落盘 + dashboard 显示  user_correction=none
2026-05-09 21:00  task=r3b-archival           agent=general-purpose          reason=events.jsonl 滚动归档（按日期 gzip）  user_correction=none

成果：
- L12 决策 trace：data/push_decisions.jsonl（per-sid cap 50） + list_sessions 注入 recent_decisions + 新 endpoint + 前端卡片底部显示
- L13 归档：max_hot_size_mb=50 + rotation_grace_days=1 + 启动时归档 + ?include_archive=1 懒加载
- 50k mock：64MB → 19MB hot + 9 .gz，3.3× 读速；真实 events.jsonl 1.7MB no-op
- 真实推送 StatusCode:0；12 session 中 2 个产生 trace
- Round 3 v3 99a0fdd

### Round 4 (2026-05-09 21:15, commit 1b27473)
2026-05-09 21:15  task=r4a-session-mute       agent=general-purpose          reason=per-session 静音 + dashboard 操作  user_correction=none
2026-05-09 21:15  task=r4b-quiet-hours        agent=general-purpose          reason=quiet_hours 夜间不打扰 + 跨午夜  user_correction=none

成果：
- L14 per-session mute：config.session_mutes（sid → {until, scope=all/stop_only}）+ 新 endpoint + dashboard 🔕 菜单 + 自动过期清理
- L15 quiet_hours：HH:MM 跨午夜 + tz + passthrough 列表 + weekdays_only + 设置面板 fieldset
- 过滤顺序：session_mute → quiet_hours → 既有 stop/notif decision
- 8/8 + 22/22 + 全 R1-R3 回归 PASS；真实 curl 端到端通过

### Round 5 audit (2026-05-09 21:25)
2026-05-09 21:25  task=r5-audit               agent=Explore                  reason=stop-check：是否还有 P1/P2 actionable  user_correction=none

判断：**stop**。8 项 P1+P2 修复完整覆盖了用户四大类痛点（错推/漏推/内容/可控）。剩余是"运营"（多机同步、L13 归档真实路径未触发、30min+ build 边界）需要真实时间验证。
建议交还用户决策。可选收尾：L13 真实场景 mock 二次验证（30min）。

### Round 5 hotfix（用户反馈链, 2026-05-09 21:30~22:50）
- L16 Stop 漏推 + Notification 副本误吞 → 调 suppress 0.5min（之后回退）
- L17 config.save 反向 diff（DEFAULTS 改了不被 config.json 旧值锁死）
- L18 恢复 3min suppress（Stop 已推 = 用户已知）
- L19 补全 Claude Code 第二种 idle prompt 'needs your attention' + 整句相等匹配
- L20 dashboard status 不被 idle prompt 副本切换（同步 dedup 决策）
- L21 严格 1 次 dedup（被 L22 取代）
- L22 3 次硬上限 idle reminder（任务完成 + 5min + 10min）
- 用户级 docs/user-guide.md 写完
- 历经 commit: 39efa26 → 5ce0fa8 → b966814 → 237ece5 → 9f92c07 → d4a1aee → 5fc03d8

### Round 6 (2026-05-10 08:30, commit d27958f)
2026-05-10 08:30  task=r6-audit               agent=Explore                  reason=multi-session 并发场景 UX 审查  user_correction=none
2026-05-10 08:35  task=r6a-feishu-project     agent=general-purpose          reason=飞书 [project] 前置 + dashboard 二级排序  user_correction=none
2026-05-10 08:35  task=r6b-grouping           agent=general-purpose          reason=dashboard 按项目分组视图 + topbar toggle  user_correction=none

成果：
- L23 飞书 L1 [project] 前缀（alias > cwd basename > cwd_short）
- L24 dashboard 同 status 内按 age 二级排序（STALE_FIRST 决定方向）
- L25 dashboard 按项目分组（topbar toggle + localStorage 持久化）
- 13/13 + 5/5 + 8/8 全 PASS；真实飞书 StatusCode:0
- Round 6 v6 d27958f

### Round 7 (2026-05-10 08:50, commit c38974d)
2026-05-10 08:50  task=r7-urgency-reminder    agent=general-purpose          reason=紧急度徽 + reminder 消息差异化  user_correction=none

成果：
- L26 dashboard 卡片紧急度徽（age 三档色） + Feishu reminder 模板差异化（5min "还在等你" / 10min "已等 X 分钟"）
- reminder_index 从 should_notify reason 解析过去（解耦 should_notify 接口）
- 14/14 PASS；R6 + L22 回归全绿

### Round 8 audit (2026-05-10 08:55)
2026-05-10 08:55  task=r8-audit               agent=Explore                  reason=收敛检查  user_correction=none

判定：**STOP**。连续无 P1 actionable；剩余 P2/P3：
- (P2) dashboard filter/drawer state localStorage 持久化（刷新丢状态）
- (P2) 飞书并发推送顺序乱（webhook 限制，非本项目 bug）
- (P3) alias 自动建议（按 cwd 推荐）
建议转维护 + 用户反馈驱动模式。

### Round 9 (2026-05-10 10:55, commit 7e456cf)
2026-05-10 10:50  task=r9-audit               agent=Explore                  reason=PM/用户视角再扫使用流程痛点  user_correction=none

audit 发现：
- **P1-1** 首次接入空状态没引导（用户不知装 hook / 配 webhook / 触发事件）
- P2-1 trace 翻译 / P2-2 LLM 可选说明 / P2-3 WS 断线 UX / P2-4 配置按钮 disabled
- P3：搜索防抖 / quiet_hours 时区 select / 分组 header 状态徽折叠

本轮实施 P1-1 + P2-2 + P2-4（一致主题：首次接入与初始可用性）：
- L27 首次接入引导：后端 `GET /api/health/setup` 检测 hook 注册 + webhook 配置 + session 数；前端在 0 sessions 时渲染检查清单卡（✓/✗ 3 项 + 具体下一步 + "重新检查" / "去配置"按钮）
- 配置面板 LLM 区块加可选说明文案
- 配置按钮在 config 加载完成前 disabled，title="配置加载中…"

测试：scripts/0510-1055-test-setup-health.py 25/25 PASS（schema + 4 种检测分支）；R6/R7 回归 13/13 + 14/14 全绿。

剩余可做（用户未要求）：
- P2-1 trace 人话翻译（3-4h，trace modal 可读性提升）
- P2-3 WS 断线 overlay + 即时 reload（2-3h，断线 UX）
- P3 全部

### Round 10 (2026-05-11 10:30)

用户要求："loop 优化用户体验 + 修严重 UI 失误 + 加记事本功能"。

audit 关键发现（Explore agent 报告 T1 #74）：
- 中段 720-1100px 视口下三处硬编码挤压点（drawer 固定 520px、session-grid `minmax(320px, 1fr)` 与 sidebar 抢空间、卡片内紧急度徽 + 状态徽 + alias 长名同行不收缩）
- 三列固定 grid 在 13" MBP 视口下溢出（见 L30 否定方案）
- notes 功能缺失，用户主控板没有"沉淀想法"入口

实施摘要：
- **后端**：`backend/notes.py` 新建 119 行（read/write/NotesOversizeError，fcntl + RLock 双锁，256KB 上限）；`backend/app.py` 加 `GET /api/notes` + `PUT /api/notes` 2 endpoint + WS broadcast `notes_updated`
- **后端测试**：`scripts/0511-1020-test-notes-api.py` 6 类 26 assertion，curl 真实验证 PASS
- **前端 notes**：`frontend/notes.js` 新建 170 行（textarea + 800ms debounce + 状态行 + 折叠 + WS 接收侧自保护）；`frontend/index.html` +16 行 notes-panel DOM；`frontend/api.js` +7 行 +2 方法（getNotes / putNotes）；`frontend/app.js` +4 行 WS 路由 notes_updated；`frontend/styles.css` +144 行 notes-panel 样式
- **响应式重构**：`frontend/styles.css` +106 行中段 @media + container query + drawer 收缩到 80vw；`.workspace` 两列布局 grid `1fr 360px`（≥ 1200px），300px（960-1199px），折叠（< 960px）；`frontend/index.html` +2 行 view-toggle span 拆分（小屏隐藏文字保留图标）
- **决策记录**：用户选 markdown 单文件 / 右侧可折叠 panel / 全局笔记 + 卡片📝引用 / 256 KB 上限 / 不加清空按钮 / 静默覆盖；4 条 LESSON 入 L28-L31

测试：
- 自动化部分：notes API 6 类 26 PASS + 静态资源通过；R6 13 / R7 14 / R9 25 / R10 26 = 78 全过
- 手测：浏览器 6 档响应式 + 多窗口同步留给用户（task #80 用户已认领）

文件清单（5 改 + 3 新建 + 1 测试新建）：
- 新建：`backend/notes.py` / `frontend/notes.js` / `scripts/0511-1020-test-notes-api.py`
- 修改：`backend/app.py` / `frontend/index.html` / `frontend/api.js` / `frontend/app.js` / `frontend/styles.css`
- 文档：`LESSONS.md`（+L28-L31）/ `docs/modules.md`（+notes 模块）/ `docs/user-guide.md`（+七、记事本）

未做的事：
- task #82 卡片"📝 加引用"按钮（追加 session 摘要到全局笔记）—— 用户同意推迟到后续 round
- 浏览器 6 档手测 + 多窗口同步手测（task #80）—— 用户自验，已认领
- markdown 实时预览 / 笔记版本历史 —— 未排期

调度日志：

2026-05-11 09:00  task=r10-audit              agent=Explore                  reason=audit 硬编码宽度盘点 + 中段 720-1100 挤压点 + notes 设计前置  user_correction=none
2026-05-11 09:20  task=r10-notes-backend      agent=general-purpose          reason=后端 notes 模块 + 2 API + WS 广播（fcntl + RLock + 256KB 上限）  user_correction=none
2026-05-11 09:45  task=r10-notes-backend-test agent=general-purpose          reason=后端 notes API 单测 6 类 26 assertion  user_correction=none
2026-05-11 10:00  task=r10-notes-frontend     agent=frontend-phase2-polisher reason=前端 notes panel + textarea + 800ms autosave + WS 接收侧自保护  user_correction=none
2026-05-11 10:15  task=r10-responsive         agent=frontend-phase2-polisher reason=响应式重构 + .workspace 两列 + container query + drawer 80vw  user_correction=none
2026-05-11 10:30  task=r10-docs               agent=general-purpose          reason=LESSONS L28-L31 + docs/modules.md + user-guide.md + review.md 收尾  user_correction=none

### Round 11 (2026-05-11 11:00:00)

用户要求三句话摘要：
1. 新功能：Claude 列菜单等用户选时（`❯ 1. xxx` / `Type something`）立即飞书推 + dashboard 显示紧急徽
2. 阈值调整：二次/最终提醒从 `[5, 10]` 改为 `[15, 45]`，避免微中断
3. Bug 修复：dead 判定永远触发不了（active_window 默认 30 = dead_threshold 默认 30，永远过滤掉自己的判定对象）；隐含修菜单 prompt 被 idle_dup 过滤吞掉

实施摘要（5 文件改后端 + 3 文件改前端 + 1 测试新建 + 4 LESSONS）：

- **后端 R11-T9**：
  - `backend/config.py`：DEFAULTS `notif_idle_reminder_minutes [5,10] → [15,45]`
  - `backend/notify_filter.py`：阈值随 DEFAULTS 同步；`_idle_reminder_decision` 首位加 `menu_detected` bypass 分支（`return True, "menu_detected_bypass_dup"`）
  - `backend/liveness_watcher.py`：`active_window` 动态默认改为 `max(60, dead_threshold * 2)`；用 `_read_raw()` 区分用户显式 vs DEFAULTS 合并
  - `scripts/hook-notify.py`：加 `_detect_menu_prompt(transcript_path)`，读 transcript 末 20 行子串匹配 `❯\s*\d+\.|Type something`，命中 → `evt.raw.menu_detected=true`
  - `backend/event_store.py`：`list_sessions` 派生 `menu_detected` 字段（Notification + raw.menu_detected → true；Stop 清零；SubagentStop 不清）
  - 测试新建：`scripts/0511-1047-test-round11-backend.py` 22 PASS
  - 旧测试修补：`scripts/0509-2330-test-user-feedback-fix.py` 显式注入阈值，验证机制而非默认值

- **前端 R11-T10**：
  - `frontend/app.js`：`renderSessionRow` 加紧急徽 HTML（menu_detected=true 时渲染 🔥"指令选择·待响应"）
  - `frontend/styles.css`：`.urgency-badge.urgency-menu` + 1.8s pulse 动画 + `prefers-reduced-motion` 守约；复用既有 `--c-suspect-soft/solid` token
  - 设计决策：不引入新 status enum（见 L35），走"派生 UI hint 字段"路线

- **文档 R11-T11**：
  - `LESSONS.md`：+L32-L35（active_window 边缘 bug / 阈值不能脱离信号源识别 / [15,45] 节奏理由 / UI 紧急徽不引入新 enum）
  - `docs/user-guide.md`：+ "三 + 1、菜单选择紧急通知" 章节 + 阈值 [5,10]→[15,45] 说明 + 场景速查项 + trace reason 字典加 `menu_detected_bypass_dup`
  - `docs/modules.md`：阈值与字段同步（见下）
  - `00TEM/NowTodo/review.md`：本段（含调度日志）

测试结果（全部回归 + 新测全过）：
- R6 13/13 / R6 user_feedback ALL / R7 14/14 / R9 25/25 / R10 26/26 / **R11 22/22**
- 真实 backend `/api/sessions` 暴露 `menu_detected` 字段验证通过
- 测试链可见新阈值生效：`notif_idle_dup(1.0min<15min)`

调度日志：

2026-05-11 10:45  task=r11-t9-backend         agent=general-purpose          reason=后端阈值[5,10]→[15,45] + active_window fix(L32) + menu 检测(L33) + bypass + 后端测试 22 PASS  user_correction=none
2026-05-11 10:55  task=r11-t10-frontend       agent=frontend-phase1-builder  reason=前端紧急徽 🔥"指令选择·待响应" + 脉冲动画 + reduced-motion 守约  user_correction=none
2026-05-11 11:00  task=r11-t11-docs           agent=general-purpose          reason=LESSONS L32-L35 + docs/user-guide.md + review.md 收尾  user_correction=none

坑 / 教训（子 agent 实施中发现，已部分写入 LESSONS）：
1. **transcript JSONL 的菜单文本是 escape 字符串**——`message.content` 字段里 `\n` 是字面字符不是真换行，`re.MULTILINE + ^` 锚定不可用，必须改成**子串匹配** `❯\s*\d+\.|Type something` 才稳定命中
2. **DEFAULTS 合并破坏 `or` 默认 fallback**——`cfg.get("active_window_minutes") or 30` 在 `cfg_mod.load()` 合并 DEFAULTS 后永远拿 30（不是 None），需要直接读 `_read_raw()` 才能识别"用户显式 vs 系统默认"——这是配置层通用陷阱（已入 L32）
3. **旧测试硬编码 [5,10] 阈值**——`scripts/0509-2330-test-user-feedback-fix.py` 在默认值改变后 fail，需补显式注入阈值（验证机制而非默认值），避免下次再调阈值时连环 break
4. **active_window ≤ dead_threshold 边缘**——dead 判定不可达的隐蔽程度极高，trace 上看 status=suspect + 心跳 31min+，但 dead 永远不出现。L32 加了"语义相关阈值需要显式不变量声明"的教训，未来加新窗口/阈值时这条要查

未做的事（不在 Round 11 范围）：
- 菜单场景独立 reminder 节奏（如菜单出来后 5min 二次催）—— 用户反馈触发再做
- backend 启动时阈值不变量检查 / `_UNSET` sentinel 抽象 —— L32 留白
- 浏览器手测脉冲动画 + reduced-motion 真实生效 —— 用户自验

文件清单（5 改后端 + 3 改前端 + 1 测试新建 + 4 文档）：
- 修改：`backend/config.py` / `backend/notify_filter.py` / `backend/liveness_watcher.py` / `backend/event_store.py` / `scripts/hook-notify.py` / `frontend/app.js` / `frontend/styles.css`
- 新建测试：`scripts/0511-1047-test-round11-backend.py`
- 修补测试：`scripts/0509-2330-test-user-feedback-fix.py`
- 文档：`LESSONS.md`（+L32-L35）/ `docs/user-guide.md`（+菜单章节 + 阈值更新 + trace reason）/ `docs/modules.md`（同步阈值）/ `00TEM/NowTodo/review.md`（本段）

## review

（首版还没产出，构建完后补）
2026-05-11 13:44  task=G4  agent=general-purpose  reason=README 用户可用性 gap 审计  user_correction=none
2026-05-11 17:55  task=R16  agent=（主 agent 自处理）  reason=推送渠道解耦：飞书+浏览器双渠道 + 桌面通知接入（L41）+ HotFix 删 visible+focused 抑制  user_correction=用户反馈"飞书有浏览器没"→ 即时改 A 方案删抑制（同回合 HotFix）

## R16 实施小结（2026-05-11，L41 + HotFix）

用户提"能否关闭飞书，只用 web 显示？" → 拆出 6 子任务（T1-T6）+ 1 HotFix：

- **后端**（3 文件）：`config.DEFAULTS["push_channels"]={feishu:true,browser:true}`；`feishu.send_event/_combined` 入口加 `channel_off` 守卫；`notify_policy.NotifyDispatcher.set_push_listener(cb)` 钩子 + 5 个 push 点前 `_emit_browser_push`；`app.py:lifespan` 注入 listener 包装 `hub.broadcast({"type":"push_event",...})`
- **前端**（4 文件，新增 `notify.js` ~100 行）：顶部"启用桌面通知"条带 + `Notification.permission` 管理；Settings 抽屉新增"推送渠道"fieldset 两个 toggle；onWsEnvelope 新增 `push_event` 分支调 `notifyOnPush`
- **文档**：`LESSONS.md` L41（含 HotFix 修正段，标删除线第 3 条 visible+focused 教训）+ `docs/configuration.md` §0 推送渠道 + `docs/modules.md`（backend.feishu/notify_policy/frontend.notify.js 三段补 L41 标记）+ `docs/user-guide.md` 新增 §〇·五 + `README.md`（顶部一句话 + §3 渠道选择 + §3a 飞书 + §3b 浏览器 3 层权限提示 + 故障排查"浏览器不弹"分支）

**HotFix 触发链**：实施完用户测试 → "浏览器又不弹了，飞书有" → 主 agent 诊断 `visible+focused` 抑制命中 → 提 AB 方案 → 用户选 A（删抑制） → 立即改 + 同步文档。教训：UX 决策不要替用户太精细；和并存渠道行为一致比"少打扰一次"更重要。

**回归保护**：R15 permission_prompt 测试 6/6 通过；WS push_event 端到端实测 collected 1 message 确认链路；config import sanity 验证 push_channels 默认值。

文件清单（4 改后端 + 4 改前端 + 1 新前端 + 5 改文档）：
- 修改后端：`backend/config.py` / `backend/feishu.py` / `backend/notify_policy.py` / `backend/app.py`
- 修改前端：`frontend/app.js` / `frontend/index.html` / `frontend/styles.css`
- 新建前端：`frontend/notify.js`
- 修改文档：`LESSONS.md`（+L41 + HotFix 修正段）/ `README.md` / `docs/configuration.md` / `docs/modules.md` / `docs/user-guide.md` / `00TEM/NowTodo/review.md`（本段）
