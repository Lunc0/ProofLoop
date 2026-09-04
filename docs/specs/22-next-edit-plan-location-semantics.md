# 规格：EditPlan 位置语义与问题-变更分层

> **阶段：**22 · **版本：**v0.20 · **状态：**已完成
>
> **前置阶段：**[阶段 13：结构化 Skill 与行为导向修改计划](13-v0.11-structured-skill-edit-plan.md) · [阶段 14：EditPlan 渲染、机械校验与不可变 Child Skill](14-v0.12-edit-plan-render-child-candidate.md) · [阶段 21：紧凑 SkillMap 与短编辑锚点](21-v0.19-compact-skill-map.md)
>
> **实施计划：**[阶段 22 v0.20 实施计划](../plans/22-v0.20-edit-plan-location-semantics.md)
>
> **实现记录：**[阶段 22 v0.20 实现记录](../design/22-v0.20-edit-plan-location-semantics.md)

## 1. 目标

消除模型把“改写既有 block”和“在 block 后插入”同时写进同一 edit 的歧义，并明确：

- **缺陷簇（cluster）按问题及跨 case 证据组织；**
- **编辑项（edit）按一次可确定渲染、不可重叠的文本变更组织。**

这使一个 edit 可以引用多个共同导致该变更的 cluster；同一 cluster 也可以由多个、位置互不重叠的 edit 落实。保持 renderer 的确定性和 fail-closed 边界。

## 2. 范围

仅影响新生成 EditPlan 的模型输出约束、prompt 和相应的严格合同测试。

新模型输出的落点组合固定如下：

| 操作 | `target_block_ids` | `placement_after_block_id` |
| --- | --- | --- |
| `add_rule` | 必须为空 | 必须恰好一个非 locked block |
| `clarify_rule`、`correct_rule`、`remove_rule`、`set_precedence`、`generalize_rule` | 必须恰好一个 block | 必须为空 |
| `merge_rules` | 两个或以上连续 block | 必须为空 |
| `reorder_workflow` | 必须恰好一个 workflow block；以 rule 文本替换整个 block | 必须为空 |

计划模型必须先形成跨 case cluster，再按不重叠的文本位置形成 edit：多个 cluster 的兼容修复落到同一位置时，归入同一 edit 的 `cluster_indices`；同一 cluster 的修复落到不同位置时，可由多个 edit 引用该 cluster。

## 3. 非目标

- 不取消跨 edit 的 target/anchor 不重叠校验。
- 不新增自动合并、编辑顺序、自由文本 patch 或 LLM 二次修复。
- 不改变历史 `SkillEditPlan` 的读取与 renderer 兼容行为；历史 `add_rule` 的 target fallback 继续可读。
- 不改变 candidate、materialize、评测、模型供应商、SQL dialect、依赖或 CLI。
- 不调用真实模型、DSH、Docker、数据库或 benchmark。

## 4. 约束与兼容性

1. 只有模型响应合同（`ModelSemanticEdit`）强制上述新组合；已持久化的 bound EditPlan 不做破坏性迁移。
2. binder 继续把 target 和 anchor 都作为 edit 占用位置，并拒绝跨 edit 重叠。
3. cluster 与 edit 的多对多关联仅验证 `cluster_indices` / bound `cluster_ids` 指向已有 cluster；cluster 是模型归因，不参与 edit 的 evidence、module、block 或 location 校验。
4. 输出仍严格拒绝未知字段、无效 edit location、locked block 与敏感文本；cluster 仅描述问题与证据，edit operation 不再按 cluster 的 defect type 限制；target contract 仅作为模型输入上下文，不要求或允许 cluster 输出 `contract_fields`。
5. 不记录或输出 GT、Gold、凭据、held-out identity、私有路径、原始 session 或完整 Skill。

## 5. 验收标准

1. `add_rule` 同时含 target 与 placement、或缺少 placement 时，在模型响应 schema 层被拒绝。
2. 改写类操作含 placement、缺少 target、或 target 数量不符合操作要求时，在模型响应 schema 层被拒绝。
3. 新 prompt 明确要求“cluster 按问题、edit 按不可重叠变更位置”，并要求共同落点的兼容 cluster 合并到一个 edit。
4. 一个 edit 可引用多个 cluster；一个 cluster 可被多个位置互不重叠的 edit 引用。cluster 到 edit 的反向关系由全部 edit 引用推导，binder 不再将 cluster 的 evidence、module 或 block 用作 edit 校验条件。
5. 两个 edit 复用任一 target 或 placement anchor 时仍 fail closed；错误信息能表明重叠位置。
6. 历史 bound plan 中 `add_rule` 使用 target fallback 的 renderer 测试继续通过。
7. 只运行相关单元测试、ruff、mypy 与 `git diff --check`；不运行完整测试或任何真实服务。

## 6. 预期改动范围

```text
src/skill_evolve/optimizer/edit_plan_contracts.py  # 新模型响应的位置组合校验
src/skill_evolve/optimizer/edit_plan_service.py    # prompt 的问题-变更分层说明与最小诊断
tests/test_edit_plan_contracts.py                  # 操作与落点组合的正反例
tests/test_edit_plan_service.py                    # 多 cluster/多位置绑定与重叠拒绝
```

## 7. 实施后验证

规格获准、计划完成且代码实施后，仅运行：

```powershell
uv run pytest tests/test_edit_plan_contracts.py tests/test_edit_plan_service.py tests/test_edit_plan_renderer.py
uv run ruff check src/skill_evolve/optimizer tests/test_edit_plan_contracts.py tests/test_edit_plan_service.py tests/test_edit_plan_renderer.py
uv run mypy src
git diff --check
```

## 8. 边界

- **始终：**模型输出 fail closed；保持 source/evidence/map/hash 绑定；保留不重叠的确定性渲染边界。
- **先询问：**规格批准、实施计划批准、任何真实模型调用、materialize 写入、评测、依赖变更或推送。
- **绝不：**自动合并语义不兼容的同块改动；让 renderer 按隐式顺序覆盖；泄露私有评测或凭据数据。

## 9. 开放问题

无。若未来需要把两种语义不兼容的操作应用到同一 block，应另立阶段定义显式的复合变换合同，而不是放宽重叠限制。
