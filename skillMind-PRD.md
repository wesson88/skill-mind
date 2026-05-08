# SkillMind：开源 Claude Code Skill 知识提炼系统 完整设计文档 v2.1

## 1. 概述

**SkillMind** 是一款面向个人学习者的命令行工具，专注于从海量开源的 Claude Code Skill 中提炼结构化知识，并将其沉淀为可供 Obsidian 直接使用的 Markdown 笔记。工具以“本地化、无侵入、学习友好”为核心理念，帮助用户将社区分散的隐性经验转化为个人第二大脑中的永久资产。

与常见的“一键生成 Skill 再分发”思路不同，SkillMind 的核心价值在于：

- 打破开源 Skill 的数据孤岛，将隐藏在 `SKILL.md` 中的流程、决策与习惯提取为标准化知识单元。
- 完全本地运行、私有存储，不依赖任何云服务。
- 以 Obsidian Markdown 文件为唯一事实来源，用户可自由编辑、链接、扩展，而不会破坏知识库的索引与检索。
- 内置人机协作审核流程，确保入库知识的质量可控。

本文档是 SkillMind 系统的最终设计方案，包含整体架构、模块职责、数据流、CLI 命令设计以及工程实施路线。

---

## 2. 核心原则

| 原则 | 说明 |
|------|------|
| **本地私有** | 所有原始文件与提取结果均存放于本地环境，不上传第三方。 |
| **无侵入解析** | 只读开源 Skill 文件，不做任何修改，提取时保留溯源信息。 |
| **Markdown 为唯一事实源** | 知识库的持久化存储选为 Obsidian 兼容的 Markdown 文件，不引入额外数据库。 |
| **人机协作** | LLM 提取的结果需经用户审核（草稿状态）后方可入库，保证质量。 |
| **防止碎片化** | 通过知识簇锚点和上下文链接，保留完整工序视图，避免原子化导致迷失。 |
| **动态 Schema** | 根据 Skill 的类型（命令型、概念型、决策型）选择性提取字段，避免冗余。 |

---

## 3. 系统架构

SkillMind 采用分层、模块化的设计，整体流程如下：

```
┌──────────────────────────────────────────────────────────┐
│                    CLI 接口层 (Typer)                      │
│  ingest | extract | review | edit | publish | search | sync │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────┼─────────────────────────────────┐
│                    核心业务层                               │
├──────────┬──────────┬──────────┬──────────┬─────────────┤
│ Collector│ Parser   │ Extractor│ Reviewer │ Renderer    │
│ (采集+哈希去重)│ (区块分割)│(动态Schema+限流)│(草稿管理) │ (Markdown生成) │
└──────────┴──────────┴──────────┴──────────┴─────────────┘
                         │
┌────────────────────────┼─────────────────────────────────┐
│                   辅助基础设施层                             │
├──────────┬──────────┬──────────┬──────────┬─────────────┤
│Version   │ Dedup    │ Trace    │ Rate     │ Prompt VCS  │
│(Git+Prompt版本)│(语义去重)│ (溯源)   │ Limiter  │ (提示词版本管理) │
└──────────┴──────────┴──────────┴──────────┴─────────────┘
                         │
┌────────────────────────┼─────────────────────────────────┐
│              本地唯一事实源：Obsidian Vault                 │
│       ~/.skillmind/vault/  (与用户 Obsidian 库关联)        │
│       Markdown 文件 + YAML front matter + 双链             │
└────────────────────────┬─────────────────────────────────┘
                         │ (可选，后台监听变更)
┌────────────────────────┴─────────────────────────────────┐
│              可选向量索引缓存 (Chroma)                      │
│         纯粹为语义搜索服务，可随时删除重建                   │
└──────────────────────────────────────────────────────────┘
```

**关键设计决策**：

1. **不依赖 SQLite 或其他关系型数据库**，所有元数据、审核状态、哈希值、版本信息均作为 YAML front matter 直接存于 Markdown 文件中。
2. **Chroma 向量库为可选缓存层**，其数据完全派生自 Markdown 文件，用于支持语义搜索；即使其丢失，只需重新扫描 Vault 即可重建。
3. **人机协作闭环**：提取结果先进入草稿区，用户可通过 `review`、`edit` 命令介入修改，再 `publish` 到 Obsidian Vault，同时更新向量索引。

---

## 4. 模块详细说明

### 4.1 采集器（Collector）

**职责**：从本地目录或 Git 仓库中获取原始 `SKILL.md` 文件，完成文件级别的去重与版本记录。

- **输入源**：
  - 本地文件夹（递归扫描所有 `SKILL.md` 或 `*.skill.md`）。
  - Git 仓库 URL（自动浅克隆到 `~/.skillmind/cache/repos/`）。
  - awesome 列表（解析 README 中的链接）。
- **哈希去重**：对每个原始文件计算 SHA256，与已缓存的 Hash 值比对；相同者直接跳过。
- **版本标记**：记录 `source_repo`、`source_path`、提取时的 `commit_sha` 和 `fetch_time`。
- **输出**：原始文件的本地缓存副本。

### 4.2 解析器（Parser）

**职责**：将 Markdown 格式的 Skill 文件解析为方便提取的结构化抽象语法树（AST）。

- 分离 YAML front matter（若存在）与正文。
- 自动识别常见 Skill 章节，例如 `## Procedure`、`## Decisions`、`## Examples`，并按其进行区块划分。
- 对无标准标题的 Skill 采用启发式分块（基于空行、缩进、关键词 `IF/THEN` 等）。
- 输出统一的内部数据结构，供提取引擎使用。

### 4.3 提取引擎（Extractor）

**职责**：利用 LLM 将解析后的 Skill 文本转换为标准化的 JSON 知识单元。这是系统的核心智能模块。

**工作流程**：

1. **类型判别**：LLM 首先判断 Skill 的主要类型（可多标签）：`command-oriented`、`concept-explanation`、`decision-tree`、`troubleshooting`。
2. **动态 Schema 选择**：根据类型激活不同的提取字段组合：
   - **命令型**：重点提取 `procedure`、`command_snippets`、`preconditions`、`halt_conditions`。
   - **概念解释型**：重点提取 `plain_summary`、`knowledge_tags`、`pain_points`，`procedure` 允许为空。
   - **决策型**：重点提取 `decision_points` 和 `cross_references`。
3. **LLM 调用**：将分块文本与对应的 JSON Schema 一起送至 LLM，要求其严格按照 Schema 输出 JSON。
4. **结果合并**：LLM 返回的 JSON 与本地正则提取的命令、工具名等进行合并，冲突之处优先采用 LLM 结果（并记录日志）。
5. **输出**：一份包含学习增强维度（难点、通俗解释、技术标签）的完整 JSON 知识单元，其状态为 `draft`。

**提取结果 JSON 结构（示例）**：

```json
{
  "uuid": "skill-8f3a1b2",
  "source": {
    "repo_url": "https://github.com/example/skills",
    "file_path": "database/pg-upgrade/SKILL.md",
    "author": "Jane Doe",
    "updated_at": "2025-03-15",
    "commit_sha": "a1b2c3d4",
    "source_hash": "e99a18c4..."
  },
  "meta": {
    "name": "零停机 PostgreSQL 大版本升级",
    "type": ["command-oriented", "decision-tree"],
    "trigger_keywords": ["postgresql", "升级", "migration", "零停机"],
    "intent": "zero-downtime major upgrade PostgreSQL",
    "os": ["linux"],
    "tools_required": ["pg_upgrade", "docker"]
  },
  "preconditions": [
    "确保存在最新备份",
    "检查磁盘空间 > 20%",
    "确认所有副本集状态正常"
  ],
  "procedure": [
    { "seq": 1, "action": "安装新版本 PostgreSQL", "command": "apt install postgresql-16" },
    { "seq": 2, "action": "执行 pg_upgrade 检查", "command": "pg_upgrade --check ..." }
  ],
  "decision_points": [
    {
      "condition": "若数据库版本 >= 16",
      "then": "使用 pg_upgrade",
      "else": "采用 dump/restore 方案"
    }
  ],
  "halt_conditions": ["pg_upgrade 检查失败", "磁盘空间不足"],
  "rollback_actions": ["停止新实例，重新启动旧实例"],
  "cross_references": ["[[系统健康检查]]", "[[备份与恢复流程]]"],
  "learning_enhancement": {
    "pain_points": ["复制槽不会自动清理，需监控磁盘", "大表转换可能在 staging 环境锁表"],
    "plain_summary": "在不停机的情况下，通过 pg_upgrade 工具安全升级 PostgreSQL 主版本。",
    "knowledge_tags": ["PostgreSQL", "逻辑复制", "pg_upgrade", "零停机部署"]
  },
  "prompt_version": "extract_v2"
}
```

### 4.4 审核与发布（Reviewer）

**职责**：给予用户对提取结果的人工干预能力，保障入库知识质量。

- **草稿存放**：所有提取结果初始状态为 `draft`，保存为单独的 JSON 文件至 `~/.skillmind/drafts/`（或直接以草稿格式的 Markdown 存入 Vault 的 `_drafts` 目录）。
- **审阅命令**：`skillmind review` 列出待审核的草稿，支持按分数、来源筛选。
- **编辑命令**：`skillmind edit <uuid>` 使用默认编辑器打开对应 JSON，用户可修改任何字段，保存时自动执行 Schema 校验。
- **发布命令**：`skillmind publish <uuid>` 将审核通过的草稿渲染为最终的 Markdown 笔记，写入 Vault 正式目录，更新向量索引，并将其状态修改为 `published`。
- **自动批准选项**：提供 `--auto-approve` 批量快速入库，用于信任度高的来源。

### 4.5 渲染器（Renderer）

**职责**：将审核后的 JSON 知识单元转换为适用于 Obsidian 的 Markdown 文件。

**生成的 Markdown 格式**：

```markdown
---
uuid: skill-8f3a1b2
name: 零停机 PostgreSQL 大版本升级
type: ["command-oriented","decision-tree"]
intent: zero-downtime major upgrade PostgreSQL
tags:
  - postgresql
  - migration
  - zero-downtime
source_repo: https://github.com/example/skills
source_path: database/pg-upgrade/SKILL.md
source_hash: e99a18c4...
prompt_version: extract_v2
status: published
draft: false
difficulty: 3
created: 2025-06-15
updated: 2025-06-20
---

# 零停机 PostgreSQL 大版本升级

## 📌 一句话总结
在不停机的情况下，通过 pg_upgrade 工具安全升级 PostgreSQL 主版本。

## ⚠️ 难点注意
- 复制槽不会自动清理，需监控磁盘
- 大表转换可能在 staging 环境锁表

## 🧩 执行流程
1. 安装新版本 PostgreSQL → `apt install postgresql-16`
2. 执行 pg_upgrade 检查 → `pg_upgrade --check ...`

## 🔀 关键决策
- 数据库版本 >= 16：使用 pg_upgrade
- 否则：采用 dump/restore 方案

## 🛑 中止条件
- pg_upgrade 检查失败
- 磁盘空间不足

## ⏪ 回滚方案
停止新实例，重新启动旧实例。

## 🔗 关联知识
- [[系统健康检查]]
- [[备份与恢复流程]]

---
*来源：[原始 Skill](https://github.com/example/skills/blob/main/database/pg-upgrade/SKILL.md)*
```

此格式完全兼容 Obsidian 的 Dataview 插件，用户可轻松编写类似查询：
```dataview
LIST FROM #migration AND #zero-downtime WHERE draft = false SORT difficulty ASC
```

### 4.6 搜索与同步（可选）

**向量索引（Chroma）**：若用户希望保留命令行下的语义搜索功能，SkillMind 可后台维护一个 Chroma 数据库。该数据库完全派生自 Vault 中的 Markdown 文件，提供 `skillmind search "模糊自然语言"` 能力。

**同步引擎**：
- 当用户通过 `skillmind publish` 或直接在 Obsidian 中修改了笔记后，系统可在后台（通过 `watchdog`）或于下次执行命令时，感知文件变更并自动增量更新 Chroma 索引。
- `skillmind sync` 命令用于手动触发全量或增量索引重建。

---

## 5. 辅助基础设施

### 5.1 版本管理
- **Skill 来源版本**：基于 Git commit SHA，记录在 Markdown front matter 中。`skillmind update --check` 对比上游，提示需重提的 Skill。
- **Prompt 版本**：每次修改提取 Prompt 模板后，版本号递增。技能卡片会记录 `prompt_version`，`skillmind status` 自动提示“有 12 个 Skill 使用旧版 Prompt，建议重新提取”。

### 5.2 去重策略
- **文件级去重**：采集时计算原始文件 SHA256，完全一致者直接跳过。
- **语义去重**：基于 LLM 判断或向量相似度，将“意图”高度近似的 Skill 标记为“可能重复”，由用户在审核阶段决定保留哪个。

### 5.3 溯源标注
所有 Markdown 笔记页脚或 front matter 中均包含源仓库、路径、作者、最后更新时间，并能生成跳转链接，确保每条知识可回溯。

### 5.4 频率限制与缓存
- 对外部 LLM API 调用加入速率控制，支持配置 QPM。
- 请求级缓存：同一 `source_hash + prompt_version` 的提取结果会被缓存，避免重复消费。

---

## 6. CLI 命令一览

| 命令 | 说明 |
|------|------|
| `skillmind ingest <path\|url>` | 将本地目录或 Git 仓库中的 Skill 文件导入缓存区 |
| `skillmind extract [--skill <name>]` | 对缓存内的 Skill 执行知识提取，生成草稿 |
| `skillmind review` | 列出所有待审核的提取草稿 |
| `skillmind edit <uuid>` | 使用编辑器手动修改某个草稿 |
| `skillmind publish <uuid\|--all>` | 将审核通过的草稿发布到 Obsidian Vault |
| `skillmind search "<query>"` | 在知识库中进行语义搜索（需启用 Chroma） |
| `skillmind sync` | 重新扫描 Vault，更新向量索引 |
| `skillmind status` | 展示知识库统计信息与待更新提示 |
| `skillmind update --check` | 检查上游 Skill 仓库是否有更新 |

---

## 7. 数据流与运行时

**首次构建知识库的完整流程**：

1. **采集**：`skillmind ingest https://github.com/anthropics/skills` 将目标仓库克隆到缓存区。
2. **提取**：`skillmind extract` 解析所有 `SKILL.md`，调用 LLM 生成草稿 JSON。
3. **审核**：`skillmind review` 列出草稿，`skillmind edit <uuid>` 微调不满意的字段。
4. **发布**：`skillmind publish --all` 将草稿渲染为 Markdown 写入 Obsidian Vault 指定目录。
5. **检索**：`skillmind search "零停机升级"` 立即获得相关知识卡片。

**增量更新流程**：

- 上游仓库有更新：`skillmind update --check` 检测到新 commit，`skillmind extract` 仅处理变更文件。
- 用户修改 Obsidian 笔记后，`skillmind sync` 更新向量索引，保证搜索时效。

**知识碎片化的预防**：

- 每个 Skill 的步骤、决策、命令均保留在同一篇笔记中，并通过 `[[上下步骤链接]]` 构建内部导航。
- 搜索返回结果时，会附带笔记概览，并提供直接打开完整笔记的路径。

---

## 8. 技术栈建议

- **语言**：Python 3.11+
- **CLI 框架**：Typer + Rich（美观、易用）
- **文本解析**：PyYAML、mistune（Markdown AST）
- **LLM 集成**：litellm（统一接口，支持 OpenAI、Anthropic、本地模型）
- **向量索引（可选）**：Chroma（嵌入式运行）
- **Git 操作**：GitPython（克隆、diff）
- **文件监听**：watchdog（用于自动感知 Vault 变更）
- **打包分发**：pip installable package，或提供 Docker 一键部署

---

## 9. 工程实施路线

### 第一阶段：核心闭环（2 周）
- 实现 Collector、Parser、Extractor（固定 LLM 调用）的基础功能。
- 完成草稿 JSON 的生成与简单的审核（手动编辑 JSON）。
- 实现 Markdown 渲染与写入 Obsidian Vault。
- 提供 `ingest`、`extract`、`publish` 命令。

### 第二阶段：人机协作与学习增强（1 周）
- 完成 `review`、`edit` 命令行交互。
- 引入动态 Schema，生成学习增强字段（难点、通俗解释、知识标签）。
- 输出格式深度兼容 Dataview（双链、类型化标签）。

### 第三阶段：增量更新与去重（1 周）
- 版本管理与增量提取（Git diff 触发）。
- 基于 front matter `source_hash` 的跳过机制。
- 语义去重提示。
- CLI 命令完善，状态展示。

### 第四阶段：可选的语义搜索与社区发布
- 集成 Chroma，支持 `search` 和 `sync` 命令。
- 编写用户文档与示例工作流。
- 开源到 GitHub，接受社区贡献。

---

## 10. 总结与展望

SkillMind 重新定义了“学习开源 Skill”的方式——它不再是一堆零散的文件，而是一套持续生长、完全为你所用的结构化经验网络。通过以 Obsidian 为核心的极简架构，系统把学习的自主权完全交还用户：你可以像打理数字花园一样，随时修剪、链接、深化这些知识，而 SkillMind 仅作为得力的知识输入助手。

未来，SkillMind 还可以轻易扩展至：自动生成学习路线、将多个 Skill 的逻辑簇合并为高阶流程图、甚至反向回馈社区以标准格式输出规范化的 Skill 本身。但无论走多远，其根基始终不变：**本地、私有、可进化**——这正是个人知识管理的内核。