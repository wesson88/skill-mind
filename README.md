# 🧠 SkillMind

**开源 Claude Code Skill 知识提炼系统**

将 GitHub 上的 `SKILL.md` 提炼为可复用的知识与方法，保存为 Obsidian 笔记或 Agent Skill。Skill 提取采用“来源台账 → 跨章节重组 → 双向语义审计”，保留原理、理由、步骤、示例教训、例外与依赖，避免逐章英译中、照抄和无依据扩写。

## 2.5.1：语义提炼与有据审计

- Skill 使用封闭结构 `semantic_distillation_v1`，提取提示词升级为 `extract_v12`；其他来源类型仍使用 `extract_v11`，沿用原有提取行为。语义审计版本为 `semantic_audit_v3`。
- 最终正文可重新组织标题、合并跨章节的知识，不要求每个原章节都变成一段解释，也不强迫概念知识变成执行规则。必要命令、参数、数值保真，正文不反复附原文引句。
- 来源片段、内容要点、正文与来源的映射单独保存；未覆盖项必须声明。映射只证明关联存在，不代替语义支持核验。
- 用途描述原方法的目的与范围；通用读者措辞本身不等于新增能力，真实新增用途、前提或受众限制变化仍需阻断。原始技能名称可保留在来源证据或元信息中，无须另写一条名称知识；调用开关等行为信息仍须核验。
- 独立条件、否定、例外和封闭术语列表逐项保留，合并后仍明确各项限制的对象。不能用关联到整个台账单元代替内容完整。
- `audit` 分别检查台账是否漏掉原文、原文内容是否覆盖、产物主张是否有来源支持、正文是否真正重组提炼。Skill 不再凭某个覆盖率百分数放行；模型未完成、证据失效或不确定时返回 `UNKNOWN`，明确缺失/杜撰/仅复述则 `FAIL`。
- 完整、有效且参数一致的结果才能复用缓存。模型、生成参数、prompt/schema 变化会使旧结果失效；`extract --all` 强制重新请求。不完整结果不会自动批准。

负面语义判断必须提供 `counterexample` 对象，字段固定为 `source_span_ids`、`source_quote`、`output_quote`、`explanation`：引用实际来源片段，以原文和对应产物的真实摘录说明差异。只有缺失覆盖及台账遗漏可使用空的产物摘录。无效或缺少反例的判断为 `UNKNOWN`；另一项有据的 `FAIL` 仍优先阻断。摘录真实性校验分别在各自语言内进行，不要求中英文逐字对应，也不替代模型对含义的判断。

发布的 Markdown 旁有同名 `.evidence.json`，记录规范数据与实际文件 SHA256；两种格式都保留该证据。审计直接读取 JSON，不从 Markdown 反引号中猜原文。编辑 Markdown 后旧证据不再匹配，需要从审阅后的规范草稿重新发布；旧 Skill 笔记没有当前证据时审计为 `UNKNOWN`。

提取的默认资源预算继承 main 原实现：每批 12000 字符、请求超时 120 秒、最多 2 次重试、输出上限 16384 token、温度 0.2；审计继承 180 秒与 32768 输出 token。它们是请求预算，不是内容质量阈值，可通过 `llm` / `llm_profiles.extract` / `llm_profiles.audit` 中的 `batch_chars_per_request`、`timeout`、`max_retries`、`max_tokens`、`temperature` 调整。推理 token 可能占用输出预算，截断不会被修补成完整成功。展示覆盖率的 weak 权重 0.5 继承旧审计公式，仅作信息展示。

---

## ✨ 核心特性

- 📥 **采集**：从本地目录或 Git 仓库采集 `SKILL.md`，SHA256 哈希去重
- 🔬 **提取**：LLM 动态 Schema 提取（命令型/概念型/决策型），支持请求级缓存
- 📋 **审核**：草稿管理 → 人工编辑 → 发布，确保知识质量
- 📄 **渲染**：生成完全兼容 Obsidian Dataview 的 Markdown 笔记
- 🔍 **搜索**：可选集成 Chroma 向量库，支持语义搜索
- 🔄 **同步**：监听 Vault 变更，增量更新索引

---

## 🚀 快速开始

### 安装

```bash
pip install -e .
# 可选：启用语义搜索
pip install -e ".[search]"
```

### 配置 LLM

```bash
# 设置 API Key（以 OpenAI 为例）
export OPENAI_API_KEY="sk-..."

# 配置模型（默认：openai/gpt-4o-mini）
skillmind config --model openai/gpt-4o-mini

# 配置 Obsidian Vault 路径
skillmind config --vault ~/Documents/ObsidianVault
```

支持所有 litellm 兼容模型，例如：
- `openai/gpt-4o-mini`
- `anthropic/claude-3-haiku-20240307`
- `ollama/llama3`（本地模型）

---

## 📖 使用流程

### 1. 采集 Skill 文件

```bash
# 从 Git 仓库采集
skillmind ingest skill https://github.com/anthropics/claude-code-skills

# 从本地目录采集
skillmind ingest skill ~/my-skills-collection
```

### 2. 提取知识

```bash
# 提取所有缓存文件
skillmind extract

# 提取指定文件（前缀匹配）
skillmind extract --skill pg-upgrade

# 提取后自动批准
skillmind extract --auto-approve
```

### 3. 审核草稿

```bash
# 查看所有待审核草稿
skillmind review

# 编辑某个草稿
skillmind edit skill-8f3a1b2
```

### 4. 发布到 Obsidian

```bash
# 发布单个（默认写入 Obsidian Vault）
skillmind publish skill-8f3a1b2

# 批量发布全部
skillmind publish --all

# 可选：同时或只发布为 Agent Skill（<dir>/<name>/SKILL.md）
skillmind config --skill-dir ~/agent-skills
skillmind publish skill-8f3a1b2 --format skill
skillmind publish skill-8f3a1b2 --format both --skill-dir ~/agent-skills
```

### 5. 语义搜索（需安装 chromadb）

```bash
# 重建索引
skillmind sync

# 搜索
skillmind search "零停机升级数据库"
skillmind search "kubernetes 滚动部署" --top-k 10
```

### 6. 状态与更新检查

```bash
# 查看统计
skillmind status

# 检查上游仓库是否有更新
skillmind update --check
```

---

## 🗂️ 目录结构

```
~/.skillmind/
├── config.yaml          # 配置文件
├── hashes.yaml          # 已采集文件的 SHA256 记录
├── cache/
│   ├── repos/           # 克隆的 Git 仓库
│   ├── raw/             # 原始 SKILL.md 缓存
│   └── extract_cache/   # LLM 提取结果缓存（避免重复调用）
├── drafts/              # 待审核草稿（JSON）
├── vault/
│   ├── _drafts/         # Vault 内草稿区
│   └── skills/          # 已发布的 Markdown 技能卡片
└── chroma/              # 向量索引（可选）
```

---

## 📝 生成的 Obsidian 笔记格式

```markdown
---
uuid: skill-8f3a1b2
name: 零停机 PostgreSQL 大版本升级
type: ["command-oriented","decision-tree"]
tags: [postgresql, migration, zero-downtime]
status: published
draft: false
---

# 零停机 PostgreSQL 大版本升级

## 📌 一句话总结
...

## ⚠️ 难点注意
...

## 🧩 执行流程
...
```

完全兼容 Dataview 查询：

```dataview
LIST FROM #migration WHERE draft = false SORT created DESC
```

---

## ⚙️ 技术栈

| 组件 | 库 |
|------|-----|
| CLI 框架 | Typer + Rich |
| LLM 集成 | litellm |
| Markdown 解析 | 内置正则 + mistune |
| Git 操作 | GitPython |
| 向量搜索 | Chroma（可选） |
| 文件监听 | watchdog（可选） |

---

## 📜 许可证

MIT License
