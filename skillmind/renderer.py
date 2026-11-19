"""渲染器（Renderer）- 将 JSON 知识单元渲染为 Obsidian Markdown 文件

v2.2 新增：
  - front matter 增加 doc_type / source_reliability / obsolescence_risk / parent_source
  - 渲染可靠性与过时风险标注
  - 一文多卡：显示卡片序号及父文档链接
  - 所有写入原子化（tmp → replace）
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
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

def _is_semantic(data: dict) -> bool:
    return data.get("schema_version") == "semantic_distillation_v1"


class _QuotedLink(str):
    """A frontmatter link scalar whose quoting is part of the vault contract."""


class _FrontmatterDumper(yaml.SafeDumper):
    pass


def _represent_quoted_link(dumper: yaml.SafeDumper, value: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"')


_FrontmatterDumper.add_representer(_QuotedLink, _represent_quoted_link)


def _render_semantic_sections(data: dict) -> list[str]:
    """Render the canonical synthesis verbatim; evidence stays in its sidecar."""
    lines: list[str] = []
    for section in data["distillation"]["sections"]:
        lines.extend([f"## {section['title']}", ""])
        for item in section["items"]:
            lines.extend([item["text"], ""])
    return lines


def _published_evidence_draft(data: dict) -> dict:
    """Keep audit evidence without publishing machine-local cache provenance."""
    draft = copy.deepcopy(data)
    source = draft.get("source")
    if isinstance(source, dict):
        source.pop("raw_path", None)
        repo = str(source.get("source_repo") or "")
        if not re.match(r"^(?:(?:https?|ssh|git)://|[^/@:\s]+@[^/:\s]+:)", repo):
            source.pop("source_repo", None)
    return draft


def _write_artifact_evidence(path: Path, data: dict) -> None:
    """Atomically replace the envelope after hashing actual published bytes."""
    envelope = {
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "draft": _published_evidence_draft(data),
    }
    sidecar = path.with_suffix(".evidence.json")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=sidecar.parent,
            prefix=sidecar.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(envelope, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(sidecar)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_artifact_evidence(path: str | Path) -> dict:
    """Return the evidence envelope only when it matches the artifact's bytes.

    This verifies publication integrity, not canonical schema validity or the
    semantic truth of the draft. The semantic auditor performs those checks.
    """
    artifact = Path(path)
    sidecar = artifact.with_suffix(".evidence.json")
    try:
        envelope = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Missing or invalid artifact evidence: {sidecar.name}") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"artifact_sha256", "draft"}
        or not isinstance(envelope.get("draft"), dict)
        or not isinstance(envelope.get("artifact_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", envelope["artifact_sha256"])
    ):
        raise ValueError(f"Invalid artifact evidence envelope: {sidecar.name}")
    try:
        actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"Cannot verify artifact evidence: {artifact.name}") from exc
    if envelope["artifact_sha256"] != actual_hash:
        raise ValueError(f"Artifact evidence hash mismatch: {artifact.name}")
    return envelope


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
        "source_description": meta.get("source_description", ""),
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
    for field in ("source_url", "source_repo", "parent_source"):
        if field in fm:
            fm[field] = _QuotedLink(fm[field])

    fm_str = yaml.dump(fm, Dumper=_FrontmatterDumper, allow_unicode=True, default_flow_style=False).strip()
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

    if _is_semantic(data):
        lines.extend(_render_semantic_sections(data))
        _render_source_footer(lines, source, doc_type)
        return "\n".join(lines)

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

def _owned_semantic_artifact(path: Path, uuid: str) -> bool:
    """Ownership requires both the original artifact hash and matching UUID."""
    if not uuid:
        return False
    try:
        draft = load_artifact_evidence(path)["draft"]
    except ValueError:
        return False
    return _is_semantic(draft) and draft.get("uuid") == uuid


def _choose_obsidian_artifact(out: Path, stem: str, uuid: str) -> Path:
    candidate = out / f"{stem}.md"
    collision = 0
    while candidate.exists() or candidate.with_suffix(".evidence.json").exists():
        if _owned_semantic_artifact(candidate, uuid):
            return candidate
        suffix = _safe_filename(uuid[-7:])
        serial = f"_{collision}" if collision else ""
        candidate = out / f"{stem}_{suffix}{serial}.md"
        collision += 1
    return candidate


def _retire_owned_obsidian_artifacts(out: Path, uuid: str, keep: Path) -> None:
    """Retire generated siblings only after the replacement is fully written."""
    for old in out.glob("*.md"):
        if old == keep or not _owned_semantic_artifact(old, uuid):
            continue
        try:
            old.unlink()
            old.with_suffix(".evidence.json").unlink()
        except OSError:
            continue


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

    # Semantic ownership comes from hash-bound evidence, never the filename.
    semantic = _is_semantic(data)
    uuid = str(data.get("uuid") or "")
    if semantic:
        dest = _choose_obsidian_artifact(out, file_prefix + _safe_filename(meta_name), uuid)
    elif dest.exists():
        uid_short = data.get("uuid", "")[-7:]
        filename = file_prefix + _safe_filename(meta_name) + f"_{uid_short}.md"
        dest = out / filename

    md_content = render_to_markdown(data, cfg)

    tmp = dest.parent / (dest.name + ".tmp")
    tmp.write_text(md_content, encoding="utf-8")
    tmp.replace(dest)
    _write_artifact_evidence(dest, data)
    if semantic:
        _retire_owned_obsidian_artifacts(out, uuid, keep=dest)
    return dest


# ---------------------------------------------------------------------------
# Agent Skill（SKILL.md）可选输出
# ---------------------------------------------------------------------------

_SKILL_NAME_MAX = 64
_UUID_MARKER = re.compile(r"<!--\s*skillmind-uuid:\s*(.*?)\s*-->")


def skill_slug(title: str, *, fallback: str = "skill") -> str:
    """把笔记标题收成 Agent Skills 要求的 kebab-case 名称。"""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    fallback_slug = re.sub(r"[^a-z0-9]+", "-", fallback.lower()).strip("-") or "skill"
    slug = (slug or fallback_slug)[:_SKILL_NAME_MAX].strip("-")
    return slug or fallback_slug[:_SKILL_NAME_MAX].strip("-") or "skill"


def _uuid_suffix(uuid: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", (uuid or "").lower())
    return (cleaned[-8:] or "skill")


def _slug_with_suffix(slug: str, suffix: str) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "-", suffix.lower()).strip("-") or "x"
    room = _SKILL_NAME_MAX - len(suffix) - 1
    base = (slug[: max(room, 1)]).strip("-") or "skill"
    return f"{base}-{suffix}"[:_SKILL_NAME_MAX].strip("-")


def _one_line(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text


def _as_items(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    return [value]


def skill_description(data: dict) -> str:
    """When-to-use description, one line, capped at 1024 characters."""
    meta = data.get("meta") or {}
    le = data.get("learning_enhancement") or {}
    intent = _one_line(meta.get("intent"))
    summary = _one_line(le.get("plain_summary"))
    name = _one_line(meta.get("name")) or "this skill"
    lead = intent or summary or name
    text = lead if lead.lower().startswith("use when") else f"Use when: {lead}"
    if summary and summary not in text:
        if text[-1:] not in ".!?。":
            text += "."
        text = f"{text} {summary}"
    triggers = [_one_line(t) for t in _as_items(meta.get("trigger_keywords")) if _one_line(t)]
    if triggers:
        if text[-1:] not in ".!?。":
            text += "."
        text = f"{text} Triggers: {', '.join(triggers[:8])}."
    return " ".join(text.split())[:1024].strip()


def _source_label(data: dict) -> str:
    source = data.get("source") or {}
    url = _one_line(source.get("source_url") or source.get("repo_url"))
    path = _one_line(source.get("file_path") or source.get("source_path"))
    if url and path and path not in url:
        return f"{url} ({path})"
    return url or path or "local SkillMind cache"


def render_skill_markdown(data: dict, *, name: str | None = None) -> str:
    """把已提取的知识单元渲染为 Agent Skills 风格的 SKILL.md。"""
    meta = data.get("meta") or {}
    uuid = str(data.get("uuid") or "unknown")
    slug = name or skill_slug(
        str(meta.get("name") or ""),
        fallback=f"skill-{_uuid_suffix(uuid)}",
    )
    description = skill_description(data)
    fm = yaml.dump(
        {"name": slug, "description": description},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    ).strip()

    title = _one_line(meta.get("name")) or slug
    le = data.get("learning_enhancement") or {}
    lines = [f"---\n{fm}\n---\n", f"# {title}\n"]

    if _is_semantic(data):
        lines.extend(_render_semantic_sections(data))
        _render_skill_source(lines, data, uuid)
        return "\n".join(lines)

    summary = _one_line(le.get("plain_summary"))
    if summary:
        lines.append(summary)
        lines.append("")

    preconditions = [_one_line(p) for p in _as_items(data.get("preconditions")) if _one_line(p)]
    if preconditions:
        lines.append("## Preconditions")
        lines.append("")
        for item in preconditions:
            lines.append(f"- {item}")
        lines.append("")

    procedure = _as_items(data.get("procedure"))
    lines.append("## Steps")
    lines.append("")
    if procedure:
        for i, step in enumerate(procedure, start=1):
            if isinstance(step, dict):
                action = _one_line(step.get("action") or step.get("step") or "")
                command = str(step.get("command") or "").strip()
                seq = step.get("seq") or i
            else:
                action = _one_line(step)
                command = ""
                seq = i
            if not action and not command:
                continue
            lines.append(f"{seq}. {action or 'Run:'}")
            if command:
                lines.append("")
                lines.append("```")
                lines.append(command)
                lines.append("```")
                lines.append("")
        if lines[-1] != "":
            lines.append("")
    else:
        lines.append("No executable steps were extracted. Follow the summary above.")
        lines.append("")

    decisions = _as_items(data.get("decision_points"))
    rendered_decisions = []
    for dp in decisions:
        if isinstance(dp, dict):
            condition = _one_line(dp.get("condition"))
            then = _one_line(dp.get("then"))
            else_ = _one_line(dp.get("else"))
            if condition or then or else_:
                rendered_decisions.append((condition, then, else_))
        else:
            text = _one_line(dp)
            if text:
                rendered_decisions.append((text, "", ""))
    if rendered_decisions:
        lines.append("## Decisions")
        lines.append("")
        for condition, then, else_ in rendered_decisions:
            lines.append(f"- {condition or 'Condition'}")
            if then:
                lines.append(f"  - Yes: {then}")
            if else_:
                lines.append(f"  - No: {else_}")
        lines.append("")

    halts = [_one_line(h) for h in _as_items(data.get("halt_conditions")) if _one_line(h)]
    if halts:
        lines.append("## Halt conditions")
        lines.append("")
        for item in halts:
            lines.append(f"- {item}")
        lines.append("")

    rollbacks = [_one_line(r) for r in _as_items(data.get("rollback_actions")) if _one_line(r)]
    if rollbacks:
        lines.append("## Rollback")
        lines.append("")
        for item in rollbacks:
            lines.append(f"- {item}")
        lines.append("")

    _render_skill_source(lines, data, uuid)
    return "\n".join(lines)


def _render_skill_source(lines: list[str], data: dict, uuid: str) -> None:
    lines.append("## Source")
    lines.append("")
    lines.append(f"Distilled by SkillMind from {_source_label(data)}.")
    lines.append(f"SkillMind uuid: {uuid}")
    lines.append(f"<!-- skillmind-uuid: {_marker_uuid(uuid)} -->")
    lines.append("")


def _marker_uuid(uuid: str) -> str:
    return (uuid or "unknown").replace("-->", "").strip() or "unknown"


def _skill_uuid_of(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    matches = list(_UUID_MARKER.finditer(text))
    return matches[-1].group(1).strip() if matches else None


def _dir_available_for(path: Path, uuid: str) -> bool:
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    skill_file = path / "SKILL.md"
    if skill_file.is_file():
        return _skill_uuid_of(skill_file) == _marker_uuid(uuid)
    return not any(path.iterdir())


def _choose_skill_dir(root: Path, slug: str, uuid: str) -> Path:
    primary = root / slug
    if _dir_available_for(primary, uuid):
        return primary
    alt = root / _slug_with_suffix(slug, _uuid_suffix(uuid))
    if _dir_available_for(alt, uuid):
        return alt
    return root / _slug_with_suffix(slug, f"{_uuid_suffix(uuid)}2")


def _retire_other_owned(root: Path, uuid: str, keep: Path) -> None:
    """Republish moves a renamed skill instead of leaving a second copy."""
    if not root.exists():
        return
    for skill_file in root.glob("*/SKILL.md"):
        if skill_file.parent == keep:
            continue
        if _skill_uuid_of(skill_file) != _marker_uuid(uuid):
            continue
        try:
            sidecar = skill_file.with_suffix(".evidence.json")
            try:
                evidence = json.loads(sidecar.read_text(encoding="utf-8"))
                owned_sidecar = (
                    isinstance(evidence, dict)
                    and isinstance(evidence.get("draft"), dict)
                    and str(evidence["draft"].get("uuid")) == uuid
                )
            except (OSError, ValueError):
                owned_sidecar = False
            skill_file.unlink()
            if owned_sidecar:
                sidecar.unlink()
            if not any(skill_file.parent.iterdir()):
                skill_file.parent.rmdir()
        except OSError:
            continue


def publish_skill(data: dict, skill_dir: str | Path) -> Path:
    """写入 `<skill_dir>/<name>/SKILL.md`。同一 uuid 再次发布覆盖原文件。"""
    root = Path(skill_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    meta = data.get("meta") or {}
    uuid = str(data.get("uuid") or "unknown")
    slug = skill_slug(
        str(meta.get("name") or ""),
        fallback=f"skill-{_uuid_suffix(uuid)}",
    )
    dest_dir = _choose_skill_dir(root, slug, uuid)
    dest_dir.mkdir(parents=True, exist_ok=True)
    text = render_skill_markdown(data, name=dest_dir.name)
    dest = dest_dir / "SKILL.md"
    tmp = dest.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
    _write_artifact_evidence(dest, data)
    _retire_other_owned(root, uuid, keep=dest_dir)
    return dest
