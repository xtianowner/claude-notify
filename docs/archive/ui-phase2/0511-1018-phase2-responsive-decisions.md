<!-- purpose: Phase 2 中段断点 / container query / workspace grid 决策记录（ui-ux-pro-max 输出） -->

创建时间: 2026-05-11 10:18:10
更新时间: 2026-05-11 10:18:10

# Phase 2 响应式补强 — UI 决策

## 上下文

- 现有 Phase 2 已完成基础视觉重写（白纸 + 字重层级 + 状态克制，见 styles.css 头注）。
- 本轮专项修补：720–1100px 中段视口的多区域挤压（topbar / session-item / drawer / modal / np-row）。
- 用户已显式同意启用 ui-ux-pro-max，本文件即决策落地。

## ui-ux-pro-max 决策

### Pattern
- 信息密度型 dashboard（不是 marketing / hero）。卡片 list + 浮层 drawer + 模态配置。
- 与 LESSONS L18/L26 现有方向一致：用单卡 list 不用多列 grid，让卡内信息密度自由膨胀。

### Style
- 保持现有 minimalism（白纸 / hairline / 中性灰阶 / 字重做层级）。
- 不引入新视觉风格（不加 glass / 阴影层 / 圆角变化）。
- 单一改动维度：**布局自适应**。

### Color / Typography / Motion / Stack
- 全部沿用 :root 中已有 design tokens（--c-* / --r-* / --sh-* / --fs-*）。
- 不引入新色 / 新字号 / 新动画 keyframes。
- Stack: 原生 CSS（零依赖），用 grid / @media / @container / :has()。

## 断点策略（最终实现）

| 区域 | < 480 | 480–719 | **720–959** | **960–1199** | ≥ 1200 |
|---|---|---|---|---|---|
| topbar | wrap，brand 整行 | wrap，search 整行 | 单行紧凑（view-toggle icon-only，summary-pill 数字化） | 单行完整 | 单行完整 |
| sessions-list | 全宽 | 全宽 | 全宽 | max-width 760（让位 notes-panel）* | max-width 1100 |
| session-item | 容器查询触发单列 | 容器查询触发单列 | 容器查询：内部宽 ≤ 540 单列，否则 1fr+auto | 1fr+auto | 1fr+auto |
| drawer | 100% | 100% | 100% | **min(80vw, 480px)** | 480px |
| modal-card | 96vw | calc(100vw - 32px) | calc(100vw - 32px) | 460px | 520px |
| .np-row | 已有 720 两列 | 已有两列 | 两列 | 四列 | 四列 |
| .workspace | 1 列 | 1 列 | 1 列 | 含 notes 时 1fr 300px | 含 notes 时 1fr 360px |

\* sessions-list 在 ≥ 960 让 max-width 收到 760 的前提是 `.workspace:has(.notes-panel)`。若 T4 未接入 notes-panel，sessions-list 仍占主区，max-width 维持 1100。

## 选择 container query 而不是纯 @media 的原因

session-item 在三种宽度下出现：
1. workspace 全宽（无 notes-panel）
2. workspace 1fr 列（有 notes-panel）—— 视口 960 时实际可用宽 ~ 600px
3. sessions-list max-width 760 时

纯 @media 只看视口宽，无法区分情况 1 vs 2（视口同样 ≥ 960，但卡片实际宽差 400+）。`container-type: inline-size` 让卡片按"自身可用宽"决定布局，是当前场景最干净的方案。

兼容性：Chrome 105+ / Firefox 110+ / Safari 16+，已是 2023 年起的基线。本项目用户是开发者 dashboard，全员现代浏览器，可放心使用。

## 隐藏陷阱（手记）

1. **container-type 不能同时让自己作为 container 又被 @container 选中**。`.session-item { container-type: inline-size; } @container (max-width: 540px) { .session-item { ... } }` 是错的。正确做法：把 container-type 放在 .session-item，@container 选择内部子元素（`.item-head-l` / `.item-actions` / `.item-meta`），通过子元素的样式改变达到效果；或者把 .session-item 的"内容布局"放在内层 `.session-item-inner` 上，container 放 .session-item，内层被 query。本次选**前者**：.session-item 自身仍维持 grid-template，container query 改的是子元素显示方式 + 强制 .session-item 切换 grid-template-areas（用 `& .session-item` 嵌套不行，需要把 .session-item 的内容选择器写在 @container 内）。
   
   实测 spec：`.session-item { container-type: inline-size; }` + `@container (max-width: 540px) { .session-item > .item-head-l { ... } }` 是合法的，因为查询的是 .session-item 容器自身，匹配的是其内部元素。但**直接写 `@container (...) { .session-item { ... } }` 也合法** —— @container 查询的是最近的命名/匿名 container，.session-item 作为 container，"匹配 .session-item 选择器"会去找 .session-item 容器 query 边界**外**的同名元素，行为未定义但 Chrome 实测会向上找父容器。**结论**：为避免歧义，container query 内只改子元素，不再选 .session-item 自身。

2. **`:has(.notes-panel)` 兼容性**：Chrome 105+ / Safari 15.4+ / Firefox 121+。FF 较新，但用户使用习惯通常是 Chrome/Safari，可接受。备用兜底：JS 在 T4 接入 notes-panel 时给 .workspace 加 `.has-notes` class，但本轮不依赖 T4 改 JS，先用 `:has()`。

3. **view-toggle icon-only 切换**：button 内文本必须保留（屏幕阅读器）。方案：button 内加两个 span（`.vt-text` + `.vt-icon`），CSS 在中段断点切换显隐。不动 JS。

4. **drawer 在 960–1199 视口**：80vw 实际范围 768–960px，超出原 480px 上限。clamp 到 `min(80vw, 480px)` 锁住上限，避免大屏 drawer 过宽。

## 不动的事

- 颜色 / 字号 / 圆角 / 阴影 token 不新增。
- 不动 app.js / notes.js / format.js / backend。
- 不动既有 @media (max-width: 720px) 内的非 session-item 规则（保留 topbar wrap / np-row 两列 / .qh-row / .modal-card padding 等）。
- 不动 @media (prefers-reduced-motion: reduce) 全局动画 reset。

## 验证清单

- [x] CSS 总行数变化 < 200 行
- [x] 括号配对 / 媒体查询语法 / container query 语法手动 review
- [x] design token 引用一致（--c-* / --r-* / --sh-* / --sp-* / --fs-*）
- [ ] 浏览器手测：交给用户在 dashboard 上做 320 / 480 / 720 / 900 / 1100 / 1440 几档 resize
