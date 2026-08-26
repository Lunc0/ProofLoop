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

## Stage documentation

- Treat a feature stage as `spec → plan → implementation → design`.
- At the start of a proposed stage, write its project document in `docs/specs/`. State the goal, scope, non-goals, constraints, acceptance criteria, open questions, stage number, version, and status. Do this before technical planning or implementation.
- Once the spec is approved and before coding, write the matching technical document in `docs/plans/`. State the implementation approach, affected modules or files, ordered tasks, checkpoints, risks, and verification. Link back to its spec.
- After the stage is implemented and verified, write or update the matching document in `docs/design/` to describe the actual implementation, public contracts, safety or compatibility boundaries, and verification result. Do not leave planned work presented as implemented behavior.
- Name spec and plan files `<stage>-<version>-<slug>.md`, where `<stage>` is a zero-padded sequence such as `05`. If the version has not been approved, use `next` and state `版本：未定`; assign a version before creating the plan.
- Update `docs/specs/README.md` and `docs/plans/README.md` whenever a stage changes status or a new stage document is added. Do not create a plan without an approved spec; do not create a design record for an unimplemented stage.

## Boundaries

- Never connect evaluation code to production databases.
- Never execute candidate SQL with a privileged database role.
- Never infer missing token or cost information.
- Ask before adding a new SQL dialect, a real DSH integration, concurrency, or automatic Skill modification.
