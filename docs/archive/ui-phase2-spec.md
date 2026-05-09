<!-- purpose: Phase 2 美化阶段 — IA + 视觉简约设计语言整合 spec（两 agent 协商后） -->

# Phase 2 Design Spec — claude-notify dashboard

创建时间: 2026-05-09 11:40:00
更新时间: 2026-05-09 11:42:00

## 来源
- IA 视角（卡片信息架构 / 用户场景）：subagent A 报告
- 视觉简约语言（tokens / 组件规范）：subagent B 报告
- 协商整合：主 agent + 用户两次决策

## 用户已锁定决策
- **卡片正面 = 5 行**（带"任务/说"两行，B 的 mockup 风格）
- **顶栏 = ⋯ 菜单收纳**（webhook / ws / 测试推送 / 配置全部进 ⋯）

---

## 1. Design Tokens（必须落到 `:root`，全部）

```css
:root {
  /* 中性灰阶 */
  --c-bg:        #f7f7f5;
  --c-surface:   #ffffff;
  --c-subtle:    #f0efec;
  --c-border:    #e5e4e0;
  --c-hairline:  #ededea;
  --c-fg-muted:  #8a8a82;
  --c-fg-soft:   #5a5a55;
  --c-fg:        #1c1c1a;

  /* 状态语义色（solid + soft 一对，6 状态） */
  --c-waiting-solid: #c2410c;  --c-waiting-soft: #fdf3ec;
  --c-suspect-solid: #b91c1c;  --c-suspect-soft: #fcefee;
  --c-dead-solid:    #6b1414;  --c-dead-soft:    #f3eeed;
  --c-ok-solid:      #15803d;  --c-ok-soft:      #eef5ef;
  --c-idle-solid:    #8a8a82;  --c-idle-soft:    #f0efec;

  /* 强调色 */
  --c-accent:      #1f4ea3;
  --c-accent-soft: #eaf0fa;
  --c-focus-ring:  #1f4ea355;

  /* Typography */
  --font-sans: -apple-system, BlinkMacSystemFont, "Helvetica Neue",
               "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
               Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --fs-xs:  11px;  --fs-sm:  12px;  --fs-md:  13px;  --fs-lg:  15px;  --fs-xl:  18px;
  --fw-regular: 400;  --fw-medium: 500;  --fw-bold: 600;
  --lh-tight: 1.35;   --lh-base: 1.55;

  /* Spacing 4px scale */
  --sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px;  --sp-4: 16px;  --sp-5: 24px;  --sp-6: 32px;

  /* Radius */
  --r-sm: 4px;  --r-md: 8px;

  /* Shadow */
  --sh-1: 0 1px 2px rgba(20, 20, 18, 0.04);
  --sh-2: 0 8px 24px -8px rgba(20, 20, 18, 0.12);

  /* Border */
  --bd-1: 1px solid var(--c-border);
  --bd-hair: 1px solid var(--c-hairline);
}
```

## 2. 五条核心原则
1. 白纸优先（鲜色仅承载状态语义，不做装饰）
2. 字重做层级 / 颜色做语义（同色 400/500/600 撑出三层）
3. 边比框轻 / 间距比边重（能用留白就不用边框）
4. 状态克制（同屏鲜色 ≤ 2，单卡填充色块 ≤ 1）
5. 静止是默认（动效仅服务"状态变化的可感知"）

## 3. Session 卡片（5 行）

### 结构
```
┌─────────────────────────────────────────┐  ← waiting 时左侧 3px 立柱
│ ● my-project              ✎  [等确认]   │   ① 标题行
│ ~/work/foo · plan-mode · high           │   ② cwd · mode · effort
│ 任务: 把 dashboard 调成极简风…           │   ③ first_user_prompt 截断（条件显示）
│ 说: 我已经…                              │   ④ last_assistant_message 截断（条件显示）
│ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─    │   ⑤ hairline dashed
│ Notification · 12 分钟前    events: 47  │   ⑥ footer
│                          心跳 30 秒前   │
└─────────────────────────────────────────┘
```

### 字段映射
| 行 | 字段 | 字号 | 字重 | 颜色 |
|---|---|---|---|---|
| ① 名字 | `display_name`（alias > first_user_prompt[:28] > project > sid[:8]）| `--fs-lg` | waiting=600 / 其它=500 / ended=400 | `--c-fg` |
| ① 徽章 | `status` | `--fs-xs` | 500 | 见 §4 |
| ② cwd 行 | `cwd_short · permission_mode · effort_level` | `--fs-sm` | 400 | `--c-fg-muted` |
| ③ 任务 | `first_user_prompt`（仅当**已起 alias** 时显示，避免与 ① 重复）| `--fs-md` | 400 | `--c-fg` |
| ④ 说 | `last_assistant_message` | `--fs-sm` | 400 | `--c-fg-soft` |
| ⑥ footer 左 | `last_event · relTime` | `--fs-xs` | 400 | `--c-fg-muted` |
| ⑥ footer 右 | `events: N · 心跳 N 秒前` | `--fs-xs` | 400 | `--c-fg-muted` |

### 关键去重规则
- **alias 已起**：① 名字 = alias；③ 任务行显示 `first_user_prompt`
- **未起 alias 且 first_user_prompt 存在**：① 名字 = `first_user_prompt[:28]`；③ 任务行**不显示**（避免重复）
- **未起 alias 且 first_user_prompt 不存在**：① 名字 = project / sid；③ 任务行不显示
- last_assistant_message 不存在 → ④ 不显示
- 不显示的行**完全省略**（卡片自适应高度 4-6 行）

### 卡片样式
- 卡片：`var(--c-surface)` 底，`var(--bd-1)` 边，`var(--r-md)`，`var(--sh-1)`
- padding: `var(--sp-3)`（4 边）
- 卡间距：grid `gap: var(--sp-3)`
- min 280px，自适应栅格
- hover：边框颜色 → `var(--c-fg-muted)`（不变颜色 / 不缩放）
- waiting 卡：左侧 `border-left: 3px solid var(--c-waiting-solid)` + 整卡 `background: var(--c-waiting-soft)`
- suspect 卡：左侧 `border-left: 2px solid var(--c-suspect-solid)`，背景仍白
- dead 卡：`border-left: 2px solid var(--c-dead-solid)`，整卡 `opacity: 0.65`，无背景色
- ended 卡：`opacity: 0.55`，无立柱无背景
- running / idle：默认无装饰

## 4. 状态徽章（小标签风）

```
背景: var(--c-{state}-soft)
文字: var(--c-{state}-solid)
边框: 1px solid color-mix(in srgb, var(--c-{state}-solid) 18%, transparent)
padding: 1px 6px;  border-radius: var(--r-sm);
font-size: var(--fs-xs); font-weight: 500; letter-spacing: .02em;
```
- waiting / suspect / dead 用各自 token
- ok（Stop / running 成功事件）用 `--c-ok-*`
- idle / ended / Heartbeat / SessionStart 等用 `--c-idle-*`

## 5. sid hash 8 色色点（CSS 替换 emoji）

```css
.dot {
  display: inline-block;
  width: 8px; height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  vertical-align: 2px;
}
```
8 色（按 `color_token` 索引 0-7，降饱和后）：
```
#c45a4a  #c47a3a  #b89a3a  #6a8f55
#4a7fb0  #8a6db0  #8a6a4a  #555550
```
- waiting 状态时该卡的 dot 走 `pulse` 动画（见 §10）
- 飞书侧仍发 emoji（已实现，不动）；前后端通过 `color_token` 索引保证同一会话 = 同色

## 6. 顶栏（极简，48px 高，单行）

```
┌──────────────────────────────────────────────────────────────────┐
│ claude-notify  [🔍 搜索…]  [⚠ 等确认 2 · 疑挂 1 ▾]  🔕 免打扰  ⋯│
└──────────────────────────────────────────────────────────────────┘
```

- **brand**: `--fs-lg / 600`
- **搜索框**：`flex: 1`，max-width 480px，`--r-sm`，h32
- **状态汇总徽**：默认显示有值的状态计数（等确认 N · 疑挂 M），点击展开为现有 6 个状态 chip 多选行（默认折叠）。无值时整个汇总徽变灰显示 "全部 N"
- **🔕 免打扰**：按钮文字直接显示状态。未静音 = "免打扰 ▾"；静音中 = "🔕 静音至 18:00"。点击展开菜单（30m / 1h / 2h / 直到下班 / 取消）
- **⋯ 菜单**：点击展开下拉，含：
  - webhook 状态（绿/红圆点 + 文字）
  - ws 状态（绿/红圆点 + 文字）
  - 「测试推送」按钮
  - 「配置」按钮
- 顶栏底边：`var(--bd-hair)`，无阴影
- padding: `var(--sp-3) var(--sp-4)`

## 7. 抽屉（width 480px，桌面）

```
┌──────────────────────────────────┐
│ ● my-project              [关闭] │  ① head（含色点 + 名字 + 状态徽 + 关闭按钮）
│ session: a1b2…  ·  ~/work/foo    │  ② sub line
├──────────────────────────────────┤
│ ▸ Meta 元数据                    │  ③ 折叠 Meta 块（默认折叠）
│   sid 全 / cwd 全 / pid /        │     展开后显示：
│   transcript / permission /      │     - sid（含「复制」）
│   effort / 完整 first_prompt /   │     - cwd 全路径（含「复制」）
│   起始时间 / 心跳时间             │     - claude_pid
├──────────────────────────────────┤     - transcript_path
│ [✓Notification][✓Stop][Hb…]      │     - permission_mode · effort_level
├──────────────────────────────────┤     - 完整 first_user_prompt（无截断）
│ [Notification] 12:30:42 · 5分前  │     - 起始 / 心跳绝对时间
│ 工具调用：等待用户确认            │
│ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─    │
└──────────────────────────────────┘
```

- **Meta 块默认折叠**，点 `▸` 展开
- 完整 `first_user_prompt` **仅在 Meta 块出现一次**（卡片正面绝不显示完整版）
- 抽屉 `box-shadow: var(--sh-2)`
- 事件行 padding: `var(--sp-3) 0`，行间 `--bd-hair` dashed

## 8. Modal（420-460 / 居中 / sh-2）
- 头：h3 `--fs-xl / 600`，`margin-bottom: var(--sp-4)`
- label 块间距：`var(--sp-3)`
- 输入：h32, padding `var(--sp-1) var(--sp-2)`, `var(--bd-1)`, `var(--r-sm)`
- 遮罩：`rgba(20,20,18,0.32)`

## 9. 输入 / 按钮 / chip

```
button (default):
  bg: var(--c-surface)  border: var(--bd-1)  color: var(--c-fg)
  height: 28px  padding: 0 var(--sp-3)  radius: var(--r-sm)  font: var(--fs-sm)/500
  hover: bg → var(--c-subtle)  border → var(--c-fg-muted)

button (primary, 仅"保存"/"测试推送"):
  bg: var(--c-fg)  color: var(--c-surface)
  hover: bg → #000

input[type=text|search|password|number]:
  height: 32px  padding: 0 var(--sp-2)  border: var(--bd-1)  radius: var(--r-sm)
  focus: border → var(--c-accent)  box-shadow: 0 0 0 3px var(--c-focus-ring)

filter chip:
  height: 22px  padding: 0 var(--sp-2)  radius: 999px
  bg: var(--c-surface)  border: var(--bd-1)  font: var(--fs-xs)
  checked: bg → var(--c-accent-soft)  border → var(--c-accent)  color → var(--c-accent)
```

## 10. 动效

| 场景 | duration | easing |
|---|---|---|
| hover / chip toggle / focus ring | 120ms | ease-out |
| 抽屉 / modal 淡入 | 220ms | cubic-bezier(.2,.7,.2,1) |
| 状态变更（卡 running → waiting）| 200ms | ease-out（淡入 + 下移 2px）|
| **waiting dot pulse**（唯一循环动效） | 1600ms | ease-in-out infinite，opacity 1↔0.45 |

**禁用**：卡片 hover 缩放 / translateY、列表 FLIP、按钮 ripple、emoji bounce、>300ms 过渡。

**Reduced motion**：尊重 `@media (prefers-reduced-motion: reduce)` —— pulse 改为静态实心 dot，过渡降到 0。

## 11. dead / ended 卡片处理
- 默认 chip 关闭时**完全隐藏**（已实现）
- 打开时按 §3 视觉规范低明度展示，不再做"已结束分组"区域
- 状态汇总徽里**不计入** dead/ended（避免拉高待办数字）

## 12. tab title（保留 v2 已实现）
`(N) claude-notify`，N = waiting + suspect 数

---

## 不要做
- 不引入 npm / 框架
- 不重写 backend（字段已齐：display_name / first_user_prompt / last_assistant_message / cwd_short / permission_mode / effort_level / last_heartbeat_ts / status / color_token / alias）
- 不做 hover 展开卡片（详情走"点卡 → 抽屉"）
- 不做暗色模式（v2.5 范围外）
- 不动 api.js 的 connectWS 实现
- 不改字段去留逻辑（IA 已锁）

---

## 重做记录（Phase 2 rework，2026-05-09 13:30）

用户反馈 v1 实施"丑/内容不完整不合理/文字与边框冲突"。本轮按 ui-ux-pro-max（minimalism + Swiss Modernism 2.0）重新校准并重写 styles.css，调整 index.html / app.js 渲染。tokens 命名/值未变，spec §1-§12 行为约束全部保留。详细审计 + 改动清单见 phase2-rework artifact。

## 实施备注（Phase 2 落地，2026-05-09 12:01）

### ui-ux-pro-max skill 校准结论
查询命中 "Real-Time Monitoring" + "Minimalism & Swiss Style" 两条 style，与 spec §1-§2 方向一致。校准点：
- **focus ring**：保持 `--c-focus-ring: #1f4ea355`（33% alpha）+ 3px 厚度；ux 域 Result 1 "visible focus rings" 验证通过。
- **pulse 动效**：spec 1600ms 在 Real-Time Monitoring 推荐 2s 区间内（更快略带紧迫感，符合"等确认"语义）；保留。
- **行高**：1.55 处于 ux 推荐 1.5-1.75 下沿，dashboard 密度合理；保留。
- **8 色色点饱和度**：与 accent `#1f4ea3` 不并排冲突；保留。
- **reduced-motion**：spec 已要求；落地为 `@media (prefers-reduced-motion: reduce)` 全局禁动效 + dot 静态。
- **新增（非 spec）**：卡片 `tabindex="0"` + Enter/Space 键开抽屉、Esc 关菜单/弹窗/抽屉，补 a11y。

### 实施清单
| 文件 | 关键改动 |
|---|---|
| `frontend/styles.css` | 整文件按 spec §1-§9 重写：tokens / topbar / 卡片 / 抽屉 / Meta 折叠 / 状态徽 / dot pulse / reduced-motion / 窄屏响应式 |
| `frontend/index.html` | 顶栏改为 brand / search / summary-pill / 免打扰 / ⋯ 菜单（webhook + ws + 测试 + 配置进 ⋯）；filter-chips 默认 hidden；抽屉头部加 dot + 状态徽，新增 `#drawer-meta` 折叠块 |
| `frontend/format.js` | `colorDot` 改返回 `<span class="dot" style="background:...">` + 8 色降饱和；新增 `colorDotValue`；`statusBadgeClass` / `eventBadgeClass` 改返回 spec §4 token class |
| `frontend/app.js` | `sessionCardHTML` 5 行 + 去重；新增 `renderSummaryPill` / `renderMetaBody` / `setMenuStatus` / `fmtAbsTime` / `closeMoreMenu`；事件绑定加 summary-pill / ⋯ 菜单 / Meta 折叠 / 复制按钮 / Esc 全局收敛；`renderSnoozeBadge` 改写顶栏按钮文案 |

### 验证
- 三个 ESM JS（`app.js` / `api.js` / `format.js`）`node --input-type=module --check` 全过
- 临时 backend 在 :8788 验证：14/14 HTML 结构断言通过；注入 Notification 事件后会话进入 `status=waiting` + `color_token=0`，前端类名 `s-waiting` + dot pulse 应可见
- 临时实例已关闭，用户主进程未受影响
