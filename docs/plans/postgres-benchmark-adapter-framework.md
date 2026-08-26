# 实施计划：公开 PostgreSQL Benchmark Adapter CLI

## 决策

现在公开稳定、面向用户的 `skill-evolve benchmark` 命令组。其第一个注册 adapter 为
`livesqlbench-base-lite`。现有 `skill-evolve livesqlbench` 命令组保留为兼容入口，继续产生相同的
公开 stdout、artifact 布局和退出码。

本次不引入第二个数据集、新 SQL 方言、依赖、网络调用、真实 DSH 调用或模型调用。

## 兼容性约定

下列每一对命令的行为必须等价：

```text
skill-evolve benchmark validate livesqlbench-base-lite <data-root>
skill-evolve livesqlbench validate <data-root>

skill-evolve benchmark fixture-check livesqlbench-base-lite <data-root> [--image ...]
skill-evolve livesqlbench fixture-check <data-root> [--image ...]

skill-evolve benchmark evaluate livesqlbench-base-lite <data-root> <runner-output> [--image ...]
skill-evolve livesqlbench evaluate <data-root> <runner-output> [--image ...]

skill-evolve benchmark run livesqlbench-base-lite <data-root> <dsh-manifest>
skill-evolve livesqlbench run <data-root> <dsh-manifest>
```

通用命令仅新增显式 adapter ID。未知或格式错误的 adapter ID 必须以用法错误码 64 失败。两种命令均不得
打印或写入 Gold SQL、GT 测试用例或 Gold 查询结果。

## 实施步骤

1. 在 `benchmark/contracts.py` 新增小而严格的 typed adapter 合约。
   - 定义 `JsonObject`/公开结果类型、`BenchmarkSuite` 和 `BenchmarkAdapter` Protocol，以及统一的
     adapter 查找错误。
   - 合约只覆盖命令边界：`load_suite`、`fixture_check`、`evaluate` 和 `run_dsh` 仅返回公开元数据或
     已有的公开 run outcome。
   - 不让 case 或 Gold SQL 字段成为合约的一部分；adapter 实现自行在进程内持有其私有 case 类型。

2. 新增带显式静态注册表的 `benchmark/registry.py`。
   - 仅从可信的仓库代码注册 `livesqlbench-base-lite`。
   - 先校验小写 ID 格式；未知 ID 返回清晰错误。
   - 不从 `data/` 中读取模块、import 路径、shell 命令、数据库 URL 或 runner argv。

3. 在仓库代码中引入 `LiveSQLBenchBaseLiteAdapter` 门面。
   - 将当前的 validate、fixture-check、evaluate 和 DSH-run 编排移到该 adapter 后，但不改变 loader、
     fixture 映射、Soft-EX evaluator 或公开 serializer。
   - 保持每题一个可销毁 PostgreSQL fixture，以及已有的 runner
     `expected_query="SELECT 1"` 边界，确保 Gold SQL 永不进入 DSH。
   - 仅将确实通用的 PostgreSQL 编排提取至 `benchmark/postgres_runner.py`；LiveSQLBench 专属的数据结构、
     dump 规则和 Soft-EX 仍留在 `livesqlbench.py`。
   - 视迁移需要，在 `livesqlbench_run.py` 保留薄兼容 wrapper，使当前 import 与测试继续有效。

4. 在 `cli.py` 中新增 `benchmark` Typer 子命令。
   - 新命令解析 adapter 后委托给它。
   - 将 `livesqlbench` 子命令改为调用同一 adapter 方法的薄 wrapper，消除命令层重复校验，同时尽可能保持
     原有参数顺序与错误文本。
   - DSH manifest 仍由 CLI 边界加载，并继续要求现有的 `dsh.runner: dsh` 规则。

5. 增加行为与安全回归测试。
   - 注册表测试：准确解析 LiveSQLBench；拒绝未知/非法 ID；不存在动态模块加载路径。
   - CLI 测试：通用 `validate` 与旧命令得到相同公开结果；通用命令以错误码 64 报告 adapter 错误。
   - Adapter 测试：使用 stub 而非 Docker/DSH，保持已有的五题、逐题 fixture 与脱敏 session 证据行为。
   - GT 隔离测试：公开命令输出、生成的 agent workspace、runner output、evaluation、result 均不含 fixture
     的 Gold SQL。

6. 更新 README。
   - 将 `benchmark` 标为推荐的可扩展入口，并标出可用 adapter `livesqlbench-base-lite`。
   - 保留简短的旧 LiveSQLBench 命令示例，说明其兼容性定位。
   - 明确新增 adapter 需有可信的仓库代码及本地、被 Git 忽略的数据集；仅向 `data/` 放入目录不会使其可执行。

7. 不执行真实测评的验证。
   - `uv run ruff check .`
   - `uv run mypy src`
   - `uv run pytest`
   - 仅运行 CLI help/validation 测试；不启动 Docker、DSH 或模型。

## 文件级改动清单

```text
src/skill_evolve/benchmark/contracts.py       新增公开 adapter 边界
src/skill_evolve/benchmark/registry.py        新增固定 adapter 注册表
src/skill_evolve/benchmark/postgres_runner.py 仅在可安全提取时新增共享编排
src/skill_evolve/benchmark/livesqlbench*.py   LiveSQLBench adapter 迁移与兼容
src/skill_evolve/benchmark/__init__.py        受控导出
src/skill_evolve/cli.py                       通用命令组与旧命令委托
tests/test_benchmark_registry.py              注册表合约
tests/test_benchmark_cli.py                   通用命令合约
tests/test_livesqlbench_*.py                  兼容性与 GT 隔离回归覆盖
README.md                                     公开使用方式与数据边界
```

## 验收标准

- `skill-evolve benchmark --help` 显示 `validate`、`fixture-check`、`evaluate` 和 `run`。
- 四个通用命令均可使用 `livesqlbench-base-lite`，且不会泄露 GT。
- 现有 `livesqlbench` 命令继续可用，并在支持的参数范围内等价。
- 没有任何代码路径会 import 或执行 benchmark 数据目录中的代码。
- 既有的 DSH timeout/非零退出 session log 保留行为不受影响。
- 不运行真实 DSH 或模型的前提下，lint、严格 type check 与完整测试套件均通过。

## 任务拆分

### 任务 1：定义合约与固定注册表

新增最小化、带类型的 adapter 边界和可信仓库 adapter 的静态解析器。

- `BenchmarkAdapter` 表达 validate、fixture-check、evaluate、DSH-run 操作，但公开结果类型不含 Gold SQL。
- `livesqlbench-base-lite` 由显式注册表解析；未知或格式错误的 ID 确定性失败。
- 注册表不扫描或 import 数据 root 中的文件。
- 验证：新增注册表单元测试；`ruff` 与 `mypy` 通过。

预计文件：`benchmark/contracts.py`、`benchmark/registry.py`、`benchmark/__init__.py`、
`tests/test_benchmark_registry.py`。

### 任务 2：将 LiveSQLBench 包装进 adapter 边界

围绕既有的 loader、PostgreSQL fixture、evaluator 和 DSH-run 流程创建 adapter 门面，并保持兼容 import。

- validate、fixture-check、evaluate、run 继续使用当前 Base-Lite 语义。
- 每个 DSH case 仍使用独立可销毁 fixture，仅接收公开 context 与虚拟 `SELECT 1` expected SQL。
- 公开 artifact 布局、Soft-EX verdict 和失败 session 证据行为不变。
- 验证：现有 LiveSQLBench 导入、run 测试和新增 GT 隔离测试均仅使用 stub 通过。

预计文件：`benchmark/livesqlbench_adapter.py`、`benchmark/livesqlbench_run.py`、
`benchmark/livesqlbench.py`、相关 LiveSQLBench 测试；只有在可安全提取时才新增共享 runner 文件。

### 检查点：adapter 基础

- 任务 1–2 的测试与严格类型检查通过。
- 审阅公开类型，确认没有携带 GT 的字段越过 adapter/CLI 边界。
- 不发生 Docker、DSH 或模型调用。

### 任务 3：新增通用 CLI 并保留旧别名

注册公开的 `benchmark validate`、`fixture-check`、`evaluate`、`run`，并让旧 `livesqlbench` 子命令委托至同一 adapter。

- `skill-evolve benchmark --help` 列出四个子命令。
- 通用 LiveSQLBench `validate` 输出与旧命令相同。
- 通用和旧命令的非法输入均以退出码 64 失败且不打印 GT。
- 验证：通用 CLI、旧 CLI 测试和 `benchmark --help` 通过。

预计文件：`cli.py`、`tests/test_benchmark_cli.py`、`tests/test_livesqlbench_cli.py`。

### 任务 4：记录扩展边界并完成完整验证

在 README 中说明推荐的通用命令、旧命令兼容性以及可信 adapter 的扩展方式。

- README 标出唯一可用 adapter，并给出通用命令示例。
- README 明确：仅有数据目录不能注册或执行 adapter。
- 完整质量套件通过，且没有 data、GT、session log 或生成 run 目录加入 Git。
- 验证：`uv run ruff check .`、`uv run mypy src`、`uv run pytest`、`git diff --check`。

预计文件：`README.md` 与最终回归测试。

## 风险与缓解措施

| 风险 | 影响 | 缓解措施 |
| --- | --- | --- |
| 第二个 adapter 到位前合约过度泛化 | 中 | 合约保持最小且聚焦操作；不预设方言抽象。 |
| 重构改变旧命令输出 | 高 | 以同一个测试 fixture 运行通用与旧命令，并比较公开 JSON。 |
| Gold 数据跨越新抽象 | 高 | 使用仅公开的返回类型，加入明确的字符串泄露回归测试。 |
| 提取共享 runner 造成额外改动 | 中 | 仅提取不变的公共机制；否则保留有说明的 LiveSQLBench wrapper。 |

## 最终就绪清单

- [ ] 所有任务满足验收标准。
- [ ] 完整 lint、mypy 与 pytest 验证为绿色。
- [ ] 未连接真实 DSH/模型或生产数据库。
- [ ] 在任何 commit 或 push 前审阅改动；push 仍需单独、明确授权。
