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

### 2.4.0 未发布前的判定修正

- 引文比较会忽略正文里的 Markdown 加粗、单词斜体、列表前缀和表格竖线。代码围栏里的 `*`、`<>` 保持原样。
- 关键项可以用同一段里的后续原文，或通用风险类的等价表述证明已保留。`Never: [...]` 这类空模板不进入关键集。模板里的 `must NOT change` 仍要保留。
- 动作和引文极性相反时不能通过。跨语言改写证据不足时忠实度是 UNKNOWN，整体不能 PASS。
- 提取后若关键句仍缺，会用缺口片段再请求一次。仍缺则 `complete: false`，缓存不复用。缓存信封带每批原始响应的摘要，不写密钥。
- 审计 JSON 用 `hallucination.checked` 说明幻觉检查有没有跑。没跑就不给 0。
- 实验规则在 `tests/fixtures/experiment_rules.jsonl`。新发现先加夹具再改代码。

### 兼容与取舍

- 下游若只判断 `coverage >= 90`，仍会看到原来的数字，但那不再等于放行。应改读 `verdict`，以及 `completeness.status` 和 `executability.status`。
- 跨卡相似句继续作为 info，不单独否决。重复的决策规则会让可执行性失败。
- 启发式降级不再被缓存成成功，所以 LLM 失败后每次提取都会重试。

## 2.3.0

- 统一发行版本号。
- 可选发布 Agent Skill 格式的 `SKILL.md`。
