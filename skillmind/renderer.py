"""渲染器（Renderer）- 将 JSON 知识单元渲染为 Obsidian Markdown 文件

v2.2 新增：
  - front matter 增加 doc_type / source_reliability / obsolescence_risk / parent_source
  - 渲染可靠性与过时风险标注
  - 一文多卡：显示卡片序号及父文档链接
  - 所有写入原子化（tmp → replace）
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import yaml

from skillmind.config import get_vault_dir, load_config


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    """将任意字符串转为跨平台安全的文件名。

    处理顺序：
    1. 去除所有控制字符（\\n \\r \\t 及 ASCII 0-31、127）
    2. 替换 Windows/Unix 文件名非法字符 \\ / : * ? " < > | 为 -
    3. 折叠连续空白为单个空格，去除首尾空白和连字符
    4. 截断至 80 字符，兜底返回 "unnamed"
    """
    # 1. 去除控制字符（\x00-\x1f 及 \x7f）
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    # 2. 替换非法字符
    name = re.sub(r'[\\/:*?"<>|]', '-', name)
    # 3. 折叠连续空白
    name = re.sub(r'\s+', ' ', name).strip().strip('-').strip()
    # 4. 截断
    return name[:80] or "unnamed"


_RELIABILITY_BADGE = {
    "high":   "🟢 高可信",
    "medium": "🟡 中等可信",
    "low":    "🔴 低可信（请交叉验证）",
}

_OBSOLESCENCE_BADGE = {
    "low":    "🟢 较稳定",
    "medium": "🟡 可能过时",
    "high":   "🔴 高过时风险（请核实版本）",
}

_DOC_TYPE_LABEL = {
    "skill":      "📋 Skill",
    "blog":       "📝 博客",
    "forum_post": "💬 论坛",
    "webpage":    "🌐 网页",
}


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------

def render_to_markdown(data: dict, cfg: dict | None = None) -> str:
    """将知识单元 dict 渲染为 Obsidian Markdown 字符串。"""
    cfg = cfg or load_config()
    meta = data.get("meta", {})
    source = data.get("source", {})
    le = data.get("learning_enhancement", {})
    reliability = data.get("source_reliability", "medium")
    obsolescence = data.get("obsolescence_risk", "medium")
    doc_type = source.get("doc_type", data.get("doc_type", "skill"))

    # --- YAML front matter ---
    tags = le.get("knowledge_tags", []) + meta.get("trigger_keywords", [])
    fm: dict = {
        "uuid": data.get("uuid", ""),
        "name": meta.get("name", ""),
        "type": meta.get("type", []),
        "intent": meta.get("intent", ""),
        "tags": list(dict.fromkeys(t for t in tags if t)),
        "doc_type": doc_type,
        "source_url": source.get("source_url", "") or source.get("repo_url", ""),
        "source_repo": source.get("repo_url", ""),
        "source_path": source.get("file_path", "") or source.get("source_path", ""),
        "source_hash": source.get("source_hash", ""),
        "author": source.get("author", ""),
        "published_at": source.get("published_at", ""),
        "source_reliability": reliability,
        "obsolescence_risk": obsolescence,
        "parent_source": source.get("parent_source", ""),
        "prompt_version": data.get("prompt_version", ""),
        "status": data.get("status", "published"),
        "draft": data.get("status", "published") != "published",
        "created": data.get("created_at", time.strftime("%Y-%m-%d")),
        "updated": time.strftime("%Y-%m-%d"),
    }
    # 一文多卡标注
    card_index = source.get("card_index", 0)
    card_total = source.get("card_total", 0)
    if card_total > 1:
        fm["card_index"] = card_index
        fm["card_total"] = card_total

    # 清理空值，减少噪声（注意：False 和 0 是合法值，不过滤）
    fm = {k: v for k, v in fm.items() if v is not None and v != "" and v != [] and v != {}}

    fm_str = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
    lines = [f"---\n{fm_str}\n---\n"]

    # --- 标题 ---
    card_suffix = f"（{card_index}/{card_total}）" if card_total > 1 else ""
    lines.append(f"# {meta.get('name', 'Untitled')}{card_suffix}\n")

    # --- 来源标注栏（可靠性 & 过时风险）---
    doc_label = _DOC_TYPE_LABEL.get(doc_type, "📄 文档")
    rel_badge = _RELIABILITY_BADGE.get(reliability, reliability)
    obs_badge = _OBSOLESCENCE_BADGE.get(obsolescence, obsolescence)
    lines.append(
        f"> {doc_label} &nbsp;|&nbsp; 可信度：{rel_badge} &nbsp;|&nbsp; 时效性：{obs_badge}\n"
    )

    # --- 一文多卡父文档链接 ---
    if card_total > 1 and source.get("parent_source"):
        lines.append(f"> 📎 父文档：[原始来源]({source['parent_source']})\n")

    # --- 一句话总结 ---
    if le.get("plain_summary"):
        lines.append("## 📌 一句话总结")
        lines.append(le["plain_summary"])
        lines.append("")

    # --- 难点注意 ---
    pain_points = le.get("pain_points", [])
    if pain_points:
        lines.append("## ⚠️ 难点注意")
        for p in pain_points:
            lines.append(f"- {p}")
        lines.append("")

    # --- 前置条件 ---
    preconditions = data.get("preconditions", [])
    if preconditions:
        lines.append("## ✅ 前置条件")
        for p in preconditions:
            lines.append(f"- {p}")
        lines.append("")

    # --- 执行流程 ---
    procedure = data.get("procedure", [])
    if procedure:
        lines.append("## 🧩 执行流程")
        for step in procedure:
            seq = step.get("seq", "")
            action = step.get("action", "")
            command = step.get("command", "")
            if command:
                lines.append(f"{seq}. {action}")
                lines.append(f"   ```")
                lines.append(f"   {command}")
                lines.append(f"   ```")
            else:
                lines.append(f"{seq}. {action}")
        lines.append("")

    # --- 关键决策 ---
    decision_points = data.get("decision_points", [])
    if decision_points:
        lines.append("## 🔀 关键决策")
        for dp in decision_points:
            condition = dp.get("condition", "")
            then = dp.get("then", "")
            else_ = dp.get("else", "")
            lines.append(f"- **{condition}**")
            if then:
                lines.append(f"  - ✅ 是：{then}")
            if else_:
                lines.append(f"  - ❌ 否：{else_}")
        lines.append("")

    # --- 中止条件 ---
    halt_conditions = data.get("halt_conditions", [])
    if halt_conditions:
        lines.append("## 🛑 中止条件")
        for h in halt_conditions:
            lines.append(f"- {h}")
        lines.append("")

    # --- 回滚方案 ---
    rollback_actions = data.get("rollback_actions", [])
    if rollback_actions:
        lines.append("## ⏪ 回滚方案")
        for r in rollback_actions:
            lines.append(f"- {r}")
        lines.append("")

    # --- 关联知识 ---
    cross_references = data.get("cross_references", [])
    if cross_references:
        lines.append("## 🔗 关联知识")
        for ref in cross_references:
            lines.append(f"- {ref}")
        lines.append("")

    # --- 核心概念 ---
    key_concepts = data.get("key_concepts", [])
    if key_concepts:
        lines.append("## 💡 核心概念")
        for kc in key_concepts:
            if not isinstance(kc, dict):
                continue
            title = kc.get("title", "")
            explanation = kc.get("explanation", "")
            example = kc.get("example", "")
            if title:
                lines.append(f"### {title}")
            if explanation:
                lines.append(explanation)
            if example:
                lines.append("")
                lines.append("> **示例**")
                # 多行示例用 blockquote 包裹
                for ex_line in example.splitlines():
                    lines.append(f"> {ex_line}" if ex_line.strip() else ">")
            lines.append("")

    # --- 环境信息 ---
    os_info = meta.get("os", [])
    tools = meta.get("tools_required", [])
    if os_info or tools:
        lines.append("## 🛠️ 环境信息")
        if os_info:
            lines.append(f"- **操作系统**: {', '.join(os_info)}")
        if tools:
            lines.append(f"- **所需工具**: {', '.join(tools)}")
        lines.append("")

    # --- 来源 ---
    _render_source_footer(lines, source, doc_type)

    return "\n".join(lines)


def _render_source_footer(lines: list[str], source: dict, doc_type: str) -> None:
    """渲染文末原文链接块，支持 Git 仓库和 Web URL 两种模式。"""
    source_url = source.get("source_url", "")
    repo_url = source.get("repo_url", "")
    file_path = source.get("file_path", "") or source.get("source_path", "")
    author = source.get("author", "")
    published_at = source.get("published_at", "")

    lines.append("---")

    # Web 文章：直接用 source_url
    if source_url and doc_type in ("blog", "forum_post", "webpage"):
        author_str = f"by {author} " if author else ""
        date_str = f"({published_at})" if published_at else ""
        lines.append(f"*来源：[原文链接]({source_url}) {author_str}{date_str}*")
        return

    # Git 仓库：拼接 blob URL
    if repo_url and file_path:
        import re as _re
        file_path_posix = file_path.replace("\\", "/")
        branch = source.get("branch", "main")

        # 如果 source_url 本身已经是指向该文件的 GitHub blob URL，直接使用，避免二次拼接
        # 例：source_url = https://github.com/owner/repo/blob/main/skills/SKILL.md
        if (source_url
                and "github.com" in source_url
                and "/blob/" in source_url
                and source_url.rstrip("/").endswith(file_path_posix.rstrip("/"))):
            lines.append(f"*来源：[原始 Skill]({source_url})*")
            return

        # 提取仓库根 URL：去掉 /tree/<branch>/... 或 /blob/<branch>/... 部分
        root_url = _re.sub(r"(/tree/|/blob/)[^/]+(/.*)?$", "", repo_url.rstrip("/"))
        full_url = f"{root_url}/blob/{branch}/{file_path_posix}"
        lines.append(f"*来源：[原始 Skill]({full_url})*")
    elif repo_url:
        lines.append(f"*来源：[原始仓库]({repo_url})*")
    elif source_url:
        lines.append(f"*来源：[原文链接]({source_url})*")


# ---------------------------------------------------------------------------
# 发布到 Vault
# ---------------------------------------------------------------------------

def publish_to_vault(
    data: dict,
    cfg: dict | None = None,
    *,
    output_dir: str | Path | None = None,
    prefix: str | None = None,
) -> Path:
    """
    将知识单元写入 Obsidian Vault，返回写入的文件路径。
    文件写入原子化（tmp → replace）。

    Parameters
    ----------
    output_dir : str | Path | None
        指定输出目录。优先级高于 cfg["vault_dir"]。
        若为相对路径，相对于 vault_dir/skills/。
    prefix : str | None
        文件名前缀，如 "SM_"、"技能_"。
        优先级：参数 > cfg["output_prefix"]。
    """
    cfg = cfg or load_config()
    vault_dir = get_vault_dir(cfg)

    # 确定输出目录
    if output_dir is not None:
        out = Path(str(output_dir).strip())
        if not out.is_absolute():
            out = vault_dir / "skills" / out
    else:
        out = vault_dir / "skills"
    out.mkdir(parents=True, exist_ok=True)

    # 确定文件名前缀
    file_prefix = prefix if prefix is not None else str(cfg.get("output_prefix", "") or "")

    meta_name = data.get("meta", {}).get("name", data.get("uuid", "unnamed"))
    filename = file_prefix + _safe_filename(meta_name) + ".md"
    dest = out / filename

    # 文件名冲突时追加 uuid 后缀
    if dest.exists():
        uid_short = data.get("uuid", "")[-7:]
        filename = file_prefix + _safe_filename(meta_name) + f"_{uid_short}.md"
        dest = out / filename

    md_content = render_to_markdown(data, cfg)

    tmp = dest.parent / (dest.name + ".tmp")
    tmp.write_text(md_content, encoding="utf-8")
    tmp.replace(dest)
    return dest
