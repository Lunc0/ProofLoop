# skill-evolve

`skill-evolve` is a local, auditable workflow shell for evaluating SQL Agent Skills. It freezes the environment, records runner evidence, executes objective PostgreSQL checks, and attributes a failed run to either the Skill or evaluator/data.

## v0.1 scope

- Manifest validation and SHA-256 environment fingerprints.
- Fake runner and real DSH evidence capture: redacted append-only session log, trajectory, candidate SQL,
  execution metadata and provider-reported usage.
- Disposable, read-only PostgreSQL fixture through Docker.
- Execution correctness, result equivalence and `fast` / `dev` / `held_out` slices.
- Conservative `skill` versus `eval_data` attribution, with regression drafts or repair checklists.
- Self-contained run artifacts and `rerun` from copied inputs.

真实 DSH runner 使用固定 headless profile 和 session-log root；每个 case 只看到专属 task
workspace、schema、明确声明的 context 和无凭据的本地只读查询 capability。并发调度、自动 Skill
修改与 champion promotion 仍明确延后。

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Docker Desktop running locally
- The pinned PostgreSQL image:

```powershell
docker pull postgres:16.6
```

## Try the example

```powershell
uv sync --all-groups
uv run skill-evolve validate examples/sql-agent/experiment.yaml
uv run skill-evolve run examples/sql-agent/experiment.yaml --suite fast
```

真实 DSH + DeepSeek 示例使用独立 manifest：

```powershell
Copy-Item examples/sql-agent/.env.example examples/sql-agent/.env
uv run skill-evolve validate examples/sql-agent/experiment.dsh.yaml
uv run skill-evolve run examples/sql-agent/experiment.dsh.yaml --suite fast
```

参见[本机配置说明](docs/configuration.md)：manifest 固定实验定义，`.env` 仅保存本机配置与凭据。

The command prints a run ID and writes evidence under `runs/<run-id>/`:

```text
manifest.resolved.yaml    copied, rerunnable input bundle
environment.json          fingerprint and component digests
raw/runner-output.json    original fake-runner output
trajectory.json           extracted trajectory, SQL and usage evidence
evaluation.json           expected versus candidate SQL verdicts
attribution.json          Skill or Eval/data conclusion
report.md                 concise human-readable summary
result.json               machine-readable terminal result
```

View or repeat a run:

```powershell
uv run skill-evolve show <run-id>
uv run skill-evolve rerun <run-id>
```

## Development checks

```powershell
uv run ruff check .
uv run mypy src
uv run pytest
```

See [the v0.1 technical design](docs/design/v0.1-workflow-shell.md) for contracts, safety boundaries and deferred scope.

## PostgreSQL Benchmark Adapter（v0.4）

推荐使用公开且可扩展的 `benchmark` 命令组。公开 smoke adapter
`livesqlbench-base-lite` 与 custody-only 最终 adapter
`livesqlbench-base-lite-query-final-v1` 都由仓库内受信任的 Python 代码静态注册。仅向 `data/` 放入一个目录
不会发现、import 或执行任何 adapter 代码。

```powershell
uv run skill-evolve benchmark validate livesqlbench-base-lite data/livesqlbench/base-lite
uv run skill-evolve benchmark fixture-check livesqlbench-base-lite data/livesqlbench/base-lite
uv run skill-evolve benchmark evaluate livesqlbench-base-lite data/livesqlbench/base-lite <runner-output.json>
uv run skill-evolve benchmark run livesqlbench-base-lite data/livesqlbench/base-lite examples/sql-agent/experiment.dsh.yaml
```

新增 adapter 必须先以可信的仓库代码实现并显式登记；其本地数据、GT 和 fixtures 必须保留在 Git 忽略的
目录。当前框架只支持 PostgreSQL，不会下载、导入或运行第二个数据集；本次实现验证也不会调用真实 DSH 或模型。

## LiveSQLBench Base-Lite（本地五题 Query，兼容入口）

本项目已接入固定的 `fast` 题集：`alien_2`、`polar_10`、`gaming_1`、`archeology_5`、`cross_db_8`。
数据必须保留在已被 Git 忽略的 `data/livesqlbench/base-lite/`：公开 Base-Lite 材料在 `public/`，获授权
的 GT/Test Cases JSONL 在 `gt/`，官方 PostgreSQL dumps 在 `dumps/`。不要把其中任何内容提交到仓库。

首次恢复官方 dump 使用其上游 PostgreSQL 版本：

```powershell
docker pull postgres:14.12
uv run skill-evolve benchmark validate livesqlbench-base-lite data/livesqlbench/base-lite
uv run skill-evolve benchmark fixture-check livesqlbench-base-lite data/livesqlbench/base-lite
```

`fixture-check` 会创建并删除一个本地容器；五个官方数据库分别恢复为容器内独立数据库，每题 reader
只能连接自己的数据库并且只有 `SELECT` 权限。它不会调用 DSH 或模型。

用已配置的真实 DSH 自动顺序运行五题、保存脱敏 session log 并评分：

```powershell
uv run skill-evolve benchmark run livesqlbench-base-lite data/livesqlbench/base-lite examples/sql-agent/experiment.dsh.yaml
```

DSH manifest 只提供非敏感的 DSH runtime 与模型标识；benchmark 不会读取其中的 cases 或 expected SQL。
每题的脱敏 session log 位于输出 run 目录的 `raw/dsh/<题目>.session.jsonl`。

已有 harness 产出的 `RunnerOutput` JSON 可用本地 Soft-EX 评分：

```powershell
uv run skill-evolve benchmark evaluate livesqlbench-base-lite data/livesqlbench/base-lite <runner-output.json>
```

该命令的 stdout 不包含 gold SQL、GT test cases 或 gold 查询结果；其评分实现了这五个 Query 题的
`conditions.order` 与官方 Soft-EX 的注释/DISTINCT/ROUND 和两位小数预处理兼容。它是本地回归分数，
不是官方 leaderboard 提交。

## LiveSQLBench 最终 Held-out（三十题）

`livesqlbench-base-lite-query-final-v1` 是版本冻结后的最终评测，不取代公开五题 smoke。它的 manifest、
逐题结果和 DSH session 必须留在仓库外的 evaluator custody root，因此不会随 Git clone 分发，也不会自动创建。

新评测者请先阅读[Held-out evaluator 操作手册](docs/heldout-evaluator.md)：它说明如何准备本地数据与 custody、
配置 DSH session、受控导入或初始化 manifest、运行无模型验证，以及在明确同意 Docker/模型成本后执行最终 run。

原有命令仍是同一实现的兼容入口，可在既有脚本中继续使用：

```powershell
uv run skill-evolve livesqlbench validate data/livesqlbench/base-lite
uv run skill-evolve livesqlbench fixture-check data/livesqlbench/base-lite
uv run skill-evolve livesqlbench evaluate data/livesqlbench/base-lite <runner-output.json>
uv run skill-evolve livesqlbench run data/livesqlbench/base-lite examples/sql-agent/experiment.dsh.yaml
```
