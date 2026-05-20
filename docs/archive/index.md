<!-- purpose: docs/archive 顶层索引 — 冻结的历史文档，与当前代码可能已不一致 -->

# docs/archive 索引

创建时间: 2026-05-20 11:30:00
更新时间: 2026-05-20 11:30:00

> 本目录是**冻结的历史文档**，与当前代码可能已不一致。仅用于回溯设计来龙去脉、审 PR、写复盘文。
> **代码事实**以 backend / frontend / scripts 实际代码为准；**当前文档**入口在 [README.md](../../README.md)、[docs/modules.md](../modules.md)、[docs/configuration.md](../configuration.md)、[docs/user-guide.md](../user-guide.md)。

## 顶层归档文件

| 路径 | 内容 | 来源 round |
|---|---|---|
| [`v1-design.md`](v1-design.md) | claude-notify v1 初版设计（CLI-only） | R0 baseline |
| [`info-architecture-v3-spec.md`](info-architecture-v3-spec.md) | dashboard 信息架构 v3 重写规格 | v2 → v3 |
| [`ui-handover.md`](ui-handover.md) | UI Phase 1 交付件 | R0 / Phase 1 |
| [`ui-phase2-spec.md`](ui-phase2-spec.md) | UI Phase 2 美化规格 | Phase 2 立项 |
| [`round-history-r1-r16.md`](round-history-r1-r16.md) | R1–R16 各 round 实施细节（含 commit hash / 文件清单 / 测试结果） | R1–R16 |

## 子目录

- [`phase-artifacts/`](phase-artifacts/) — Phase 1 / Phase 2 各轮 impl handover 文件（mmdd-hhmm-phaseN-* 命名）
- [`ui-phase2/`](ui-phase2/) — Phase 2 UI 阶段决策（响应式断点 / 记事本设计 / 内部 index）

## 后续 round（R17+）的归档去向

R17 起不再产 phase-artifacts。功能演进/修复要点直接进 [CHANGELOG.md](../../CHANGELOG.md)，设计教训进 [LESSONS.md](../../LESSONS.md)。Claude Desktop 桥接的初始设计单独放在 [`docs/desktop-bridge/design.md`](../desktop-bridge/design.md)（R29 baseline，实现细节已演进）。
