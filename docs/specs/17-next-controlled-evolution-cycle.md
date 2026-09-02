# 规格：受控自动进化循环

> **阶段：**17 · **版本：**v0.15 · **状态：**已完成
>
> **前置阶段：**[阶段 16：Smoke 3/5 Gate](16-v0.14-smoke-three-of-five-gate.md)；复用阶段 11、13、14 的优化建议、EditPlan 与 immutable child candidate 合同。

## 1. 目标

为已验证的本地 candidate 提供一个**显式、有界、可审计**的自动进化入口，消除用户在每一代中手动复制 manifest ID、run ID 与 child candidate ID 的操作。入口一次接收起始 candidate、DataRoot、DSH manifest 和最大代数，依次完成 Evolution、一次面向人阅读的 advice、一次结构化 EditPlan、确定性 materialize，并把成功 child 作为下一代的输入。

无父代（`parent_candidate_id == null`）的起始 candidate 是原始基线。它仅在本次 cycle 的第 1 代跳过 Smoke，直接进行 Evolution，避免尚未经过一次失败驱动优化的原始 Skill 被 Smoke gate 阻塞。成功生成的 child 及后续各代仍必须先取得 Smoke 3/5 gate；有父代的 candidate 即使在新 cycle 中作为起点，也不享有这项豁免。

成功不表示自动“晋级”。每个生成物仍是不可变 `draft` child，效果只由下一代独立 Smoke 与 Evolution 证明；Final/held-out、candidate 选择、推广与回退均不自动发生。

建议的正式入口为：

```powershell
uv run skill-evolve evolution cycle $DataRoot $DshManifest `
  --candidate <candidate-id> `
  --max-generations 3 `
  --child-name-prefix evolution
```

其中 `<candidate-id>` 必须是既有、可验证的 local candidate。首次导入原始 `SKILL.md` 仍使用既有 `candidate init`；循环不会从任意工作区路径隐式创建基线 candidate。

## 2. 工作流与范围

每代严格按下列顺序执行，且只在上一步成功后进入下一步：

```text
generation 1: verified baseline candidate (no parent)
  -> ensure rotation reserve
  -> create + run Evolution (optimizer disabled; Smoke exempt)
  -> advice (one LLM call)
  -> plan (one LLM call)
  -> deterministic materialize
  -> immutable draft child
generation 2..N: verified child candidate
  -> create + run Smoke
  -> 3/5 gate
  -> create + run Evolution (optimizer disabled)
  -> advice -> plan -> deterministic materialize -> next draft child
```

本阶段应完成：

1. 新增 `skill-evolve evolution cycle` Typer 命令和一个独立的 typed orchestration service；命令不通过 shell 拼接或递归调用 CLI，而是复用现有 candidate、rotation、runner、optimizer 与 materialization 服务。
2. 输入 candidate 必须先由 existing local store 的 `resolve_and_verify` 验证。每代仅把上代成功 materialize 的 child 作为下代 candidate；不会读取或改写父 `SKILL.md`。
3. rotation reserve 不存在时才初始化；存在时只验证其可用性，不重置、不补抽样、不覆盖 custody。原始 baseline 的第 1 代仅创建 Evolution manifest；其成功 child 和后续每代先创建一个新的 Smoke manifest，gate 通过后才创建同代 Evolution manifest。manifest ID 仅留在本地 custody/run evidence。
4. 仅当起始 candidate 的 `parent_candidate_id` 为 `null` 且 generation 为 1 时，允许跳过 Smoke gate 并直接运行 Evolution。其他代的 Smoke 必须完整执行五题、无基础设施无效且达到既有 3/5 gate，才可放行同代 Evolution；不达标时本代和整个 cycle 受控结束，绝不调用 Evolution、advice、plan 或 materialize。
5. cycle 读取 DSH manifest 中完整的 optimizer 配置，但**必须拒绝** `environment.optimizer.enabled: true`。Evolution runner 不得向 provider 发起 advice 请求；gate 通过后的 orchestrator 才用同一 provider/model/非秘密限制、仅将 `enabled` 置为 true，顺序调用一次 advice 和一次 plan。
6. 每个可执行的 Evolution run（包括 Smoke-exempt baseline）恰好有两次优化模型请求：一次 `advice`、一次 `plan`。advice 不成功时不得执行 plan；plan 未形成可 materialize 的 plan 时不得创建 child；materialization 一律不调用模型。
7. child 名称由 `--child-name-prefix` 和一代内单调序号确定，例如 `evolution-001`、`evolution-002`。在任何 provider 调用前检查目标名称冲突；冲突时 fail closed，不自动换名或覆盖已有 candidate。
8. `--max-generations` 为必填的 1 至 100 的整数上限。达到上限后返回成功的、可续跑的最后一个 child ID；不得提供默认无限循环、后台 daemon、计划任务或并发代际执行。
9. 写入一个仅含安全摘要的 cycle receipt：起始/最终 candidate ID、每代的 run ID、manifest hash 或安全引用、gate 决策、optimizer/materialization terminal status、停止原因和 child ID。stdout 同样只输出该摘要，不输出 case、题目、GT/Gold、完整 Skill、EditPlan、patch、session、私有路径或凭据。
10. 保留现有 `benchmark`、`optimizer advise`、`optimizer plan`、`optimizer materialize` 的单步入口及其语义；cycle 只是以同一合同提供批量编排。

## 3. 非目标

- 不自动执行 Final/held-out，不把 held-out identity、答案、逐 case 结果或 session 输入 optimizer，也不改变 custody/ACL。
- 不自动 `select`、推广、回退、删除、覆盖 candidate，或根据分数修改 DSH manifest、模型、提示词、gate 或 optimizer 参数。
- 不新增 SQL dialect、并发、多进程/后台服务、重试导致的额外模型请求、依赖、CI、云端控制面或定时任务。
- 不把普通 `benchmark run` 改为自动 materialize；只有用户显式执行有界 `evolution cycle` 才授权本次链路中的 DSH、评测和两次模型调用。
- 不将“3/5 准入”误表述为 run 成功，也不将 mechanical materialization 误表述为效果验证；baseline 的一次性 Smoke 豁免也不构成效果证明。

## 4. 命令、项目结构与代码风格

开发与验证命令：

```powershell
uv run pytest tests/test_evolution_cycle.py tests/test_livesqlbench_rotation.py tests/test_livesqlbench_rotation_run.py tests/test_optimizer_cli.py tests/test_edit_plan_cli.py tests/test_materialization_cli.py tests/test_candidate_store.py
uv run ruff check .
uv run mypy src
git diff --check
```

预计结构：

```text
src/skill_evolve/
  cli.py                         # evolution cycle 的显式 CLI
  evolution/                     # 新增：typed cycle contract、service、receipt
  benchmark/livesqlbench_rotation*.py  # 复用 reserve、manifest、gate、run 合同
  optimizer/                     # 复用 advice、EditPlan、deterministic materialization
  candidate/                     # 复用 immutable local store 与 lineage 验证
tests/test_evolution_cycle.py    # 新增：全链路合成编排和每个 fail-closed 分支
docs/specs/, docs/plans/, docs/design/  # 阶段文档与索引
```

实现采用严格 typed Python、`snake_case` 与显式依赖注入；服务层返回结构化 terminal result，而不是依赖 stdout 重新解析或捕获异常猜测状态。例如：

```python
def run_cycle(request: EvolutionCycleRequest, dependencies: CycleDependencies) -> CycleReceipt:
    current_candidate = dependencies.candidates.resolve_and_verify(request.candidate_id)
    for generation in range(1, request.max_generations + 1):
        outcome = _run_generation(current_candidate, generation, request, dependencies)
        if outcome.child_candidate_id is None:
            return _stopped_receipt(outcome)
        current_candidate = dependencies.candidates.resolve_and_verify(outcome.child_candidate_id)
    return _completed_receipt(current_candidate)
```

实际命名、模块边界和 public contract 以获准后的计划为准；不得以这个示意代码绕过错误处理、来源 hash 验证或 custody 边界。

## 5. 测试策略

只使用 synthetic rotation custody、fake DSH runner、fake optimizer client、临时 local candidate store 与临时 run store；不执行真实 DSH、模型、Docker、数据库、benchmark 或 Final/held-out。

定向测试至少覆盖：

1. 两代成功：无父代 baseline 的第 1 代只有 Evolution；其 optimizer child 的第 2 代具有 Smoke/Evolution；每个 Evolution 恰好一次 advice 和一次 plan，child chain 与最终 candidate ID 正确。
2. manifest optimizer 启用、初始 candidate 无效、reserve 已存在但无效、manifest 创建失败、名称冲突等前置错误均在 DSH/provider 调用前失败。
3. 第 2 代及后续代 Smoke 为 2/5、未完成或基础设施无效时停止，且计数器证明后续步骤为零调用；baseline 第 1 代不得创建或运行 Smoke。
4. Evolution 失败、无可用 trajectory、advice/provider 失败、plan `findings_only` 或失败、renderer/materialize 失败时保留现有 parent，未创建 child，且不发生不应有的后续调用。
5. 关闭 optimizer 的 Evolution run 不发生 provider advice 请求；orchestrator 的两次请求使用从 manifest 继承的 provider/model/限额，且不会泄露 API key 环境变量内容。
6. receipt/result/stdout 只含公开摘要；针对 case、GT/Gold、完整 Skill/plan/patch、session、私有路径及 secret-like 文本的拒绝或缺失断言继续成立。
7. 原有单步 CLI 与 rotation/optimizer/materialization 回归通过；Ruff、mypy 与 `git diff --check` 通过。

## 6. 验收标准

1. 用户执行一条带明确 `--max-generations` 的命令后，不再需要手工传递 manifest ID、run ID 或中间 child ID，即可完成 N 代受控循环；无父代 baseline 的第 1 代直接运行 Evolution。
2. 除无父代 baseline 的第 1 代外，每个成功代必须先留下有效 3/5 Smoke 准入，再运行对应 Evolution；任意拒绝或失败都可从 cycle receipt 解释停止原因，并且没有后续副作用。
3. 每个成功 Evolution 只有两次 provider 请求，分别为 advice 与 plan；不会因 runner 内隐式行为产生第三次请求。
4. 每个成功 child 可由既有 candidate verify 验证，保留 parent lineage、source run 和 optimizer provenance，状态仍为 `draft`。
5. 所有输入/输出和 persisted evidence 保持现有 secret、GT、held-out、session 与 candidate-store 隔离边界；无真实外部运行的测试证明上述分支。

## 7. 安全与兼容边界

- **始终：**按顺序、单代串行运行；仅对无父代起始 candidate 的第 1 代应用一次 Smoke 豁免；在每一项会写入或调用 provider 的步骤前验证输入和状态；保留现有 run/candidate/optimizer hash binding；把停止而非猜测或重试作为默认故障处理。
- **先询问：**批准本规格后的实施计划；在实现后运行任何真实 cycle、DSH、模型、Docker、数据库或 benchmark；增加依赖、改变 custody/ACL、提高 gate、修改模型/optimizer 配置，或推送远端。
- **绝不：**无界循环、后台执行、隐式额外模型调用、自动推广/覆盖/删除 candidate、读取或泄露秘密/GT/held-out/session、绕过 locked 区块或安全校验。

既有 manual commands 和历史 run 继续可读；不迁移或重写任何 reserve、manifest、run receipt、candidate metadata 或 optimizer artifact。

## 8. 待确认事项

已确认规则：无父代原始 baseline 的第 1 代直接执行 Evolution；其 child 和所有后续代必须先通过 Smoke 3/5 gate。

1. 正式版本为 `v0.15`，CLI 名称为 `evolution cycle`。
2. `--max-generations` 必填、范围为 1 至 100，且无后台无限循环；这是避免失控收费和不可审计写入的固定边界。
3. child 命名采用 `<prefix>-001` 起的连续形式。
