# 阶段实施计划（Plans）

本目录保存**规格获准后、编码开始前**的技术文档：技术方案、模块或文件改动、任务顺序、检查点和验证方式。
它回答“如何实现、按什么顺序验证”；阶段完成后，实际落地情况写入 `../design/`，而不是继续把计划当作现状。

文件名以阶段顺序开头。计划必须指向一个 spec；没有获准的 spec 时不得创建 plan。

| 顺序 | 版本 | 状态 | 实施计划 | 依据规格 | 阶段完成后的实现记录 |
| --- | --- | --- | --- | --- | --- |
| 01 | v0.1 | 已废弃 | [早期 MVP 计划](01-v0.1-sql-skill-evolution-mvp.md) | [早期 MVP 草案](../specs/01-v0.1-sql-skill-evolution-mvp.md) | [v0.1 workflow shell](../design/v0.1-workflow-shell.md) |
| 02 | v0.2 | 历史缺失 | — | — | [真实 DSH 接入](../design/v0.2-real-dsh-deepseek-skill-evolution.md) |
| 03 | v0.3 | 已完成 | [LiveSQLBench Base-Lite 五题导入](03-v0.3-livesqlbench-base-lite-five-query-import.md) | [规格](../specs/03-v0.3-livesqlbench-base-lite-five-query-import.md) | [DSH session 与 LiveSQLBench](../design/v0.3-dsh-session-and-livesqlbench.md) |
| 04 | v0.4 | 已完成 | [PostgreSQL Benchmark Adapter](04-v0.4-postgres-benchmark-adapter-framework.md) | [规格](../specs/04-v0.4-postgres-benchmark-adapter-framework.md) | [公开 adapter](../design/v0.4-postgres-benchmark-adapters.md) |
| 05 | v0.5 | 已完成 | [本地 Skill 候选版本库](05-v0.5-local-skill-evolution.md) | [规格](../specs/05-v0.5-local-skill-evolution.md) | [实现记录](../design/v0.5-local-skill-evolution.md) |
| 06 | v0.6 | 已完成；baseline 待授权 | [Base-Lite 三十题最终 Held-out 评测集](06-v0.6-livesqlbench-final-thirty-query-suite.md) | [规格](../specs/06-v0.6-livesqlbench-final-thirty-query-suite.md) | [实现记录](../design/06-v0.6-livesqlbench-final-thirty-query-suite.md) |
| 07 | v0.7 | 已完成 | [分层滚动评测器](07-v0.7-tiered-rolling-evaluator.md) | [规格](../specs/07-v0.7-tiered-rolling-evaluator.md) | [实现记录](../design/07-v0.7-tiered-rolling-evaluator.md) |
| 08 | v0.8 | 已完成 | [DSH 轨迹压缩与 Evolution Coding Agent 输入](08-v0.8-dsh-trajectory-compression.md) | [规格](../specs/08-v0.8-dsh-trajectory-compression.md) | [实现记录](../design/08-v0.8-dsh-trajectory-compression.md) |
| 09 | v0.8.1 | 已完成 | [失败轨迹的 Agent 投影压缩](09-v0.8.1-agent-projection-evidence-compression.md) | [规格](../specs/09-v0.8.1-agent-projection-evidence-compression.md) | [实现记录](../design/09-v0.8.1-agent-projection-evidence-compression.md) |
| 10 | v0.8.2 | 已完成 | [失败恢复窗口与 Skill 接触证据](10-v0.8.2-failure-recovery-skill-access-evidence.md) | [规格](../specs/10-v0.8.2-failure-recovery-skill-access-evidence.md) | [实现记录](../design/10-v0.8.2-failure-recovery-skill-access-evidence.md) |
| 11 | v0.9 | 已完成 | [Evolution 后 LLM 优化建议 MVP](11-v0.9-evolution-llm-optimization-advice.md) | [规格](../specs/11-v0.9-evolution-llm-optimization-advice.md) | [实现记录](../design/11-v0.9-evolution-llm-optimization-advice.md) |
| 12 | v0.10 | 已完成 | [Benchmark DSH 禁用 Web Search 配置 Patch](12-v0.10-dsh-no-web-profile-patch.md) | [规格](../specs/12-v0.10-dsh-no-web-profile-patch.md) | [实现记录](../design/12-v0.10-dsh-no-web-profile-patch.md) |
| 13 | v0.11 | 已完成 | [结构化 Skill 与行为导向修改计划](13-v0.11-structured-skill-edit-plan.md) | [规格](../specs/13-v0.11-structured-skill-edit-plan.md) | [实现记录](../design/13-v0.11-structured-skill-edit-plan.md) |
| 14 | v0.12 | 已完成 | [EditPlan 渲染、机械校验与不可变 Child Skill](14-v0.12-edit-plan-render-child-candidate.md) | [规格](../specs/14-v0.12-edit-plan-render-child-candidate.md) | [实现记录](../design/14-v0.12-edit-plan-render-child-candidate.md) |
| 15 | v0.13 | 已完成 | [Smoke 固定 Simple/Moderate 配额与 4/5 Gate](15-v0.13-smoke-quota-and-gate.md) | [规格](../specs/15-v0.13-smoke-quota-and-gate.md) | [实现记录](../design/15-v0.13-smoke-quota-and-gate.md) |
| 16 | v0.14 | 已完成 | [Smoke 3/5 Gate](16-v0.14-smoke-three-of-five-gate.md) | [规格](../specs/16-v0.14-smoke-three-of-five-gate.md) | [实现记录](../design/16-v0.14-smoke-three-of-five-gate.md) |
| 17 | v0.15 | 已完成 | [受控自动进化循环](17-v0.15-controlled-evolution-cycle.md) | [规格](../specs/17-next-controlled-evolution-cycle.md) | [实现记录](../design/17-v0.15-controlled-evolution-cycle.md) |
| 18 | v0.16 | 已完成 | [Evolution Gold Preflight 与部分无效 Case 隔离](18-v0.16-evolution-gold-preflight-and-partial-invalid.md) | [规格](../specs/18-next-evolution-gold-preflight-and-partial-invalid.md) | [实现记录](../design/18-v0.16-evolution-gold-preflight-and-partial-invalid.md) |
| 19 | v0.17 | 已完成 | [Optimizer 无应用层输出上限](19-v0.17-unbounded-optimizer-output.md) | [规格](../specs/19-v0.17-unbounded-optimizer-output.md) | [实现记录](../design/19-v0.17-unbounded-optimizer-output.md) |
| 20 | v0.18 | 已完成 | [候选内置 SkillMap](20-v0.18-candidate-owned-skill-map.md) | [规格](../specs/20-v0.18-candidate-owned-skill-map.md) | [实现记录](../design/20-v0.18-candidate-owned-skill-map.md) |
| 21 | v0.19 | 已完成 | [紧凑 SkillMap 与短编辑锚点](21-v0.19-compact-skill-map.md) | [规格](../specs/21-v0.19-compact-skill-map.md) | [实现记录](../design/21-v0.19-compact-skill-map.md) |
