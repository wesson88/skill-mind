"""提取引擎（Extractor）v2.5

职责：将单个已缓存的原始文档转成 1~N 张结构化草稿（一文多卡）。

W2.2 新增 —— evidence 阶段（第 4 个 stage）：
- 为前三个阶段产出的 procedure 步 / decision 分支 / cross_references 标注：
  * source_quote：原文中能字面找到的短引文（≤60 字符）
  * chunk_id：引文所在的 <<CHUNK N>> 编号
- 关键副作用：找不到证据的 cross_references **直接丢弃**（破除 LLM 凭空编造 wiki 链接的幻觉）
- 配置开关：extract.enable_evidence（默认 true）；关闭时跳过该阶段，行为退化为 v2.4

W2.4 改动 —— 分类轴重构：
- 两条独立的轴：
  * doc_type = 容器标识 → 仅 identity / enhancement 用
  * focus_mode = 内容形态（procedure / decision / concept）→ 按卡判断，驱动 structure
- 目的：GitHub 仓库里 SKILL.md / prompt / agent / README 不再被一刀切成"skill" 形态

继承自前几波（保留）：
- 一文多卡：LLM 可返回 JSON 数组，每项渲染为独立笔记
- 可信度 / 过时风险：source_reliability / obsolescence_risk 由 LLM 标注
- 阶段级提取缓存：同 (source_hash, prompt_version, stage) 命中直接复用
- 凭证缺失（resolve_llm_credentials 抛出）属配置错误，向上抛，不静默降级
- chunker 接入：原文带 <<CHUNK N>> 标记送入 LLM，预算 30K 字符
- 每 stage 独立 JSON Schema 校验 + retry-with-feedback 一次
- LLM 全失败仍走 _heuristic_extract 兜底
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from skillmind.chunker import Chunker, join_chunks_for_prompt
from skillmind.config import (
    CURRENT_PROMPT_VERSION,
    EXTRACT_CACHE_DIR,
    ensure_dirs,
    resolve_llm_credentials,
)
from skillmind.parser import _read_text_auto, parse_skill_file
from skillmind.reviewer import save_draft


# ---------------------------------------------------------------------------
# Prompt 文本
# ---------------------------------------------------------------------------

_DOC_TYPE_LABEL = {
    # W2.4：skill 容器现在涵盖 GitHub 仓库中的多种文档（SKILL.md / prompt / agent / README 等），
    # 不再特指 Claude Code Skill。形态由 focus_mode（procedure/decision/concept）按卡决定。
    "skill":      "Git 仓库内的知识文档（SKILL.md / 提示词 / agent 定义 / README / 教程等）",
    "blog":       "技术博客文章",
    "forum_post": "论坛主题帖（含问答）",
    "webpage":    "通用网页",
}

# focus_mode：W2.4 新增"内容形态"轴，由 identity stage 按卡判断
# structure stage 按 focus_mode 选择对应的结构提取规则
_FOCUS_MODE_ENUM = ["procedure", "decision", "concept"]

# 各 focus_mode 的判断标准（写入 identity 提示词，让 LLM 自行选择）
_FOCUS_MODE_HINTS = """- procedure ：步骤型 / 命令型。原文有"先做 A，再做 B"的可复现流程、命令片段、配置示例。
              典型：how-to 教程、SKILL.md 执行流程、安装文档。
- decision  ：判断型 / 分支型。原文有多条件、多方案对比、选型 tradeoff、故障排查分支。
              典型：故障排查贴、架构选型博客、prompt 设计文档、"X vs Y"对比文章。
- concept   ：解释型 / 知识型。原文主要说明 what / why，没有显式步骤或决策分支。
              典型：概念解释博客、agent 角色定义、prompt 库说明、术语 glossary。"""

# ---------------------------------------------------------------------------
# 3 阶段 prompt 系统（W2.1 引入，W2.4 重构分类轴）
#
# Stage 1 identity:    doc → cards 身份信息 + 每卡的 focus_mode（形态判定）
# Stage 2 structure:   doc + cards → 每张卡的骨架，按各自 focus_mode 应用对应规则
# Stage 3 enhancement: doc + cards + structure → 每张卡的加值信息（summary/tags/reliability/risk）
#
# 设计原则：
# - 每 stage 独立 LLM 调用，独立 schema，独立 retry-with-feedback
# - identity 决定卡数与每卡的 focus_mode；后续阶段按 card_index 对齐
# - identity / enhancement 阶段仍按 doc_type 路由（容器层规则）
# - structure 阶段在单次调用内枚举 3 套 focus 规则，LLM 按每张卡的 focus_mode 自选
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "你是结构化知识提取助手。你的任务是从给定文档中按指定阶段抽取知识单元，"
    "并以严格 JSON 输出。\n"
    "重要：直接输出 JSON，不要附带任何解释、不要使用 Markdown 代码块包裹。"
)

# identity / enhancement focus rules（按 doc_type 容器层）
_STAGE_FOCUS_RULES: dict[str, dict[str, str]] = {
    "identity": {
        "skill": "- 来源是 Git 仓库内文档，可能是执行手册、提示词、agent 配置、README、教程。"
                 "  请按主题/可独立成卡的子结构判断卡数，不要因为是仓库就强行单卡。",
        "blog":  "- 若长文涵盖多个独立主题（如「全栈 DevOps 指南」含 CI/CD、监控、部署），按主题拆成多张卡。",
        "forum_post": "- 主帖问题为主卡；若不同回答属于明显不同方案/不同适用场景，可拆多卡。",
        "webpage": "- 通常单卡；除非页面明显是分主题汇总（如 awesome 列表），否则不要拆。",
    },
    "enhancement": {
        "skill": "- 来源是 Git 仓库：官方维护项目 high / 活跃社区项目 medium / 个人零散仓库 low。"
                 "  过时风险按是否涉及具体版本号判断；pain_points 保留作者明确写的避坑。",
        "blog":  "- 可信度依作者/站点判断（个人博客 low / 大型站点 medium / 官方 high）；过时风险偏 medium。",
        "forum_post": "- 可信度通常 low（个人回答）；除非明显官方/高赞被采纳；过时风险 medium-high。",
        "webpage": "- 按内容类型判断；概念解释 obsolescence_risk=low，版本相关 high。",
    },
}

# structure focus rules（按 focus_mode 内容形态层，W2.4 新轴；W2.4-fix concept 重写）
_STRUCTURE_FOCUS_RULES: dict[str, str] = {
    "procedure": "严格保留执行流程顺序与命令片段；preconditions / halt_conditions / rollback_actions 按原文摘出；"
                 "decision_points 通常为空。命令片段必须从原文照搬，不要泛化。"
                 "key_concepts 通常为空（不是概念卡）。",
    "decision":  "重点抽取 decision_points（多条件 / 多方案对比 / 选型 tradeoff），condition+then+else 完整成对；"
                 "procedure 写最终采纳方案的步骤；preconditions 写共同前置；halt 写「应当放弃此路线」的条件。"
                 "key_concepts 通常为空。",
    "concept":   "本卡是概念解释型。**主要产出 key_concepts 数组**，把原文里每个独立概念拆成完整条目：\n"
                 "  - title：概念名（≤60 字符，与原文小节标题或核心术语对齐）\n"
                 "  - explanation：**2-4 句白话解释**，覆盖 what / why / 关键要点；**不要只写 1 句话**，卡的灵魂在这里\n"
                 "  - example（可选）：若原文有示例代码、目录结构、配置模板、对照例子，原样保留（保留 ```代码块``` 形式）\n"
                 "preconditions / procedure / halt_conditions / rollback_actions 通常为空（除非原文有显式步骤）；\n"
                 "cross_references **严格**：仅当原文出现 `[[xxx]]` 包裹形式、可识别的文件路径（含扩展名）或 URL 时才填；"
                 "**禁止把原文小节标题转成 [[wiki]] 链接**（小节内容应写进 key_concepts，不是 cross_references）。",
}


# ── Stage 1: identity ────────────────────────────────────────────────────

_STAGE_IDENTITY_PROMPT = """【阶段】身份识别（identity）

【任务】
判断以下{doc_type_label}应拆成几张知识卡，并给每张卡输出身份字段。
不要在本阶段输出 procedure / decision_points / 等结构字段，那是下一阶段的任务。

【关键判断：每张卡的 focus_mode】
按内容形态从三类中选一种，决定下一阶段如何抽取结构：
{focus_mode_hints}

一份文档内不同卡可以是不同 focus_mode（例如一个仓库里同时有教程类卡和概念类卡）。

【阶段聚焦】
{focus_rules}

【字段定义】
{
  "cards": [
    {
      "name": "笔记标题（简洁，<= 30 字）",
      "type": ["command-oriented" | "concept-explanation" | "decision-tree" | "troubleshooting"],
      "focus_mode": "procedure | decision | concept",
      "intent": "一句话目的描述",
      "trigger_keywords": ["关键词", "..."],
      "os": ["linux", "macos", "..."],
      "tools_required": ["工具名", "..."]
    }
  ]
}

注意：focus_mode 必填且单选；type 是辅助标签可多选。两者关系参考但不强绑：
- 单纯 command-oriented 通常对应 focus_mode=procedure
- decision-tree / troubleshooting 通常对应 focus_mode=decision
- concept-explanation 通常对应 focus_mode=concept

【文档元信息】
{metadata_json}

【文档正文（doc_type={doc_type}）】
正文以 <<CHUNK N>> 标记切分（N 为 chunk 序号），这些标记不要视为内容也不要复述。
若文档较长，请覆盖所有 chunk 的主题，不要只看前面几个。

{content}

请直接输出 JSON。"""


# ── Stage 2: structure ───────────────────────────────────────────────────

_STAGE_STRUCTURE_PROMPT = """【阶段】结构骨架（structure）

【任务】
基于前一阶段已确定的卡片身份，为每张卡提取结构化骨架字段。
卡片数量、顺序、name 严格对齐 cards_context；不要新增/删除/重排卡片。

【关键：按每张卡的 focus_mode 应用对应规则】
cards_context 中每张卡都标了 focus_mode，请逐张套用下面对应的规则。
同一份文档内不同卡可以采用不同规则。

[procedure 规则] {focus_procedure}
[decision  规则] {focus_decision}
[concept   规则] {focus_concept}

【前一阶段输出（cards_context）】
{cards_json}

【字段定义】
{
  "units": [
    {
      "preconditions": ["前置条件", "..."],
      "procedure": [
        {"seq": 1, "action": "动作描述", "command": "可选命令"}
      ],
      "decision_points": [
        {"condition": "判断条件", "then": "成立时", "else": "不成立时"}
      ],
      "halt_conditions": ["停止条件", "..."],
      "rollback_actions": ["回滚动作", "..."],
      "cross_references": ["[[关联笔记]] 或 实际文件路径", "..."],
      "key_concepts": [
        {"title": "概念名", "explanation": "2-4 句白话解释（concept 卡必填且字数充实，其它卡留空）", "example": "可选原文示例（含代码/模板）"}
      ]
    }
  ]
}

units 数组顺序必须与 cards_context 一一对应。某字段在原文中不存在或按 focus_mode 规则不适用时保留为空数组。
concept 类卡片要求 key_concepts 非空且每条 explanation 至少 2 句话；procedure / decision 类卡片 key_concepts 通常为空。

【文档元信息】
{metadata_json}

【文档正文（doc_type={doc_type}）】
{content}

请直接输出 JSON。"""


# ── Stage 3: enhancement ─────────────────────────────────────────────────

_STAGE_ENHANCEMENT_PROMPT = """【阶段】加值信息（enhancement）

【任务】
基于前两阶段已确定的卡片身份与结构，为每张卡补充学习增强字段、可信度与过时风险。
units 数组顺序必须与 cards_context 一一对应。

【阶段聚焦】
{focus_rules}

【前一阶段输出（cards_context）】
{cards_json}

【字段定义】
{
  "units": [
    {
      "learning_enhancement": {
        "pain_points": ["难点 / 避坑", "..."],
        "plain_summary": "白话一句话总结",
        "knowledge_tags": ["标签", "..."]
      },
      "source_reliability": "high | medium | low",
      "obsolescence_risk": "low | medium | high"
    }
  ]
}

【可信度参考】
- high   : 官方文档、知名团队的 Skill 仓库
- medium : 大型技术博客、活跃维护的开源项目
- low    : 个人博客、论坛回答

【过时风险参考】
- high   : 涉及具体版本号、API 细节、易变命令
- medium : 涉及部署流程、第三方工具
- low    : 概念解释、设计原则

【文档元信息】
{metadata_json}

【文档正文（doc_type={doc_type}）】
{content}

请直接输出 JSON。"""


# ── Stage 4: evidence（W2.2，原文证据回填）────────────────────────────────

_STAGE_EVIDENCE_PROMPT = """【阶段】原文证据回填（evidence）

【任务】
前面三个阶段已确定 procedure / decision_points / cross_references。本阶段为每一项标注：
- source_quote：原文中**能字面找到**的连续短引文，证明该项来自原文
- chunk_id：该引文所在的 <<CHUNK N>> 编号

【关键约束】
1. source_quote 必须是原文出现的连续字符串，不要改写、不要总结、不要翻译。
2. **严格 ≤80 字符上限**（中英文混合通用）。**只引最关键的 4-8 个词组或短语，不要引完整句子。**
   - ❌ 反例（超长）："Wait to write test prompts until you've got this part ironed out." (64)
   - ✅ 正例（关键词组）："Wait to write test prompts"（30）或 "got this part ironed out"（28）
   - ❌ 反例（完整句）："research in parallel via subagents if available, otherwise inline." (66)
   - ✅ 正例（关键短语）："research in parallel via subagents"（35）
   宁可只引关键词，也不要凑完整意思。reader 看到关键词能在原文里搜到就够了。
3. **如果某项在原文中找不到对应支撑（说明上一阶段是 LLM 编造的），就不要为它输出 evidence 条目。** 留空是允许的，是甄别幻觉的关键。
4. cross_ref_evidence 的 ref 必须与 cards_with_structure 中给出的字符串完全一致（包括 [[ ]] 包裹）。

【字段定义】
{
  "units": [
    {
      "procedure_evidence": [
        {"seq": 1, "source_quote": "spawn all runs in same turn", "chunk_id": 5}
      ],
      "decision_evidence": [
        {"index": 0, "source_quote": "Claude.ai: no subagents", "chunk_id": 9}
      ],
      "cross_ref_evidence": [
        {"ref": "[[agents/grader.md]]", "source_quote": "agents/grader.md", "chunk_id": 7}
      ]
    }
  ]
}

units 顺序与 cards_with_structure 严格对齐。各 evidence 数组在找不到对应支撑时留空（数组为 []）。

【前三阶段输出 cards_with_structure】
{cards_with_structure_json}

【文档元信息】
{metadata_json}

【文档正文（doc_type={doc_type}）】
{content}

请直接输出 JSON。"""


_STAGE_PROMPTS: dict[str, str] = {
    "identity":    _STAGE_IDENTITY_PROMPT,
    "structure":   _STAGE_STRUCTURE_PROMPT,
    "enhancement": _STAGE_ENHANCEMENT_PROMPT,
    "evidence":    _STAGE_EVIDENCE_PROMPT,
}

_STAGE_ORDER: tuple[str, ...] = ("identity", "structure", "enhancement", "evidence")


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

    流程（W2.1）：
      1. 读取原文
      2. _llm_extract 内部完成切分 + 3 阶段 LLM 调用（各阶段独立缓存）
      3. 任一阶段失败 → 整篇走启发式兜底
      4. 组装草稿落盘
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

    text = _read_text_auto(raw_p)

    try:
        units = _llm_extract(text, doc_type, source_info, cfg, console=console)
    except _CredentialError:
        raise
    except Exception as e:
        if console:
            console.print(f"  [yellow]⚠ LLM 提取失败，降级启发式:[/yellow] {e}")
        units = [_heuristic_extract(text, raw_p, source_info)]

    if not units:
        units = [_heuristic_extract(text, raw_p, source_info)]

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


def _llm_extract(
    text: str,
    doc_type: str,
    source_info: dict,
    cfg: dict,
    console=None,
) -> list[dict]:
    """4 阶段编排（W2.1 → W2.2）：

    1. chunker 切分 + 拼回带 <<CHUNK N>> 标记的字符串（送给所有 stage 共用）
    2. identity → structure → enhancement → evidence 顺序执行
    3. 每 stage 独立缓存（key 含 stage 名）+ 独立 retry-with-feedback
    4. 按 card_index 合并前 3 阶段输出 → 第 4 阶段 evidence 回填到合并结果
    5. evidence 阶段可经 extract.enable_evidence 关闭
    """
    try:
        creds = resolve_llm_credentials(cfg)
    except RuntimeError as e:
        raise _CredentialError(str(e))

    source_hash = source_info.get("source_hash") or ""
    prompt_version = CURRENT_PROMPT_VERSION

    # 1. chunker（每 stage 共用同一份切分结果）
    chunker_cfg = cfg.get("chunker", {}) or {}
    chunker = Chunker(
        chunk_size_tokens=int(chunker_cfg.get("chunk_size_tokens", 1500)),
        chunk_overlap_tokens=int(chunker_cfg.get("chunk_overlap_tokens", 150)),
        chars_per_token=int(chunker_cfg.get("chars_per_token", 3)),
        min_chunk_size_tokens=int(chunker_cfg.get("min_chunk_size_tokens", 100)),
    )
    chunks = chunker.chunk(text)
    max_chars = int(cfg.get("llm", {}).get("max_content_chars", 30000))
    text_for_llm, truncated = join_chunks_for_prompt(chunks, max_chars)
    if console:
        total_tokens = sum(c["estimated_tokens"] for c in chunks)
        console.print(
            f"  [dim]切分: {len(chunks)} chunks, "
            f"≈{total_tokens} tokens, 送入 LLM {len(text_for_llm)} 字符[/dim]"
        )
        if truncated:
            console.print(
                f"  [yellow]⚠ 超长文档已按 chunk 边界截尾，预算 {max_chars} 字符[/yellow]"
            )

    metadata = {
        "doc_type": doc_type,
        "title": source_info.get("title", ""),
        "author": source_info.get("author", ""),
        "source_url": source_info.get("source_url", ""),
        "source_repo": source_info.get("source_repo", ""),
        "source_path": source_info.get("source_path", ""),
        "published_at": source_info.get("published_at", ""),
    }

    # 2. Stage 1: identity
    identity = _run_or_load_stage(
        stage="identity",
        source_hash=source_hash,
        prompt_version=prompt_version,
        text=text_for_llm,
        doc_type=doc_type,
        metadata=metadata,
        prev_context={},
        creds=creds,
        cfg=cfg,
        console=console,
    )
    cards = identity.get("cards") or []
    if not cards:
        raise ValueError("identity 阶段未输出任何 cards")
    if console:
        names = ", ".join((c.get("name") or "?")[:20] for c in cards)
        console.print(f"  [cyan]identity:[/cyan] {len(cards)} 卡 → {names}")

    # 3. Stage 2: structure
    structure = _run_or_load_stage(
        stage="structure",
        source_hash=source_hash,
        prompt_version=prompt_version,
        text=text_for_llm,
        doc_type=doc_type,
        metadata=metadata,
        prev_context={"cards": cards},
        creds=creds,
        cfg=cfg,
        console=console,
    )
    struct_units = structure.get("units") or []

    # 4. Stage 3: enhancement
    enhancement = _run_or_load_stage(
        stage="enhancement",
        source_hash=source_hash,
        prompt_version=prompt_version,
        text=text_for_llm,
        doc_type=doc_type,
        metadata=metadata,
        prev_context={"cards": cards},
        creds=creds,
        cfg=cfg,
        console=console,
    )
    enh_units = enhancement.get("units") or []

    # 5. 合并前 3 阶段
    units = _merge_stage_outputs(cards, struct_units, enh_units)

    # 6. Stage 4: evidence（W2.2，可选）
    if cfg.get("extract", {}).get("enable_evidence", True):
        try:
            evidence = _run_or_load_stage(
                stage="evidence",
                source_hash=source_hash,
                prompt_version=prompt_version,
                text=text_for_llm,
                doc_type=doc_type,
                metadata=metadata,
                prev_context={"cards": cards, "structure": struct_units},
                creds=creds,
                cfg=cfg,
                console=console,
            )
            ev_units = evidence.get("units") or []
            units = _apply_evidence(units, ev_units, console=console)
        except Exception as e:
            # evidence 阶段失败不阻塞主流程，保留前 3 阶段输出
            if console:
                console.print(f"  [yellow]⚠ evidence 阶段失败，跳过证据回填:[/yellow] {e}")
    elif console:
        console.print(f"  [dim]evidence: 已关闭（extract.enable_evidence=false）[/dim]")

    return units


def _run_or_load_stage(
    *,
    stage: str,
    source_hash: str,
    prompt_version: str,
    text: str,
    doc_type: str,
    metadata: dict,
    prev_context: dict,
    creds: dict,
    cfg: dict,
    console=None,
) -> dict:
    """读取或运行单个阶段：缓存命中直接返回；否则调 LLM + schema 校验 + 反馈重试。

    返回该阶段的原始 JSON 输出（dict）。
    """
    cached = _load_stage_cache(source_hash, prompt_version, stage)
    if cached is not None:
        if console:
            console.print(f"  [dim]{stage}: 阶段缓存命中[/dim]")
        return cached

    base_prompt = _build_stage_prompt(
        stage=stage,
        text=text,
        doc_type=doc_type,
        metadata=metadata,
        prev_context=prev_context,
    )

    timeout = int(cfg.get("llm", {}).get("timeout", 120))
    max_retries = int(cfg.get("llm", {}).get("max_retries", 2))

    # content 层最多两轮：第 1 轮原 prompt，第 2 轮带 schema 错误反馈
    current_prompt = base_prompt
    last_errors: list[str] = []

    for content_attempt in range(2):
        parsed = _transport_retry_call(
            creds, current_prompt,
            timeout=timeout, max_retries=max_retries, console=console,
        )
        # _transport_retry_call 把 dict 包装成 [dict]；stage 输出必为 dict
        data = parsed[0] if parsed else {}

        last_errors = _validate_stage(stage, data)
        if not last_errors:
            if content_attempt > 0 and console:
                console.print(f"  [green]✓ {stage}: 反馈重试后 schema 校验通过[/green]")
            _save_stage_cache(source_hash, prompt_version, stage, data)
            return data

        if content_attempt == 0:
            if console:
                console.print(
                    f"  [yellow]⚠ {stage}: schema 校验失败（{len(last_errors)} 处），"
                    "反馈给 LLM 重试一次[/yellow]"
                )
            current_prompt = base_prompt + (
                f"\n\n【上一次 {stage} 输出 schema 校验失败】\n"
                + "\n".join(f"- {e}" for e in last_errors[:10])
                + "\n请按上述 schema 要求修正后重新输出完整 JSON，"
                "不要省略必填字段，不要改变数据类型。"
            )

    raise ValueError(
        f"{stage} 阶段 schema 校验在反馈重试后仍失败: {last_errors[:3]}"
    )


def _build_stage_prompt(
    *,
    stage: str,
    text: str,
    doc_type: str,
    metadata: dict,
    prev_context: dict,
) -> str:
    """组装某 stage 的 user_prompt，做手动字符串替换以避免 str.format 与正文 { } 冲突。

    W2.4 路由：
      - identity:    {focus_rules} 仍按 doc_type；额外注入 {focus_mode_hints}
      - structure:   注入 3 个独立 focus 规则 {focus_procedure/decision/concept}，
                     LLM 按 cards_context 里每张卡的 focus_mode 自选
      - enhancement: {focus_rules} 按 doc_type
    """
    template = _STAGE_PROMPTS[stage]

    p = (
        template
        .replace("{doc_type_label}", _DOC_TYPE_LABEL.get(doc_type, "文档"))
        .replace("{metadata_json}", json.dumps(metadata, ensure_ascii=False))
        .replace("{doc_type}", doc_type)
        .replace("{content}", text)
    )

    # identity / enhancement: 单一 focus_rules 占位（按 doc_type）
    if "{focus_rules}" in p:
        focus_rules = _STAGE_FOCUS_RULES.get(stage, {}).get(
            doc_type,
            _STAGE_FOCUS_RULES.get(stage, {}).get("webpage", ""),
        )
        p = p.replace("{focus_rules}", focus_rules)

    # identity 特有：focus_mode 判定标准
    if "{focus_mode_hints}" in p:
        p = p.replace("{focus_mode_hints}", _FOCUS_MODE_HINTS)

    # structure 特有：3 套 focus 规则按 focus_mode 自选
    if stage == "structure":
        p = (
            p
            .replace("{focus_procedure}", _STRUCTURE_FOCUS_RULES["procedure"])
            .replace("{focus_decision}", _STRUCTURE_FOCUS_RULES["decision"])
            .replace("{focus_concept}", _STRUCTURE_FOCUS_RULES["concept"])
        )

    if "{cards_json}" in p:
        cards_brief = [
            {
                "name": c.get("name", ""),
                "intent": c.get("intent", ""),
                "type": c.get("type") or [],
                # W2.4：把 focus_mode 暴露给 structure / enhancement
                "focus_mode": c.get("focus_mode", "concept"),
            }
            for c in prev_context.get("cards", [])
        ]
        p = p.replace(
            "{cards_json}",
            json.dumps(cards_brief, ensure_ascii=False, indent=2),
        )

    # evidence 阶段特有：构造含 procedure/decision/cross_refs 的丰富上下文
    if "{cards_with_structure_json}" in p:
        cards = prev_context.get("cards", []) or []
        structs = prev_context.get("structure", []) or []
        rich: list[dict] = []
        for i, c in enumerate(cards):
            s = structs[i] if i < len(structs) else {}
            rich.append({
                "name": c.get("name", ""),
                "focus_mode": c.get("focus_mode", ""),
                "procedure": [
                    {"seq": step.get("seq"), "action": (step.get("action") or "")[:120]}
                    for step in (s.get("procedure") or [])
                ],
                "decision_points": [
                    {"index": idx, "condition": (dp.get("condition") or "")[:80]}
                    for idx, dp in enumerate(s.get("decision_points") or [])
                ],
                "cross_references": s.get("cross_references") or [],
            })
        p = p.replace(
            "{cards_with_structure_json}",
            json.dumps(rich, ensure_ascii=False, indent=2),
        )

    # 净化潜在的 surrogate / 半码字符（litellm 严格模式下会触发 UnicodeEncodeError）
    return p.encode("utf-8", errors="replace").decode("utf-8")


def _apply_evidence(
    units: list[dict],
    evidence_units: list[dict],
    *,
    console=None,
) -> list[dict]:
    """W2.2：把 evidence 阶段输出合并进已 merge 完的 units。

    - procedure: 按 seq 匹配，添加 source_quote / chunk_id
    - decision_points: 按 index 匹配，添加 source_quote / chunk_id
    - cross_references: 由 str 列表升级为 dict 列表 {ref, source_quote, chunk_id}；
      没有 evidence 支撑的引用 **直接丢弃**（破除幻觉链接）

    返回原地修改后的 units（同一对象引用）。
    """
    dropped_refs = 0
    annotated_steps = 0
    for i, unit in enumerate(units):
        if i >= len(evidence_units):
            break
        ev = evidence_units[i] or {}

        # procedure: by seq
        proc_map: dict[int, dict] = {}
        for e in ev.get("procedure_evidence", []) or []:
            if isinstance(e, dict) and isinstance(e.get("seq"), int):
                proc_map[e["seq"]] = e
        for step in unit.get("procedure", []) or []:
            seq = step.get("seq")
            if seq in proc_map:
                step["source_quote"] = proc_map[seq].get("source_quote", "")
                step["chunk_id"] = proc_map[seq].get("chunk_id", -1)
                annotated_steps += 1

        # decision_points: by index
        dec_map: dict[int, dict] = {}
        for e in ev.get("decision_evidence", []) or []:
            if isinstance(e, dict) and isinstance(e.get("index"), int):
                dec_map[e["index"]] = e
        for idx, dp in enumerate(unit.get("decision_points", []) or []):
            if idx in dec_map:
                dp["source_quote"] = dec_map[idx].get("source_quote", "")
                dp["chunk_id"] = dec_map[idx].get("chunk_id", -1)
                annotated_steps += 1

        # cross_references: keep only those with evidence
        cref_map: dict[str, dict] = {}
        for e in ev.get("cross_ref_evidence", []) or []:
            if isinstance(e, dict) and isinstance(e.get("ref"), str):
                cref_map[e["ref"]] = e
        original_refs = unit.get("cross_references", []) or []
        new_refs: list = []
        for ref in original_refs:
            if isinstance(ref, str):
                if ref in cref_map:
                    new_refs.append({
                        "ref": ref,
                        "source_quote": cref_map[ref].get("source_quote", ""),
                        "chunk_id": cref_map[ref].get("chunk_id", -1),
                    })
                else:
                    dropped_refs += 1
            elif isinstance(ref, dict):  # 罕见兼容路径
                if ref.get("ref") in cref_map:
                    ref["source_quote"] = cref_map[ref["ref"]].get("source_quote", "")
                    ref["chunk_id"] = cref_map[ref["ref"]].get("chunk_id", -1)
                    new_refs.append(ref)
                else:
                    dropped_refs += 1
        unit["cross_references"] = new_refs

    if console:
        console.print(
            f"  [cyan]evidence:[/cyan] 回填 {annotated_steps} 项；"
            f"丢弃无证据的 cross_references {dropped_refs} 条"
        )
    return units


def _merge_stage_outputs(
    cards: list[dict],
    struct_units: list[dict],
    enh_units: list[dict],
) -> list[dict]:
    """按 card_index 合并 3 阶段输出为统一的 unit 列表。

    缺位（structure / enhancement 长度短于 cards）按空字典处理，保证下游能拿到完整字段。
    """
    units: list[dict] = []
    for i, card in enumerate(cards):
        struct = struct_units[i] if i < len(struct_units) else {}
        enh = enh_units[i] if i < len(enh_units) else {}
        units.append({
            "meta": {
                "name": card.get("name", ""),
                "type": card.get("type") or [],
                # W2.4：focus_mode 留在 meta 里，供 renderer / 下游消费
                "focus_mode": card.get("focus_mode", "concept"),
                "intent": card.get("intent", ""),
                "trigger_keywords": card.get("trigger_keywords") or [],
                "os": card.get("os") or [],
                "tools_required": card.get("tools_required") or [],
            },
            "preconditions": struct.get("preconditions") or [],
            "procedure": struct.get("procedure") or [],
            "decision_points": struct.get("decision_points") or [],
            "halt_conditions": struct.get("halt_conditions") or [],
            "rollback_actions": struct.get("rollback_actions") or [],
            "cross_references": struct.get("cross_references") or [],
            # W2.4-fix：concept 卡的主要内容承载字段
            "key_concepts": struct.get("key_concepts") or [],
            "learning_enhancement": enh.get("learning_enhancement") or {},
            "source_reliability": enh.get("source_reliability", "medium"),
            "obsolescence_risk": enh.get("obsolescence_risk", "medium"),
        })
    return units


def _transport_retry_call(
    creds: dict,
    user_prompt: str,
    *,
    timeout: int,
    max_retries: int,
    console=None,
) -> list[dict]:
    """transport 层重试：超时/连接错误/JSON 语法错误均指数退避重试。

    返回 parse 出来的 list[dict]（一文多卡），还未做 schema 校验。
    """
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return _llm_call_once(creds, user_prompt, timeout=timeout)
        except _CredentialError:
            raise
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
    }
    if "api_base" in creds:
        kwargs["api_base"] = creds["api_base"]

    resp = completion(**kwargs)
    raw = (resp.choices[0].message.content or "").strip()
    return _parse_json_response(raw)


_CODEBLOCK_RE = re.compile(r"```(?:json|JSON)?\s*\n([\s\S]{1,30000}?)```")


# ---------------------------------------------------------------------------
# 阶段 schema（jsonschema Draft 2020-12，W2.1）
# ---------------------------------------------------------------------------
# 每阶段独立 schema，additionalProperties: True 允许 LLM 多输出字段
# _validate_against() 通用校验 entry point

_TYPE_ENUM = [
    "command-oriented",
    "concept-explanation",
    "decision-tree",
    "troubleshooting",
]
_RELIABILITY_ENUM = ["high", "medium", "low"]


# Stage 1: identity → {"cards": [{name, type, focus_mode, intent, ...}, ...]}
# W2.4：focus_mode required + enum，作为后续 structure 阶段的形态路由依据
_IDENTITY_SCHEMA: dict = {
    "type": "object",
    "required": ["cards"],
    "properties": {
        "cards": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "focus_mode"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 80},
                    "type": {
                        "type": "array",
                        "items": {"type": "string", "enum": _TYPE_ENUM},
                    },
                    "focus_mode": {"type": "string", "enum": _FOCUS_MODE_ENUM},
                    "intent": {"type": "string"},
                    "trigger_keywords": {"type": "array", "items": {"type": "string"}},
                    "os": {"type": "array", "items": {"type": "string"}},
                    "tools_required": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": True,
            },
        }
    },
    "additionalProperties": True,
}


# Stage 2: structure → {"units": [{procedure, decision_points, ...}, ...]}
_STRUCTURE_SCHEMA: dict = {
    "type": "object",
    "required": ["units"],
    "properties": {
        "units": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "preconditions": {"type": "array", "items": {"type": "string"}},
                    "procedure": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "seq": {"type": "integer"},
                                "action": {"type": "string"},
                                "command": {"type": "string"},
                            },
                            "additionalProperties": True,
                        },
                    },
                    "decision_points": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "condition": {"type": "string"},
                                "then": {"type": "string"},
                                "else": {"type": "string"},
                            },
                            "additionalProperties": True,
                        },
                    },
                    "halt_conditions": {"type": "array", "items": {"type": "string"}},
                    "rollback_actions": {"type": "array", "items": {"type": "string"}},
                    "cross_references": {"type": "array", "items": {"type": "string"}},
                    # W2.4-fix：concept 类卡片的主要承载字段
                    "key_concepts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["title", "explanation"],
                            "properties": {
                                "title": {"type": "string", "minLength": 1, "maxLength": 60},
                                "explanation": {"type": "string", "minLength": 10},
                                "example": {"type": "string"},
                            },
                            "additionalProperties": True,
                        },
                    },
                },
                "additionalProperties": True,
            },
        }
    },
    "additionalProperties": True,
}


# Stage 3: enhancement → {"units": [{learning_enhancement, source_reliability, obsolescence_risk}, ...]}
_ENHANCEMENT_SCHEMA: dict = {
    "type": "object",
    "required": ["units"],
    "properties": {
        "units": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "learning_enhancement": {
                        "type": "object",
                        "properties": {
                            "pain_points": {"type": "array", "items": {"type": "string"}},
                            "plain_summary": {"type": "string"},
                            "knowledge_tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "additionalProperties": True,
                    },
                    "source_reliability": {"enum": _RELIABILITY_ENUM},
                    "obsolescence_risk": {"enum": _RELIABILITY_ENUM},
                },
                "additionalProperties": True,
            },
        }
    },
    "additionalProperties": True,
}


# Stage 4: evidence → {"units": [{procedure_evidence/decision_evidence/cross_ref_evidence: [...]}, ...]}
# W2.2：所有 evidence 数组可为空（找不到原文证据时留空是允许的）
_EVIDENCE_SCHEMA: dict = {
    "type": "object",
    "required": ["units"],
    "properties": {
        "units": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "procedure_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["seq", "source_quote", "chunk_id"],
                            "properties": {
                                "seq": {"type": "integer"},
                                "source_quote": {"type": "string", "minLength": 1, "maxLength": 80},
                                "chunk_id": {"type": "integer", "minimum": 0},
                            },
                            "additionalProperties": True,
                        },
                    },
                    "decision_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["index", "source_quote", "chunk_id"],
                            "properties": {
                                "index": {"type": "integer", "minimum": 0},
                                "source_quote": {"type": "string", "minLength": 1, "maxLength": 80},
                                "chunk_id": {"type": "integer", "minimum": 0},
                            },
                            "additionalProperties": True,
                        },
                    },
                    "cross_ref_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["ref", "source_quote", "chunk_id"],
                            "properties": {
                                "ref": {"type": "string", "minLength": 1},
                                "source_quote": {"type": "string", "minLength": 1, "maxLength": 80},
                                "chunk_id": {"type": "integer", "minimum": 0},
                            },
                            "additionalProperties": True,
                        },
                    },
                },
                "additionalProperties": True,
            },
        }
    },
    "additionalProperties": True,
}


_STAGE_SCHEMAS: dict[str, dict] = {
    "identity":    _IDENTITY_SCHEMA,
    "structure":   _STRUCTURE_SCHEMA,
    "enhancement": _ENHANCEMENT_SCHEMA,
    "evidence":    _EVIDENCE_SCHEMA,
}

_STAGE_VALIDATORS: dict[str, Draft202012Validator] = {
    name: Draft202012Validator(schema) for name, schema in _STAGE_SCHEMAS.items()
}


def _validate_stage(stage: str, data: dict) -> list[str]:
    """对某 stage 的 LLM 输出做 schema 校验，返回错误描述列表（空表示通过）。"""
    if not isinstance(data, dict):
        return [f"{stage} 响应不是 object，实际类型: {type(data).__name__}"]
    validator = _STAGE_VALIDATORS.get(stage)
    if validator is None:
        return [f"未知 stage: {stage}"]
    errors: list[str] = []
    for err in validator.iter_errors(data):
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{stage}.{path}: {err.message}")
    return errors


def _parse_json_response(text: str) -> list[dict]:
    """剥离可能的 ``` 包裹，解析为 list[dict]。防回溯：限制匹配长度上限。"""
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

    parsed = json.loads(text)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        units = [u for u in parsed if isinstance(u, dict)]
        if not units:
            raise ValueError("数组中没有有效对象")
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

    # W2.4：启发式兜底也要给出 focus_mode（避免下游消费者拿到默认空）
    if decision_points:
        focus_mode = "decision"
    elif procedure:
        focus_mode = "procedure"
    else:
        focus_mode = "concept"

    return {
        "meta": {
            "name": title[:60],
            "type": list(dict.fromkeys(types)),
            "focus_mode": focus_mode,
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
        "key_concepts": [],
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
        # 优先用 collector 探测到的实际分支；旧条目缺失时 main 兜底
        "branch": source_info.get("branch") or "main",
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
        # W2.4-fix：concept 卡的主要承载字段
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

def _stage_cache_path(source_hash: str, prompt_version: str, stage: str) -> Path:
    """W2.1：阶段缓存 key 为 (hash, prompt_version, stage)。

    改任一阶段 prompt 只需重跑该阶段，其他阶段仍可复用缓存。
    旧的 v2 顶层缓存（{hash}_{ver}.json）在 v3 升版后自然失效（prompt_version 不同）。
    """
    return EXTRACT_CACHE_DIR / f"{source_hash}_{prompt_version}_{stage}.json"


def _load_stage_cache(source_hash: str, prompt_version: str, stage: str) -> dict | None:
    p = _stage_cache_path(source_hash, prompt_version, stage)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _save_stage_cache(
    source_hash: str, prompt_version: str, stage: str, data: dict,
) -> None:
    p = _stage_cache_path(source_hash, prompt_version, stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(p)
