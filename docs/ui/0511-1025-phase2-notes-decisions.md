<!-- purpose: Phase 2 notes-panel 设计决策（ui-ux-pro-max 输出 + 项目落地约束） -->

创建时间: 2026-05-11 10:25:00
更新时间: 2026-05-11 10:25:00

# 记事本面板设计决策（Phase 2）

## 上下文

claude-notify Round 10 在主控板右侧（≥960px 双栏）/ 下方（<960px 单栏堆叠）新增"记事本"。
后端 T2 已就绪：`GET/PUT /api/notes`，WS 广播 `notes_updated`。
T5 已把 `<main>` 包成 `.workspace`，`:has(.notes-panel)` 选择器已就位。

## ui-ux-pro-max 输出

query: "developer dashboard notes panel monospace markdown editor minimal"

- **Pattern**: Minimal Single Column（克制 / 留白 / 单一焦点）
- **Style**: Data-Dense Dashboard（minimal padding / grid layout / 最大化数据可见性）
- **Typography**: Space Mono（monospace / brutalist / 技术感）
- **Colors**: 黑 #171717 + 白 + 一抹 accent
- **Effects**: hover tooltips / smooth transitions
- **Anti-patterns**: 装饰性设计 / 无过滤

## 项目落地适配（不照搬，避免风格分裂）

claude-notify 既有 design tokens 已锁定一套克制中性主题：
- 字体已有 `--font-mono`（ui-monospace / SF Mono / Menlo）→ **不引入 Space Mono**，复用既有
- 颜色已有 `--c-surface / --c-fg / --c-fg-muted / --c-hairline` → **全部沿用**
- 间距尺度 `--sp-1..6` / 圆角 `--r-sm/md` → **直接套**

→ Notes panel 视觉与 sessions-list 卡片同源，差异仅在「这是输入区，不是数据展示区」。

## 关键决策

| 维度 | 决策 | 理由 |
|------|------|------|
| 容器 | `<aside>` + `.notes-panel`（白底 + bd-1 + r-md） | 与 session-item 卡片一致，工程化无割裂感 |
| Head bar 高度 | 40px | 与既有 topbar 子件视觉重量相当；折叠后只剩它 |
| 折叠交互 | 整个 head 可点 toggle，`aria-expanded` 同步 | 点击区域大，无需额外 icon-only 按钮 |
| caret 动画 | `transition: transform 120ms`，折叠时 -90deg | 与既有 drawer-meta-toggle 完全一致 |
| 字体 | textarea = `--font-mono / --fs-md / --lh-base` | 记事本本质是 markdown 草稿，等宽字符 align 列对齐 |
| 内边距 | textarea `padding: var(--sp-3)` | 与 session-item 同 |
| 边框 | textarea `border: 0; outline: none` | 边框在 panel 外层一次；内部无重复边 |
| 保存状态色 | saving = `--c-waiting-solid`（橙）/ error = `--c-suspect-solid`（红）/ saved = `--c-fg-muted`（不抢眼） | 静默优先；saved 不用绿色避免和 ok 事件混淆 |
| 字节计数 | `tabular-nums` + `--fs-xs` | 单位 "B"，不做 KB 换算（用户已说 100KB 上限） |
| meta 分隔符 | "·" 用 `--c-hairline` 颜色 | 视觉权重低于文字 |
| 最小高度 | desktop 320px / mobile 200px | 既不占太多视区，又能装下若干行调试笔记 |
| reduce-motion | caret 不旋转 | 既有项目 `prefers-reduced-motion` 已支持 |

## 显式不做

- 不做 markdown 预览（用户拍板）
- 不做 toolbar / 工具栏（破坏 "minimal" 原则）
- 不做"保存"按钮（autosave 已覆盖）
- 不做清空按钮（Cmd+A delete 即可）
- 不做 conflict banner（last-write-wins 静默）
- 不引入新字体 / 新颜色 token（用既有）

## 可访问性

- `aria-label="记事本"` 在 `<aside>` 上
- toggle 按钮 `aria-expanded`，与折叠态联动
- textarea `spellcheck="false"`（笔记多 code/路径，红波浪线噪声大）
- 颜色对比：muted 色 #8a8a82 在 #fff 上对比 3.94:1，已是既有 muted 规约，记事本沿用项目基线
- focus ring：textarea 不画边框，但 hairline 在 panel 外层；focus 状态由浏览器原生 outline 接管（既有 reset 不会破坏），需要时可在 `#notes-textarea:focus-visible` 内补一行 box-shadow
