# Spec: DSH SQL Skill Evolution CLI（v0.1）

> **Superseded:** v0.1 scope has been reduced to the Python workflow shell documented in [`docs/design/v0.1-workflow-shell.md`](../design/v0.1-workflow-shell.md). This earlier draft remains only as future-scope reference; its TypeScript references are obsolete and it is not an implementation specification.

## Objective

构建一个可在本地与 CI 中运行的命令行工具，为 DeepSeek Harness（DSH）中的 SQL Skill 提供可重复、可审计的评估与演进闭环。每次实验必须冻结模型、DSH profile / patch、工具、数据库 fixture 和 evaluator；记录完整执行证据；使用客观 SQL 评估归因失败；并以分层 gate 决定 candidate 是否可以成为 champion 或需要回滚。

首版服务于维护 SQL Agent Skill 的工程师。它不训练模型、不自动修改 Skill，也不以 LLM Judge 作为正确性的最终裁决。

### Success criteria

1. `validate` 拒绝缺失或不可哈希的环境、任务、评估器配置。
2. `run` 为每个案例写入不可变 run 目录和 SQLite 元数据：环境指纹、DSH session trace、生成 SQL、执行结果、token / 成本（若 DSH 事件提供）和 evaluator 版本。
3. 对至少一个 SQLite fixture，能够区分 `pass`、`sql_execution_error`、`wrong_result`、`unsafe_sql` 与 `environment_error`。
4. `gate` 按 fast → dev → held-out 的顺序阻止不合格 candidate 晋级；只有通过 held-out regression 才可 `promote`，并可 `rollback`。
5. CI 能运行验证与 fast suite，上传报告，并在 gate 失败时使工作流失败。

## Assumptions and open decisions

- DSH 是 DeepSeek Harness；首版把它当作外部、固定版本的 headless runner，而非直接导入其尚处 developer preview 的内部 API。
- DSH 的具体 headless 命令、profile 名称和 session-log 路径由 YAML 配置提供；测试使用可控的 fake runner，因此不依赖本机已安装 DSH。
- v0.1 的参考数据库为 **SQLite fixture**，只允许只读 `SELECT` / `WITH ... SELECT`。其 adapter 接口为 PostgreSQL 扩展预留，但 PostgreSQL 容器不属于 v0.1。
- case 的真值通过 `expected_query`（在同一冻结 fixture 上执行）或 `expected_rows` 表示。结果比较默认忽略行顺序，但比较列名、行数和规范化后的值。
- candidate 是一个由 Git ref / path / archive digest 识别的 Skill artifact；本工具只记录和评估它，不自动改写它。

**待确认：**如果目标生产库是 PostgreSQL、MySQL 或 Snowflake，应在 v0.1 就把对应方言作为参考数据库；不同方言的“等价 SQL”不能仅以 SQLite 结果代替。

## Tech stack

- Node.js 22 LTS、TypeScript 5、`tsx` CLI。
- YAML 配置、SQLite run registry、Node 原生 `node:test`。
- `dsh` 以外部进程方式调用；DSH 版本、profile bundle / patch 与 plugins 的精确版本或 Git SHA 均进入环境指纹。
- 不需要云服务、队列或 Web UI。

## Commands

```powershell
npm install
npm run build
npm test
npm run lint
npm run cli -- validate examples/sql-agent/experiment.yaml
npm run cli -- run examples/sql-agent/experiment.yaml --suite fast --candidate candidates/baseline
npm run cli -- gate --run <run-id> --stage fast
npm run cli -- promote --candidate candidates/baseline --run <run-id>
npm run cli -- rollback --to <champion-id>
```

运行真实 DSH case 前，操作者须自行安装并固定 DSH；工具不会在运行时下载或升级 DSH / plugins。

## Project structure

```text
src/
  cli.ts                 command registration and exit codes
  config/                YAML schema, validation, canonicalisation
  fingerprint/           stable SHA-256 manifests and artifact digests
  runner/                DSH external-process adapter and fake test runner
  sql/                   read-only guard, executor, result normalisation
  evaluator/             deterministic SQL evaluator and evidence model
  attribution/           ordered failure-attribution rules
  registry/              SQLite run / candidate / champion persistence
  gates/                 fast, dev, held-out rules; promote and rollback
  reporting/             JSON and Markdown reports
tests/
  unit/                  pure logic tests
  integration/           fake-DSH end-to-end fixture tests
examples/sql-agent/      runnable sample suite and SQLite fixture
.github/workflows/       validate and fast-gate CI workflow
docs/specs/              approved specifications
docs/plans/              approved implementation plans
```

## Configuration model

```yaml
schema_version: v1
experiment: sql-agent-quality
environment:
  dsh:
    binary: dsh
    version: 0.1.0-rc.6
    profile: sql-eval
    profile_patch: profiles/sql-eval.patch.yml
    runner_args: [headless, --json]
  model:
    provider: deepseek
    name: deepseek-v4
    params: { temperature: 0 }
  tools:
    - id: sql-readonly
      version: 1.0.0
  database:
    kind: sqlite
    fixture: fixtures/sales.db
    schema: fixtures/schema.sql
  evaluator:
    version: sql-result-v1
    result_order: ignore
    numeric_tolerance: 0
candidate:
  ref: skills/sql-analyst
suites:
  fast: [customer-count]
  dev: [customer-count, monthly-revenue]
  held_out: [top-product]
cases:
  - id: customer-count
    prompt: 每个国家有多少客户？
    expected_query: SELECT country, COUNT(*) AS customer_count FROM customers GROUP BY country
    policy: read_only
```

`environment_fingerprint` is the SHA-256 of canonicalised, resolved non-secret environment data plus the bytes / digests of declared profile patches, candidate artifact, database fixture, schema, evaluator version and tool manifests. Secret values are never written into YAML, command lines, fingerprints, logs or reports.

## Execution and evaluation

1. Resolve and hash every declared artifact before launching DSH.
2. Give DSH an isolated per-case workspace and a read-only database capability.
3. Capture the original DSH append-only session log and declared artifacts; derive SQL statements, tool results, timing, token usage and cost only from trace evidence. Missing cost is `unknown`.
4. Reject multiple statements and non-read-only SQL before executing it. Execute candidate SQL and the golden expectation independently, with limits and a timeout.
5. Normalise results deterministically, compare them, and emit pass/fail with evidence.
6. Attribute the first demonstrated cause in this precedence order: environment / evaluator validity → harness/tool → execution policy / generated SQL → result mismatch → unknown. A failed fixture integrity check invalidates the run instead of blaming a Skill.

## Code style

```ts
export function compareRows(actual: QueryResult, expected: QueryResult): Evaluation {
  const normalisedActual = normaliseQueryResult(actual);
  const normalisedExpected = normaliseQueryResult(expected);

  return deepEqual(normalisedActual, normalisedExpected)
    ? { status: "pass", evidence: { actual: normalisedActual } }
    : { status: "fail", reason: "wrong_result", evidence: { actual: normalisedActual, expected: normalisedExpected } };
}
```

- Strict TypeScript; no `any`, no implicit coercion, camelCase identifiers.
- Small pure functions with typed discriminated-union outcomes.
- Subprocess arguments use arrays, never a shell string; all subprocess output is treated as untrusted data.
- JSON report keys are stable and snake_case only at the external compatibility boundary.

## Testing strategy

- Unit tests: canonical hashing, YAML validation, SQL read-only guard, row normalisation, comparator, attribution precedence and gate rules.
- Integration tests: fake DSH runner emits representative JSONL trace; end-to-end runs cover pass, syntax error, unsafe SQL, wrong result and corrupt evaluator / fixture.
- CI runs `npm run lint`, `npm test`, `validate`, then `run --suite fast` against the fake runner. Real DSH / model credentials are never required in pull-request CI.
- A manual / scheduled protected workflow may run the real DSH dev and held-out suites with repository secrets and a pre-provisioned runtime.

## Boundaries

- Always: pin and fingerprint environment components; run deterministic tests; redact credentials; preserve raw trace evidence; keep held-out cases out of prompt-visible artifacts.
- Ask first: add database dialects, change evaluation equivalence semantics, add a cloud store, add a dependency, or run a real model / DSH command that consumes API credits.
- Never: execute write DDL/DML from candidate SQL; use production credentials or production databases; mutate a fixture during evaluation; promote on fast/dev results alone; modify held-out cases based solely on candidate failures.
