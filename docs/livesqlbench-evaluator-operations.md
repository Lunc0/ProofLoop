# LiveSQLBench evaluator 操作手册

本手册是当前本机 LiveSQLBench 的日常操作入口，覆盖固定五题、滚动 Smoke、Evolution 与 Final
held-out。它只说明可信评测者在本机如何运行评测；不会把 Final 的题目、manifest、GT、Gold SQL、逐题结果或
完整 session 提供给未来负责优化 Skill 的 coding agent。

> **成本提示：**`benchmark run` 会启动 Docker、调用 DSH，并可能调用真实模型。只有在确认模型、DSH provider、
> Docker image 与成本预算后才运行。`validate` 不启动 Docker、DSH 或模型。

## 1. 当前目录布局

```text
D:\skill-evolve-evaluator\
  data\livesqlbench\base-lite\
    public\                         # 公开任务材料
    gt\                             # 仅可信 evaluator 读取
    dumps\                          # 仅可信 fixture 读取
  heldout\                          # Final custody
    livesqlbench-base-lite-query-final-v1.json
    runs\
    dsh-session-logs\
    enter-heldout.ps1
  rotation\                         # Smoke/Evolution custody
    (reserve、manifests、gate receipts 由 CLI 创建)
```

在仓库根目录打开 PowerShell，并定义这三个本机路径：

```powershell
$EvaluatorRoot = 'D:\skill-evolve-evaluator'
$DataRoot = Join-Path $EvaluatorRoot 'data\livesqlbench\base-lite'
$DshManifest = 'D:\Learn\harness-agnostic-skill-evolution\examples\sql-agent\experiment.dsh.yaml'
```

`$DataRoot` 是每个 LiveSQLBench 命令的第二个位置参数。`$DshManifest` 是 DSH 的非秘密实验配置；请按你实际
使用的 manifest 调整它。不要把 API key、custody 内容或 manifest 内容放入仓库 `.env`。

## 2. 固定五题 Base-Lite

固定五题 adapter ID 是 `livesqlbench-base-lite`，用于与既有历史结果兼容。它不需要 rotation 或 Final 环境变量。

```powershell
# 不启动 Docker、DSH 或模型
uv run skill-evolve benchmark validate livesqlbench-base-lite $DataRoot

# 会启动 Docker，恢复后删除本地 fixture；不调用 DSH 或模型
uv run skill-evolve benchmark fixture-check livesqlbench-base-lite $DataRoot

# 不带 Skill：运行 baseline
uv run skill-evolve benchmark run livesqlbench-base-lite $DataRoot $DshManifest

# 带指定 Skill candidate
uv run skill-evolve benchmark run livesqlbench-base-lite $DataRoot $DshManifest --candidate <candidate-id>
```

不带 `--candidate` 表示不注入 candidate Skill；带 `--candidate` 时 CLI 从本地 candidate store 读取并验证该
candidate，再把其 `SKILL.md` 转换为通用 `RunSubject` 交给 runner。两种模式的 DSH 输入都只包含公开任务上下文。

## 3. 滚动 Smoke 与 Evolution

滚动评测从同一个开发题池抽题：Smoke 的每份 manifest 有 5 题，Evolution 的每份有 15 题。它们可以选到
同一道题；Final 永远不会进入滚动题池。reserve、manifest 和 Smoke gate receipt 都位于仓库外的
`rotation` custody。

滚动 run 必须提供稳定的 `RunSubject`，当前 CLI 通过 `--candidate <candidate-id>` 提供它。因此固定五题和
Final 可以运行无 Skill baseline，而 Smoke/Evolution **必须**带 `--candidate`；省略该选项会在启动 fixture、
DSH 或模型之前被拒绝。

先在当前 PowerShell 指向 rotation custody：

```powershell
$env:SKILL_EVOLVE_ROTATION_ROOT = Join-Path $EvaluatorRoot 'rotation'
```

### 首次初始化（只执行一次）

以下命令会从真实题库抽取并写入私有 reserve（每个难度 5 题）。当前 `rotation` 为空时才需要它；reserve
已存在时不应重跑。

```powershell
uv run skill-evolve benchmark initialize-rotation $DataRoot
```

### 创建并运行 Smoke

创建 manifest 会进行一次新的真实选题；stdout 只返回安全的 receipt，其中的 `<smoke-manifest-id>` 是随后运行时
传给 `--manifest` 的值。

```powershell
uv run skill-evolve benchmark create-rotation-manifest $DataRoot --kind smoke

# 必须带 Skill candidate，以建立可审计的 RunSubject binding
uv run skill-evolve benchmark run `
  livesqlbench-base-lite-smoke-rotation-v1 $DataRoot $DshManifest `
  --manifest <smoke-manifest-id> --candidate <candidate-id>
```

只有全部 5/5 `passed` 的 Smoke 才会为相同的被测对象写入 gate receipt。任何 `task_failure`、
`runner_invalid` 或 `evaluator_invalid` 都不会放行 Evolution；后两者属于基础设施问题，而不是 Skill 结论。

### 创建并运行 Evolution

Evolution 必须使用同一被测对象已经通过的 Smoke gate。它的 manifest 也会进行一次新的真实选题。

```powershell
uv run skill-evolve benchmark create-rotation-manifest $DataRoot --kind evolution

# 必须与通过 Smoke 的同一 candidate 相同
uv run skill-evolve benchmark run `
  livesqlbench-base-lite-evolution-v1 $DataRoot $DshManifest `
  --manifest <evolution-manifest-id> --candidate <candidate-id>
```

同一份既有 Evolution manifest 可以运行多个被测对象，或在 runner/evaluator 故障后用于审计重试；这不算新的
题目暴露。普通 Smoke/Evolution 的脱敏 evidence 位于仓库的 `runs\<run-id>\`。未来优化 agent 如需诊断，
只应获得指定 run 目录，不能获得 `rotation` custody 或 Final 目录。

## 4. Final held-out（三十题）

Final 是固定的 `livesqlbench-base-lite-query-final-v1`，不滚动、不创建 Smoke gate，也不进入优化反馈。
先在仅用于 Final 的 PowerShell 窗口执行 custody 自带的设置脚本：

```powershell
. (Join-Path $EvaluatorRoot 'heldout\enter-heldout.ps1')
```

该脚本只设置 `SKILL_EVOLVE_HELDOUT_ROOT`、`SKILL_EVOLVE_DSH_SESSION_LOG_ROOT` 与 `DSH_HOME`；它不会运行
Docker、DSH 或模型。已迁移的 `heldout` 目录中已有 Final manifest，因此**不要**对该目录再运行
`initialize-heldout`。

```powershell
# 无模型、无 Docker 的一致性验证
uv run skill-evolve benchmark validate `
  livesqlbench-base-lite-query-final-v1 $DataRoot

# 不带 Skill：Final baseline
uv run skill-evolve benchmark run `
  livesqlbench-base-lite-query-final-v1 $DataRoot $DshManifest

# 带指定 Skill candidate
uv run skill-evolve benchmark run `
  livesqlbench-base-lite-query-final-v1 $DataRoot $DshManifest `
  --candidate <candidate-id>
```

Final 的 run evidence 位于 `D:\skill-evolve-evaluator\heldout\runs\`，完整 DSH session 位于
`D:\skill-evolve-evaluator\heldout\dsh-session-logs\`。stdout 仅输出 aggregate result；不要复制、提交或
提供这些目录给优化 agent。

## 5. 命令选择速查

| 目标 | adapter ID | 必须的额外设置 | 是否可加 `--candidate` |
| --- | --- | --- | --- |
| 固定五题 | `livesqlbench-base-lite` | 无 | 可选 |
| 滚动 Smoke | `livesqlbench-base-lite-smoke-rotation-v1` | `SKILL_EVOLVE_ROTATION_ROOT`、`--manifest` 与 `--candidate` | 必须 |
| 滚动 Evolution | `livesqlbench-base-lite-evolution-v1` | Smoke 5/5 gate、`SKILL_EVOLVE_ROTATION_ROOT`、`--manifest` 与 `--candidate` | 必须 |
| Final 三十题 | `livesqlbench-base-lite-query-final-v1` | 先执行 `enter-heldout.ps1` | 可选 |

关于 Final custody 创建、DSH provider route 和独立复现实例，另见
[Held-out evaluator 操作手册](heldout-evaluator.md)。
