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
- Validation: run targeted checks by default; run the full test suite only when the user asks, a release is being prepared, or the change is cross-cutting.

## Conventions

- Use strict, typed Python and `snake_case` names.
- Persist only non-secret configuration and artifact evidence.
- Treat runner output and manifests as untrusted input.
- Use argv arrays and `shell=False` for subprocesses.

## Change classification and documentation

- Classify work before changing code. A **stage** introduces or changes a public contract, cross-module architecture, evaluation or candidate lifecycle, or a security/compatibility boundary. A **minor change** is a contained bug fix, refactor, test, documentation edit, or routine configuration adjustment. If unclear, treat it as a stage and ask.
- A stage follows `spec → plan → implementation → design`. Before technical planning or implementation, create a spec in `docs/specs/` with the goal, scope, non-goals, constraints, acceptance criteria, open questions, stage number, version, and status.
- Once the spec is approved and before coding, create its plan in `docs/plans/`: implementation approach, affected modules or files, ordered tasks, checkpoints, risks, and verification. Link it to the spec.
- After implementation and verification, create or update its design record in `docs/design/`: actual implementation, public contracts, safety or compatibility boundaries, and verification result. Do not present planned work as implemented.
- A minor change needs no new stage documents or index updates. Update an existing design record only when it changes an already documented public contract, safety boundary, or behavior that users need to understand.
- Name spec and plan files `<stage>-<version>-<slug>.md`, where `<stage>` is a zero-padded sequence such as `05`. If the version has not been approved, use `next` and state `版本：未定`; assign a version before creating the plan.
- For a stage, update the relevant `docs/*/README.md` indexes when its status changes or a stage document is added. Do not create a plan without an approved spec; do not create a design record for an unimplemented stage.
- Read documentation selectively: normally load only the relevant current-stage document, its matching index, and the source/tests in scope. Do not traverse historical `docs/` files unless the task needs them. Link to prior decisions instead of repeating their background or constraints.

## Boundaries

- Never connect evaluation code to production databases.
- Never execute candidate SQL with a privileged database role.
- Never infer missing token or cost information.
- Ask before adding a new SQL dialect, a real DSH integration, concurrency, or automatic Skill modification.
