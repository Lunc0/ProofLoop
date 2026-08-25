# 本机配置与实验配置

## 文件职责

| 文件 | 是否提交 | 用途 |
|---|---|---|
| `examples/sql-agent/experiment.dsh.yaml` | 是 | 定义可比较的实验：模型标识、DSH profile、fixture、题集和 evaluator。 |
| `examples/sql-agent/.env.example` | 是 | 本机配置模板；不含任何秘密。 |
| `examples/sql-agent/.env` | 否 | 每台电脑自己的凭据、DSH 主目录和 executable 路径。 |

`experiment.dsh.yaml` 说明“评测什么”；`.env` 说明“这台电脑如何连接并启动 DSH”。不要将 API Key、
token、密码、连接串或生产数据库信息放入 manifest。

## 支持的本机变量

| 变量 | 读取者 | 是否进入 run artifact |
|---|---|---|
| `DEEPSEEK_API_KEY` | DSH | 否 |
| `DSH_HOME` | DSH | 否 |
| `SKILL_EVOLVE_DSH_BINARY` | skill-evolve | 是，作为非敏感 resolved DSH binary |

`SKILL_EVOLVE_DSH_BINARY` 的优先级是：进程环境变量 > manifest workspace 中的 `.env` >
`experiment.dsh.yaml` 的 `dsh.binary`。它用于处理 DSH 未加入 PATH 的电脑。

Python 不会加载或复制 `.env` 中的凭据；DSH 在 manifest workspace 中启动，自行按其 credential
provider 读取 `DEEPSEEK_API_KEY`、`DSH_HOME` 等变量。`dsh.profile`、模型标识、DSH version、
permission mode、可用 tool、PostgreSQL image digest、SQL policy 和 cases 仍必须固定在 manifest，
不能由 `.env` 覆盖。

## 新机器运行

1. 安装 DSH、Docker Desktop、Python 3.11+ 和 `uv`。
2. 复制 `examples/sql-agent/.env.example` 为同目录 `.env`，或者在 DSH credential store 配置 provider。
3. 必要时设置 `SKILL_EVOLVE_DSH_BINARY`。
4. 运行：

   ```powershell
   uv sync --all-groups
   uv run skill-evolve validate examples/sql-agent/experiment.dsh.yaml
   uv run skill-evolve run examples/sql-agent/experiment.dsh.yaml --suite fast
   ```

运行产生的 `manifest.resolved.yaml` 会保存解析后的非敏感 binary，但永远不会保存 `.env` 中的凭据。
