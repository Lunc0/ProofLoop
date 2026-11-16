# 阶段实现记录（Design）

本目录保存阶段完成后对**实际实现**的说明：最终架构、对外合同、关键安全边界、兼容性和验证结果。
它不承担新阶段的需求确认或待办任务；这些分别属于 [`../specs/`](../specs/README.md) 与
[`../plans/`](../plans/README.md)。

最新完成阶段：[Stage 34 / v0.32：DSH Submit Spill 安全解析与语义判分](34-v0.32-dsh-submit-spill-safe-evaluation.md)。

> `Stage` 是连续的项目工作序号；`v0.x` 是该阶段采用的能力版本。两者都不等同于 `pyproject.toml` 中的
> Python 包版本。早期文件名没有 Stage 前缀，属于保留的历史命名。

| Stage | 版本 | 实现记录 |
| --- | --- | --- |
| 01 | v0.1 | [Workflow shell](v0.1-workflow-shell.md) |
| 02 | v0.2 | [真实 DSH 接入](v0.2-real-dsh-deepseek-skill-evolution.md) |
| 03 | v0.3 | [DSH session 与 LiveSQLBench](v0.3-dsh-session-and-livesqlbench.md) |
| 04 | v0.4 | [PostgreSQL benchmark adapters](v0.4-postgres-benchmark-adapters.md) |
| 05 | v0.5 | [本地 Skill evolution](v0.5-local-skill-evolution.md) |
| 06 | v0.6 | [LiveSQLBench Final 三十题](06-v0.6-livesqlbench-final-thirty-query-suite.md) |
| 07 | v0.7 | [分层滚动评测器](07-v0.7-tiered-rolling-evaluator.md) |
| 08 | v0.8 | [DSH 轨迹压缩与 Evolution 输入](08-v0.8-dsh-trajectory-compression.md) |
| 09 | v0.8.1 | [依赖感知 Agent 投影压缩](09-v0.8.1-agent-projection-evidence-compression.md) |
| 10 | v0.8.2 | [失败恢复窗口与 Skill 接触证据](10-v0.8.2-failure-recovery-skill-access-evidence.md) |
| 11 | v0.9 | [Evolution 后 LLM 优化建议](11-v0.9-evolution-llm-optimization-advice.md) |
| 12 | v0.10 | [DSH 禁用 Web Search 配置 Patch](12-v0.10-dsh-no-web-profile-patch.md) |
| 13 | v0.11 | [结构化 Skill EditPlan](13-v0.11-structured-skill-edit-plan.md) |
| 14 | v0.12 | [EditPlan 渲染与 Child Skill](14-v0.12-edit-plan-render-child-candidate.md) |
| 15 | v0.13 | [Smoke 固定配额与 4/5 Gate](15-v0.13-smoke-quota-and-gate.md) |
| 16 | v0.14 | [Smoke 3/5 Gate](16-v0.14-smoke-three-of-five-gate.md) |
| 17 | v0.15 | [受控自动进化循环](17-v0.15-controlled-evolution-cycle.md) |
| 18 | v0.16 | [Evolution Gold Preflight 与部分无效隔离](18-v0.16-evolution-gold-preflight-and-partial-invalid.md) |
| 19 | v0.17 | [Optimizer 无应用层输出上限](19-v0.17-unbounded-optimizer-output.md) |
| 20 | v0.18 | [Candidate 内置 SkillMap](20-v0.18-candidate-owned-skill-map.md) |
| 21 | v0.19 | [紧凑 SkillMap 与短编辑锚点](21-v0.19-compact-skill-map.md) |
| 22 | v0.20 | [EditPlan 位置语义](22-v0.20-edit-plan-location-semantics.md) |
| 23 | v0.21 | [层级 Markdown SkillMap](23-v0.21-hierarchical-markdown-skill-map.md) |
| 24 | v0.22 | [Smoke 仅按通过数 Gate](24-v0.22-smoke-pass-count-only-gate.md) |
| 25 | v0.23 | [Smoke 2 Pass Gate](25-v0.23-smoke-two-pass-gate.md) |
| 26 | v0.24 | [Smoke 1 Pass Gate](26-v0.24-smoke-one-pass-gate.md) |
| 27 | v0.25 | [Optimizer Request Failure Codes](27-v0.25-optimizer-request-failure-codes.md) |
| 29 | v0.27 | [Codex Coding Agent 原子 Skill 编辑](29-v0.27-codex-coding-agent-optimizer-path.md) |
| 30 | v0.28 | [移除 Evolution 私有结果整行过滤](30-v0.28-remove-private-line-filter.md) |
| 31 | v0.29 | [Runner Error 计为错题](31-v0.29-runner-error-as-incorrect.md) |
| 32 | v0.30 | [Held-out 单题诊断运行](32-v0.30-heldout-single-case-run.md) |
| 33 | v0.31 | [DSH 原生 SQL 工具与 Harness Policy](33-v0.31-dsh-native-sql-harness-policy.md) |
| 34 | v0.32 | [DSH Submit Spill 安全解析与语义判分](34-v0.32-dsh-submit-spill-safe-evaluation.md) |

Stage 28 / v0.26 没有对应阶段文档，因此索引保留缺号，不补造历史记录。
