"""提取覆盖率审计（Auditor）

对一份已 publish 到 Vault 的提取笔记做"原文 vs 提取"对照：

1. Pass 1 — LLM 从原文按章节列出可验证条目(≤30 字陈述,带 section 引用)
2. Pass 2 — LLM 对每条逐条核验:complete / weak / missing,各给 verbatim 片段(多卡时注明归属)
3. 本地规则 — 数字 / wikilink 反查(幻觉检测)
4. 跨卡重复检测(多卡时,token Jaccard ≥ 0.65)

输出结构化 `AuditReport`(不渲染报告,留给调用方决定展示)。

接口:
    audit_source(source_hash, cfg, console) -> AuditReport
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skillmind.config import (
    AUDIT_PROMPT_VERSION,
    CURRENT_PROMPT_VERSION,
    RAW_DIR,
    SCHEMA_VERSION,
    get_vault_dir,
    load_config,
    resolve_llm_credentials,
)
from skillmind.gates import (
    PASS_COVERAGE_THRESHOLD,
    build_audit_summary,
    checklist_from_items,
    evaluate_completeness,
    evaluate_executability,
    load_checklist,
    merge_critical_items,
    notes_are_current,
    read_frontmatter,
    require_same_source,
    save_checklist,
    units_from_extract_markdown,
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SourceItem:
    """从 source 抽出的一条可验证条目(Pass 1 的输出)。"""
    id: str
    section: str
    kind: str          # rule | concept | procedure_step | enum | number | example | warning | principle | link | constraint | threshold | exception | safety_limit | abort
    statement: str
    critical: bool = False
    anchors: list[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    """单条 item 在 extract 中的核对结果(Pass 2 的输出)。"""
    item_id: str
    status: str        # complete | weak | missing | unverified
    verbatim: str
    location: str      # 在哪张卡的哪一节找到的
    notes: str


@dataclass
class AuditIssue:
    """审计发现的单个问题(规则化检查产出)。"""
    category: str      # number | wikilink | duplicate | ...
    severity: str      # blocker | warning | info
    description: str
    evidence: str = ""


@dataclass
class AuditReport:
    """整个 source 的审计结果。

    字段:
        source_hash / source_title / source_path / original_chars:基本元信息
        extract_files: 涉及的 extract 卡片路径列表
        items / verify / duplicates / hallucinations:完整数据(给调试/详细报告用)
        coverage_weighted: 加权覆盖率 (0-100)
        missing: 简化版的 missing 列表(给 se-skill-distill 等下游用)
            每个: {"section", "statement", "reason"}
        verdict: "PASS" | "FAIL" | "UNKNOWN"
            只有完整性与可执行性都是 PASS 才是 PASS。未执行的门是 UNKNOWN。
        needs_human_review: bool — verdict 不是 PASS 时为 True
    """
    source_hash: str
    source_title: str
    source_path: str
    original_chars: int
    extract_files: list[str] = field(default_factory=list)
    items: list[SourceItem] = field(default_factory=list)
    verify: list[VerifyResult] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    hallucinations: list[dict] = field(default_factory=list)
    issues: list[AuditIssue] = field(default_factory=list)
    # 精简输出字段(默认 JSON)
    missing: list[dict] = field(default_factory=list)        # [{section, statement, reason}]
    coverage_weighted: float = 0.0
    verdict: str = "UNKNOWN"   # PASS | FAIL | UNKNOWN
    needs_human_review: bool = True
    pass1_elapsed: float = 0.0
    pass2_elapsed: float = 0.0
    audit_time: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    prompt_version: str = ""
    schema_version: str = ""
    completeness: dict = field(default_factory=dict)
    executability: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    checklist_id: str = ""
    checklist_total: int = 0
    checklist_source: str = ""
    checklist_meta: dict = field(default_factory=dict)
    quality_current: bool = False
    coverage_reported: float | None = None
    summary: dict = field(default_factory=dict)

    # 内部详细统计(只用于调试)
    n_complete: int = 0
    n_weak: int = 0
    n_missing: int = 0

    def to_dict(self) -> dict:
        """默认 JSON 输出:精简。"""
        return self.to_summary_dict()

    def to_summary_dict(self) -> dict:
        """精简 JSON。保留 coverage / verdict / missing，并给出两道门的状态。"""
        if self.summary:
            return self.summary
        coverage = self.coverage_reported
        if coverage is None and self.completeness.get("status") not in ("", "UNKNOWN"):
            coverage = round(self.coverage_weighted, 1)
        return {
            "source_hash": self.source_hash,
            "source_title": self.source_title,
            "coverage": coverage,
            "verdict": self.verdict,
            "needs_human_review": self.needs_human_review,
            "missing": self.missing,
            "gates": {
                "completeness": self.completeness.get("status", "UNKNOWN"),
                "executability": self.executability.get("status", "UNKNOWN"),
            },
            "completeness": self.completeness,
            "executability": self.executability,
            "counts": self.counts,
            "checklist": self.checklist_meta,
            "checklist_source": self.checklist_source,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "quality_current": self.quality_current,
        }

    def to_detailed_dict(self) -> dict:
        """完整 JSON:所有字段(给调试用)。"""
        return {
            "source_hash": self.source_hash,
            "source_title": self.source_title,
            "source_path": self.source_path,
            "original_chars": self.original_chars,
            "extract_files": self.extract_files,
            "coverage_weighted": self.coverage_reported if self.coverage_reported is not None else round(self.coverage_weighted, 1),
            "coverage": self.coverage_reported,
            "verdict": self.verdict,
            "needs_human_review": self.needs_human_review,
            "missing": self.missing,
            "items": [it.__dict__ for it in self.items],
            "verify": [v.__dict__ for v in self.verify],
            "duplicates": self.duplicates,
            "hallucinations": self.hallucinations,
            "issues": [i.__dict__ for i in self.issues],
            "pass1_elapsed": self.pass1_elapsed,
            "pass2_elapsed": self.pass2_elapsed,
            "audit_time": self.audit_time,
            "prompt_version": self.prompt_version,
            "n_complete": self.n_complete,
            "n_weak": self.n_weak,
            "n_missing": self.n_missing,
            "completeness": self.completeness,
            "executability": self.executability,
            "counts": self.counts,
            "checklist": self.checklist_meta,
            "checklist_source": self.checklist_source,
            "schema_version": self.schema_version,
            "quality_current": self.quality_current,
        }


# ---------------------------------------------------------------------------
# Pass 1 — LLM 从原文按章节抽可验证条目
# ---------------------------------------------------------------------------

_AUDIT_SYSTEM_PROMPT = (
    "你是文档拆条员。请把给定文档按结构拆成可单独验证的条目,以 JSON 输出。"
    "直接输出 JSON,不要解释,不要 Markdown 包裹。"
)

_EXTRACT_ITEMS_PROMPT = """【任务】
读 source 文档,按 ## / ### 章节结构列出**全部**可验证条目。

【粒度】
- 每条 ≤ 30 字陈述,**不要**贴 verbatim 原文
- 章节标题作为 section 字段(用最近一层 ## 或 ### 标题)
- 一条 item = 一个**可独立验证的具体观点**

【kind 分类】
- constraint / threshold / exception / safety_limit / abort
                  : 关键保留项。每条单独列出，critical=true。
                    包括 blanket approve、复审或截止日期、区域策略、
                    token / rate / recursion 上限、must / never、中止条件。
                    这些不得并进章节摘要。
- rule            : 其他可执行规则
- concept         : 定义、解释、原理、原则、说明段
- procedure_step  : 操作步骤中的单步动作
- enum            : 列表中的一个类别或一个成员(独立成员各算一条)
- number          : 含具体数值 / 比例 / 计数 / 尺寸 / 版本号
- example         : 独立示例 / 案例 / 范例
- warning         : 警告 / 反模式 / anti-pattern / 难点注意
- principle       : 设计原则 / 价值观 / 通用规则
- link            : 单个外部链接

【拆分粒度原则(关键!)】
- **同质列表收敛**:8 个调色板 / 8 种视觉模式 / 6 种 layout → 合并为 1 条 enum,
  statement 简短总结(如 "8 种参考调色板:black+cyan+coral 等")
  不要每个成员单独 1 条(否则 8+8+6=22 条不必要的膨胀)
- **关键约束不合并**:上面的 constraint / threshold / exception / safety_limit / abort
  每条保留自己的 anchors（原文短引），即使看起来相邻
- **章节级摘要句**:整段叙事的"原则段"(reference style DNA / core principle) → 1 条 concept 即可,不要拆到每个形容词
- **重复/冗余检测**:同一句话在多个 bullets 出现 → 只列 1 次
- **超长 numbered list**(10+ steps) → 1 条 procedure_step 包含序号,不要每步 1 条

【覆盖目标】
- 章节下**主要内容**:原则、规则、警告、链接、关键数值、关键示例 → 必列
- **不**逐字列所有形容词、副词、连接词
- **不**列页面装饰元素(如 frontmatter 的 "Skill" 标签、source 描述的格式说明)

【数量硬上限】
- 1000 字符文档 ≤ 20 条
- 3000 字符文档 ≤ 60 条
- 10000 字符文档 ≤ 150 条
- 30000 字符文档 ≤ 250 条
- **超过上限必然是过拆了**——回到上面"拆分粒度原则"重新合并

【输出 JSON 数组】
[
  {"section": "SAFETY", "kind": "safety_limit", "statement": "禁止 blanket-approve", "critical": true, "anchors": ["Never blanket-approve"]},
  {"section": "VISUAL MODES", "kind": "enum", "statement": "共 8 种视觉模式", "critical": false, "anchors": []},
  ...
]

【source 文档(共 {char_count} 字符)】
{content}

请直接输出 JSON 数组。"""


def _llm_extract_items(source_text: str, cfg: dict) -> list[dict]:
    """Pass 1:从 source 抽按章节组织的可验证条目。"""
    creds = resolve_llm_credentials(cfg, command="audit")
    text = source_text
    if len(text) > 60000:
        text = text[:60000] + "\n\n[... 原文过长,已截至 60K 字符。审计基于本段。]"

    user_prompt = (
        _EXTRACT_ITEMS_PROMPT
        .replace("{char_count}", str(len(text)))
        .replace("{content}", text)
    )
    resp = _call_llm(creds, _AUDIT_SYSTEM_PROMPT, user_prompt, cfg)
    items = _parse_json_array(resp)
    numbered = []
    for idx, it in enumerate(items, start=1):
        if not isinstance(it, dict) or not str(it.get("statement", "")).strip():
            continue
        item = dict(it)
        item["id"] = str(item.get("id") or f"i-{idx:04d}")
        numbered.append(item)
    return numbered


# ---------------------------------------------------------------------------
# Pass 2 — LLM 对每条逐条核对
# ---------------------------------------------------------------------------

_VERIFY_ITEMS_PROMPT = """【任务】
对 source 抽出的每一条 items,判断它**在 extract 中是否被保留**。

【3 档判定】
- complete : extract 中能找到该 items 的核心信息(语义匹配即可,措辞可以不同)
            → 必须给 verbatim 片段(≤ 80 字,从 extract 抠)和所在位置(card 名 / 章节标题)
- weak     : 出现了但简化 / 改写损失了精度
            → 也要给 extract 中的对应片段
- missing  : extract 中完全找不到
            → verbatim 留空,notes 注明该 items 应在的章节

【判定注意】
- 跨语言译文(英文 source / 中文 extract)算语义匹配
- 数值必须保留原始精度(精确或相近单位)
- 链接必须 URL 完整
- 章节 / 子项归属:抽出 verbatim 时注明所在 extract card 名

【输出 JSON 数组】
[
  {
    "item_id": 1,
    "status": "complete",
    "verbatim": "<extract 中对应的 verbatim 片段>",
    "location": "Card1.md - 💡 核心概念 / REFERENCE STYLE DNA",
    "notes": ""
  },
  {
    "item_id": 2,
    "status": "missing",
    "verbatim": "",
    "location": "",
    "notes": "extract 完全没有该 items;该 items 应在 source 的 X 章节"
  },
  ...
]

【硬性要求】
- 必须为每条 items 都输出一项
- 顺序与 items 输入一致
- missing 状态 **必须** 在 notes 字段写明原因:该 items 在 source 哪一节,extract 为什么漏(没提到 / 简化掉了 / 放在错误的章节 等)

【source items 列表(JSON)】
{points_json}

【extract 内容(多个 card 用 === EXTRACT CARD: <name> === 分隔)】
{extract_text}

请直接输出 JSON 数组。"""


def _llm_verify_extract(items: list[dict], extract_text: str, cfg: dict) -> list[dict]:
    """Pass 2:对每个 item 在 extract 中判断 complete/weak/missing + verbatim。"""
    creds = resolve_llm_credentials(cfg, command="audit")
    if not items:
        return []
    items_payload = [
        {"id": it["id"], "section": it.get("section", ""), "kind": it.get("kind", ""),
         "statement": it.get("statement", "")}
        for it in items
    ]
    et = extract_text
    if len(et) > 60000:
        et = et[:60000] + "\n\n[... extract 过长,已截至 60K 字符。]"
    user_prompt = (
        _VERIFY_ITEMS_PROMPT
        .replace("{points_json}", json.dumps(items_payload, ensure_ascii=False))
        .replace("{extract_text}", et)
    )
    resp = _call_llm(creds, _AUDIT_SYSTEM_PROMPT, user_prompt, cfg)
    verify = _parse_json_array(resp)
    by_id: dict[str, dict] = {}
    for row in verify:
        if not isinstance(row, dict) or "item_id" not in row:
            continue
        by_id[str(row.get("item_id")).strip()] = row
    for it in items:
        item_id = str(it.get("id") or "")
        if item_id in by_id:
            continue
        match = re.search(r"(\d+)$", item_id)
        if not match:
            continue
        alias = str(int(match.group(1)))
        owners = []
        for other in items:
            other_id = str(other.get("id") or "")
            tail = re.search(r"(\d+)$", other_id)
            if tail and str(int(tail.group(1))) == alias:
                owners.append(other_id)
        if owners == [item_id] and alias in by_id:
            by_id[item_id] = by_id[alias]
    return [
        {
            "item_id": str(it["id"]),
            "status": (by_id.get(str(it["id"]), {}).get("status", "missing") or "missing").lower(),
            "verbatim": (by_id.get(str(it["id"]), {}).get("verbatim", "") or "").strip()[:200],
            "location": (by_id.get(str(it["id"]), {}).get("location", "") or "").strip()[:200],
            "notes": (by_id.get(str(it["id"]), {}).get("notes", "") or "").strip()[:200],
        }
        for it in items
    ]


# ---------------------------------------------------------------------------
# 规则 — 数字 / wikilink 反查(幻觉检测)
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"(\d+(?:\.\d+)?(?:px|rem|em|vw|vh|%|s|ms|fps)\b"
    r"|\d+\s*:\s*\d+"
    r"|\d+\s*-\s\d+\s*px"
    r"|\d+×\d+"
    r"|\d+\s*:\s*\d+\s*:\s*\d+"
    r"|clamp\([^)]*\d[^)]*\))"
)

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def _normalize_for_hall_match(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 4)
        if end >= 0:
            return text[end + 4:].lstrip()
    return text


def _detect_hallucinations(extract_files: list[Path], raw_text: str) -> list[dict]:
    """扫 extract 笔记里的精确数字 / wikilink 反查原文是否真存在。"""
    halls: list[dict] = []
    raw_norm = _normalize_for_hall_match(raw_text)

    for fp in extract_files:
        try:
            full_text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        note_text = _strip_frontmatter(full_text)

        # 数字
        for m in _NUMBER_RE.finditer(note_text):
            value = m.group(0)
            value_norm = _normalize_for_hall_match(value)
            if value_norm in raw_norm:
                continue
            num_only = re.search(r"\d+(?:\.\d+)?", value)
            if num_only and num_only.group(0) in raw_text:
                continue
            if num_only and len(num_only.group(0)) <= 1:
                continue
            ctx_start = max(0, m.start() - 30)
            ctx_end = min(len(note_text), m.end() + 30)
            context = note_text[ctx_start:ctx_end].replace("\n", " ").strip()
            halls.append({
                "category": "number",
                "value": value,
                "found_in": f"{fp.name} : {context}",
                "note": "原文未出现该精确数值",
            })

        # wikilink
        for m in _WIKILINK_RE.finditer(note_text):
            target = m.group(1).strip()
            if not target or "/" in target:
                continue
            target_norm = _normalize_for_hall_match(target)
            if target_norm in raw_norm:
                continue
            words = [w for w in re.split(r"[\s\-_]+", target.lower()) if len(w) > 2]
            if words and all(w in raw_text.lower() for w in words):
                continue
            halls.append({
                "category": "wikilink",
                "value": f"[[{target}]]",
                "found_in": fp.name,
                "note": "原文未提及该关联词",
            })

    # 去重
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    for h in halls:
        key = (h["category"], h["value"], h["found_in"].split(" : ")[0])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)
    return deduped


# ---------------------------------------------------------------------------
# 规则 — 跨卡重复检测(只在多卡时启用)
# ---------------------------------------------------------------------------

def _split_sentences(text: str, min_len: int = 12) -> list[str]:
    """中文/英文句末标点切句。同时跳过 vault 通用装饰 banner(每张卡都一样的模板元信息)。"""
    body = _strip_frontmatter(text)
    raw = re.split(r"[\n。.!?;；]+", body)
    out: list[str] = []
    for s in raw:
        s = s.strip()
        if len(s) < min_len:
            continue
        # 跳过 vault 通用装饰 banner(每个 card 都一样,不算重复)
        if re.match(r"^[📋📎]?\s*(Skill|父文档|来源|可信度|时效性)", s):
            continue
        if s.startswith(">") and len(s) < 60:  # 短 blockquote 通常是 banner/标注
            continue
        if re.match(r"^[-*_=]{3,}$", s):  # 分隔线
            continue
        out.append(s)
    return out


def _token_overlap(a: str, b: str) -> float:
    """Jaccard token overlap,作为句子相似度的简单代理。"""
    ta = {t for t in re.split(r"\s+", a.lower()) if len(t) > 1}
    tb = {t for t in re.split(r"\s+", b.lower()) if len(t) > 1}
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


def _detect_duplicates_across_cards(extract_files: list[Path], sim_threshold: float = 0.65) -> list[dict]:
    """跨卡找高度相似的句子。"""
    if len(extract_files) < 2:
        return []
    per_card_sents: list[tuple[str, list[str]]] = []
    for fp in extract_files:
        sents = _split_sentences(fp.read_text(encoding="utf-8", errors="replace"))
        per_card_sents.append((fp.name, sents))

    pairs: list[dict] = []
    for i in range(len(per_card_sents)):
        for j in range(i + 1, len(per_card_sents)):
            name_i, sents_i = per_card_sents[i]
            name_j, sents_j = per_card_sents[j]
            for si in sents_i:
                for sj in sents_j:
                    sim = _token_overlap(si, sj)
                    if sim >= sim_threshold:
                        pairs.append({
                            "card_a": name_i,
                            "card_b": name_j,
                            "text": si[:120] + ("..." if len(si) > 120 else ""),
                            "similarity": round(sim, 2),
                        })
    return pairs


# ---------------------------------------------------------------------------
# LLM 调用 + JSON 解析
# ---------------------------------------------------------------------------

def _call_llm(creds: dict, system: str, user: str, cfg: dict) -> str:
    from litellm import completion  # type: ignore
    llm_cfg = dict(cfg.get("llm", {}))
    profiles = cfg.get("llm_profiles", {})
    audit_profile = profiles.get("audit", {})
    if isinstance(audit_profile, dict):
        for k in ("timeout", "max_retries"):
            v = audit_profile.get(k)
            if v is not None:
                llm_cfg[k] = v
    timeout = int(llm_cfg.get("timeout", 180))
    max_retries = int(llm_cfg.get("max_retries", 2))
    max_tokens = int(audit_profile.get("max_tokens", 32768)) if isinstance(audit_profile, dict) else 32768
    kwargs: dict[str, Any] = {
        "model": creds["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "timeout": timeout,
        "api_key": creds["api_key"],
        "max_tokens": max_tokens,
    }
    if "api_base" in creds:
        kwargs["api_base"] = creds["api_base"]
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = completion(**kwargs)
            return resp.choices[0].message.content or ""
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(1.0 * (2 ** attempt))
    raise RuntimeError(f"LLM 调用失败 {max_retries + 1} 次:{last_err}")


_CODEBLOCK_RE = re.compile(r"```(?:json|JSON)?\s*\n([\s\S]{1,200000}?)```")


def _parse_json_array(text: str) -> list[dict]:
    """从 LLM 响应中解析 JSON 数组(兼容代码块包裹 / 末尾解释)。"""
    text = text.strip()
    if len(text) > 200000:
        text = text[:200000]
    m = _CODEBLOCK_RE.search(text)
    if m:
        text = m.group(1).strip()
    if not text.startswith("["):
        idx = text.find("[")
        if idx >= 0:
            text = text[idx:]
    if not text.startswith("["):
        raise ValueError("LLM 响应不是 JSON 数组")
    last = text.rfind("]")
    if last >= 0:
        text = text[:last + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = json.loads(_fix_array_truncation(text))
    if not isinstance(data, list):
        raise ValueError("响应解析后不是数组")
    return [item for item in data if isinstance(item, dict)]


def _fix_array_truncation(text: str) -> str:
    last_brace = text.rfind("}")
    if last_brace < 0:
        return text
    truncated = text[:last_brace + 1]
    depth = 0
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
            depth += 1
        elif ch == "]":
            depth -= 1
    return truncated + "]" * max(0, depth)


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def _resolve_source(source_hash_prefix: str) -> tuple[str, dict]:
    from skillmind.collector import _load_hashes
    hashes = _load_hashes()
    matched = [sha for sha in hashes if sha.startswith(source_hash_prefix)]
    if not matched:
        matched = [
            sha for sha, info in hashes.items()
            if source_hash_prefix in info.get("source_path", "") or source_hash_prefix in info.get("title", "")
        ]
    if not matched:
        raise RuntimeError(f"未找到 source_hash 前缀 / 路径关键词:{source_hash_prefix}")
    if len(matched) > 1:
        sample = "\n".join(f"  {sha[:16]}  {hashes[sha].get('title', '')[:60]}" for sha in matched[:5])
        raise RuntimeError(f"匹配到 {len(matched)} 个 source_hash,请用更长的前缀:\n{sample}")
    full_hash = matched[0]
    info = dict(hashes[full_hash])
    info["source_hash"] = full_hash
    return full_hash, info


def _read_raw_text(source_info: dict) -> str:
    raw_path = source_info.get("raw_path", "")
    if not raw_path:
        raise RuntimeError("source_info 缺少 raw_path")
    p = Path(raw_path)
    if not p.exists():
        sha = source_info.get("source_hash", "")
        if sha:
            candidate = RAW_DIR / sha[:2] / f"{sha}.md"
            if candidate.exists():
                p = candidate
    if not p.exists():
        raise RuntimeError(f"原文不存在:{raw_path}")
    return p.read_text(encoding="utf-8", errors="replace")


def _find_extract_notes(source_hash: str, vault_skills_dir: Path, *, max_n: int = 20) -> list[Path]:
    """扫 vault skills 目录,找 source_hash 匹配的卡片。"""
    import yaml as _yaml
    if not vault_skills_dir.exists():
        return []
    matched: list[Path] = []
    for fp in vault_skills_dir.rglob("*.md"):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 4)
        if end < 0:
            continue
        fm_text = text[3:end].strip()
        try:
            fm = _yaml.safe_load(fm_text)
        except Exception:
            continue
        if not isinstance(fm, dict):
            continue
        fm_hash = str(fm.get("source_hash", "")).strip()
        if fm_hash and (
            fm_hash == source_hash
            or source_hash.startswith(fm_hash)
            or fm_hash.startswith(source_hash[:7])
        ):
            matched.append(fp)
            if len(matched) >= max_n:
                break
    return matched


def _concat_extracts(files: list[Path]) -> str:
    """拼接多张 extract 卡片正文,卡名前缀标识归属。"""
    parts: list[str] = []
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        body = _strip_frontmatter(text)
        parts.append(f"=== EXTRACT CARD: {fp.name} ===\n{body}")
    return "\n\n---\n\n".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def audit_source(
    source_hash: str,
    cfg: dict | None = None,
    *,
    console=None,
    max_extract_files: int = 20,
    vault_skills_override: str | Path | None = None,
    checklist_path: str | Path | None = None,
    save_checklist_path: str | Path | None = None,
) -> AuditReport:
    """对一个 source_hash 做覆盖率审计,产出结构化 AuditReport。

    流程:
        1. Pass 1 (LLM, ~3-10s):从 source 按章节抽可验证条目
        2. Pass 2 (LLM, ~5-15s):对每条逐条核对 + verbatim 对齐
        3. 规则:数字 / wikilink 反查 → hallucinations
        4. 规则(多卡):跨卡句子相似度 → duplicates

    返回 AuditReport,字段:
        items: Pass 1 产出的 source items
        verify: Pass 2 产出的逐条核对结果
        hallucinations: 规则扫出的疑似幻觉
        duplicates: 跨卡重复句子(单卡时为空)
        verdict: PASS / FAIL / UNKNOWN。两道门都 PASS 才是 PASS。
        n_complete / n_weak / n_missing / coverage_weighted: 派生统计
    """
    cfg = cfg or load_config()

    full_hash, source_info = _resolve_source(source_hash)
    raw_text = _read_raw_text(source_info)
    if not raw_text.strip():
        raise RuntimeError(f"原文为空:{source_info.get('raw_path', '')}")

    if vault_skills_override:
        vault_skills = Path(vault_skills_override)
    else:
        vault_skills = get_vault_dir(cfg) / "skills"

    extract_files = _find_extract_notes(full_hash, vault_skills, max_n=max_extract_files)
    if not extract_files:
        raise RuntimeError(
            f"未在 vault skills/ 下找到 source_hash={full_hash[:16]} 的提取笔记。\n请确认已 publish。"
        )

    if console:
        console.print(
            f"  [dim]原文 {len(raw_text)} 字符,{len(extract_files)} 张 extract 卡片[/dim]"
        )

    # ============ 清单：复用冻结清单，或 Pass 1 后冻结 ============
    frozen = None
    checklist_source = "absent"
    pass1_ok = False
    items_raw: list[dict] = []
    t0 = time.time()
    if checklist_path:
        frozen = load_checklist(checklist_path)
        require_same_source(frozen, full_hash)
        items_raw = [item.to_dict() for item in frozen.items]
        checklist_source = f"frozen:{frozen.checklist_id}"
        pass1_ok = True
        if console:
            console.print(
                f"  [cyan]复用冻结清单 {frozen.checklist_id}，共 {frozen.total} 条[/cyan]"
            )
    else:
        if console:
            console.print("  [cyan]Pass 1 — 抽取原文可验证条目...[/cyan]")
        try:
            items_raw = merge_critical_items(_llm_extract_items(raw_text, cfg), raw_text)
            pass1_ok = True
        except Exception as e:
            if console:
                console.print(f"  [red]Pass 1 失败:{e}[/red]")
            items_raw = []
            pass1_ok = False
        if pass1_ok:
            frozen = checklist_from_items(
                items_raw,
                source_hash=full_hash,
                prompt_version=CURRENT_PROMPT_VERSION,
                schema_version=SCHEMA_VERSION,
                audit_prompt_version=AUDIT_PROMPT_VERSION,
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            checklist_source = f"generated:{frozen.checklist_id}"
            items_raw = [item.to_dict() for item in frozen.items]
            if save_checklist_path:
                save_checklist(save_checklist_path, frozen)
                if console:
                    console.print(f"  [dim]清单已冻结: {save_checklist_path}[/dim]")
    pass1_elapsed = time.time() - t0
    items = [
        SourceItem(
            id=str(it["id"]),
            section=it.get("section", ""),
            kind=it.get("kind", "rule"),
            statement=it.get("statement", ""),
            critical=bool(it.get("critical")),
            anchors=list(it.get("anchors") or []),
        )
        for it in items_raw
    ]

    if console:
        kinds: dict[str, int] = {}
        for it in items:
            kinds[it.kind] = kinds.get(it.kind, 0) + 1
        kind_str = " / ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        console.print(f"  [dim]  抽出 {len(items)} 条 items:{kind_str}[/dim]")

    # ============ Pass 2:逐条核对 ============
    extract_text = _concat_extracts(extract_files)
    if console:
        console.print(
            f"  [cyan]Pass 2 — 对照 {len(items)} 条 vs {len(extract_files)} 张卡片...[/cyan]"
        )
    t0 = time.time()
    pass2_ok = False
    try:
        if not items_raw:
            verify_raw = []
        else:
            verify_raw = _llm_verify_extract(items_raw, extract_text, cfg)
            pass2_ok = True
    except Exception as e:
        if console:
            console.print(f"  [red]Pass 2 失败:{e}[/red]")
        verify_raw = []
        pass2_ok = False
    pass2_elapsed = time.time() - t0

    # ============ 规则扫 ============
    if console:
        console.print("  [cyan]本地规则 — 幻觉 + 跨卡重复...[/cyan]")
    halls = _detect_hallucinations(extract_files, raw_text)
    duplicates = _detect_duplicates_across_cards(extract_files)

    # ============ 双门禁 ============
    local_cover = bool(items) and all(item.anchors for item in items)
    completeness_executed = bool(frozen) and (pass2_ok or local_cover)
    verify_by_id = {str(row["item_id"]): row for row in verify_raw} if pass2_ok else {}
    import re as _re
    quote_hits = _re.findall(r"原文：`([^`]*)`", extract_text)
    completeness = evaluate_completeness(
        frozen.items if frozen else [],
        verify_by_id,
        executed=completeness_executed,
        extract_text=extract_text,
        source=checklist_source,
        source_text=raw_text,
        quotes=quote_hits,
    )
    prior_by_id = {str(row.get("item_id")): row for row in verify_raw if isinstance(row, dict)}
    verify = []
    for row in completeness.get("per_item") or []:
        prior = prior_by_id.get(str(row["id"]), {})
        status = row["status"] if row["status"] in ("complete", "weak", "missing", "unverified", "not_applicable") else "unverified"
        verify.append(VerifyResult(
            item_id=str(row["id"]),
            status=status,
            verbatim=str(prior.get("verbatim") or "")[:200],
            location=str(prior.get("location") or "")[:200],
            notes=str(prior.get("notes") or row.get("reason") or "")[:200],
        ))

    frontmatters = []
    extracted_units: list[dict] = []
    for fp in extract_files:
        try:
            note = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatters.append(read_frontmatter(note))
        extracted_units.extend(units_from_extract_markdown(note))
    quality_current = notes_are_current(
        frontmatters,
        schema_version=SCHEMA_VERSION,
        prompt_version=CURRENT_PROMPT_VERSION,
    )
    executability = evaluate_executability(
        extracted_units,
        raw_text,
        executed=bool(extract_text.strip()),
    )
    # 跨卡相似句保留为 issue。相同决策规则由可执行性门的 duplication 判定，
    # 避免一句重复的注意事项单独否决整篇。
    if duplicates and executability.get("status") != "UNKNOWN":
        executability.setdefault("reasons", []).append(
            f"{len(duplicates)} cross-card similar sentence(s); informational"
        )

    note_prompts = sorted({
        str(fm.get("prompt_version") or "")
        for fm in frontmatters
        if fm.get("prompt_version")
    })
    note_schemas = sorted({
        str(fm.get("schema_version") or "")
        for fm in frontmatters
        if fm.get("schema_version")
    })
    # 顶层身份写笔记自己的 prompt/schema。当前标准写在清单上，避免旧稿被标成 extract_v10。
    reported_prompt = note_prompts[0] if len(note_prompts) == 1 else CURRENT_PROMPT_VERSION
    reported_schema = note_schemas[0] if len(note_schemas) == 1 else SCHEMA_VERSION
    summary = build_audit_summary(
        source_hash=full_hash,
        source_title=source_info.get("title", "") or source_info.get("source_path", ""),
        completeness=completeness,
        executability=executability,
        checklist=frozen,
        checklist_source=checklist_source,
        prompt_version=reported_prompt,
        schema_version=reported_schema,
        quality_current=quality_current,
        hallucination_checked=True,
        hallucination_count=len(halls),
    )
    coverage_value = summary.get("coverage")
    coverage_weighted = float(coverage_value) if isinstance(coverage_value, (int, float)) else 0.0

    issues: list[AuditIssue] = []
    for h in halls:
        issues.append(AuditIssue(
            category=f"hallucination_{h.get('category', 'unknown')}",
            severity="warning",
            description=f"{h.get('note', '可疑 hallucination')}: {h.get('value', '')}",
            evidence=h.get("found_in", ""),
        ))
    for d in duplicates:
        issues.append(AuditIssue(
            category="duplicate_across_cards",
            severity="info",
            description=f"重复句子 sim={d['similarity']}: {d['text']}",
            evidence=f"{d['card_a']} ↔ {d['card_b']}",
        ))
    for missing_constraint in completeness.get("critical_missing") or []:
        issues.append(AuditIssue(
            category="critical_missing",
            severity="blocker",
            description=missing_constraint.get("statement", ""),
            evidence=missing_constraint.get("section", ""),
        ))

    return AuditReport(
        source_hash=full_hash,
        source_title=summary["source_title"],
        source_path=source_info.get("source_path", "") or source_info.get("source_url", ""),
        original_chars=len(raw_text),
        extract_files=[str(p) for p in extract_files],
        items=items,
        verify=verify,
        duplicates=duplicates,
        hallucinations=halls,
        issues=issues,
        missing=summary["missing"],
        coverage_weighted=coverage_weighted,
        verdict=summary["verdict"],
        needs_human_review=summary["needs_human_review"],
        pass1_elapsed=pass1_elapsed,
        pass2_elapsed=pass2_elapsed,
        prompt_version=reported_prompt,
        schema_version=reported_schema,
        completeness=summary["completeness"],
        executability=summary["executability"],
        counts=summary["counts"],
        checklist_id=summary["checklist"]["id"],
        checklist_total=summary["checklist"]["total"],
        checklist_source=checklist_source,
        checklist_meta=summary["checklist"],
        quality_current=quality_current,
        coverage_reported=coverage_value if isinstance(coverage_value, (int, float)) else None,
        summary=summary,
        n_complete=summary["counts"]["complete"],
        n_weak=summary["counts"]["weak"],
        n_missing=summary["counts"]["missing"],
    )
