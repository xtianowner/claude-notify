<!-- purpose: Phase 2 微调 — 抽屉关闭动画对称化（CSS-only） -->

创建时间: 2026-05-09 17:55:35
更新时间: 2026-05-09 17:55:35

## 改动
仅 `frontend/styles.css`，纯 CSS。

## 决策（ui-ux-pro-max + Material Motion 对照）
- 入场 220ms / 退场 180ms — Material "exit-faster-than-enter"，关闭比打开略快显得"轻"，落在 ux 域 `duration-timing` 推荐 150–300ms 区间。
- 入场 easing 保留 `cubic-bezier(.2,.7,.2,1)`（decelerated），退场用 `cubic-bezier(.4,.0,1,1)`（accelerated）— 标准 Material easing 配对。
- transform/opacity 驱动，复合层，符合 ux 域 `transform-performance`。
- `prefers-reduced-motion` 由全局规则压扁 transition-duration，自动生效。

## 关键 selector
- `.drawer` — 增 `transition: transform/opacity/visibility`（visibility 用 0s+180ms-delay 实现退场结束后再隐藏）；移除原顶层 `animation: drawerIn` 无条件声明，避免 className toggle 重播。
- `.drawer:not(.hidden)` — 入场 keyframes 触发点。
- `.drawer.hidden` — **专属覆盖**：`display: flex !important` 抵消通用 `.hidden { display: none !important }`，改用 `visibility: hidden + pointer-events: none + transform: translateX(16px) + opacity: 0`。

## 与其它 .hidden 的隔离
- 通用 `.hidden { display: none !important; }` 未动。
- 仅 `.drawer.hidden` 通过更高特异性（`.drawer.hidden`，class+class）+ `display: flex !important` 局部反转。
- modal / status-filter chip / snooze-menu / more-menu / drawer-meta-body 等其它使用 `.hidden` 的元素行为完全不变。

## JS
未改。`closeDrawer()` 仍是 `$drawer.classList.add("hidden")`，纯 CSS 接管动画。

## 自检
- 抽屉默认 `class="drawer hidden"` 加载 → 直接 hidden，无入场播放（transition 仅在属性变化时触发）。
- 打开（移除 hidden）→ drawerIn 220ms slide-in + fade-in（保留原行为）。
- 关闭（添加 hidden）→ 180ms slide-out（translateX 16px）+ fade-out，结束后 visibility hidden + pointer-events none。
- 卡片切换抽屉内容 → 不 toggle hidden，仅替换内容，无关闭动画干扰。
- reduced-motion → 全局 transition-duration 压到 0.01ms，瞬时切换。

## 不影响
- `frontend/app.js` outside-click handler / closeDrawer 逻辑未动
- `frontend/index.html` 结构未动
- 抽屉内部样式（drawer-head / drawer-body / drawer-meta / drawer-sub）未动
