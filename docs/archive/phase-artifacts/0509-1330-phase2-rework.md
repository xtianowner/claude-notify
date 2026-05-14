<!-- purpose: Phase 2 美化阶段 v2 重做记录（用户对 v1 不满意后返工） -->

# Phase 2 Rework — claude-notify dashboard

创建时间: 2026-05-09 13:30:35
更新时间: 2026-05-09 13:30:35

## 触发
用户原话："ui 不行，有点丑"、"内容显示不完整/不合理"、"文字和 ui 边框冲突"。

## ui-ux-pro-max skill 决策摘要
- 风格主轴：Minimalism & Swiss Style（styles.csv result 1）+ Swiss Modernism 2.0（result 2）—— "white space / mathematical spacing / single accent / clean borders / no shadow unless necessary"
- 排除：Bento Grid（卡片大小不一）、Exaggerated Minimalism（巨字号不适合 dashboard）、Dark Mode（用户禁）
- ux 引用规则：Line Height 1.5-1.75（已用 1.55）、Hover States 颜色不变形（去 scale）、Focus States 不与 hover 状态混用、Touch Spacing ≥ 8px、Overflow Hidden 谨慎用（避免裁掉关键内容）
- 拒做（用户决策已锁）：暗色模式 / npm / 改后端 / 多列 grid / 改 token 命名

## 审计 16 条问题（精简单行）
1. session-item grid 三段错位（time 与 head baseline 不齐）
2. status-badge 18px 与 name fs-lg baseline 错位
3. summary "进展/任务" chip 灰底块抢眼且 vertical-align 不齐
4. → 终端按钮视觉重量与摘要相近，主次不清
5. item-meta `border-top: 1px dashed` 与 border-radius 8px 在角部漏色
6. waiting `border-left: 3px` 直接破坏 border-radius，左上左下露白
7. 长 alias 时 ✎ 和状态徽被挤掉/挤到下行
8. 卡间距 8px 偏紧，三行内容拥挤
9. 顶栏右侧三按钮组内部间距不清晰
10. dead/ended `opacity 0.65` 全卡置灰，文字几乎不可读
11. drawer Meta 折叠展开后行密度高，与事件流分隔不清
12. notify_policy 6 行控件挤，窄屏溢出
13. 多处 letter-spacing 用在中文区（PingFang 已紧凑）
14. dot 用 vertical-align 与 flex baseline 冲突
15. focus-visible 与 hover 都改 border-color，状态混淆
16. line-clamp summary 与 actions 同行，行高被 actions 拉开

## 重构改动清单
| 文件 | 关键改动 |
|---|---|
| `frontend/styles.css` | 整文件重写。grid 改 `head/actions/summary/meta` 四区；waiting/suspect/dead 立柱改 `inset box-shadow` 不破坏 border-radius；status-badge 改 height 20 line-height 1；summary-label 去 chip 化（uppercase + border-right 竖线）；item-meta 拆 left/right 两端对齐，去 dashed border-top；btn-focus 改 ghost（默认无边、hover 才显边）；alias-edit 默认 opacity:0、hover 出现；中文区段去 letter-spacing；dot 不用 vertical-align；focus-visible 仅加 box-shadow ring；np-row 改 grid 4 列、窄屏 wrap 2 列；drawer-meta open 时背景 var(--c-bg) 区分；窄屏断点改 720px。 |
| `frontend/index.html` | 顶栏右侧三按钮（summary-pill / 免打扰 / ⋯）包进 `.topbar-actions` 容器，统一 gap |
| `frontend/app.js` | `sessionCardHTML`：grid area 改为 head+actions 同行 / summary 单行 / meta 单行；摘要 label "进展" → "刚才说"（更口语）；meta 拆 meta-left（cwd · mode · effort）/ meta-right（time · 心跳）；btn-focus 去掉 `.btn-icon` class（避免 28x28 方形约束）；np-row 中"秒"unit 加 `.np-unit` class |
| `frontend/format.js` | 未改（colorDot / statusBadgeClass 行为不变） |
| `docs/ui/0509-1140-phase2-spec.md` | 顶部添加"重做记录"段，引用本文件 |

## QA 5 场景（实测渲染描述）
注入 7 个 session 覆盖 5+ 场景，浏览器 1280×900 / 1440×1000 / 600×1000 三种宽度截屏验证。
1. **A 短数据 + running + alias=foo-工程**：head 行 dot 橙色（token 6） · "foo-工程" · ✎ hover 显 · "运行中"绿徽；摘要 "刚才说 | 已读取文件，开始下一步。"；meta "work/foo · default · low | 几秒前"
2. **B 长 alias(>30字) + waiting + 长 last_assistant(>200字)**：waiting 橙立柱 inset shadow 圆角完整；alias 单行省略；"等确认"橙徽完整；摘要 line-clamp 2 行截断；meta cwd 较长但 ellipsis 干净；dot pulse 动画
3. **C 长 first_user_prompt + 长 cwd + suspect**：name = first_prompt[:40]+"…" 单行；红立柱；"疑挂起"红徽；空摘要 italic；meta "project/cnotify-test-c · acceptEdits · medium | 几秒前"
4. **D 空态 + idle (Stop)**：name = cwd_short "tian/empty"；灰"待命"徽；摘要 "暂无活动信息，点开看事件流" italic muted；meta 紧凑
5. **E dead + reason**：暗红 inset 立柱；name + meta 全部 fg-muted 灰显但仍可读；"已死亡"暗红徽；默认状态 chip 关 dead 时整卡隐藏（用户勾选 dead 后仍可阅读）

补充：
- 抽屉打开（hash #s=...）：drawer-head dot + 名字 + 状态徽 + 关闭；drawer-sub 用 monospace 显示 sid + cwd_short；▸ Meta 元数据 折叠按钮带 hover bg；事件 chip 过滤行；事件流正序显示
- 配置 modal：fieldset 圆角 + uppercase legend；np-row 4 列 grid；窄屏自动 wrap 成 name / mode+sec 两行
- alias modal：标题 fs-xl bold；两个 label + 表单 actions 右对齐
- 测试推送 toast：3s 自动消失，色按 ok/err 区分
- 窄屏 600px：顶栏 wrap，搜索框换行；session 卡单列 grid 4 行；alias-edit ✎ 常驻可见

## 已知遗留 / 风险
- last_assistant_message 含 markdown 标记（## / `` ` ``）时直接 escape 显示偏丑——属数据问题，前端不解析 md（如要改需共识）
- dead 状态推 SessionDead 后状态汇总徽不计入，与 spec §11 一致；用户勾选 dead chip 才看得到这些卡
- liveness watcher 30s 周期，fake transcript 路径不存在会被立刻判 dead——这是后端正确行为，仅影响测试注入数据的可见性
- prefers-reduced-motion 已支持但本机无法直接验证；CSS 规则文本已落到 stylesheet
- 未做单元测试；视觉回归只通过截图人眼 review

## 执行环境
local mac (darwin 24.5)；后端临时启动后已 kill；events.jsonl / config.json / aliases.json 全部还原
