# SkillMind：全源知识提炼系统 完整设计文档 v2.2

## 1. 定位与概述

**SkillMind** 是一款面向个人学习者的命令行工具，以**本地 Obsidian 库为唯一事实来源**，将任意来源（开源 Skill、技术博客、论坛问答、RSS 订阅等）的非结构化经验，通过人机协作转化为结构化、可关联、可检索的学习笔记。

> **v2.2 定位升级**：从"Skill 提炼工具"进化为"全源知识中枢"。核心能力不变——"将任何形式的经验陈述转化为可执行的、可关联的结构化笔记"——加入博客和论坛支持，本质上只是为这台知识压榨机增加了新的进料斗。

与常见的"一键生成 Skill 再分发"思路不同，SkillMind 的核心价值在于：

- **打破数据孤岛**：将隐藏在 `SKILL.md`、博客文章、论坛帖子中的流程、决策与习惯提取为标准化知识单元。
- **完全本地私有**：不依赖任何云服务，所有数据存于本地。
- **Markdown 为唯一事实源**：以 Obsidian Markdown 文件为持久化存储，用户可自由编辑、链接、扩展。
- **人机协作审核**：确保入库知识质量可控，LLM 提取结果须经用户确认。

---

## 2. 核心原则

| 原则 | 说明 |
|------|------|
| **本地私有** | 所有原始文件与提取结果均存放于本地，不上传第三方。 |
| **无侵入解析** | 只读源文件，不做任何修改，提取时保留完整溯源信息。 |
| **Markdown 为唯一事实源** | 知识库持久化存储为 Obsidian 兼容的 Markdown 文件，不引入额外数据库。 |
| **人机协作** | LLM 提取结果须经用户审核（草稿状态）后方可入库，保证质量。 |
| **防止碎片化** | 通过知识簇锚点和上下文链接，保留完整工序视图；一文多卡时自动建立父文档链接。 |
| **动态 Schema** | 根据文档类型（Skill/博客/论坛/网页）和内容类型选择性提取字段，避免冗余。 |
| **信噪比治理** | 对 Web 来源清洗正文噪声，并标注来源可靠性与过时风险。 |

---

## 3. 总体架构

```
CLI 命令层 (ingest / extract / review / edit / publish / search / sync)
        │
        ├── 采集层 (Collector 插件)
        │     ├── SkillCollector:  Git 仓库 / 本地 SKILL.md
        │     ├── BlogCollector:   RSS Feed / 单篇 URL / Pocket
        │     ├── ForumCollector:  Discourse API / Reddit / HN
        │     └── WebCollector:    通用 URL (Crawl4AI/Firecrawl → Markdown)
        │
        ├── 预处理 & 清洗 (Cleaner)
        │     └── 去除广告、侧边栏、无关评论，保留正文和代码块
        │
        ├── 提取引擎 (Extractor)
        │     ├── 动态 Schema & Prompt 路由 (根据 doc_type 选择策略)
        │     ├── LLM 提取 (结构化 JSON，含可靠性 & 过时风险标注)
        │     └── 一文多卡拆分 (长文 → 多个独立笔记)
        │
        ├── 审核发布 (Reviewer + Renderer)
        │     └── 草稿 → 人工编辑 → 发布为 Markdown 笔记 (with front matter)
        │
        └── 存储与同步
              ├── Obsidian Vault (唯一事实源)
              └── (可选) Chroma 向量索引 → 语义搜索
```

**关键设计决策**：

1. **不依赖 SQLite 或其他关系型数据库**，所有元数据、审核状态、哈希值均作为 YAML front matter 直接存于 Markdown 文件中。
2. **Chroma 向量库为可选无状态缓存层**，数据完全派生自 Markdown 文件，随时可删除重建。
3. **人机协作闭环**：Draft → Review → Edit → Publish，用户可在任意环节介入修改。

---

## 4. 模块详细说明

### 4.1 采集器（Collector）— 插件化

**职责**：统一接口，将任意来源转化为标准 `RawDocument`，完成文件级去重与版本记录。

**统一数据结构**：

```python
@dataclass
class RawDocument:
    content: str            # 清洗后的 Markdown 正文
    metadata: dict          # 来源、作者、时间、URL、doc_type 等
    content_hash: str       # SHA256，用于去重
    doc_type: str           # "skill" / "blog" / "forum_post" / "webpage"
```

**采集器类型与触发方式**：

| 子命令 | 采集器 | 典型输入 |
|--------|--------|----------|
| `skillmind ingest skill <path\|url>` | SkillCollector | Git 仓库 / 本地目录 |
| `skillmind ingest rss <feed_url>` | BlogCollector | RSS/Atom Feed URL |
| `skillmind ingest url <article_url>` | BlogCollector | 单篇博客 / 文章 URL |
| `skillmind ingest forum <topic_url>` | ForumCollector | Discourse / Reddit / HN 帖子 |

**哈希去重**：对每个原始文档计算 SHA256 `content_hash`，已存在者直接跳过；所有去重记录原子写入 `~/.skillmind/hashes.yaml`。

**版本标记**：记录 `source_repo`、`source_path`、`commit_sha`（Git 来源）、`fetch_time`、`doc_type`。

### 4.2 预处理与清洗（Cleaner）

**职责**：对 Web 来源进行信噪分离，保留高质量正文。

- 集成 **crawl4ai** 或 **firecrawl**，将任意网页提取为干净的 Markdown。
- 自动丢弃侧栏、广告、评论区噪声，只保留正文和代码块。
- 对 RSS 条目，优先使用全文内容，降级时抓取原始 URL。
- SKILL.md 来源无需清洗，直接进入解析器。

### 4.3 解析器（Parser）

**职责**：将 Markdown 格式的文档解析为结构化内部数据（区块 AST）。

- 自动检测文件编码（UTF-8 / GBK / Big5 / Shift-JIS 等），支持中英文 Skill。
- 分离 YAML front matter 与正文。
- 识别常见 Skill 区块（Procedure / Decisions / Prerequisites 等），按关键词分类。
- 对无标准标题的文档采用启发式分块（空行、缩进、`IF/THEN` 等）。

### 4.4 提取引擎（Extractor）

**职责**：利用 LLM 将解析后的文档转换为标准化 JSON 知识单元。

#### 4.4.1 Prompt 路由（按 doc_type）

| doc_type | 提取重点 |
|----------|----------|
| `skill` | 严格流程、决策点、命令片段、前置条件、中止条件 |
| `blog` | 核心步骤、避坑指南、代码片段、作者观点总结 |
| `forum` | 问题描述、多个回答对比、最终采纳方案 |
| `webpage` | 主要概念、关键信息点、相关链接 |

#### 4.4.2 动态 Schema 选择

根据内容类型（可多标签）激活不同提取字段：

- **command-oriented**：重点提取 `procedure`、`command_snippets`、`preconditions`、`halt_conditions`。
- **concept-explanation**：重点提取 `plain_summary`、`knowledge_tags`、`pain_points`。
- **decision-tree**：重点提取 `decision_points`、`cross_references`。
- **troubleshooting**：重点提取问题描述、原因分析、解决步骤。

#### 4.4.3 一文多卡拆分

对于长博客或话题帖，Extractor 允许返回 **JSON 数组**，每个数组项对应一个独立知识单元。

- 拆分逻辑由 LLM 完成（例如一篇"全栈 DevOps 指南"拆分为 CI/CD、监控、部署 3 张笔记）。
- 系统为每个单元生成独立 Markdown，并保留源链接和父文档链接（`parent_source`）。
- 防止大杂烩笔记，同时保持知识间的关联可追溯。

#### 4.4.4 可靠性与过时风险标注

LLM 在提取时额外输出两个元字段，写入笔记 front matter：

```yaml
source_reliability: high   # high / medium / low（个人博客→low，官方文档→high）
obsolescence_risk: medium  # low / medium / high（是否可能快速过时）
```

支持 Dataview 按可靠性筛选：
```dataview
LIST FROM #migration WHERE source_reliability = "high" AND obsolescence_risk != "high"
```

#### 4.4.5 提取结果 JSON 结构示例

```json
{
  "uuid": "skill-8f3a1b2",
  "source": {
    "repo_url": "https://github.com/example/skills",
    "file_path": "database/pg-upgrade/SKILL.md",
    "doc_type": "skill",
    "author": "Jane Doe",
    "updated_at": "2025-03-15",
    "commit_sha": "a1b2c3d4",
    "source_hash": "e99a18c4...",
    "source_reliability": "high",
    "obsolescence_risk": "low"
  },
  "meta": {
    "name": "零停机 PostgreSQL 大版本升级",
    "type": ["command-oriented", "decision-tree"],
    "trigger_keywords": ["postgresql", "升级", "migration", "零停机"],
    "intent": "zero-downtime major upgrade PostgreSQL",
    "os": ["linux"],
    "tools_required": ["pg_upgrade", "docker"]
  },
  "preconditions": ["确保存在最新备份", "检查磁盘空间 > 20%"],
  "procedure": [
    { "seq": 1, "action": "安装新版本 PostgreSQL", "command": "apt install postgresql-16" },
    { "seq": 2, "action": "执行 pg_upgrade 检查", "command": "pg_upgrade --check ..." }
  ],
  "decision_points": [
    { "condition": "数据库版本 >= 16", "then": "使用 pg_upgrade", "else": "采用 dump/restore 方案" }
  ],
  "halt_conditions": ["pg_upgrade 检查失败", "磁盘空间不足"],
  "rollback_actions": ["停止新实例，重新启动旧实例"],
  "cross_references": ["[[系统健康检查]]", "[[备份与恢复流程]]"],
  "learning_enhancement": {
    "pain_points": ["复制槽不会自动清理，需监控磁盘"],
    "plain_summary": "在不停机的情况下，通过 pg_upgrade 工具安全升级 PostgreSQL 主版本。",
    "knowledge_tags": ["PostgreSQL", "逻辑复制", "pg_upgrade", "零停机部署"]
  },
  "prompt_version": "extract_v2",
  "status": "draft"
}
```

### 4.5 审核与发布（Reviewer）

**职责**：给予用户对提取结果的人工干预能力，保障入库知识质量。

- **草稿存放**：所有提取结果初始状态为 `draft`，保存为单独 JSON 文件至 `~/.skillmind/drafts/`。
- **审阅命令**：`skillmind review` 列出待审核草稿，展示来源类型、可靠性评级。
- **编辑命令**：`skillmind edit <uuid>` 使用系统编辑器打开 JSON，保存时自动 Schema 校验。
- **发布命令**：`skillmind publish <uuid>` 将草稿渲染为 Markdown 写入 Vault，更新向量索引。
- **自动批准**：`--auto-approve` 用于信任度高的来源（如官方 Skill 仓库）。

### 4.6 渲染器（Renderer）

**职责**：将审核后的 JSON 知识单元转换为适用于 Obsidian 的 Markdown 文件。

生成格式包含完整 YAML front matter（含 `source_reliability`、`obsolescence_risk`、`doc_type`），正文按区块结构渲染（总结、难点、流程、决策、来源），文末自动追加原文链接，完全兼容 Dataview 查询。

### 4.7 搜索与同步（可选）

**向量索引（Chroma）**：后台维护派生自 Vault 的 Chroma 数据库，支持 `skillmind search` 自然语言语义搜索。

**同步引擎**：`skillmind sync` 手动触发全量/增量索引重建；未来版本可通过 `watchdog` 自动感知 Vault 文件变更。

---

## 5. 辅助基础设施

### 5.1 版本管理
- **来源版本**：基于 Git commit SHA，`skillmind update --check` 对比上游提示需重提的内容。
- **Prompt 版本**：版本号记录在每张笔记 front matter 中，`skillmind status` 自动提示旧版 Prompt 覆盖数量。

### 5.2 去重策略
- **文件级去重**：采集时计算 SHA256 `content_hash`，完全一致者直接跳过（原子写 `hashes.yaml`）。
- **语义去重**：基于向量相似度标记"可能重复"，由用户在审核阶段决定保留策略（`skillmind dedup --interactive`）。

### 5.3 溯源与版权
所有笔记 front matter 强制保留：`source_url`、`author`、`published_at`、`doc_type`，并在文末自动生成"原文链接"块。`skillmind trace <uuid>` 一键打开原始网页或本地文件。

### 5.4 频率限制与缓存
- LLM API 调用加入速率控制，支持配置 QPM（默认 10）。
- 请求级缓存：同一 `content_hash + prompt_version` 的提取结果缓存复用，避免重复消费 Token。
- LLM 调用带超时（默认 120s）与指数退避重试。

### 5.5 原子写入与数据安全
所有关键文件（`hashes.yaml`、草稿 JSON、发布 Markdown、提取缓存 JSON）均采用 **tmp → replace** 原子写入，防止崩溃导致数据损坏。

---

## 6. CLI 命令一览

```bash
# 采 集
skillmind ingest skill <path|url>      # 开源 Skill（Git / 本地）
skillmind ingest rss <feed_url>        # 订阅博客 RSS
skillmind ingest url <article_url>     # 单篇文章 / 博客
skillmind ingest forum <topic_url>     # 论坛主题帖

# 提 取（对所有未处理文档）
skillmind extract [--type blog] [--auto-approve]

# 审 核
skillmind review [--status draft|approved|all]
skillmind edit <uuid>
skillmind publish <uuid|--all>

# 检 索
skillmind search "<query>"             # 语义搜索（需 Chroma）
skillmind sync                         # 手动同步向量索引
skillmind status                       # 统计信息 & 更新提示

# 维 护
skillmind update --check               # 检查源仓库更新
skillmind dedup --interactive          # 交互式语义去重
skillmind trace <uuid>                 # 溯源：打开原始页面 / 文件
skillmind config [--show|--test|...]   # 配置管理（Key / 模型 / Vault 路径）
```

---

## 7. 数据流与运行时

**首次构建知识库**：

1. `skillmind ingest skill https://github.com/anthropics/skills` → 采集 Skill 仓库
2. `skillmind ingest rss https://blog.example.com/feed.xml` → 采集博客 RSS
3. `skillmind extract` → LLM 提取所有未处理文档，生成草稿
4. `skillmind review` → 查看草稿列表，`edit <uuid>` 微调
5. `skillmind publish --all` → 渲染 Markdown 写入 Obsidian Vault
6. `skillmind search "零停机升级"` → 语义检索

**增量更新**：

- 上游仓库更新：`skillmind update --check` 检测 → `skillmind ingest skill <url>` + `extract` 仅处理变更文件。
- 新增 RSS 文章：`skillmind ingest rss <url>` 按 `content_hash` 自动跳过已存在条目。
- 笔记修改后：`skillmind sync` 更新向量索引。

**知识碎片化的预防**：

- 同一篇文档的步骤、决策、命令保留在同一笔记中，通过 `[[上下步骤链接]]` 构建内部导航。
- 一文多卡拆分时，所有子笔记自动包含 `parent_source` 字段相互关联。

---

## 8. 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| CLI 框架 | Typer + Rich |
| 文本解析 | PyYAML、正则 + 启发式分块 |
| 编码检测 | 内置多编码 fallback（UTF-8 / GBK / Big5 / Shift-JIS） |
| LLM 集成 | litellm（支持 OpenAI、Anthropic、DeepSeek、Gemini、本地模型等） |
| Web 抓取（可选） | crawl4ai 或 firecrawl |
| 向量索引（可选） | Chroma（嵌入式运行，无需独立服务） |
| Git 操作 | GitPython |
| 文件监听（可选） | watchdog |
| 打包分发 | pip install（pyproject.toml + entry_points） |

---

## 9. 工程实施路线

### ✅ Phase 0：核心 Skill 闭环（已完成 v1.0）
- Collector（Git / 本地）、Parser、Extractor（LLM + 启发式兜底）。
- 全流程 CLI：ingest / extract / review / edit / publish / search / sync / status / update / config。
- 多 Provider API Key 管理、原子写入、编码自动检测、跨平台兼容。

### 🚧 Phase 1：博客与 Web 支持（v2.x）
- 实现 `BlogCollector`（RSS + 单 URL），集成 crawl4ai。
- 调整 Extractor Prompt 路由处理博客内容。
- 验证"一文多卡"拆分逻辑。
- 实现 `source_reliability` 与 `obsolescence_risk` 标注。

### 📋 Phase 2：论坛支持与审核体验增强（v2.x）
- 添加 `ForumCollector`（Discourse API 优先）。
- `review` 列表展示来源类型标签和可靠性评级。
- `dedup --interactive` 语义去重交互。
- `trace <uuid>` 溯源命令。

### 🔮 Phase 3：自动化与生态（v3.x）
- 对接 Readwise / Pocket，高亮文章直接进入知识提炼流。
- watchdog 自动感知 Vault 变更，增量更新向量索引。
- 发布 v2.0 开源版，附完整中英文文档。

---

## 10. 总结与展望

SkillMind v2.2 将个人知识管理的输入端从"Skill 文件"扩展到整个互联网的公开经验。通过插件化采集、动态 Prompt 路由、一文多卡拆分和可靠性标注，它能从任意来源为你提炼养分，并一视同仁地存入你完全掌控的 Obsidian 知识花园。

系统设计始终围绕三个不变的根基：

> **本地、私有、可进化** —— 你不需要一个新工具，你需要的是一台持续运转的知识压榨机。