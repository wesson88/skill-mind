# 实验规则夹具

`experiment_rules.jsonl` 是回归目录。每一行一条规则，`id` 稳定且唯一。

后续每次真实实验里新发现的误报、漏报或设计缺口，**先在这里加一条，再改判定**。只改代码、不补夹具，不算修完。

字段：

- `id`：稳定编号
- `origin`：实验出处。`unit-regression` 是原先单测里的规则，`deepseek-2.4.0-20261118` 是那次五篇真实运行
- `source_hash`：能对应到那次原文时填写，否则空字符串
- `source_excerpt`：判定用的原文片段，不是整篇
- `candidate`：草稿或候选片段
- `expect`：支持、缺失、忠实度或 verdict
- `kind`：`audit_false_positive`、`real_omission`、`design_gap`、`positive`、`negative`
- `fix`：这条规则约束的机制
- `evidence`：`mock_unit` 表示确定性夹具。`real_model` 不能出现在这个文件里，模型效果要单独标，不能当作回归通过

`expect.faithfulness = UNKNOWN` 是 REVIEW：跨语言或证据不够时不得判 PASS。

跑法：`python3 -m unittest tests.test_experiment_rules`
