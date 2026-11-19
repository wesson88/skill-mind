"""提取引擎（Extractor）

把缓存原文转成 1~N 张草稿。技能类文档先保留关键约束，再写可执行规则；
原文没有的槽位标 unknown，不按章节做长篇复述。

缓存键包含 prompt、schema、模型与分块参数。截断 JSON 和失败批次
不会被写成可复用的完整成功。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skillmind.config import (
    CACHE_FORMAT_VERSION,
    CURRENT_PROMPT_VERSION,
    EXTRACT_CACHE_DIR,
    SCHEMA_VERSION,
    ensure_dirs,
    resolve_llm_credentials,
)
from skillmind.chunker import Chunker, pack_chunks_for_prompt
from skillmind.gates import norm_ws, quote_in_source, scan_critical_anchors
from skillmind.parser import _read_text_auto, parse_skill_file
from skillmind.reviewer import save_draft


_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Prompt 文本
# ---------------------------------------------------------------------------

_DOC_TYPE_LABEL = {
    "skill":         "Claude Code Skill 文档",
    "design_system": "设计系统规范文档",
    "blog":          "技术博客文章",
    "forum_post":    "论坛主题帖（含问答）",
    "webpage":       "通用网页",
}

_COMMON_FIDELITY_RULES = (
    "- 【先约束、后规则】先抽取关键约束、阈值、例外、安全限制和中止条件，"
    "再写 condition / judgment / action / verification / boundary。\n"
    "- 【禁止按章节复述】不要为每个章节写 100-500 字解释，不要把列表整段 verbatim 抄进 explanation。"
    "key_concepts.explanation 只做不超过 80 字的索引，指向对应约束或规则。\n"
    "- 【未知即 unknown】原文没有写明的槽位填 \"unknown\"。禁止编造阈值、日期、区域、批准条件或命令。\n"
    "- 【原文依据】每条 constraint 和 rule 必须带 source_quote，即原文中的连续短引。"
    "找不到原句就写 \"unknown\"，不要另写一句看起来像原文的话。\n"
    "- 【数值】数字、上限、日期、区域范围放进 constraint.value 和 source_quote，保持原样，"
    "不要概括成“若干”或“适当”，也不要补原文没有的数字。\n"
    "- 【禁 wikilink 幻觉】cross_references 中的 [[xxx]] 必须是原文真实提及的关联名词。"
)

_FOCUS_RULES: dict[str, str] = {
    "skill": (
        "- 这是可执行技能，不是译文。读者要能据此做决定，而不是按章节把原文再读一遍。\n"
        "- constraints 逐条收录：blanket 批准或拒绝、复审或截止日期、区域策略、"
        "令牌/速率/递归上限，以及其他 must/never、例外和中止条件。\n"
        "- rules 只写可执行决策。五个槽位里原文没写的填 unknown，不要用口号补齐。\n"
        "- halt_conditions 只放原文写明的停止或禁止条件。procedure 只放原文中的操作步骤。\n"
        "- 具名章节不必各做一条长 key_concept。无法归入约束或规则的术语才保留短索引。\n"
        "- 【严禁幻觉】title、source_quote 和数值必须能在原文中找到；找不到就写 unknown。\n"
        + _COMMON_FIDELITY_RULES
    ),
    "design_system": (
        "- 设计系统文档的核心价值在于【视觉规则】和【设计决策】，而非操作步骤。\n"
        "- 从文档中提取所有具名设计维度（如'Configuration Dials'、'Color Palette'、'Typography'、'Key Rules'等），\n"
        "  每个维度作为独立的 key_concept 条目（title=维度名，explanation=该维度的规则和取值，example=具体值/示例）。\n"
        "- 若文档含表格（如配置表、颜色表），将每行作为一条 example 纳入对应 key_concept。\n"
        "- 若文档有'不可违反的铁律'类规则（Key Rules / Forbidden / Must Not），提取到 halt_conditions。\n"
        "- 若文档有'适用条件'类描述，提取到 decision_points。\n"
        "- 【关键】每个具名章节/区块必须作为独立 key_concept 条目保留，使用原标题作为 title，\n"
        "  禁止将多个命名章节合并为一条综合摘要。\n"
        "- 【严禁幻觉】key_concepts 中每个条目的 title 必须来自原文实际存在的章节/区块标题或明确命名的设计概念，\n"
        "  禁止虚构任何综合摘要、汇总或归纳性标题（如'设计原则汇总'、'核心规范'等原文中不存在的标题）。\n"
        "- 【一文多卡】若文档描述多个独立的可复用设计维度（如同时有色彩/字体/布局规则），\n"
        "  可按维度拆成多张卡，每张聚焦一个维度。\n"
        "- 阈值、例外和禁止项写入 constraints / halt_conditions，不要只留在长篇 explanation 里。\n"
        + _COMMON_FIDELITY_RULES
    ),
    "blog": (
        "- 提取核心步骤、避坑指南、关键命令片段；\n"
        "- 总结作者观点（learning_enhancement.plain_summary）；\n"
        "- 重要概念/方法写入 key_concepts；\n"
        "- 若长文涵盖多个独立主题，按主题拆成多张卡。\n"
        "- 【严禁幻觉】key_concepts 中每个条目的 title 必须来自原文实际存在的章节或概念，禁止虚构综合摘要标题。\n"
        + _COMMON_FIDELITY_RULES
    ),
    "forum_post": (
        "- 主帖：明确「问题描述」（写入 plain_summary）；\n"
        "- 多个回答：在 procedure 或 decision_points 中对比，标记最终采纳方案；\n"
        "- 不同方案适用条件写入 decision_points。\n"
        "- 【严禁幻觉】key_concepts 中每个条目的 title 必须来自原文实际存在的章节或概念，禁止虚构综合摘要标题。\n"
        + _COMMON_FIDELITY_RULES
    ),
    "webpage": (
        "- 抽取主要概念、关键信息点，写入 learning_enhancement.knowledge_tags；\n"
        "- 若文中含可执行命令则一并提取到 procedure。\n"
        "- 【严禁幻觉】key_concepts 中每个条目的 title 必须来自原文实际存在的章节或概念，禁止虚构综合摘要标题。\n"
        + _COMMON_FIDELITY_RULES
    ),
}

_SYSTEM_PROMPT = (
    "你是结构化知识提取助手。先抽取原文有依据的关键约束，再提炼可执行决策规则，"
    "并以严格 JSON 输出。\n"
    "重要：直接输出 JSON，不要附带任何解释、不要使用 Markdown 代码块包裹。\n"
    "不要按章节写长篇复述。原文没有说明的字段填 unknown，禁止编造阈值、日期、区域或命令。\n"
    "source_quote 必须是原文中的连续短引；找不到就填 unknown。"
)

_USER_PROMPT_TEMPLATE = """【任务】
从以下{doc_type_label}中提取知识单元。

【提取重点】
{focus_rules}

【一文多卡】
若文档包含多个相对独立的主题，可返回 JSON 数组（每项一张笔记）；
否则返回单个 JSON 对象。

【边界约束】（重要）
每个 chunk 前有 <<CHUNK N — SOURCE_SECTION: xxx>> 标记。
**每张卡片必须严格对应源文档中一个明确的 Section**（以 SOURCE_SECTION 标记为准）。
不同卡片的内容不得跨 Section 混合——同一段原文不得出现在两张卡片中。
如果两个主题都引用了相同的原文内容，只保留一张卡。

【字段定义】
{
  "meta": {
    "name": "笔记标题（简洁，<= 30 字）",
    "type": ["command-oriented" | "concept-explanation" | "decision-tree" | "troubleshooting"],
    "intent": "一句话目的描述",
    "source_description": "若原文 frontmatter 有 description: 字段，必须 verbatim 保留（含领域列表 / 触发关键词等具体内容）；若无则空字符串",
    "trigger_keywords": ["关键词", "..."],
    "os": ["linux", "macos", "..."],
    "tools_required": ["工具名", "..."]
  },
  "preconditions": ["前置条件", "..."],
  "procedure": [
    {"seq": 1, "action": "动作描述", "command": "可选命令"}
  ],
  "decision_points": [
    {"condition": "判断条件", "then": "成立时", "else": "不成立时"}
  ],
  "halt_conditions": ["停止条件", "..."],
  "rollback_actions": ["回滚动作", "..."],
  "cross_references": ["[[关联笔记名]]", "..."],
  "constraints": [
    {
      "kind": "threshold | exception | safety_limit | abort | policy | other",
      "statement": "原文中的一条约束，短句，不要写整节译文",
      "value": "阈值或上限；原文没有则 unknown",
      "scope": "适用边界；原文没有则 unknown",
      "source_section": "所在章节标题",
      "source_quote": "原文连续短引；找不到则 unknown",
      "support": "sourced | unknown"
    }
  ],
  "rules": [
    {
      "condition": "何时",
      "judgment": "如何判断；原文没有则 unknown",
      "action": "做什么",
      "verification": "如何确认做到了；原文没有则 unknown",
      "boundary": "例外、停止边界或适用范围；原文没有则 unknown",
      "source_section": "所在章节标题",
      "source_quote": "原文连续短引；找不到则 unknown",
      "support": "sourced | unknown"
    }
  ],
  "key_concepts": [
    {"title": "术语或章节名（来自原文，不可虚构）", "explanation": "不超过80字的索引，指向约束或规则，不要复述整节", "example": "原文示例；没有则空字符串"}
  ],
  "learning_enhancement": {
    "pain_points": ["难点 / 避坑", "..."],
    "plain_summary": "白话一句话总结",
    "knowledge_tags": ["标签", "..."]
  },
  "source_reliability": "high | medium | low",
  "obsolescence_risk": "low | medium | high"
}

【可信度判断】
- high   : 官方文档、知名团队的 Skill 仓库
- medium : 大型技术博客、活跃维护的开源项目
- low    : 个人博客、论坛回答

【过时风险】
- high   : 涉及具体版本号、API 细节、易变命令
- medium : 涉及部署流程、第三方工具
- low    : 概念解释、设计原则

【文档元信息】
{metadata_json}

【文档正文（doc_type={doc_type}）】
{content}

请直接输出 JSON。"""


# ---------------------------------------------------------------------------
# 提取结果
# ---------------------------------------------------------------------------

@dataclass
class ExtractOutcome:
    units: list[dict]
    complete: bool
    truncated: bool = False
    failed_batches: list = field(default_factory=list)


def _repair_critical_gaps(source: str, gaps: list[dict], cfg: dict, console=None, evidence: list | None = None) -> list[dict]:
    """只把缺失关键句所在的原文片段再送一次。失败则保持缺口，不假装成功。"""
    snippets: list[str] = []
    for gap in gaps[:12]:
        anchor = str((gap.get("anchors") or [""])[0])
        idx = source.find(anchor[:80]) if anchor else -1
        if idx < 0 and anchor:
            idx = source.lower().find(anchor[:40].lower())
        snippets.append(source[max(0, idx - 160):idx + 420] if idx >= 0 else anchor)
    blob = (
        "下面是原文中尚未进入 constraints/rules 的关键句。只为这些句子补对象，"
        "source_quote 必须是片段里的连续原文。不要复述整篇。\n\n"
        + "\n\n---\n\n".join(snippets)
    )
    return _llm_extract(
        blob, "skill", {"title": "", "source_hash": "repair"}, cfg,
        console=console, evidence=evidence, batch_index=(len(evidence or []) + 1),
    )


# ---------------------------------------------------------------------------
# 提取入口
# ---------------------------------------------------------------------------

def extract_skill(
    raw_path: str,
    source_info: dict,
    cfg: dict,
    console=None,
) -> list[dict]:
    """
    对单个已缓存文档执行提取，返回草稿列表（已写盘）。

    Parameters
    ----------
    raw_path : str
        缓存的 .md 文件路径。
    source_info : dict
        list_cached() 中的一条记录，含 source_hash / doc_type / 等。
    cfg : dict
        全局配置（load_config()）。
    console : rich.console.Console | None
        可选输出。
    """
    ensure_dirs()

    raw_p = Path(raw_path)
    if not raw_p.exists():
        raise FileNotFoundError(f"原始文件不存在: {raw_path}")

    source_hash = source_info.get("source_hash") or ""
    if not source_hash:
        raise ValueError("source_info 缺少 source_hash")

    doc_type = source_info.get("doc_type", "skill")
    prompt_version = CURRENT_PROMPT_VERSION
    cache_params = extraction_cache_params(cfg)

    # 1. 读取原文
    text = _read_text_auto(raw_p)

    # 2. 只有完整成功的当前参数缓存才能复用
    cached_units = _load_extract_cache(source_hash, prompt_version, cache_params)
    if cached_units is not None:
        if console:
            console.print("  [dim]提取缓存命中，跳过 LLM 调用[/dim]")
        units = cached_units
        extract_complete = True
    else:
        # 3. 使用 chunker 分批提取，确保全文覆盖，不硬截断
        llm_cfg = _merged_llm_cfg(cfg)
        chunk_size = int(llm_cfg.get("chunk_size_tokens", 3000))
        chunk_overlap = int(llm_cfg.get("chunk_overlap_tokens", 200))
        # 每批发给 LLM 的字符上限（默认 12000 ≈ 4000 token，给 prompt 模板留余量）
        batch_chars = int(llm_cfg.get("batch_chars_per_request", 12000))

        chunker = Chunker(
            chunk_size_tokens=chunk_size,
            chunk_overlap_tokens=chunk_overlap,
        )
        chunks = chunker.chunk(text)
        total_chunks = len(chunks)

        if console:
            console.print(
                f"  [dim]文档 {len(text)} 字符，分为 {total_chunks} 个 chunk，"
                f"每批上限 {batch_chars} 字符[/dim]"
            )

        # 4. LLM 提取（凭证缺失向上抛，让用户配置；其他失败重试 → 启发式）
        evidence: list[dict] = []
        try:
            if total_chunks <= 1 or len(text) <= batch_chars:
                # 短文档：单次提取
                units = _llm_extract(
                    text, doc_type, source_info, cfg, console=console, evidence=evidence,
                )
                outcome = ExtractOutcome(units=units, complete=True)
            else:
                # 长文档：分批提取，同名卡片合并而不是丢掉后批
                outcome = _llm_extract_chunked(
                    chunks, batch_chars, doc_type, source_info, cfg,
                    console=console, evidence=evidence,
                )
            units = outcome.units
            extract_complete = outcome.complete
        except _CredentialError:
            raise
        except Exception as e:
            if console:
                console.print(f"  [yellow]⚠ LLM 提取失败，降级启发式:[/yellow] {e}")
            units = [_heuristic_extract(text, raw_p, source_info)]
            extract_complete = False
            outcome = ExtractOutcome(
                units=units,
                complete=False,
                failed_batches=["heuristic"],
            )
            evidence = []

        from skillmind.gates import find_critical_gaps
        gaps = find_critical_gaps(text, units)
        if gaps and extract_complete:
            try:
                repaired = _repair_critical_gaps(text, gaps, cfg, console=console, evidence=evidence)
                if repaired:
                    units = combine_batch_results([units, repaired], doc_type=doc_type, console=console)
                gaps = find_critical_gaps(text, units)
            except _CredentialError:
                raise
            except Exception as repair_error:
                if console:
                    console.print(f"  [yellow]关键句补提取失败：{repair_error}[/yellow]")
        if gaps:
            extract_complete = False
            if console:
                console.print(f"  [yellow]仍缺 {len(gaps)} 条关键句，不记为完整成功[/yellow]")

        # 5. 失败、截断和关键缺口都落盘，但 complete=false，下次不会当成功复用
        _save_extract_cache(
            source_hash,
            prompt_version,
            units,
            cache_params,
            complete=extract_complete,
            truncated=outcome.truncated,
            failed_batches=outcome.failed_batches,
            batches=evidence,
            critical_gaps=[str((gap.get("anchors") or [""])[0])[:180] for gap in gaps],
        )

    if not units:
        units = [_heuristic_extract(text, raw_p, source_info)]
        extract_complete = False

    # 6. 组装并落盘草稿
    drafts: list[dict] = []
    total = len(units)
    for idx, unit in enumerate(units, start=1):
        draft = _assemble_draft(
            unit,
            source_info,
            idx=idx,
            total=total,
            prompt_version=prompt_version,
            extract_complete=extract_complete,
        )
        save_draft(draft)
        drafts.append(draft)

    return drafts


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

class _CredentialError(RuntimeError):
    """LLM 凭证缺失/无效；属配置错误，不应被启发式吃掉。"""


class _EmptyResponseError(ValueError):
    """LLM 返回空数组/空对象；重试无意义，应直接降级启发式。"""


class _IncompleteJSONError(ValueError):
    """截断 JSON 即使能补括号，也不是完整成功。"""

    def __init__(self, message: str, partial: list | None = None):
        super().__init__(message)
        self.partial = partial or []


def _blank_to_unknown(record: dict, slots: tuple[str, ...]) -> None:
    for slot in slots:
        if not str(record.get(slot) or "").strip():
            record[slot] = _UNKNOWN


def _normalize_unit(unit: dict, source_text: str) -> dict:
    """空槽位写成 unknown。不根据原文编造 quote；对不上原文的引用标 unsupported。"""
    if not isinstance(unit, dict):
        return {"constraints": [], "rules": []}
    constraints: list[dict] = []
    for raw in unit.get("constraints") or []:
        if isinstance(raw, str):
            raw = {"statement": raw}
        if not isinstance(raw, dict):
            continue
        _blank_to_unknown(raw, ("kind", "value", "scope", "source_section"))
        statement = str(raw.get("statement") or "").strip()
        quote = str(raw.get("source_quote") or "").strip()
        if not statement and not quote:
            continue
        if not statement:
            raw["statement"] = _UNKNOWN
        raw["source_quote"] = quote or _UNKNOWN
        asserts = statement not in ("", _UNKNOWN)
        if not quote or quote == _UNKNOWN:
            raw["support"] = "unsupported" if asserts else _UNKNOWN
        elif quote_in_source(quote, source_text):
            raw["support"] = "sourced"
        else:
            raw["support"] = "unsupported"
        constraints.append(raw)
    unit["constraints"] = constraints

    rules: list[dict] = []
    for raw in unit.get("rules") or []:
        if not isinstance(raw, dict):
            continue
        _blank_to_unknown(
            raw,
            ("condition", "judgment", "action", "verification", "boundary", "source_section"),
        )
        quote = str(raw.get("source_quote") or "").strip()
        asserts = any(not _is_unknown_slot(raw.get(name)) for name in (
            "condition", "judgment", "action", "verification", "boundary",
        ))
        raw["source_quote"] = quote or _UNKNOWN
        if not quote or quote == _UNKNOWN:
            raw["support"] = "unsupported" if asserts else _UNKNOWN
        elif quote_in_source(quote, source_text):
            raw["support"] = "sourced"
        else:
            raw["support"] = "unsupported"
        rules.append(raw)
    unit["rules"] = rules
    return unit


def _is_unknown_slot(value: object) -> bool:
    return norm_ws(str(value or "")).casefold() in {"", _UNKNOWN, "未知", "未说明"}


def _normalize_title(title: str) -> str:
    """标准化标题用于去重比较：小写、去标点、折叠空白。"""
    t = title.lower()
    t = re.sub(r'[^\w\s]', '', t)   # 去标点
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _dedup_key_concepts(concepts: list[dict]) -> list[dict]:
    """对 key_concepts 列表做去重，保留首次出现的条目。

    去重规则（按优先级）：
    1. 精确 title 匹配（标准化后）
    2. 一方 title 是另一方的子串（标准化后长度差 ≤ 10 字符）
    """
    result: list[dict] = []
    seen: list[str] = []  # 已见的标准化 title 列表

    for kc in concepts:
        if not isinstance(kc, dict):
            continue
        raw_title = kc.get("title", "")
        norm = _normalize_title(raw_title)
        if not norm:
            result.append(kc)
            continue

        duplicate = False
        for s in seen:
            # 精确匹配
            if norm == s:
                duplicate = True
                break
            # 子串包含（短的包含在长的里，且长度差在阈值内）
            shorter, longer = (norm, s) if len(norm) <= len(s) else (s, norm)
            if shorter in longer and (len(longer) - len(shorter)) <= 12:
                duplicate = True
                break

        if not duplicate:
            seen.append(norm)
            result.append(kc)

    return result


def _kc_title_set(unit: dict) -> set[str]:
    """提取一张卡的 key_concepts 标准化 title 集合，用于卡间 Jaccard 相似度。"""
    titles: set[str] = set()
    for kc in unit.get("key_concepts", []) or []:
        if not isinstance(kc, dict):
            continue
        norm = _normalize_title(kc.get("title", ""))
        if norm:
            titles.add(norm)
    return titles


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _union_dicts(existing: list, incoming: list, key_fn) -> list:
    merged = [item for item in existing or [] if isinstance(item, dict)]
    seen = {key_fn(item) for item in merged}
    seen.discard("")
    for item in incoming or []:
        if not isinstance(item, dict):
            continue
        key = key_fn(item)
        if key and key in seen:
            continue
        merged.append(item)
        if key:
            seen.add(key)
    return merged


def _union_strs(existing: list, incoming: list) -> list:
    merged = [str(item) for item in existing or [] if str(item).strip()]
    seen = {_normalize_title(item) for item in merged}
    for item in incoming or []:
        text = str(item).strip()
        key = _normalize_title(text)
        if not text or (key and key in seen):
            continue
        merged.append(text)
        if key:
            seen.add(key)
    return merged


def _absorb_unit(existing: dict, incoming: dict) -> None:
    """把后一批同名卡片的约束、规则和步骤并入先出现的卡。"""
    existing["key_concepts"] = _union_dicts(
        existing.get("key_concepts") or [],
        incoming.get("key_concepts") or [],
        lambda item: _normalize_title(str(item.get("title") or "")),
    )
    existing["constraints"] = _union_dicts(
        existing.get("constraints") or [],
        incoming.get("constraints") or [],
        lambda item: _normalize_title(str(item.get("statement") or item.get("source_quote") or "")),
    )
    existing["rules"] = _union_dicts(
        existing.get("rules") or [],
        incoming.get("rules") or [],
        lambda item: _normalize_title(
            f"{item.get('condition') or ''} {item.get('action') or ''}"
        ),
    )
    existing["decision_points"] = _union_dicts(
        existing.get("decision_points") or [],
        incoming.get("decision_points") or [],
        lambda item: _normalize_title(str(item.get("condition") or "")),
    )
    existing["procedure"] = _union_dicts(
        existing.get("procedure") or [],
        incoming.get("procedure") or [],
        lambda item: _normalize_title(str(item.get("action") or "")),
    )
    for name in ("halt_conditions", "preconditions", "rollback_actions"):
        existing[name] = _union_strs(existing.get(name) or [], incoming.get(name) or [])


def combine_batch_results(batch_units: list[list[dict]], *, doc_type: str = "skill", console=None) -> list[dict]:
    """合并各批结果。同名卡片保留后批中新出现的约束和规则。"""
    flat = [unit for batch in batch_units for unit in batch if isinstance(unit, dict)]
    return _merge_units(flat, doc_type=doc_type, console=console)


def _merge_units(all_units: list[dict], *, doc_type: str = "skill", console=None) -> list[dict]:
    """跨批次合并 units：

    1. 卡片级去重：meta.name 相同（标准化）→ 保留首次，但把后续卡片独有的
       key_concepts 合并进首次卡片，避免信息丢失。
    2. 卡间内容重叠合并：不同 name 但 key_concepts 标题集 Jaccard ≥ 0.4 视为同一逻辑卡，
       合并 key_concepts 并保留先出现的 meta（避免不同语言同义名重复保留）。
    3. 单卡内 key_concepts 去重：同一张卡内重复条目去除。
    4. 跨卡 key_concepts 去重：已在其他卡出现的 key_concept title 不重复出现。
    """
    # --- 第一步：卡片级合并（同名）---
    merged: list[dict] = []
    name_to_idx: dict[str, int] = {}  # 标准化 name → merged 中的下标

    for unit in all_units:
        name = unit.get("meta", {}).get("name", "")
        norm_name = _normalize_title(name)

        if norm_name and norm_name in name_to_idx:
            # 同名卡合并后批独有的约束、规则和概念，不丢弃后出现的批次。
            _absorb_unit(merged[name_to_idx[norm_name]], unit)
        else:
            if norm_name:
                name_to_idx[norm_name] = len(merged)
            merged.append(unit)

    # --- 第一步 b：基于 key_concepts Jaccard 的卡间重叠合并 ---
    # 处理"不同语言同义名"（如 'Brandkit Image Generation Skill' vs '品牌视觉识别设计技能'）
    # 阈值 0.4：两张卡的 key_concept titles 集合并集中 40%+ 重合即视为同一卡
    JACCARD_THRESHOLD = 0.4
    merged_jc: list[dict] = []
    kc_sets: list[set[str]] = []
    for unit in merged:
        cur_set = _kc_title_set(unit)
        merged_target: int | None = None
        if cur_set:
            for i, prev_set in enumerate(kc_sets):
                if _jaccard(cur_set, prev_set) >= JACCARD_THRESHOLD:
                    merged_target = i
                    break
        if merged_target is None:
            merged_jc.append(unit)
            kc_sets.append(cur_set)
        else:
            existing = merged_jc[merged_target]
            _absorb_unit(existing, unit)
            kc_sets[merged_target] = _kc_title_set(existing)
            if console:
                a_name = existing.get("meta", {}).get("name", "")
                b_name = unit.get("meta", {}).get("name", "")
                console.print(
                    f"  [dim]内容重叠合并：'{b_name}' → '{a_name}' "
                    f"（Jaccard ≥ {JACCARD_THRESHOLD}）[/dim]"
                )
    merged = merged_jc

    # --- 第二步：每张卡内部 key_concepts 去重 ---
    for unit in merged:
        kcs = unit.get("key_concepts", [])
        if kcs:
            unit["key_concepts"] = _dedup_key_concepts(kcs)

    # --- 第三步：跨卡 key_concepts 去重（后出现的卡不重复前面卡已有的条目）---
    global_kc_seen: list[str] = []
    for unit in merged:
        kcs = unit.get("key_concepts", [])
        if not kcs:
            continue
        deduped = []
        for kc in kcs:
            norm = _normalize_title(kc.get("title", ""))
            if not norm:
                deduped.append(kc)
                continue
            # 检查是否已在全局出现
            is_dup = False
            for g in global_kc_seen:
                shorter, longer = (norm, g) if len(norm) <= len(g) else (g, norm)
                if shorter == longer or (shorter in longer and (len(longer) - len(shorter)) <= 12):
                    is_dup = True
                    break
            if not is_dup:
                global_kc_seen.append(norm)
                deduped.append(kc)
        unit["key_concepts"] = deduped

    # --- 第四步：近重复卡检测与删除（仅对 blog/design_system 生效）---
    # skill 文档各 section 之间有天然重叠（如多个阶段都提到同一前置条件），
    # 这些重叠是合法结构，不应删除。
    if doc_type not in ("blog", "design_system"):
        return merged

    NEAR_DUP_THRESHOLD = 0.7
    deduped: list[dict] = []
    deduped_kc_sets: list[set[str]] = []

    for unit in merged:
        cur_set = _kc_title_set(unit)
        if not cur_set:
            deduped.append(unit)
            deduped_kc_sets.append(set())
            continue

        # 检查是否与已保留卡近重复
        absorbed = False
        for i, prev_set in enumerate(deduped_kc_sets):
            if _jaccard(cur_set, prev_set) >= NEAR_DUP_THRESHOLD and len(cur_set) <= len(prev_set):
                absorbed = True
                if console:
                    a_name = deduped[i].get("meta", {}).get("name", "")
                    b_name = unit.get("meta", {}).get("name", "")
                    console.print(
                        f"  [dim]近重复卡删除：'{b_name}' 被 '{a_name}' 吸收"
                        f"（Jaccard {NEAR_DUP_THRESHOLD}，content overlap 70%+）[/dim]"
                    )
                break

        if not absorbed:
            deduped.append(unit)
            deduped_kc_sets.append(cur_set)

    return deduped


def _llm_extract_chunked(
    chunks: list[dict],
    batch_chars: int,
    doc_type: str,
    source_info: dict,
    cfg: dict,
    console=None,
    evidence: list | None = None,
) -> ExtractOutcome:
    """按预算分批提取。装不下的正文进入下一批，同名卡片再合并。

    任一批失败或正文被丢弃时，结果 complete=False，不能当完整成功缓存。
    """
    pending = list(chunks)
    batch_units: list[list[dict]] = []
    failed: list = []
    truncated = False
    guard = 0
    while pending:
        guard += 1
        if guard > len(chunks) + 8:
            failed.append("packer-stuck")
            truncated = True
            break
        batch_text, leftover, dropped = pack_chunks_for_prompt(pending, batch_chars)
        if console:
            console.print(
                f"  [dim]  批次 {guard}：{len(batch_text)} 字符，剩余 {len(leftover)} 段[/dim]"
            )
        if dropped or not batch_text.strip():
            failed.append(guard)
            truncated = True
            break
        try:
            batch_units.append(
                _llm_extract(
                    batch_text, doc_type, source_info, cfg, console=console,
                    evidence=evidence, batch_index=guard,
                )
            )
        except _CredentialError:
            raise
        except Exception as e:
            if console:
                console.print(f"  [yellow]  批次 {guard} 失败：{e}[/yellow]")
            failed.append(guard)
        if not leftover:
            break
        if leftover == pending:
            failed.append("no-progress")
            truncated = True
            break
        pending = leftover

    if not batch_units:
        raise RuntimeError("所有批次均提取失败")

    merged = combine_batch_results(batch_units, doc_type=doc_type, console=console)
    complete = not failed and not truncated
    return ExtractOutcome(
        units=merged,
        complete=complete,
        truncated=truncated,
        failed_batches=failed,
    )


def _llm_extract(
    text: str,
    doc_type: str,
    source_info: dict,
    cfg: dict,
    console=None,
    evidence: list | None = None,
    batch_index: int = 1,
) -> list[dict]:
    """调用 LLM 并解析 JSON。失败按 max_retries 指数退避重试。"""
    try:
        creds = resolve_llm_credentials(cfg, command="extract")
    except RuntimeError as e:
        # 凭证类错误明确包装，让上层不要降级
        raise _CredentialError(str(e))

    metadata = {
        "doc_type": doc_type,
        "title": source_info.get("title", ""),
        "author": source_info.get("author", ""),
        "source_url": source_info.get("source_url", ""),
        "source_repo": source_info.get("source_repo", ""),
        "source_path": source_info.get("source_path", ""),
        "published_at": source_info.get("published_at", ""),
    }

    # 注意：不能用 str.format()，文档正文 text 可能含 { } （代码/JSON/Shell），
    # 会导致 KeyError / ValueError。改用手动字符串替换，安全无副作用。
    user_prompt = (
        _USER_PROMPT_TEMPLATE
        .replace("{doc_type_label}", _DOC_TYPE_LABEL.get(doc_type, "文档"))
        .replace("{focus_rules}", _FOCUS_RULES.get(doc_type, _FOCUS_RULES["webpage"]))
        .replace("{metadata_json}", json.dumps(metadata, ensure_ascii=False))
        .replace("{doc_type}", doc_type)
        .replace("{content}", text)
    )

    llm_cfg = _merged_llm_cfg(cfg)
    timeout = int(llm_cfg.get("timeout", 120))
    max_retries = int(llm_cfg.get("max_retries", 2))
    temperature = float(llm_cfg.get("temperature", 0.2))

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            units, raw = _llm_call_once(
                creds, user_prompt, timeout=timeout, temperature=temperature,
            )
            if evidence is not None:
                evidence.append(_evidence_record(
                    user_prompt, raw, index=batch_index, truncated=False,
                ))
            return [_normalize_unit(unit, text) for unit in units]
        except _CredentialError:
            raise
        except _EmptyResponseError as e:
            # 空数组/无效结构：重试不会改变结果，直接抛出让上层降级启发式
            raise RuntimeError(f"LLM 返回空结果，降级启发式: {e}") from e
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait = 1.0 * (2 ** attempt)  # 1s, 2s, 4s ...
                if console:
                    console.print(
                        f"  [yellow]LLM 失败（第 {attempt+1}/{max_retries+1} 次），"
                        f"{wait:.0f}s 后重试: {e}[/yellow]"
                    )
                time.sleep(wait)

    raise RuntimeError(f"LLM 调用 {max_retries+1} 次后仍失败: {last_err}")


def _llm_call_once(
    creds: dict,
    user_prompt: str,
    *,
    timeout: int,
    temperature: float = 0.2,
) -> list[dict]:
    from litellm import completion  # type: ignore

    kwargs: dict[str, Any] = {
        "model": creds["model"],
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "api_key": creds["api_key"],
        "timeout": timeout,
        "max_tokens": 16384,   # 长文档可能生成大量 key_concepts，防止响应被截断
    }
    if "api_base" in creds:
        kwargs["api_base"] = creds["api_base"]

    resp = completion(**kwargs)
    raw = (resp.choices[0].message.content or "").strip()
    return _parse_json_response(raw), raw


_CODEBLOCK_RE = re.compile(r"```(?:json|JSON)?\s*\n([\s\S]{1,30000}?)```")


def _fix_truncated_json(text: str) -> str:
    """尝试修复被截断的 JSON 字符串。

    常见场景：LLM 在 max_tokens 处被截断，导致 JSON 末尾缺少 `}]` 等。
    策略：找到最后一个完整的 `}` 位置，补全缺失的 `]` 或 `}`。
    """
    text = text.strip()
    if not text:
        return text

    # 如果已经是合法 JSON 就直接返回
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # 找最后一个完整对象结束符 }
    last_brace = text.rfind("}")
    if last_brace < 0:
        return text

    truncated = text[:last_brace + 1]

    # 统计未闭合的 [ 和 { 数量（简单计数，忽略字符串内的括号）
    depth_square = 0
    depth_curly = 0
    in_string = False
    escape = False
    for ch in truncated:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
        elif ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1

    # 补全未闭合的括号
    suffix = "}" * max(0, depth_curly) + "]" * max(0, depth_square)
    return truncated + suffix


def _parse_json_response(text: str) -> list[dict]:
    """剥离可能的 ``` 包裹，解析为 list[dict]。含截断修复。"""
    text = text.strip()
    # 截断超长响应，防止正则回溯地狱（LLM 偶尔返回超大文本）
    if len(text) > 32000:
        text = text[:32000]
    # 1. 整体被 ``` 包裹
    m = _CODEBLOCK_RE.search(text)
    if m:
        text = m.group(1).strip()

    # 2. 兜底：截取最外层 [...] 或 {...}
    if not text.startswith(("{", "[")):
        idx_obj = text.find("{")
        idx_arr = text.find("[")
        candidates = [i for i in (idx_obj, idx_arr) if i >= 0]
        if not candidates:
            raise ValueError("响应中未找到 JSON 起始符")
        text = text[min(candidates):]
        last = max(text.rfind("}"), text.rfind("]"))
        if last >= 0:
            text = text[:last + 1]

    # 3. 完整 JSON 才算成功。截断后补括号得到的对象不能当完整结果。
    incomplete = False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        fixed = _fix_truncated_json(text)
        try:
            parsed = json.loads(fixed)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败（修复后仍无效）: {e}") from e
        incomplete = True

    if isinstance(parsed, dict):
        units = [parsed]
    elif isinstance(parsed, list):
        units = [u for u in parsed if isinstance(u, dict)]
        if not units:
            raise _EmptyResponseError(
                f"数组中没有有效对象（共 {len(parsed)} 项，类型：{[type(x).__name__ for x in parsed[:3]]}）"
            )
    else:
        raise ValueError(f"LLM 响应不是 JSON 对象/数组: {type(parsed).__name__}")
    if incomplete:
        raise _IncompleteJSONError(
            "JSON 被截断，修复结果不能当作完整成功",
            partial=units,
        )
    return units


# ---------------------------------------------------------------------------
# 启发式兜底（LLM 失败时使用 parser 提取最低限度信息）
# ---------------------------------------------------------------------------

def _extract_list_items(content: str) -> list[str]:
    """从 Markdown 片段提取列表项；无列表时返回前几行非空文本。"""
    items: list[str] = []
    for line in content.splitlines():
        s = line.strip()
        m = re.match(r"^[-*+]\s+(.*)", s) or re.match(r"^\d+[.、)]\s+(.*)", s)
        if m:
            t = m.group(1).strip()
            if t:
                items.append(t)
    if items:
        return items
    return [ln.strip() for ln in content.splitlines() if ln.strip()][:5]


def _heuristic_extract(text: str, raw_path: Path, source_info: dict) -> dict:
    """LLM 不可用时，按 parser 区块结果生成最小可发布草稿。"""
    parsed = parse_skill_file(raw_path)
    title = parsed.get("title") or raw_path.stem
    sections = parsed.get("sections", [])

    procedure: list[dict] = []
    preconditions: list[str] = []
    halt_conditions: list[str] = []
    rollback_actions: list[str] = []
    decision_points: list[dict] = []
    notes: list[str] = []
    summary = ""

    for sec in sections:
        st = sec.get("section_type", "other")
        content = sec.get("content", "")
        if not content.strip():
            continue
        if st == "procedure":
            for step in _extract_list_items(content):
                procedure.append({"seq": len(procedure) + 1, "action": step})
        elif st == "preconditions":
            preconditions.extend(_extract_list_items(content))
        elif st == "halt_conditions":
            halt_conditions.extend(_extract_list_items(content))
        elif st == "rollback":
            rollback_actions.extend(_extract_list_items(content))
        elif st == "decisions":
            decision_points.append({
                "condition": sec.get("heading", "")[:80],
                "then": content[:200],
            })
        elif st == "overview" and not summary:
            summary = content[:300]
        elif st == "notes":
            notes.extend(_extract_list_items(content)[:5])

    if not summary:
        for sec in sections:
            if sec.get("content", "").strip():
                summary = sec["content"][:200]
                break

    types = ["command-oriented"] if procedure else ["concept-explanation"]
    if decision_points:
        types.append("decision-tree")

    constraints = []
    for item in scan_critical_anchors(text):
        quote = (item.get("anchors") or [""])[0]
        constraints.append({
            "kind": item.get("kind") or "constraint",
            "statement": item.get("statement") or _UNKNOWN,
            "value": _UNKNOWN,
            "scope": _UNKNOWN,
            "source_section": item.get("section") or _UNKNOWN,
            "source_quote": quote or _UNKNOWN,
            "support": "sourced" if quote_in_source(quote, text) else "unsupported",
        })
    rules = []
    for point in decision_points:
        condition = str(point.get("condition") or "").strip()
        action = str(point.get("then") or "").strip()
        quote = action if quote_in_source(action, text) else _UNKNOWN
        rules.append({
            "condition": condition or _UNKNOWN,
            "judgment": _UNKNOWN,
            "action": action or _UNKNOWN,
            "verification": _UNKNOWN,
            "boundary": _UNKNOWN,
            "source_section": condition or _UNKNOWN,
            "source_quote": quote,
            "support": "sourced" if quote != _UNKNOWN else "unsupported",
        })

    return _normalize_unit({
        "meta": {
            "name": title[:60],
            "type": list(dict.fromkeys(types)),
            "intent": title[:80],
            "trigger_keywords": [],
            "os": [],
            "tools_required": [],
        },
        "preconditions": preconditions,
        "procedure": procedure,
        "decision_points": decision_points,
        "halt_conditions": halt_conditions,
        "rollback_actions": rollback_actions,
        "cross_references": [],
        "constraints": constraints,
        "rules": rules,
        "learning_enhancement": {
            "pain_points": notes,
            "plain_summary": summary or title,
            "knowledge_tags": [],
        },
        "source_reliability": "medium",
        "obsolescence_risk": "medium",
    }, text)


# ---------------------------------------------------------------------------
# 草稿组装
# ---------------------------------------------------------------------------

def _assemble_draft(
    unit: dict,
    source_info: dict,
    *,
    idx: int,
    total: int,
    prompt_version: str,
    extract_complete: bool = False,
) -> dict:
    """
    把 LLM/启发式输出的 unit 与 source_info 合并为完整草稿。

    多卡时 uuid 形如 'skill-<sha7>-2'；单卡 'skill-<sha7>'。
    顶层 doc_type 字段冗余存放，cli review 在 source 缺失时兜底使用。
    """
    source_hash = source_info["source_hash"]
    doc_type = source_info.get("doc_type", "skill")

    # uuid：优先使用 LLM 给出的；否则按 hash + 序号生成
    uuid = unit.get("uuid")
    if not uuid or not isinstance(uuid, str):
        suffix = f"-{idx}" if total > 1 else ""
        uuid = f"{doc_type}-{source_hash[:7]}{suffix}"

    # 仅当 source_repo 是 http(s) URL 时才作为 repo_url 暴露给 trace/renderer
    repo_url_candidate = source_info.get("source_repo", "")
    repo_url = repo_url_candidate if repo_url_candidate.startswith(("http://", "https://")) else ""

    source_block = {
        "doc_type": doc_type,
        "source_hash": source_hash,
        "source_repo": source_info.get("source_repo", ""),
        "source_path": source_info.get("source_path", ""),
        "file_path": source_info.get("source_path", ""),
        "source_url": source_info.get("source_url", ""),
        "repo_url": repo_url,
        "commit_sha": source_info.get("commit_sha", "") or "",
        "fetch_time": source_info.get("fetch_time", 0),
        "raw_path": source_info.get("raw_path", ""),
        "author": source_info.get("author", ""),
        "published_at": source_info.get("published_at", ""),
        "branch": "main",
    }

    if total > 1:
        source_block["card_index"] = idx
        source_block["card_total"] = total
        source_block["parent_source"] = (
            source_info.get("source_url")
            or source_info.get("source_repo")
            or ""
        )

    return {
        "uuid": uuid,
        "doc_type": doc_type,
        "source": source_block,
        "meta": unit.get("meta", {}),
        "preconditions": unit.get("preconditions", []),
        "procedure": unit.get("procedure", []),
        "decision_points": unit.get("decision_points", []),
        "halt_conditions": unit.get("halt_conditions", []),
        "rollback_actions": unit.get("rollback_actions", []),
        "cross_references": unit.get("cross_references", []),
        "constraints": unit.get("constraints", []),
        "rules": unit.get("rules", []),
        "key_concepts": unit.get("key_concepts", []),
        "learning_enhancement": unit.get("learning_enhancement", {}),
        "source_reliability": unit.get("source_reliability", "medium"),
        "obsolescence_risk": unit.get("obsolescence_risk", "medium"),
        "prompt_version": prompt_version,
        "schema_version": SCHEMA_VERSION,
        "extract_complete": bool(extract_complete),
        "status": "draft",
        "created_at": time.strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# 提取缓存
# ---------------------------------------------------------------------------

def _merged_llm_cfg(cfg: dict) -> dict:
    llm = dict(cfg.get("llm") or {})
    profile = (cfg.get("llm_profiles") or {}).get("extract") or {}
    if isinstance(profile, dict):
        for key, value in profile.items():
            if value not in (None, ""):
                llm[key] = value
    return llm


def extraction_cache_params(cfg: dict | None) -> dict:
    """影响提取结果的参数。模型、分块和温度变化都会使缓存失效。"""
    llm = _merged_llm_cfg(cfg or {})
    return {
        "model": str(llm.get("model") or ""),
        "chunk_size_tokens": int(llm.get("chunk_size_tokens", 3000)),
        "chunk_overlap_tokens": int(llm.get("chunk_overlap_tokens", 200)),
        "batch_chars_per_request": int(llm.get("batch_chars_per_request", 12000)),
        "min_chunk_size_tokens": int(llm.get("min_chunk_size_tokens", 100)),
        "temperature": float(llm.get("temperature", 0.2)),
        "schema_version": SCHEMA_VERSION,
    }


def cache_param_token(params: dict) -> str:
    blob = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _extract_cache_path(source_hash: str, prompt_version: str, param_token: str = "") -> Path:
    if param_token:
        return EXTRACT_CACHE_DIR / f"{source_hash}_{prompt_version}_{param_token}.json"
    return EXTRACT_CACHE_DIR / f"{source_hash}_{prompt_version}.json"


def read_extract_cache_any(path: str | Path) -> dict:
    """读取旧的裸列表或 v2 信封。旧格式 complete=false，不能冒充当前成功提取。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {
            "cache_version": 1,
            "legacy": True,
            "complete": False,
            "truncated": False,
            "failed_batches": [],
            "schema_version": "legacy",
            "units": [unit for unit in data if isinstance(unit, dict)],
        }
    if isinstance(data, dict) and isinstance(data.get("units"), list):
        record = dict(data)
        record.setdefault("legacy", record.get("cache_version", 2) < 2 and not record.get("schema_version"))
        return record
    if isinstance(data, dict):
        return {
            "cache_version": 1,
            "legacy": True,
            "complete": False,
            "schema_version": "legacy",
            "units": [data],
        }
    raise ValueError(f"无法识别的提取缓存: {path}")


def _cache_is_reusable(record: dict, params: dict) -> bool:
    if not isinstance(record, dict) or record.get("legacy"):
        return False
    if not record.get("complete") or record.get("truncated") or record.get("failed_batches") or record.get("critical_gaps"):
        return False
    if int(record.get("cache_version") or 0) != CACHE_FORMAT_VERSION:
        return False
    if record.get("schema_version") != params.get("schema_version"):
        return False
    if record.get("model") != params.get("model"):
        return False
    stored = record.get("params") or {}
    for key, value in params.items():
        if stored.get(key) != value:
            return False
    units = record.get("units")
    return isinstance(units, list) and any(isinstance(unit, dict) for unit in units)


def _load_extract_cache(source_hash: str, prompt_version: str, params: dict) -> list[dict] | None:
    path = _extract_cache_path(source_hash, prompt_version, cache_param_token(params))
    if not path.exists():
        return None
    try:
        record = read_extract_cache_any(path)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not _cache_is_reusable(record, params):
        return None
    return [unit for unit in record["units"] if isinstance(unit, dict)]


def has_reusable_extract_cache(source_hash: str, cfg: dict) -> bool:
    return _load_extract_cache(source_hash, CURRENT_PROMPT_VERSION, extraction_cache_params(cfg)) is not None


def _redact_secrets(text: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "[redacted]", text or "")


def _evidence_record(prompt: str, raw: str, *, index: int, truncated: bool) -> dict:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return {
        "index": index,
        "prompt_sha256": digest,
        "raw_response": _redact_secrets(raw),
        "truncated": bool(truncated),
    }


def _save_extract_cache(
    source_hash: str,
    prompt_version: str,
    units: list[dict],
    params: dict,
    *,
    complete: bool,
    truncated: bool = False,
    failed_batches: list | None = None,
    batches: list | None = None,
    critical_gaps: list | None = None,
) -> None:
    failures = list(failed_batches or [])
    gaps = list(critical_gaps or [])
    reusable = bool(complete) and not truncated and not failures and not gaps
    envelope = {
        "cache_version": CACHE_FORMAT_VERSION,
        "source_hash": source_hash,
        "prompt_version": prompt_version,
        "schema_version": params.get("schema_version", SCHEMA_VERSION),
        "model": params.get("model", ""),
        "params": params,
        "complete": reusable,
        "truncated": bool(truncated),
        "failed_batches": failures,
        "critical_gaps": gaps,
        "batches": [
            {key: value for key, value in batch.items() if key != "api_key"}
            for batch in (batches or [])
            if isinstance(batch, dict)
        ],
        "legacy": False,
        "units": units,
    }
    path = _extract_cache_path(source_hash, prompt_version, cache_param_token(params))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False, indent=2)
    tmp.replace(path)


def clean_stale_extract_caches(console=None) -> int:
    """清理旧的 structure/evidence/raw 后缀缓存。

    当前文件名是 {hash}_{prompt}_{param_token}.json，不能按“多一段后缀”删除。
    """
    deleted = 0
    if not EXTRACT_CACHE_DIR.exists():
        return 0
    legacy = re.compile(r"_extract_v\d+_(structure|evidence|raw)$")
    for path in EXTRACT_CACHE_DIR.glob("*.json"):
        if not legacy.search(path.stem):
            continue
        try:
            path.unlink()
            deleted += 1
            if console:
                console.print(f"  [dim]删除旧缓存：{path.name}[/dim]")
        except OSError as exc:
            if console:
                console.print(f"  [yellow]删除失败：{path.name}：{exc}[/yellow]")
    return deleted
