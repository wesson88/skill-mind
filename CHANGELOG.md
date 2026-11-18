# Changelog

## 2.4.0

提取从“按章节复述”改为先保留关键约束，再写可执行规则。审计改为完整性与可执行性两道门，并可以冻结同一份检查清单。

### 提取

- Prompt `extract_v10`，schema `skill_unit_v2`。
- 技能类结果包含 `constraints` 与 `rules`（条件、判定、动作、验证、边界）。原文没有的槽位写 `unknown`，不编造。
- 不再要求每个章节 100-500 字解释，也不再把长列表整段抄进 explanation。
- 缓存键加入模型、分块大小、重叠、每批字符数、温度和 schema。参数变化即失效。
- 截断 JSON、失败批次和启发式降级写入 `complete: false`，不会被当作完整成功复用。
- 同名卡片跨批次合并约束和规则，不再丢掉后一批。
- 超长 chunk 不再被整段省略成一句注记。短片段里的代码占位符会保留，`Promise<Task>` 这类泛型不会在 HTML 清洗时被当成标签。

### 审计

- 完整性：覆盖率阈值仍是 90%，另外检查关键保留集。关键项缺失不能通过。
- 可执行性：规则是否可执行、是否有原文依据、是否重复。译文和口号不能自动通过。
- 没执行的门是 `UNKNOWN`，整体 verdict 不会因此变成 `PASS`。
- `coverage`、`verdict`、`missing`、`needs_human_review` 仍在。`verdict` 现在可以是 `UNKNOWN`。
- `--save-checklist` / `--checklist` 落盘并复用清单。清单带 source hash、prompt、schema，复审时 id 与 total 不变。
- JSON 给出 complete / weak / missing / total，以及清单来源。
- 旧草稿没有当前 schema，不能得到 `PASS`。

### 兼容与取舍

- 下游若只判断 `coverage >= 90`，仍会看到原来的数字，但那不再等于放行。应改读 `verdict`，以及 `completeness.status` 和 `executability.status`。
- 跨卡相似句继续作为 info，不单独否决。重复的决策规则会让可执行性失败。
- 启发式降级不再被缓存成成功，所以 LLM 失败后每次提取都会重试。

## 2.3.0

- 统一发行版本号。
- 可选发布 Agent Skill 格式的 `SKILL.md`。
