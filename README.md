# skill-evolve

`skill-evolve` 是一个本地、可审计的 SQL Agent Skill 评测与受控进化工具。它固定实验环境，保存 runner
证据，在一次性 PostgreSQL fixture 上执行客观检查，并将 Skill 修改限制在可验证、可回放的候选生命周期内。

## 当前状态

- 最新完成阶段：[`Stage 34 / v0.32`](docs/design/34-v0.32-dsh-submit-spill-safe-evaluation.md)。
- Python 包版本：`0.4.0`，定义在 `pyproject.toml`。
- `Stage / v0.x` 表示项目能力演进，不等同于 Python 包发布版本。
- 完整历史见 [Specs](docs/specs/README.md)、[Plans](docs/plans/README.md) 和
  [Design](docs/design/README.md) 索引。

## 当前能力

核心工作流为：

```text
manifest + candidate Skill
          ↓
隔离的 DSH task workspace
          ↓
只读 PostgreSQL fixture + SQL Harness Policy
          ↓
runner evidence + Soft-EX evaluator
          ↓
Smoke gate → Evolution evidence
          ↓
EditPlan 或隔离 Coding Agent
          ↓
不可变 draft child candidate
```

目前包括：

- manifest 校验、SHA-256 环境指纹和可重放的 run artifact；
- Fake runner 与真实 DSH session/trajectory 证据采集；
- 一次性、非特权、只读 PostgreSQL fixture；
- 固定五题、滚动 Smoke/Evolution 和 custody-only Final adapter；
- SQL Harness Policy 提供 `read`、`grep`、`sql_probe`、`sql_submit` 的固定工具闭集；
- 本地不可变 Skill candidate、lineage、选择状态和确定性 SkillMap；
- DeepSeek 结构化 EditPlan 路径；
- 隔离 Codex Coding Agent 原子 section 编辑路径；
- 有界、显式的 Smoke/Evolution/optimizer candidate cycle。

Candidate 生成后保持 `draft`。单独执行 optimizer 命令不会自动 benchmark、select、promote 或把 child
设为下一代 parent。

## 安全与证据边界

- 评测代码不得连接生产数据库；candidate SQL 只经 disposable fixture 的非特权只读角色执行。
- runner output、session、manifest 和 optimizer 输出均视为不可信输入。
- Gold、GT、私有结果和凭据不进入 candidate workspace 或公开报告。
- token/cost 缺失时不推断数值。
- optimizer 只能修改 Skill 中允许演进的 section；runtime 权限和 SQL Harness Policy 由受信任代码控制。
- 真实 DSH、模型、Docker benchmark 和付费 API 调用必须由操作者显式启动。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- 运行数据库评测时需要 Docker Desktop
- 对应 manifest 固定的 PostgreSQL image

安装并查看 CLI：

```powershell
uv sync --all-groups
uv run skill-evolve --help
```

## 快速开始

仓库内 fake 示例不调用真实模型：

```powershell
uv run skill-evolve validate examples/sql-agent/experiment.yaml
uv run skill-evolve run examples/sql-agent/experiment.yaml --suite fast
```

命令返回 run ID，并在 `runs/<run-id>/` 下保存解析后的 manifest、环境指纹、runner 证据、trajectory、
evaluation、attribution、报告和机器可读结果。

```powershell
uv run skill-evolve show <run-id>
uv run skill-evolve rerun <run-id>
```

真实 DSH 使用独立 manifest：

```powershell
uv run skill-evolve validate examples/sql-agent/experiment.dsh.yaml
uv run skill-evolve run examples/sql-agent/experiment.dsh.yaml --suite fast
```

凭据、DSH profile 和 workspace 配置见[本机配置说明](docs/configuration.md)。不要把凭据写入 manifest、
candidate 或 run artifact。

## Benchmark

Benchmark adapter 由仓库内受信任代码显式注册；仅向数据目录放入文件不会发现、导入或执行新 adapter。

```powershell
$DataRoot = 'D:\skill-evolve-evaluator\data\livesqlbench\base-lite'
uv run skill-evolve benchmark validate livesqlbench-base-lite $DataRoot
uv run skill-evolve benchmark fixture-check livesqlbench-base-lite $DataRoot
uv run skill-evolve benchmark evaluate livesqlbench-base-lite $DataRoot <runner-output.json>
uv run skill-evolve benchmark run livesqlbench-base-lite $DataRoot examples/sql-agent/experiment.dsh.yaml
```

`fixture-check` 和 `benchmark run` 会使用 Docker；`benchmark run` 还可能调用真实 DSH 和模型。固定五题、
Smoke、Evolution、Final、custody 目录和 candidate 绑定要求见
[LiveSQLBench evaluator 操作手册](docs/livesqlbench-evaluator-operations.md)与
[Held-out evaluator 操作手册](docs/heldout-evaluator.md)。

## Candidate 与 Skill 优化

```powershell
uv run skill-evolve candidate --help
uv run skill-evolve optimizer --help
uv run skill-evolve evolution --help
```

现有 Evolution run 可以显式进入两种优化路径：

```powershell
# 结构化 LLM EditPlan
uv run skill-evolve optimizer plan <run-id>
uv run skill-evolve optimizer materialize <run-id> --candidate-name <child-name>

# 隔离 Coding Agent 原子编辑
uv run skill-evolve optimizer agent-edit <run-id>
uv run skill-evolve optimizer agent-materialize <run-id> --candidate-name-prefix <prefix>
```

完整配置、artifact 和权限边界见[配置说明](docs/configuration.md)。

## 开发检查

日常修改优先运行定向检查；跨模块修改或发布准备时运行完整集合：

```powershell
uv run ruff check .
uv run mypy src
uv run pytest
```
