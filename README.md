# 🧠 SkillMind

**开源 Claude Code Skill 知识提炼系统**

将分散在 GitHub 上的 `SKILL.md` 文件，通过 LLM 提炼为结构化知识，沉淀为可供 Obsidian 直接使用的 Markdown 笔记。

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
skillmind ingest https://github.com/anthropics/claude-code-skills

# 从本地目录采集
skillmind ingest ~/my-skills-collection
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
# 发布单个
skillmind publish skill-8f3a1b2

# 批量发布全部
skillmind publish --all
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
