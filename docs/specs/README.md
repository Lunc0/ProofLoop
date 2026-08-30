# 阶段规格（Specs）

本目录保存**阶段开始前**的项目文档：目标、范围、非目标、约束、验收标准和待确认事项。
它回答“为什么做、要做到什么程度”；获准实施后，技术方案与任务拆分写入对应的 `../plans/` 文档。

文件名以阶段顺序开头；缺号表示当时没有留下对应类型的前置文档，不能事后补造为已存在的决策。

| 顺序 | 版本 | 状态 | 规格 | 对应实施计划 | 阶段完成后的实现记录 |
| --- | --- | --- | --- | --- | --- |
| 01 | v0.1 | 已废弃 | [早期 MVP 草案](01-v0.1-sql-skill-evolution-mvp.md) | [早期 MVP 计划](../plans/01-v0.1-sql-skill-evolution-mvp.md) | [v0.1 workflow shell](../design/v0.1-workflow-shell.md) |
| 02 | v0.2 | 历史缺失 | — | — | [真实 DSH 接入](../design/v0.2-real-dsh-deepseek-skill-evolution.md) |
| 03 | v0.3 | 已完成 | [LiveSQLBench Base-Lite 五题导入](03-v0.3-livesqlbench-base-lite-five-query-import.md) | [实施计划](../plans/03-v0.3-livesqlbench-base-lite-five-query-import.md) | [DSH session 与 LiveSQLBench](../design/v0.3-dsh-session-and-livesqlbench.md) |
| 04 | v0.4 | 已完成 | [PostgreSQL Benchmark Adapter](04-v0.4-postgres-benchmark-adapter-framework.md) | [实施计划](../plans/04-v0.4-postgres-benchmark-adapter-framework.md) | [公开 adapter](../design/v0.4-postgres-benchmark-adapters.md) |
| 05 | v0.5 | 已完成 | [本地 Skill 候选版本库](05-v0.5-local-skill-evolution.md) | [实施计划](../plans/05-v0.5-local-skill-evolution.md) | [实现记录](../design/v0.5-local-skill-evolution.md) |
| 06 | v0.6 | 已完成；baseline 待授权 | [Base-Lite 三十题最终 Held-out 评测集](06-v0.6-livesqlbench-final-thirty-query-suite.md) | [实施计划](../plans/06-v0.6-livesqlbench-final-thirty-query-suite.md) | [实现记录](../design/06-v0.6-livesqlbench-final-thirty-query-suite.md) |
| 07 | v0.7 | 已完成 | [分层滚动评测器](07-v0.7-tiered-rolling-evaluator.md) | [实施计划](../plans/07-v0.7-tiered-rolling-evaluator.md) | [实现记录](../design/07-v0.7-tiered-rolling-evaluator.md) |
| 08 | v0.8 | 已完成 | [DSH 轨迹压缩与 Evolution Coding Agent 输入](08-v0.8-dsh-trajectory-compression.md) | [实施计划](../plans/08-v0.8-dsh-trajectory-compression.md) | [实现记录](../design/08-v0.8-dsh-trajectory-compression.md) |
