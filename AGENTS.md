# Project: skill-evolve

## Tech stack

- Python 3.11+, `uv`, Typer, Pydantic, PyYAML and psycopg
- PostgreSQL fixture in a disposable Docker container

## Commands

- Install: `uv sync --all-groups`
- Lint: `uv run ruff check .`
- Type check: `uv run mypy src`
- Test: `uv run pytest`
- CLI: `uv run skill-evolve --help`

## Conventions

- Use strict, typed Python and `snake_case` names.
- Persist only non-secret configuration and artifact evidence.
- Treat runner output and manifests as untrusted input.
- Use argv arrays and `shell=False` for subprocesses.

## Boundaries

- Never connect evaluation code to production databases.
- Never execute candidate SQL with a privileged database role.
- Never infer missing token or cost information.
- Ask before adding a new SQL dialect, a real DSH integration, concurrency, or automatic Skill modification.
