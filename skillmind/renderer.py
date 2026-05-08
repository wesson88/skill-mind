"""渲染器（Renderer）- 将 JSON 知识单元渲染为 Obsidian Markdown 文件"""

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
    name = re.sub(r'[\\/:*?"<>|]', "-", name)
    return name.strip("-").strip()[:80] or "unnamed"


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------

def render_to_markdown(data: dict, cfg: dict | None = None) -> str:
    """将知识单元 dict 渲染为 Obsidian Markdown 字符串。"""
    cfg = cfg or load_config()
    meta = data.get("meta", {})
    source = data.get("source", {})
    le = data.get("learning_enhancement", {})

    # --- YAML front matter ---
    fm: dict = {
        "uuid": data.get("uuid", ""),
        "name": meta.get("name", ""),
        "type": meta.get("type", []),
        "intent": meta.get("intent", ""),
        "tags": le.get("knowledge_tags", []) + meta.get("trigger_keywords", []),
        "source_repo": source.get("repo_url", ""),
        "source_path": source.get("source_path", ""),
        "source_hash": source.get("source_hash", ""),
        "prompt_version": data.get("prompt_version", ""),
        "status": data.get("status", "published"),
        "draft": data.get("status", "published") != "published",
        "created": data.get("created_at", time.strftime("%Y-%m-%d")),
        "updated": time.strftime("%Y-%m-%d"),
    }

    # 去重 tags
    fm["tags"] = list(dict.fromkeys(t for t in fm["tags"] if t))

    fm_str = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
    lines = [f"---\n{fm_str}\n---\n"]

    # --- 标题 ---
    lines.append(f"# {meta.get('name', 'Untitled')}\n")

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

    # --- 元信息 ---
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
    repo_url = source.get("repo_url", "")
    file_path = source.get("file_path", "") or source.get("source_path", "")
    if repo_url and file_path:
        # 统一正斜杠，避免 Windows 路径污染 URL
        file_path_posix = file_path.replace("\\", "/")
        # 优先用记录的 branch，兜底 main
        branch = source.get("branch", "main")
        full_url = f"{repo_url.rstrip('/')}/blob/{branch}/{file_path_posix}"
        lines.append("---")
        lines.append(f"*来源：[原始 Skill]({full_url})*")
    elif repo_url:
        lines.append("---")
        lines.append(f"*来源：[原始仓库]({repo_url})*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 发布到 Vault
# ---------------------------------------------------------------------------

def publish_to_vault(data: dict, cfg: dict | None = None) -> Path:
    """
    将知识单元写入 Obsidian Vault，返回写入的文件路径。
    """
    cfg = cfg or load_config()
    vault_dir = get_vault_dir(cfg)
    published_dir = vault_dir / "skills"
    published_dir.mkdir(parents=True, exist_ok=True)

    meta_name = data.get("meta", {}).get("name", data.get("uuid", "unnamed"))
    filename = _safe_filename(meta_name) + ".md"
    dest = published_dir / filename

    # 若文件名冲突，追加 uuid 前缀区分
    if dest.exists():
        uid_short = data.get("uuid", "")[-7:]
        filename = _safe_filename(meta_name) + f"_{uid_short}.md"
        dest = published_dir / filename

    md_content = render_to_markdown(data, cfg)
    # 原子写：防止崩溃损坏已发布文件
    tmp = dest.with_suffix(".md.tmp")
    tmp.write_text(md_content, encoding="utf-8")
    tmp.replace(dest)
    return dest
