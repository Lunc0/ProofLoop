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

滚动评测从同一个开发题池抽题：新建 Smoke manifest 固定为 Simple 3、Moderate 2、Challenging 0，共 5 题；
Evolution 仍为每个 tier 5 题、共 15 题。它们可以选到同一道题；Final 永远不会进入滚动题池。reserve、manifest
和 Smoke gate receipt 都位于仓库外的 `rotation` custody。

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

完整形成五条 evaluation 后，若至少 1/5 `passed`，Smoke 会为相同的被测对象写入 v2 gate receipt；其余 case 的
`wrong_result`、timeout 或 runner error 都按一题失败、零个通过计入 evaluation，但不额外拒绝 gate。`evaluator_invalid`
也不覆盖既有的通过数 Gate 语义。run status 仍如实保留；0/5 及以下或少于五条 evaluation 都不会放行 Evolution。历史 `2/2/1` Smoke manifest 与 v1 gate
receipt 只保留审计用途，不能再运行或放行 Evolution。

### 创建并运行 Evolution

Evolution 必须使用同一被测对象已经通过的 Smoke gate。它的 manifest 也会进行一次新的真实选题。启动 Evolution 后，
trusted evaluator 会在 **DSH 或模型调用前**，使用临时 PostgreSQL fixture 对本次选中的 Gold SQL 做预检；这仍会启动 Docker，
因此同样需要确认 image、成本与本机权限。预检失败的个别 case 不会启动 DSH，也不会出现在普通 run evidence 或优化器输入中；
custody 仅保存带 manifest hash、计数与脱敏错误类别的最小 receipt。

```powershell
uv run skill-evolve benchmark create-rotation-manifest $DataRoot --kind evolution

# 必须与通过 Smoke 的同一 candidate 相同
uv run skill-evolve benchmark run `
  livesqlbench-base-lite-evolution-v1 $DataRoot $DshManifest `
  --manifest <evolution-manifest-id> --candidate <candidate-id>
```

如果预检/运行期只排除了部分 Gold 无效 case，run status 是 `partial_evaluator_invalid`：它不产生正式 macro score、
cost 汇总或 comparison key，不能用于候选比较；但其余有效且失败 case 的脱敏轨迹仍可进入 Evolution 优化链路。
若没有任何有效 case，仍是 `evaluator_invalid`。runner error（无论是 case 输出状态还是受控 `RunnerError`）则作为一题错误
保留在有效评测统计中，不会停止剩余 case、也不会产生 `runner_invalid` 或 `partial_runner_error`。若没有其他 evaluator 无效，
包含 runner error 的完整 Evolution run 状态是 `task_failure`。

DSH 明确超时且已有脱敏 session 成功压缩为可行动轨迹时，该轨迹仍可交给优化器以减少重复探索；无 session、不可压缩 timeout、
进程退出或 synthetic `RunnerError` 只计为错题，不会进入优化器。Smoke 和 Final 同样将 runner error 计为错题；Smoke 是否放行
仍只取决于完整五题中的通过数。

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

### Provider 凭据运行前检查

`enter-heldout.ps1` 会把 `DSH_HOME` 切换到 Final custody，以隔离完整 session 与运行状态。它**不会**复制、
导出或写入任何 provider 凭据。因此，“平时手工 DSH 已能调用模型”不代表这个 Held-out PowerShell 已能使用
同一凭据：要以 `dsh-session-logs\settings.yaml` 中所选 provider route 为准。

若该 route 使用 `apiKeyEnv`，其中指定的环境变量必须在**当前 PowerShell 进程**中已有值。当前随项目示例使用
DeepSeek route 时，该变量为 `DEEPSEEK_API_KEY`。在启动 Docker、DSH 或模型前执行：

```powershell
$ProviderKeyEnv = 'DEEPSEEK_API_KEY'

if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($ProviderKeyEnv, 'Process'))) {
  throw "$ProviderKeyEnv is not available in this Held-out PowerShell session."
}
```

若你使用另一 provider，先查看 custody `settings.yaml` 中对应的 `apiKeyEnv` 名称，再将 `$ProviderKeyEnv`
改为该名称。应通过组织认可的 secret manager 或 Windows 用户级环境变量在新 PowerShell 中提供该值；不要将 key
写进仓库、普通 `.env`、DSH `settings.yaml`、manifest、Git 或聊天。这个检查只确认变量是否存在，绝不显示其值。

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
| 滚动 Evolution | `livesqlbench-base-lite-evolution-v1` | 当前 v2 Smoke 1/5 gate、`SKILL_EVOLVE_ROTATION_ROOT`、`--manifest` 与 `--candidate` | 必须 |
| Final 三十题 | `livesqlbench-base-lite-query-final-v1` | 先执行 `enter-heldout.ps1` | 可选 |

关于 Final custody 创建、DSH provider route 和独立复现实例，另见
[Held-out evaluator 操作手册](heldout-evaluator.md)。
