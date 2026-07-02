"""提取引擎（Extractor）v2.2

职责：将单个已缓存的原始文档转成 1~N 张结构化草稿（    "blog": (
        "- 首先识别文章的所有 H2/H3 小节标题；若无明显标题，按自然段落主题划分内容段。\n"
        "- 每个小节或内容段必须映射为一个 key_concept 条目（title=小节标题或推断的主题名，\n"
        "  explanation=该段核心内容，example=代码/示例/引用）。\n"
        "- 若文章有可执行步骤，同时填入 procedure；作者的核心观点写入 plain_summary。\n"
        "- 若长文涵盖多个独立主题，按主题拆成多张卡。\n"
        "- 【覆盖要求】所有小节/段落必须在 key_concepts 或 procedure 中有对应条目，不得遗漏任何段落。\n"
        "- 【严禁幻觉】key_concepts[].title 必须来自原文真实存在的章节标题或明确命名的概念，"
        "禁止虚构综合摘要标题。"
    ),
    "forum_post": (
        "- 将主帖问题写入 plain_summary（问题描述）。\n"
        "- 每个独立的回答/方案必须映射为一个 key_concept 条目\n"
        "  （title=该方案核心思路的简短命名，explanation=方案详情，example=代码/命令）。\n"
        "- 不同方案的适用条件写入 decision_points，标记最终采纳方案。\n"
        "- 【覆盖要求】每个有实质内容的回答都必须在 key_concepts 或 decision_points 中体现。\n"
        "- 【严禁幻觉】key_concepts[].title 必须来自原文真实存在的章节标题或明确命名的概念，"
        "禁止虚构综合摘要标题。"
    ),
    "webpage": (
        "- 识别页面的主要区块（如 Hero/Features/Pricing/FAQ 等），每个区块映射为一个 key_concept。\n"
        "- 若页面含可执行命令或操作步骤，提取到 procedure。\n"
        "- 页面核心价值主张写入 plain_summary，重要关键词写入 knowledge_tags。\n"
        "- 【覆盖要求】所有主要区块必须在 key_concepts 中有对应条目，不得只提取显眼标题而忽略内容区块。\n"
        "- 【严禁幻觉】key_concepts[].title 必须来自原文真实存在的章节标题或明确命名的概念，"
        "禁止虚构综合摘要标题。"
    ),RD §4.4 一致）：
- 动态 Prompt 路由：按 doc_type 选择提取重点。
- 一文多卡：LLM 可返回 JSON 数组，每项渲染为独立笔记。
- 可信度 / 过时风险：source_reliability / obsolescence_risk 由 LLM 标注。
- 提取缓存：同 (source_hash, prompt_version) 命中直接复用，避免重复消费 Token。
- LLM 失败兜底：超时/JSON 解析失败 → 指数退避重试 → 全部失败后走启发式规则。
- 凭证缺失（resolve_llm_credentials 抛出）属配置错误，向上抛，不静默降级。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from skillmind.config import (
    CURRENT_PROMPT_VERSION,
    EXTRACT_CACHE_DIR,
    ensure_dirs,
    resolve_llm_credentials,
)
from skillmind.chunker import Chunker, join_chunks_for_prompt
from skillmind.parser import _read_text_auto, parse_skill_file
from skillmind.reviewer import save_draft


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
    "- 【长列表强制全保留】原文若出现连续 5 条以上的 bullet / numbered list（如 18 个组件、\n"
    "  20 个 baseline 参数、27 项检查清单），该列表必须 verbatim 完整保留到对应 key_concept 的\n"
    "  explanation 或 example 字段，禁止用'等'、'诸如此类'、'...'省略；宁可单条 explanation 长一些。\n"
    "- 【数值阈值 verbatim】原文中出现的 `Choose exactly N`、`1-10 评分`、`clamp(4rem, 10vw, 15rem)`、\n"
    "  `py-32 md:py-48`、`max-w-...` 等具体数值 / 比例 / 像素值 / 字号 / 计数，必须 verbatim 原样保留，\n"
    "  禁止概括为'高纪律'、'若干'、'适当'等抽象表述。\n"
    "- 【禁数字幻觉】严禁在 example、pain_points、plain_summary 中编造原文未出现的精确数字：\n"
    "  不要凭直觉填 16px / 4:1 / 120px / 24px 等具体值；若举例需要数字，必须来自原文实际出现的数字。\n"
    "  此约束同样适用 key_concepts[].example 字段 —— 示例段编造数字与主张段编造同样不可；\n"
    "  原文未给数字时，example 字段宁可保留定性表述（'tiny spacing'）也不要补具体值。\n"
    "- 【禁 wikilink 幻觉】cross_references 中的 [[xxx]] 必须是原文真实提及的关联名词；\n"
    "  禁止为了'美化'编造 [[Anti-Generic Design Rules]] 这类原文从未出现的链接目标。"
)

_FOCUS_RULES: dict[str, str] = {
    "skill": (
        "- 着重提取严格的执行流程（procedure）、决策点（decision_points）、\n"
        "  命令片段（procedure[].command）、前置条件、中止条件、回滚步骤。\n"
        "- 保留作者写明的避坑提示（learning_enhancement.pain_points）。\n"
        "- 对文档中的重要概念、设计原则、方法论提炼到 key_concepts（每项含 title / explanation / example）。\n"
        "- 【关键】文档中每个具名章节（如'# Reference Style DNA'、'# 2×3 Layout'、'## Visual Modes'等）\n"
        "  必须作为独立的 key_concept 条目保留，使用原标题作为 title，禁止将多个命名章节合并为一条。\n"
        "- 【严禁幻觉】key_concepts 中每个条目的 title 必须来自原文实际存在的章节标题或明确命名的概念；\n"
        "  禁止虚构任何综合摘要条目（如'§16-22 综合规则'、'核心规则汇总'等原文中不存在的标题）。\n"
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
        "  可按维度拆成多张卡，每张聚焦一个维度。"
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
    "你是结构化知识提取助手。你的任务是从给定文档中抽取标准化知识单元，"
    "并以严格 JSON 输出。\n"
    "重要：直接输出 JSON，不要附带任何解释、不要使用 Markdown 代码块包裹。\n"
    "严禁幻觉：所有 key_concepts[].title 必须来自原文真实存在的章节标题或明确命名的概念，"
    "禁止虚构任何原文中不存在的综合摘要、汇总或归纳性标题。"
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
  "key_concepts": [
    {"title": "概念/章节/方案名称（来自原文，不可虚构）", "explanation": "详细解释（100-300字）", "example": "示例或代码片段（可空字符串）"}
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

    # 1. 读取原文
    text = _read_text_auto(raw_p)

    # 2. 提取缓存命中（同一 hash + prompt 版本）
    cached_units = _load_extract_cache(source_hash, prompt_version)
    if cached_units is not None:
        if console:
            console.print("  [dim]提取缓存命中，跳过 LLM 调用[/dim]")
        units = cached_units
    else:
        # 3. 使用 chunker 分批提取，确保全文覆盖，不硬截断
        llm_cfg = cfg.get("llm", {})
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
        try:
            if total_chunks <= 1 or len(text) <= batch_chars:
                # 短文档：单次提取
                units = _llm_extract(text, doc_type, source_info, cfg, console=console)
            else:
                # 长文档：分批提取，合并去重
                units = _llm_extract_chunked(
                    chunks, batch_chars, doc_type, source_info, cfg, console=console
                )
        except _CredentialError:
            raise
        except Exception as e:
            if console:
                console.print(f"  [yellow]⚠ LLM 提取失败，降级启发式:[/yellow] {e}")
            units = [_heuristic_extract(text, raw_p, source_info)]

        # 5. 写入提取缓存（仅 LLM/启发式新结果）
        _save_extract_cache(source_hash, prompt_version, units)

    if not units:
        units = [_heuristic_extract(text, raw_p, source_info)]

    # 6. 组装并落盘草稿
    drafts: list[dict] = []
    total = len(units)
    for idx, unit in enumerate(units, start=1):
        draft = _assemble_draft(unit, source_info, idx=idx, total=total,
                                prompt_version=prompt_version)
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
            # 已有同名卡：把新卡的 key_concepts 补充进去（新出现的）
            existing = merged[name_to_idx[norm_name]]
            existing_kc = existing.get("key_concepts", [])
            new_kc = unit.get("key_concepts", [])
            existing_kc_titles = {_normalize_title(k.get("title", "")) for k in existing_kc}
            for kc in new_kc:
                if _normalize_title(kc.get("title", "")) not in existing_kc_titles:
                    existing_kc.append(kc)
            existing["key_concepts"] = existing_kc
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
            # 合并：把当前卡独有的 key_concepts 补进先出现的卡
            existing = merged_jc[merged_target]
            existing_kc = existing.get("key_concepts", [])
            existing_titles = {_normalize_title(k.get("title", ""))
                               for k in existing_kc if isinstance(k, dict)}
            for kc in unit.get("key_concepts", []) or []:
                if isinstance(kc, dict) and _normalize_title(kc.get("title", "")) not in existing_titles:
                    existing_kc.append(kc)
            existing["key_concepts"] = existing_kc
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
) -> list[dict]:
    """将 chunks 分批送给 LLM 提取，合并所有批次结果并去重同名卡片。

    合并策略：
    - 不同批次出现标题相同的卡片 → 保留第一次出现的（通常信息更完整）
    - 保持各卡片的相对顺序
    """
    # 将 chunks 按 batch_chars 组合成多个批次
    batches: list[list[dict]] = []
    current_batch: list[dict] = []
    current_chars = 0
    for ch in chunks:
        cost = len(ch["content"]) + 20  # +20 for <<CHUNK N>> marker
        if current_batch and current_chars + cost > batch_chars:
            batches.append(current_batch)
            current_batch = [ch]
            current_chars = cost
        else:
            current_batch.append(ch)
            current_chars += cost
    if current_batch:
        batches.append(current_batch)

    total_batches = len(batches)
    if console:
        console.print(f"  [dim]分批提取：共 {total_batches} 批[/dim]")

    all_units: list[dict] = []
    seen_names: set[str] = set()

    for batch_idx, batch in enumerate(batches, start=1):
        batch_text, truncated = join_chunks_for_prompt(batch, batch_chars)
        if console:
            status = "（已截尾）" if truncated else ""
            console.print(
                f"  [dim]  批次 {batch_idx}/{total_batches}：{len(batch_text)} 字符{status}[/dim]"
            )
        try:
            batch_units = _llm_extract(batch_text, doc_type, source_info, cfg, console=console)
        except _CredentialError:
            raise
        except Exception as e:
            if console:
                console.print(f"  [yellow]  批次 {batch_idx} 失败，跳过：{e}[/yellow]")
            continue

        for unit in batch_units:
            name = unit.get("meta", {}).get("name", "")
            norm = _normalize_title(name)
            if norm and norm in seen_names:
                continue
            if norm:
                seen_names.add(norm)
            all_units.append(unit)

    if not all_units:
        raise RuntimeError("所有批次均提取失败")

    # 跨批次去重合并：卡片级 + key_concepts 级 + Jaccard 内容重叠合并
    merged = _merge_units(all_units, doc_type=doc_type, console=console)
    if console and len(merged) < len(all_units):
        console.print(
            f"  [dim]合并去重：{len(all_units)} 张 → {len(merged)} 张"
            f"（去除 {len(all_units) - len(merged)} 张重复卡片）[/dim]"
        )
    return merged


def _llm_extract(
    text: str,
    doc_type: str,
    source_info: dict,
    cfg: dict,
    console=None,
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

    timeout = int(cfg.get("llm", {}).get("timeout", 120))
    max_retries = int(cfg.get("llm", {}).get("max_retries", 2))

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return _llm_call_once(creds, user_prompt, timeout=timeout)
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


def _llm_call_once(creds: dict, user_prompt: str, *, timeout: int) -> list[dict]:
    from litellm import completion  # type: ignore

    kwargs: dict[str, Any] = {
        "model": creds["model"],
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "api_key": creds["api_key"],
        "timeout": timeout,
        "max_tokens": 8192,   # 防止响应被截断导致 JSON 残缺
    }
    if "api_base" in creds:
        kwargs["api_base"] = creds["api_base"]

    resp = completion(**kwargs)
    raw = (resp.choices[0].message.content or "").strip()
    return _parse_json_response(raw)


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

    # 3. 尝试解析，失败则尝试修复截断后再解析
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        fixed = _fix_truncated_json(text)
        try:
            parsed = json.loads(fixed)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败（修复后仍无效）: {e}") from e

    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        units = [u for u in parsed if isinstance(u, dict)]
        if not units:
            # 空数组/全非 dict：重试无意义，直接降级启发式
            raise _EmptyResponseError(
                f"数组中没有有效对象（共 {len(parsed)} 项，类型：{[type(x).__name__ for x in parsed[:3]]}）"
            )
        return units
    raise ValueError(f"LLM 响应不是 JSON 对象/数组: {type(parsed).__name__}")


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

    return {
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
        "learning_enhancement": {
            "pain_points": notes,
            "plain_summary": summary or title,
            "knowledge_tags": [],
        },
        "source_reliability": "medium",
        "obsolescence_risk": "medium",
    }


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
        "key_concepts": unit.get("key_concepts", []),
        "learning_enhancement": unit.get("learning_enhancement", {}),
        "source_reliability": unit.get("source_reliability", "medium"),
        "obsolescence_risk": unit.get("obsolescence_risk", "medium"),
        "prompt_version": prompt_version,
        "status": "draft",
        "created_at": time.strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# 提取缓存
# ---------------------------------------------------------------------------

def _extract_cache_path(source_hash: str, prompt_version: str) -> Path:
    return EXTRACT_CACHE_DIR / f"{source_hash}_{prompt_version}.json"


def _load_extract_cache(source_hash: str, prompt_version: str) -> list[dict] | None:
    p = _extract_cache_path(source_hash, prompt_version)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [u for u in data if isinstance(u, dict)] or None
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _save_extract_cache(source_hash: str, prompt_version: str, units: list[dict]) -> None:
    p = _extract_cache_path(source_hash, prompt_version)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(units, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def clean_stale_extract_caches(console=None) -> int:
    """清理 extract_cache 目录中旧命名格式的文件（如 *_structure.json、*_evidence.json 等）。

    旧格式：{hash}_{version}_{suffix}.json（含第三段 suffix）
    新格式：{hash}_{version}.json（仅两段）

    返回删除的文件数。
    """
    deleted = 0
    if not EXTRACT_CACHE_DIR.exists():
        return 0
    for f in EXTRACT_CACHE_DIR.glob("*.json"):
        # 去掉 .json 后缀，按 _ 拆分
        stem = f.stem  # e.g. "abc123_extract_v1_structure"
        parts = stem.split("_")
        # 合法新格式：{hash}_{version} → 拆分后为 [hash, "extract", "v1"]（3段）
        # 旧格式多了一个 suffix，即 4 段以上
        # 判断依据：第三个 _ 后还有内容
        # 格式示例：b0c4837..._extract_v1_structure → stem 含 4+ 部分用 _ 分隔
        # 用更精确的方式：检查是否匹配 *_extract_v*_*.json（含尾缀词）
        import re as _re
        if _re.search(r'_extract_v\d+_.+$', stem):
            try:
                f.unlink()
                deleted += 1
                if console:
                    console.print(f"  [dim]删除旧缓存：{f.name}[/dim]")
            except OSError as e:
                if console:
                    console.print(f"  [yellow]删除失败：{f.name}：{e}[/yellow]")
    return deleted
