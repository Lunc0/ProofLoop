# Spec: LiveSQLBench Base-Lite 五题 Query 导入

## Objective

将 LiveSQLBench Base-Lite 中五道公开 `Query` 任务作为本项目的本地、可审计评测集：
`alien_2`、`polar_10`、`gaming_1`、`archeology_5`、`cross_db_8`。

成功时，公开任务材料、私有 GT 和数据库 fixture 的来源可分别确认；Agent 只看到题目、schema 和
允许的 HKB/字段说明；评分器读取 GT，但不把 GT 交给 Agent 或提交到 Git。

## Tech Stack

- Python 3.11+、uv、Pydantic、PyYAML、psycopg 和 disposable PostgreSQL Docker fixture。
- 官方公开 Base-Lite 数据集；用户提供的 Base-Lite GT/Test Cases JSONL；官方 PostgreSQL dumps
  或等价的、可验证的预构建数据库镜像。

## Commands

```powershell
uv run ruff check .
uv run mypy src
uv run pytest
```

导入与离线评分命令：

```powershell
uv run skill-evolve livesqlbench validate data/livesqlbench/base-lite
uv run skill-evolve livesqlbench fixture-check data/livesqlbench/base-lite
uv run skill-evolve livesqlbench evaluate data/livesqlbench/base-lite <runner-output.json>
```

## Project Structure

```text
data/livesqlbench/base-lite/
  public/  # Hugging Face 的公开题目、schema、HKB、column meanings；Git 忽略
  gt/      # 用户取得的 sol_sql/test_cases；Git 忽略，Agent 不可见
  dumps/   # 官方 PostgreSQL 数据库 dump 或等价 fixture；Git 忽略
docs/specs/
  livesqlbench-base-lite-five-query-import.md
```

## Code Style

新增 Python 代码使用严格类型、`snake_case`、Pydantic 输入验证和 `subprocess` argv 数组：

```python
def select_query_instances(instance_id: str) -> bool:
    return instance_id in {"alien_2", "polar_10", "gaming_1", "archeology_5", "cross_db_8"}
```

## Testing Strategy

- 单元测试：GT 与公开 JSONL 按 `instance_id` 对齐；拒绝非 Query、重复 ID、缺失 GT、缺失数据库
  fixture 和未授权的任务可见文件。
- 集成测试：使用小型合成 fixture 验证五题 adapter 的 task workspace 不包含 gold SQL。
- 实机 smoke test：仅在五个对应数据库数据可用时，逐题创建隔离 PostgreSQL fixture，并执行
  `fast` suite；不调用真实 DSH 或模型，除非另行请求。

## Boundaries

- Always: 哈希公开数据、GT 与数据库 fixture；不把 GT 写入 Git、agent workspace 或普通报告。
- Ask first: 执行真实 DSH/模型、增加 Management/CRUD、改变评测语义、下载或运行未知数据库镜像。
- Never: 使用生产数据库、把候选 SQL 的写权限交给 Agent、执行上游 `test_cases` 的任意 Python 代码。

## Success Criteria

1. 本地 GT 文件包含并能唯一匹配五个指定 `instance_id`。
2. 每题的公开 prompt、schema、HKB/字段说明和数据库 fixture 均可定位并被内容哈希。
3. 任务工作区不含 `sol_sql`、`test_cases` 或 `external_knowledge` 的 GT 映射。
4. 缺失 dumps/镜像时导入或运行 fail closed，明确报告数据不完整。
5. 所有现有 lint、type check 和测试保持通过。

## Open Questions

None. The approved defaults are: official dumps, all five cases in `fast`, and both data import
and runtime fixture support without running real DSH.
