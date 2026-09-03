# 本机配置与实验配置

## 文件职责

| 文件 | 是否提交 | 用途 |
|---|---|---|
| `examples/sql-agent/experiment.dsh.yaml` | 是 | 定义可比较的实验：模型标识、DSH profile、fixture、题集和 evaluator。 |
| `.env.example` | 是 | 根目录本机配置模板；不含任何秘密。 |
| `.env` | 否 | 根目录的本机 DeepSeek optimizer 凭据；也可在 manifest workspace 放置非敏感 DSH 路径覆盖。 |

`experiment.dsh.yaml` 说明“评测什么”。不要将 API Key、token、密码、连接串或生产数据库信息放入
manifest；本地 optimizer 凭据只能放在根目录 Git 忽略的 `.env` 或进程环境变量。

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

### SQL benchmark 的 DSH Web 工具

仓库中的 `examples/sql-agent/experiment.dsh.yaml` 通过同目录
`dsh-no-web.patch.yml` 将 DSH `tool-web` 的 `search` 与 `fetch` 都设为 `false`。因此经该示例 manifest
启动的 headless DSH 不会向模型注册 `web_search` 或 `web_fetch`；它不能通过该官方工具检索
LiveSQLBench、Hugging Face 或其他公开 benchmark 材料。

`environment.tools` 不是 DSH runtime 工具 allowlist，不能单独用于关闭 DSH 内建工具。真实 Final 的私有
manifest 不随 Git 分发，受信任 evaluator 必须在其受控 manifest workspace 中复制并显式引用同一 patch。
该 patch 只删除 DSH web 工具，不限制 `pwsh` 的网络访问，也不隔离调用账户可读取的本机文件；这些边界仍需
OS 访问控制和网络出口策略。

## Evolution 自动优化建议（v0.9）

仅 Evolution 可在完成评测后自动调用一次无工具的 DeepSeek 优化器；Smoke、Final、held-out、固定题集和
runner/evaluator 无效的运行绝不会调用它。需要在该运行使用的 manifest 中显式启用：

```yaml
environment:
  optimizer:
    enabled: true
    provider: deepseek
    model: deepseek-v4-flash
    api_key_env: DEEPSEEK_API_KEY
    max_input_bytes: 1572864  # 1.5 MiB
```

`api_key_env`（DSH settings 中常写作 `apiKeyEnv`）是环境变量**名称**，不是密钥值。优化器按以下顺序
读取 `DEEPSEEK_API_KEY`：启动 `skill-evolve` 的非空进程环境变量、当前工作目录 Git 忽略的 `.env`。
它只读取这一键到内存，不修改 `os.environ`，也不会将值写入 manifest、run artifact、candidate store、
异常或 stdout。未启用或该变量不可用时，Evolution 评测仍照常结束，run 只写 optimizer 的 `unavailable`
receipt，不会进行网络请求。

首次使用时，在仓库根目录执行：

```powershell
Copy-Item .env.example .env
```

随后使用本机编辑器在 `.env` 填写 `DEEPSEEK_API_KEY` 的值。此 `.env` 只供本地 optimizer 使用；DSH
评测的 provider 凭据仍应保留在 DSH credential store 或受控进程环境，不能放入 manifest workspace。

在有效失败 evidence 可用时，程序只向 DeepSeek 提供已校验的
`agent-input/trajectory.json`（`evolution-coding-agent-input-v3`），不提供 `raw/dsh/`、原始 stderr、GT、
Gold、custody 路径或任何凭据。建议产物保留在该次 run 的 `optimizer/advice.json`、`optimizer/advice.md`
与 `optimizer/receipt.json`；`result.json` 仅记录状态、相对路径和非机密的 provider/model/prompt 标识。
建议不会改写、导入、选择或晋级 candidate。

若某次 Evolution 已完成，也可明确指定其 run ID 生成一次建议，而不重跑评测：

```powershell
uv run skill-evolve optimizer advise <run-id>
```

该命令只接受当前工作目录 `runs/` 下的已完成 Evolution run；会验证其固化 subject 和 v3
agent input，并更新该 run 的 `optimizer/` artifact 与 `result.json` 摘要。显式命令本身即是
调用授权，所以不要求原始 manifest 曾设置 `optimizer.enabled: true`，但仍需要进程环境中的
`DEEPSEEK_API_KEY` 或当前工作目录 `.env` 中的同名值。它不执行 DSH、Docker、数据库、evaluator 或 candidate 操作，也不在
stdout 输出 Skill、evidence 或建议正文。

## 结构化 Skill 修改计划（v0.11）

阶段 13 在 v0.9 advice 之外增加一个显式、非自动的结构化计划命令：

```powershell
uv run skill-evolve optimizer plan <run-id>
```

该命令只接受当前工作目录 `runs/` 下已经完成的
`livesqlbench-base-lite-evolution-v1` run。它验证固化 subject、canonical
`evolution-coding-agent-input-v3`、Skill SHA-256 和受信任 target contract；不会重跑 DSH、数据库、
evaluator 或 benchmark。命令本身代表一次真实 optimizer 调用授权，因此仍需要
`DEEPSEEK_API_KEY`，但不会改变 manifest 或 rotation 的 v0.9 自动 advice 配置。

模型只提出缺陷簇和有限种语义修改。Skill map、block ID、修改权限、target contract、来源 hash、
cluster/edit ID 和最终计划状态都由本地代码生成或绑定。最多产生三个非重叠 edit；`locked` 区域只能形成
`findings_only`，v0.11 对 `guarded` 区域会记录历史兼容状态 `requires_guarded_approval`。阶段 14 不再把该状态
解释为人工审批门槛，而是以 target binding 与 diff 机械校验决定能否渲染。该阶段不输出机械 patch，不修改
`SKILL.md`，也不创建、选择、晋级或回退 candidate。

成功或纯 finding 的产物位于：

```text
optimizer/edit-plan/
  skill-map.json
  edit-plan.json
  edit-plan.md
  receipt.json
```

无效 evidence、模型失败或非法返回只保留该子目录的 terminal `receipt.json`。根 `result.json` 使用独立
`optimizer_edit_plan` 摘要键记录状态、相对路径、hash 和数量，不覆盖 v0.9 的 `optimizer` advice 摘要；
stdout 不输出 Skill、任务内容、evidence 或计划正文。

## EditPlan 渲染与 Child Skill（v0.12）

对已有的、完成的 Evolution run，可显式将其 EditPlan 渲染为不可变 draft child：

```powershell
uv run skill-evolve optimizer materialize <run-id> --candidate-name <child-name>
```

命令不调用模型、不运行 DSH、数据库或 benchmark。它验证 parent candidate 与 run subject 的绑定，渲染
`SKILL.md` patch，并在本地 candidate store 原子创建 child。`guarded` 区块不再需要人工审批；历史
`requires_guarded_approval` artifact 在读取时等价处理为 `proposed`，`locked` 区块始终拒绝。

成功的 run artifact 位于 `optimizer/materialization/`，包含 `rendered.patch` 和 `receipt.json`；根
`result.json` 的 `optimizer_materialization` 及 stdout 只输出安全摘要。新 child 保持 `draft`，不会自动评测、选择、
晋级或回退。

## 新机器运行

1. 安装 DSH、Docker Desktop、Python 3.11+ 和 `uv`。
2. 如需 Evolution optimizer，复制根目录 `.env.example` 为 `.env`，在本机填写 `DEEPSEEK_API_KEY`；DSH
   provider 则配置在 DSH credential store。
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
[Held-out evaluator 操作手册](heldout-evaluator.md)。Final 切换独立 `DSH_HOME` 后，必须在同一个 PowerShell
检查 custody provider route 所需的 `apiKeyEnv` 已存在；不要把 custody 路径、manifest 或凭据写入仓库 `.env`。

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
