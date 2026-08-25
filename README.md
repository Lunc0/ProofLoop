# skill-evolve

`skill-evolve` is a local, auditable workflow shell for evaluating SQL Agent Skills. It freezes the environment, records runner evidence, executes objective PostgreSQL checks, and attributes a failed run to either the Skill or evaluator/data.

## v0.1 scope

- Manifest validation and SHA-256 environment fingerprints.
- Fake runner evidence capture: trajectory, candidate SQL, execution metadata and provider-reported cost.
- Disposable, read-only PostgreSQL fixture through Docker.
- Execution correctness, result equivalence and `fast` / `dev` / `held_out` slices.
- Conservative `skill` versus `eval_data` attribution, with regression drafts or repair checklists.
- Self-contained run artifacts and `rerun` from copied inputs.

真实 DSH runner 已支持 headless profile；并发调度、自动 Skill 修改与 champion promotion 仍明确延后。

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
