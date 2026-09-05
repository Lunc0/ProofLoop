# 规格：移除 Evolution 文本的私有结果整行过滤

> **阶段：**30 · **版本：**v0.28 · **状态：**已完成
>
> **关联阶段：**[阶段 09：Agent 投影压缩](09-v0.8.1-agent-projection-evidence-compression.md)

## 1. 目标

移除 `_PRIVATE_RESULT_LINE_PATTERN` 对文本中 `gold`、`gt`、`expected` 与
`sql`、`query`、`result` 组合的整行替换。该规则会把 Candidate Skill 中
“不要推断 expected SQL”一类安全指令误判为私有评测材料，导致 agent input
的清洗文本与原始 Candidate 不同。

同时，agent input loader 不再要求 `evaluated_skill.content_sha256` 等于原始
Candidate 的 SHA-256，也不再要求 `payload.skill` 等于原始 Candidate 文本。
Candidate 标识、canonical payload 哈希、manifest 绑定、输入大小、环境指纹、
projection 版本与轨迹数量校验保持不变。

## 2. 范围

- 从共享 `_safe_text()` 删除 `_PRIVATE_RESULT_LINE_PATTERN` 及其整行替换。
- 删除 `load_prepared_evolution_input()` 中上述两项原始 Skill 内容相等性校验。
- 保持 `evaluated_skill.subject_id == subject.subject_id` 校验；Advice receipt
  仍记录被测 Candidate 的原始 SHA-256，作为 Candidate 谱系绑定。
- 保留凭据清洗、绝对路径清洗、文本大小限制及 agent projection 的结构化字段
  排除规则。
- 为“含 expected SQL 的 Skill 可形成有效 agent input”与“凭据/路径仍被清洗”
  添加离线回归测试。

## 3. 不在范围内

- 不重新解释或改写既有 run artifact，不重跑 Smoke、Evolution、DSH、Docker 或模型。
- 不修改 Candidate 内容、SkillMap、EditPlan、materialization、Smoke gate、
  evaluator、GT/Gold custody 或输出格式。
- 不增加依赖、SQL dialect、并发或自动 Candidate 修改。

## 4. 技术结构与风格

```text
src/skill_evolve/trajectory/compressor.py  -> shared text cleaning
src/skill_evolve/optimizer/evidence.py     -> canonical agent-input validation
tests/test_trajectory_compression.py       -> text-cleaning regression
tests/test_optimizer_service.py            -> sanitized-Skill loader regression
```

沿用 Python 3.11+、严格类型、snake_case 与 canonical JSON；不新增依赖。

## 5. 测试与验收标准

1. 含 “expected SQL” 的 Candidate Skill 原文保留在 agent input 中，不再被替换为
   `<private-evaluator-material-redacted>`。
2. 该 agent input 仍满足 manifest 的 canonical payload 哈希、大小、环境、版本、
   case-count 与 subject ID 校验，并可进入假的 optimizer client。
3. 当 agent input Skill 经仍保留的凭据或绝对路径清洗而与原始 Candidate 不同时，
   loader 不以此为由返回 `skipped_invalid_evidence`。
4. `api_key`、`password`、`secret`、`token`、`authorization`、Bearer token、
   `sk-`/`ds-` 密钥及既有 Windows/Unix 绝对路径的清洗测试继续通过。
5. 篡改 payload、manifest 哈希、环境指纹、projection 版本、case 数量或 subject ID
   仍会 fail closed，且不会调用 optimizer client。

计划验证命令：

```powershell
uv run pytest tests/test_trajectory_compression.py tests/test_optimizer_service.py
uv run ruff check src/skill_evolve/trajectory src/skill_evolve/optimizer tests/test_trajectory_compression.py tests/test_optimizer_service.py
uv run mypy src
git diff --check
```

## 6. 边界

- **始终：**保留凭据和绝对路径清洗；保持未列出的完整性校验 fail closed；只用离线测试。
- **先询问：**批准本规格、创建实施计划、运行真实模型/DSH/Docker 或推送。
- **绝不：**把凭据、held-out GT/Gold、私有 dump、原始 reasoning 或完整 session
  作为本阶段变更的一部分外发或写入 Git。

## 7. 开放问题

无。用户已明确要求全局删除该整行过滤，并取消原始 Candidate Skill 与清洗后
agent-input Skill 的内容哈希/文本相等性校验。
