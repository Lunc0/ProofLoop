# LiveSQLBench Held-out evaluator 操作手册

本手册面向希望在本机运行最终三十题评测的维护者或受信任评测者。普通 clone 可以直接运行公开的
五题 smoke；最终 held-out 则有意不随 Git 分发，因为它的 manifest、逐题结果与 DSH session 都不能进入
仓库或优化 Skill agent 的输入。

当前本机的统一目录布局，以及固定五题、滚动 Smoke/Evolution 与 Final 的可直接复制命令，见
[LiveSQLBench evaluator 操作手册](livesqlbench-evaluator-operations.md)。本手册保留 Final 的 custody 创建、
复现与 DSH provider 配置细节。

## 先选择正确的路径

| 目标 | 应做什么 | 分数是否可与维护者结果比较 |
| --- | --- | --- |
| 运行公开 smoke | 按 README 的五题流程准备本地数据并运行 | 可以比较五题 smoke 指标 |
| 复现同一 held-out 集 | 由维护者通过 Git 之外的受控渠道提供 manifest | 可以，前提是 manifest hash、数据、模型与配置相同 |
| 建立自己的独立 held-out 集 | 由受信任 evaluator 创建 custody root 并初始化 manifest | 不可以；新 manifest 有新的 hash 与题目选择 |

不要把 manifest 放入 Git、repo 的 `data/`、`.local/skill-evolve/`、普通 `runs/` 或 coding agent
workspace。也不要把 held-out 的逐题输出、prompt 或 session log 反馈给会继续改写 Skill 的 agent。

## 1. 准备本地依赖与数据

在 clone 目录中安装项目依赖：

```powershell
uv sync --all-groups
```

你还需要本机安装 DSH、Docker Desktop、Python 3.11+ 与 `uv`。从官方、获授权的来源准备完整的
LiveSQLBench Base-Lite 本地数据根；该目录不随 Git 分发，布局必须包含：

```text
<data-root>/public/livesqlbench_data.jsonl
<data-root>/gt/<one-jsonl-file>
<data-root>/dumps/base-lite/livesqlbench-base-lite-dumps/
```

首次只读检查不启动 Docker、DSH 或模型：

```powershell
uv run skill-evolve benchmark validate livesqlbench-base-lite <data-root>
```

## 2. 创建 evaluator custody root

选择一个**仓库和 `.local/skill-evolve/` 之外**的绝对目录。不要使用默认路径、repo 下的目录或 agent
workspace。下面仅为 Windows 示例；路径由评测者自行决定。

```powershell
$heldoutRoot = 'D:\evaluator-custody\livesqlbench-final'
New-Item -ItemType Directory -Force -Path `
  $heldoutRoot, `
  "$heldoutRoot\runs", `
  "$heldoutRoot\dsh-session-logs"
```

程序不会在 clone 后自动创建这个目录：这样可避免每个 clone 静默生成不同的最终题集，并避免误把“创建了
文件夹”当作操作系统访问控制。若评测与 coding agent 使用同一个 Windows 账户，隔离主要依赖工作流：不要向
agent 提供该目录、manifest 或其中的日志；需要对抗恶意本机进程时，再使用独立账户、ACL 或独立机器。

## 3. 为本次 held-out shell 配置 DSH

在**仅用于 held-out 评测**的 PowerShell 窗口设置以下变量。它们只影响该窗口及其子进程；关闭窗口后失效，
不会影响日常 smoke。

```powershell
$env:SKILL_EVOLVE_HELDOUT_ROOT = $heldoutRoot
$env:SKILL_EVOLVE_DSH_SESSION_LOG_ROOT = "$heldoutRoot\dsh-session-logs"
$env:DSH_HOME = "$heldoutRoot\dsh-session-logs"
```

`DSH_HOME` 必须指向 custody 中的 session root，否则 DSH 仍可能把原始 session 写到默认用户目录。由于
runner 会从 `$env:DSH_HOME\settings.yaml` 读取 provider route，请由评测者在该目录准备可工作的 DSH
`settings.yaml`：只保留所需的 `llm-*` provider route，并以 `apiKeyEnv` 之类的环境变量引用凭据。绝不可
写入 API key、token、password 或 Authorization 值。模型 provider/name 仍由传给 benchmark 的 DSH manifest
固定；settings 只负责 provider route。

例如，结构应类似下列无密钥形式；provider 名与变量名必须使用你本机 DSH 已验证的配置：

```yaml
llm-pi-ai:
  providers:
    <provider>:
      apiKeyEnv: <PROVIDER_API_KEY_ENV>
```

不要把上述变量或 custody 路径保存到仓库 `.env`、Git 或 coding agent 的长期环境中。

### Provider 凭据运行前检查

`enter-heldout.ps1` 或上面的手工设置会切换 `DSH_HOME`，但不会复制、导出或写入 provider 凭据。因此即使日常
DSH 已配置并可用，仍必须在这个 Held-out PowerShell 中确认 custody `settings.yaml` 所用 route 可以取到凭据。
若 route 使用 `apiKeyEnv`，其变量必须已存在于当前进程；此检查不显示密钥：

```powershell
$ProviderKeyEnv = '<the apiKeyEnv value from custody settings.yaml>'

if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($ProviderKeyEnv, 'Process'))) {
  throw "$ProviderKeyEnv is not available in this Held-out PowerShell session."
}
```

请通过组织认可的 secret manager 或操作系统用户级环境变量提供该值后，重新打开 Held-out PowerShell；不要把它
写进 repository、`.env`、manifest 或 `settings.yaml`。如果 route 使用 DSH credential store 而不是 `apiKeyEnv`，
请按照该 provider 的认证方式配置，并在明确授权真实运行前先做一次受控检查。

## 4. 获得或初始化 manifest

### 复现维护者的最终集

通过 Git 之外的受控渠道把维护者提供的 manifest 文件复制到 `$heldoutRoot`。该文件名必须是
`livesqlbench-base-lite-query-final-v1.json`。维护者应通过独立渠道提供预期 manifest hash；不要把它写入
issue、PR、repo 文档或 optimizer feedback。

### 创建独立最终集

仅由受信任 evaluator 执行一次：

```powershell
uv run skill-evolve benchmark initialize-heldout <data-root>
```

成功时 stdout 只会显示 suite ID 与 manifest SHA-256。manifest 一经创建不可原地覆盖；若数据不完整，命令会在
写入前失败。新的 hash 表示新的评测集，不能与维护者的既有 held-out 分数直接比较。

## 5. 无模型的运行前验证

确认 manifest、数据、三十题材料与 custody 配置一致：

```powershell
uv run skill-evolve benchmark validate `
  livesqlbench-base-lite-query-final-v1 <data-root>
```

安全输出只含 suite ID、manifest hash、总题数和每个难度的数量。若 manifest、路径、GT、dump 或 context 不一致，
命令会 fail closed，且不会启动 Docker、DSH 或模型。

## 6. 单题诊断运行

如果只需复现 held-out manifest 中一个已知的精确 `instance_id`，使用独立的单题命令。不要把 prompt、
数据库名或模糊标题传给 `--case`：

```powershell
$caseId = '<exact-instance-id>'

uv run skill-evolve benchmark run-heldout-case `
  <data-root> `
  <dsh-manifest> `
  --case $caseId
```

若要评测一个已验证的本地 candidate，使用完整命令：

```powershell
uv run skill-evolve benchmark run-heldout-case `
  <data-root> `
  <dsh-manifest> `
  --case $caseId `
  --candidate dbhub/005-semantic-core
```

该命令只启动这一题的 PostgreSQL fixture 和一次 DSH/模型调用。完整运行 artifact 写入：

```text
D:\skill-evolve-evaluator\heldout\single-runs\<YYYYMMDDTHHMMSSZ>-<instance_id>\
```

以上路径假设 `$env:SKILL_EVOLVE_HELDOUT_ROOT` 已设置为
`D:\skill-evolve-evaluator\heldout`。目录不可覆盖；若同一秒对同一题重复启动导致名字冲突，第二次运行会
fail closed。DSH 自身的完整 session 仍按安全配置写入 `$heldoutRoot\dsh-session-logs`，单题目录保存本次运行的
环境、model-selection、脱敏 runner artifact、evaluation、attribution、result 与 report。

stdout 只返回最小 diagnostic receipt，不显示 case ID、逐题 verdict、SQL、日志路径或 custody 绝对路径。
单题结果仅用于诊断，不是三十题 Final 分数，不生成 tier/macro 指标，也不能进入 optimizer、Smoke gate 或
candidate 晋级流程。

真实单题运行仍会启动 Docker、DSH 和模型并产生费用；必须在已核对 provider、凭据、模型、image 与预算后
由操作者显式执行。

## 7. 最终运行

确认模型、DSH provider、Docker image 和成本预算后，在同一个已配置的 held-out PowerShell 窗口运行。无 Skill
baseline 不传 `--candidate`：

```powershell
uv run skill-evolve benchmark run `
  livesqlbench-base-lite-query-final-v1 <data-root> <dsh-manifest>
```

该命令会启动每题独立的可销毁 PostgreSQL Docker fixture，并调用模型。输出只包含 suite/hash、candidate binding、
状态、分难度通过率、macro-average、总耗时及安全可得的总成本；manifest 内容、case ID、候选 SQL、逐题结果和
session 都保留在 custody root。

结果只应用于版本冻结后的里程碑判断；不要把它自动反馈到下一轮 Skill 改写。
