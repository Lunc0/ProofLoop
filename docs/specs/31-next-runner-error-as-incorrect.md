# 规格：Benchmark Runner Error 计为错题

> **阶段：**31 · **版本：**v0.29 · **状态：**已完成
>
> **关联阶段：**[阶段 18：Evolution Gold Preflight 与部分无效 Case 隔离](18-next-evolution-gold-preflight-and-partial-invalid.md)，以及既有 LiveSQLBench rolling evaluator。

## 1. 目标

从本阶段实施起，benchmark 中每个属于 runner 的错误都按该题答错处理：它进入该次
run 的错题统计与失败计数，且不再因 runner error 被排除、暂停或标记为不完整评测。

此规则覆盖 runner 在单个 case 返回的 `runner_error`，以及 runner 在执行单 case 时抛出的
受控 `RunnerError`。两者都必须形成该 case 的失败评测记录，使结果中的 evaluated、failed 和
passed 统计满足“runner error = 1 道错题”。

## 2. 范围

- 统一 rolling Smoke、Evolution、Final 及其共享单 case 执行路径的 runner-error 归类。
- 让 runner error 对题目统计的影响与普通未通过题一致：`passed` 为 false，计入 evaluated
  与失败数，不计入通过数。
- Evolution 不再将 runner error 从 `evaluations`、公开的 aggregate 或普通失败计数中剔除；
  既有 `partial_runner_error` 和“仅因 runner error 得到 `runner_invalid`”语义应移除或迁移，
  结果状态按包含该失败题后的正常评测结果确定。
- 保留 runner error 的脱敏运行证据与安全 aggregate，便于诊断，但诊断字段不改变错题计数。
- 更新 CLI/报告、结果合同和离线 fake-runner 测试，明确说明计分规则。

## 3. 不在范围内

- 不把 `evaluator_invalid`、fixture/Gold preflight 失败或 custody 证据损坏改算成错题；它们仍是
  evaluator/infrastructure 无效并 fail closed。
- 不执行真实 DSH、Docker、模型、benchmark、网络调用或重写既有 run artifact。
- 不改动 Gold、GT、冻结 manifest、候选、Smoke gate 阈值、SQL dialect、并发或自动优化行为。
- 不把 runner 的错误详情、私有 case ID、Gold、session、凭据或路径写进 stdout、Git、普通结果
  或 optimizer 输入。

## 4. 结果合同与安全边界

```text
case execution
  -> runner success       -> existing evaluator verdict
  -> runner error         -> synthetic failed case evaluation
  -> evaluator failure    -> existing evaluator-invalid handling
```

合成的 runner-error 失败记录必须只使用该 case 已可公开的身份和固定、脱敏的错误类别；不能
捏造 candidate SQL、模型输出、Gold、成本、轨迹或解释。若 runner 已提供可安全保存的原始
artifact，仍按既有清洗后路径保存。

Smoke gate 仍严格按通过题数判断；runner error 作为 0 个通过、1 个已评测失败题参与该计数。
Evolution 的 optimizer 输入仍只允许由现有的、可压缩且安全的失败证据构造；“计为错题”本身
不授权把缺失或不可压缩的 runner/session 内容交给优化器。Final 的总分和各 tier 分母必须包含
runner-error 题。

## 5. 代码与测试范围

- `src/skill_evolve/benchmark/livesqlbench_run.py`：将受控 `RunnerError` 归入 case 级失败，而非
  整体 paused run。
- `src/skill_evolve/benchmark/livesqlbench_rotation_run.py`：删除 runner-error 排除/无效分支，
  建立安全的失败记录并修正 rolling 状态、计数和报告。
- `src/skill_evolve/benchmark/livesqlbench_rotation_adapter.py`、`src/skill_evolve/cli.py` 及相关
  公共合同：移除把 runner error 标为 infrastructure-only 的错误归类。
- `tests/test_livesqlbench_rotation_run.py`、`tests/test_dsh_runner.py` 与相关 benchmark/CLI 测试：
  覆盖返回 `runner_error`、抛出 `RunnerError`、全错、部分错、Smoke gate、Evolution aggregate
  和 Final/tier 分母。
- 相关运行操作文档：记录 runner error 从本阶段起按错题计分，且 evaluator-invalid 未改变。

## 6. 验收标准

1. 任一 suite 的单 case 返回 `runner_error` 时，该题出现在评测集合中，`passed == false`，并进入
   失败数和分母；该 case 不再仅增加 discarded/partial runner-error 计数。
2. 单 case 执行抛出 `RunnerError` 时，系统以同样的脱敏失败记录继续后续 case；不因这一错误
   产生 `runner_invalid` 或暂停整个 benchmark。
3. 仅含 runner-error 题的 Smoke、Evolution 或 Final run 的通过数为零、错题数和分母等于选中题数；
   Smoke 不写成功 gate，Final 的 tier/总分不丢题。
4. 仍存在通过题或普通任务失败题时，runner-error 题与它们一起计入同一结果，不改变既有通过数
   Gate 语义。
5. `evaluator_invalid`、fixture/Gold preflight 失败仍按现有 fail-closed 和部分 evaluator-invalid
   合同处理，不能被重新标记为错题。
6. 公开结果和 optimizer 输入不新增 Gold、原始异常、私有 case ID、凭据或 session 泄漏；不可压缩
   runner error 只影响计分，不能凭空成为 optimizer 证据。
7. 使用 fake/stub runner 的定向 pytest、Ruff、mypy 与 `git diff --check` 通过；不运行真实
   Docker、DSH、模型或 benchmark。

## 7. 计划验证命令

```powershell
uv run pytest tests/test_livesqlbench_rotation_run.py tests/test_dsh_runner.py
uv run pytest tests/test_evolution_cycle.py
uv run ruff check src/skill_evolve/benchmark tests/test_livesqlbench_rotation_run.py tests/test_dsh_runner.py tests/test_evolution_cycle.py
uv run mypy src
git diff --check
```

## 8. 开放问题

无。
