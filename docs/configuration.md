# 本机配置与实验配置

## 文件职责

| 文件 | 是否提交 | 用途 |
|---|---|---|
| `examples/sql-agent/experiment.dsh.yaml` | 是 | 定义可比较的实验：模型标识、DSH profile、fixture、题集和 evaluator。 |
| `examples/sql-agent/.env.example` | 是 | 本机配置模板；不含任何秘密。 |
| `examples/sql-agent/.env` | 否 | 每台电脑的非敏感 DSH executable / session-log 路径覆盖。 |

`experiment.dsh.yaml` 说明“评测什么”；`.env` 说明“这台电脑如何连接并启动 DSH”。不要将 API Key、
token、密码、连接串或生产数据库信息放入 manifest。

## 支持的本机变量

| 变量 | 读取者 | 是否进入 run artifact |
|---|---|---|
| `SKILL_EVOLVE_DSH_BINARY` | skill-evolve | 是，作为非敏感 resolved DSH binary |
| `SKILL_EVOLVE_DSH_SESSION_LOG_ROOT` | skill-evolve | 是，作为非敏感 resolved DSH session-log root |
| `SKILL_EVOLVE_HELDOUT_ROOT` | Final adapter | 否；仅指向仓库外 Final custody |
| `SKILL_EVOLVE_ROTATION_ROOT` | 滚动 adapter | 否；仅指向仓库外 rolling custody |

前两个 DSH 覆盖项的优先级都是：进程环境变量 > manifest workspace 中的 `.env` > manifest 值
（session-log root 未声明时默认 `%USERPROFILE%\\.dsh\\sessions`）。两个 custody root 只应在可信
评测者当前 PowerShell 设置，并且不能与 repository、candidate store 或彼此重叠。

Python 不会加载或复制 `.env` 中的凭据。真实 DSH case 在 run 专属 task workspace 中启动，避免
Agent 读取 manifest 的 expected query、seed 或 fake result；因此应将 provider credential 配置在
DSH credential store（推荐）或由操作者预先设置在进程环境中，而不要放入 manifest workspace 的
`.env`。`dsh.profile`、模型标识、DSH version、permission mode、可用 tool、PostgreSQL image digest、
SQL policy 和 cases 仍必须固定在 manifest，不能由 `.env` 覆盖。

## DSH 模型选择

DSH 用户目录中的 `settings.yaml` 可以保存交互式默认模型，并且它的
`agent-default-model` 设置会覆盖 profile 的默认值。为保证评测实际使用 manifest 的
`environment.model.provider` 与 `environment.model.name`，runner 会在每个 run 创建一个短生命周期的
settings 文件，并将它作为最后一个 DSH `--patch` 传入。该文件只保留用户 settings 中经检查的 `llm-*`
provider route 配置，再写入 manifest 的 `agent-default-model`；交互式默认模型和无关 UI 设置不会继承。
故你的全局默认模型仍可用于手工 DSH，但不会再覆盖评测；已在 DSH 中配置的 OpenAI 等 provider route
仍可使用，credential store 也不会被复制或重定向。

provider route 必须使用 `apiKeyEnv` 这类环境变量引用，不能把 API key、token、password 或 Authorization
值写进 DSH settings。runner 发现此类明文 credential 字段会在启动 DSH 前安全暂停。

每个 case 的 `raw/dsh/<case>.invocation.json` 会记录实际强制的 provider/model，旁边的
`<case>.model-selection.yaml` 和 `<case>.model-selection.patch.yml` 则保存这次使用的无密钥覆盖内容。
在当前 DSH `0.1.1-rc.2` 的 headless public protocol 中，这个覆盖只负责 provider/model 选择；
`environment.model.params` 仍会进入环境指纹，但不能被视为已传递给 DSH provider 的请求参数。

## 新机器运行

1. 安装 DSH、Docker Desktop、Python 3.11+ 和 `uv`。
2. 复制 `examples/sql-agent/.env.example` 为同目录 `.env`，或者在 DSH credential store 配置 provider。
3. 必要时设置 `SKILL_EVOLVE_DSH_BINARY`。
4. 运行：

   ```powershell
   uv sync --all-groups
   uv run skill-evolve validate examples/sql-agent/experiment.dsh.yaml
   uv run skill-evolve run examples/sql-agent/experiment.dsh.yaml --suite fast
   ```

运行产生的 `manifest.resolved.yaml` 会保存解析后的非敏感 binary，但永远不会保存 `.env` 中的凭据。

## 最终 Held-out evaluator

最终三十题评测使用独立的 `SKILL_EVOLVE_HELDOUT_ROOT`，并要求 DSH session root 位于其中；它与固定五题
或滚动测评的普通 `runs/` 不同。滚动 Smoke/Evolution 另使用 `SKILL_EVOLVE_ROTATION_ROOT` 保存 reserve、
manifest 和 Smoke gate。完整的目录布局与命令见
[LiveSQLBench evaluator 操作手册](livesqlbench-evaluator-operations.md)；Final 细节见
[Held-out evaluator 操作手册](heldout-evaluator.md)。不要把 custody 路径、manifest 或凭据写入仓库 `.env`。

## LiveSQLBench Base-Lite 本地数据

LiveSQLBench 数据根应位于仓库外。当前本机布局使用
`D:\skill-evolve-evaluator\data\livesqlbench\base-lite\`，不能放在普通 experiment manifest、runner
workspace 或 `runs/` 中：

| 目录 | 内容 | 可见性 |
|---|---|---|
| `$DataRoot\public\` | 官方公开题目、schema、HKB、字段说明 | Agent 可使用 |
| `$DataRoot\gt\` | 获授权的 `sol_sql`、知识映射、Test Cases | 仅本地 evaluator |
| `$DataRoot\dumps\` | 官方 PostgreSQL table dumps | 仅本地 fixture |

校验、恢复与离线评分依次为：

```powershell
$DataRoot = 'D:\skill-evolve-evaluator\data\livesqlbench\base-lite'
uv run skill-evolve benchmark validate livesqlbench-base-lite $DataRoot
uv run skill-evolve benchmark fixture-check livesqlbench-base-lite $DataRoot
uv run skill-evolve benchmark run livesqlbench-base-lite $DataRoot examples/sql-agent/experiment.dsh.yaml
uv run skill-evolve benchmark evaluate livesqlbench-base-lite $DataRoot <runner-output.json>
```

`fixture-check` 和 `evaluate` 需要 Docker Desktop 与本地 `postgres:14.12` 镜像。若数据或 dump 缺失、GT
不匹配，命令会失败并不产生评测产物。测试用例内的任意 Python 不会被执行。

自动 `run` 是串行的：每题在新的 disposable PostgreSQL 14.12 容器中运行，DSH 只获得该题的公开
schema、HKB/字段说明和本地只读查询能力。每题结束后，来自 DSH session-log root 的日志会先脱敏，再保存
为 `runs/<run-id>/raw/dsh/<题目>.session.jsonl`；GT 不会进入 DSH workspace、runner output 或评测报告。

固定五题、Smoke、Evolution 和 Final 的带/不带 candidate 命令见
[LiveSQLBench evaluator 操作手册](livesqlbench-evaluator-operations.md)。
