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
