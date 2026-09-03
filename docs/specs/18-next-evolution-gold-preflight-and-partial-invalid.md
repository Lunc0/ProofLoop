# 规格：Evolution Gold Preflight 与部分无效 Case 隔离

> **阶段：**18 · **版本：**v0.16 · **状态：**已完成
>
> **前置阶段：**[阶段 17：受控自动进化循环](17-next-controlled-evolution-cycle.md)，以及既有 LiveSQLBench rolling evaluator、轨迹投影与优化器合同。

## 1. 目标

修复 Evolution 评测中单个 case 的 Gold SQL 无法在已恢复 fixture 上执行时，整条 cycle 在已消耗 DSH 调用后被 `evaluator_invalid` 中断、且没有足够诊断证据的问题。

对每份冻结的 Evolution manifest，系统应在 DSH 或模型调用前验证各个选中 Gold SQL 的可执行性；不能执行的 case 只从本次 Evolution 的可评估集合与优化输入中排除。只要仍存在至少一个**有效且失败**的 case，系统继续生成只含有效失败轨迹的 advice、EditPlan 和 child materialization 链路。该行为不把部分无效的运行伪装为完整通过或正式可比分数。

## 2. 范围

1. 为 rolling **Evolution** 增加 trusted evaluator-side Gold preflight：在固定 manifest、GT 与 dump 已完成完整性校验后，以 disposable PostgreSQL fixture 执行每个选中 Gold SQL。
2. Gold preflight 对每个无法执行的 case 生成脱敏的 evaluator 诊断类别；Gold SQL、原始数据库错误、case ID、data-root、dump 路径与凭据均不进入 stdout、普通 run artifact、candidate store、`RunSubject` 或优化 LLM 输入。
3. Evolution 仅向 DSH 发送 preflight 通过的 case；运行阶段若仍出现单 case `evaluator_invalid`，同样将该 case 标记为排除并继续处理其余 case。
4. 为结果合同增加明确的“部分 evaluator 无效”语义：
   - 零个可评估 case：保留 `evaluator_invalid`，cycle fail-closed；
   - 同时存在可评估与 evaluator-invalid case：使用独立的 `partial_evaluator_invalid` 状态；该运行不是正式分数，macro-average、成本汇总与 comparison key 不得作为可比结论；
   - 有效 case 的失败仍保留其既有任务/runner verdict；仅这些有效失败 case 可进入 Evolution optimizer projection。
5. `evolution cycle` 在 `partial_evaluator_invalid` 下，仅当 optimizer projection 至少含一个有效失败 case 时继续调用 advice、EditPlan 与 materialization；若没有有效失败 case，停止且不调用优化 LLM。
6. evaluator custody 保存不可变、最小化的 preflight receipt：manifest hash、evaluator/image fingerprint、选中数、可评估数、排除数、按脱敏类别汇总的计数和 receipt digest。它不得改变既有 manifest、reserve 或 Gold 数据。

## 3. 非目标

- 不放宽 Smoke 或 Final/held-out 的基础设施无效处理；二者仍 fail-closed。
- 不自动修订、替换或重新导入 Gold SQL、官方数据、dump、rotation reserve 或 manifest。
- 不把被排除 case 的 prompt、轨迹、candidate SQL、Gold、原始 PostgreSQL 错误或身份传给优化器。
- 不新增 SQL dialect、并发、依赖、云服务、后台任务、自动 promotion、rollback 或 push。
- 不把 `partial_evaluator_invalid` 解释为通过、candidate 得分或可与完整 Evolution run 比较的结果。

## 4. 约束与安全边界

### 始终

- 在 DSH/模型前完成 Gold preflight；preflight 失败的 case 必须零 DSH 调用。
- 使用既有 disposable fixture、只读执行器、argv array 与 `shell=False`；所有临时容器在成功、异常和超时路径均清理。
- 验证 manifest、GT、dump、custody 路径、hash 和 runner 输出；保持 Gold、GT、凭据与 session 隔离。
- optimizer 输入必须只从已完成评测且 verdict 非 `evaluator_invalid` 的失败 case 派生。

### 需先确认

- 对真实本机数据运行 Docker preflight、真实 DSH、真实模型、完整评测或任何远端操作。
- 修改官方数据、Gold、dump、PostgreSQL image 版本或 custody ACL。
- 为本阶段分配正式版本号、创建实施计划和开始代码修改。

### 绝不

- 不在 stdout、Git、普通 run artifact、candidate store 或 optimizer prompt 中写入 Gold SQL、原始数据库错误、private case 身份或 custody 内容。
- 不因排除无效 case 而改写 frozen manifest，或将部分结果当作完整指标。
- 不吞掉所有 evaluator 无效：零有效 case、Smoke 与 Final 必须继续阻断链路。

## 5. 公共行为与结果语义

```text
frozen Evolution manifest
  -> Gold preflight
  -> eligible cases only -> DSH -> evaluation
  -> valid failed cases only -> optimizer projection
```

`partial_evaluator_invalid` 必须携带安全的 aggregate 计数，至少能区分 selected、evaluable、discarded 和 valid-failure 数；逐 case 诊断只保留在 evaluator custody。它使下游可以明确拒绝把本次运行当作完整评测，同时让 cycle 可在存在有效失败证据时继续。

现有 `passed`、`task_failure`、`runner_invalid`、`evaluator_invalid` 的完整运行语义保持；对 `partial_evaluator_invalid`，优化资格取决于有效失败投影，而不是把该状态降级为普通 `task_failure`。

## 6. 代码与测试范围

- `src/skill_evolve/benchmark/livesqlbench_rotation_run.py`：preflight、部分无效状态、指标与安全投影边界。
- `src/skill_evolve/benchmark/livesqlbench_rotation*.py`：custody-only receipt 合同、路径和 digest 校验。
- `src/skill_evolve/evolution/contracts.py`、`src/skill_evolve/evolution/service.py`：cycle stop/continue 语义和安全摘要。
- `src/skill_evolve/optimizer/*`：仅在存在有效失败投影时接收部分无效 Evolution run。
- `tests/test_livesqlbench_rotation_run.py`、`tests/test_evolution_cycle.py`、相关 optimizer/contract 测试：fake fixture 与 fake runner 的定向回归。
- `docs/livesqlbench-evaluator-operations.md`：说明 preflight 成本、部分无效不具可比性及运行限制。

代码保持严格类型和 `snake_case`；例如状态分支必须显式区分完整无效与部分无效：

```python
if evaluable_case_count == 0:
    return "evaluator_invalid"
if discarded_evaluator_invalid_count > 0:
    return "partial_evaluator_invalid"
```

## 7. 验收标准

1. 对已冻结的 Evolution manifest，Gold preflight 在任何 DSH/模型调用前运行；其无法执行的 case 不创建 agent workspace、不启动 DSH。
2. 两个独立 Gold 失败的 fake case 不会中止剩余有效 case；有效失败轨迹仍进入 optimizer projection，且任何无效 case 的内容不进入 projection。
3. 零个有效 case 时结果仍为 `evaluator_invalid`，cycle 不调用 advice、EditPlan 或 materialization。
4. 有有效 case 且存在无效 case 时结果为 `partial_evaluator_invalid`；其完整分数、cost 与 comparison key 不可作为可比结论。
5. 部分无效但没有有效失败 case 时，不调用优化 LLM；部分无效且存在有效失败 case 时，按既有 advice → EditPlan → materialization 顺序继续。
6. receipt 与 CLI/cycle 输出只含安全 aggregate；Gold、原始 PostgreSQL 错误、case ID 与 custody 路径不泄漏。
7. 定向 fake/stub 测试、`uv run ruff check .`、`uv run mypy src` 通过；真实 Docker preflight 仅在新授权后运行。

## 8. 验证命令

实现后默认执行不调用模型的定向验证：

```powershell
uv run pytest tests/test_livesqlbench_rotation_run.py tests/test_evolution_cycle.py
uv run pytest tests/test_optimizer_service.py tests/test_edit_plan_service.py
uv run ruff check .
uv run mypy src
git diff --check
```

使用真实 Base-Lite data-root 的 Docker preflight 属于额外授权操作，不包含在默认验证中。

## 9. 已确认事项与开放问题

已确认：Evolution 中单个 `evaluator_invalid` case 应被抛弃，不能阻断仍有有效失败证据的优化链路；优化器只接收有效失败 case。

待确认：实施中若发现已冻结数据本身需修订，必须另行确认数据版本与 manifest 作废策略。
