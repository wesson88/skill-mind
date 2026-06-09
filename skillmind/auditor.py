"""提取覆盖率审计（Auditor）

对一份已 publish 到 Vault 的提取笔记做"原文 vs 提取"的客观对照：

1. Pass 1 — LLM 从原文抽 atomic 观点清单（rule / enum / number / example 四类）
2. Pass 2 — LLM 对每条观点逐条核对提取笔记：✅ 完整 / 🟡 弱化 / ❌ 缺失
3. 本地规则 — 扫提取笔记里的精确数字 / wikilink 反查原文是否真存在（幻觉检测）
4. 渲染 markdown 报告：覆盖率百分比 + 缺失清单 + 弱化清单 + 幻觉清单

设计目标：作为 prompt_version 升级的质量回归基线，让"提取改了一版，质量是好是坏"
有客观数据可依。

接口：
    audit_source(source_hash, cfg, console) -> AuditReport
    render_audit_report(report) -> str
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skillmind.config import (
    RAW_DIR,
    get_vault_dir,
    load_config,
    resolve_llm_credentials,
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class AtomicPoint:
    """从原文抽取的一条 atomic 观点。"""
    id: int
    statement: str           # 观点本身，一句话
    kind: str                # rule | enum | number | example
    source_quote: str = ""   # 原文最贴近的引用（≤ 80 字）


@dataclass
class PointMatch:
    """单条观点在提取笔记里的命中情况。"""
    point_id: int
    status: str              # complete | weak | missing
    evidence: str = ""       # 提取笔记里命中的片段（若有）
    notes: str = ""          # LLM 给出的差异说明


@dataclass
class Hallucination:
    """疑似幻觉：提取笔记里有但原文没有的精确数字或 wikilink。"""
    kind: str                # number | wikilink
    value: str
    found_in_extract: str    # 命中的提取笔记文件名 + 行号片段
    note: str = ""


@dataclass
class AuditReport:
    source_hash: str
    source_title: str
    source_path: str
    original_chars: int
    extract_files: list[str]
    points: list[AtomicPoint]
    matches: list[PointMatch]
    hallucinations: list[Hallucination]
    coverage_rate: float                          # (✅ + 0.5×🟡) / total
    complete_count: int = 0
    weak_count: int = 0
    missing_count: int = 0
    audit_time: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def audit_source(
    source_hash: str,
    cfg: dict | None = None,
    *,
    console=None,
    max_extract_files: int = 10,
    vault_skills_override: str | Path | None = None,
) -> AuditReport:
    """对一个 source_hash 对应的原文 + 提取笔记做覆盖率审计。

    Parameters
    ----------
    source_hash : str
        原文 SHA256（hashes.yaml 的 key）。支持前缀匹配。
    cfg : dict | None
        skillmind 配置；缺省 load_config()。
    console : rich.console.Console | None
        进度输出。
    max_extract_files : int
        最多审计的提取笔记数量（一文多卡时；默认 10）。

    Returns
    -------
    AuditReport
    """
    cfg = cfg or load_config()

    # 1. 解析 source_hash → 原文 + metadata
    full_hash, source_info = _resolve_source(source_hash)
    raw_text = _read_raw_text(source_info)
    if not raw_text.strip():
        raise RuntimeError(f"原文为空或读取失败：{source_info.get('raw_path', '')}")

    # 2. 扫 vault 找对应提取笔记（按 frontmatter source_hash 匹配）
    extract_files = _find_extract_notes(
        full_hash, cfg, max_n=max_extract_files,
        vault_skills_override=vault_skills_override,
    )
    if not extract_files:
        raise RuntimeError(
            f"未在 vault skills/ 下找到 source_hash={full_hash[:16]} 的提取笔记。\n"
            f"请确认已 publish。"
        )

    if console:
        console.print(
            f"  [dim]审计原文 {len(raw_text)} 字符，{len(extract_files)} 篇提取笔记[/dim]"
        )

    # 3. Pass 1 — LLM 抽 atomic 观点清单
    if console:
        console.print("  [cyan]Pass 1 — 抽取原文 atomic 观点清单...[/cyan]")
    points = _extract_atomic_points(raw_text, source_info, cfg)
    if console:
        kinds = {"rule": 0, "enum": 0, "number": 0, "example": 0}
        for p in points:
            kinds[p.kind] = kinds.get(p.kind, 0) + 1
        console.print(
            f"  [dim]  抽出 {len(points)} 条观点："
            f"rule={kinds['rule']} / enum={kinds['enum']} / "
            f"number={kinds['number']} / example={kinds['example']}[/dim]"
        )

    # 4. Pass 2 — LLM 逐条核对提取笔记
    extract_text = _concat_extracts(extract_files)
    if console:
        console.print(f"  [cyan]Pass 2 — 对照 {len(points)} 条观点 vs 提取笔记...[/cyan]")
    matches = _compare_with_extracts(points, extract_text, cfg)

    # 5. 本地幻觉检测（数字 + wikilink）
    if console:
        console.print("  [cyan]本地规则 — 幻觉检测（数字 / wikilink）...[/cyan]")
    halls = _detect_hallucinations(extract_files, raw_text)

    # 6. 统计
    complete = sum(1 for m in matches if m.status == "complete")
    weak = sum(1 for m in matches if m.status == "weak")
    missing = sum(1 for m in matches if m.status == "missing")
    total = len(matches) or 1
    coverage = (complete + 0.5 * weak) / total

    return AuditReport(
        source_hash=full_hash,
        source_title=source_info.get("title", "") or source_info.get("source_path", ""),
        source_path=source_info.get("source_path", "") or source_info.get("source_url", ""),
        original_chars=len(raw_text),
        extract_files=[str(p) for p in extract_files],
        points=points,
        matches=matches,
        hallucinations=halls,
        coverage_rate=coverage,
        complete_count=complete,
        weak_count=weak,
        missing_count=missing,
    )


# ---------------------------------------------------------------------------
# Pass 1：抽 atomic 观点
# ---------------------------------------------------------------------------

_AUDIT_SYSTEM_PROMPT = (
    "你是文档保真度审计员。你的任务是从给定文档中按 atomic 粒度抽取每一条独立观点，"
    "并以严格 JSON 输出。\n"
    "重要：直接输出 JSON，不要附带任何解释、不要使用 Markdown 代码块包裹。"
)

_POINTS_PROMPT_TEMPLATE = """【任务】
从以下原文中按 atomic 粒度（一句话能表达完整意思）抽取每一条规则 / 枚举项 / 数值阈值 / 示例性论断。

【粒度要求】
- atomic = 拆到不能再拆。"hero 字号 60-72px"是一条；"hero 字号 60-72px + 间距 64px"必须拆成两条。
- 必须覆盖所有 must / never / ban / prefer / avoid / required 等结构性硬规则。
- 必须覆盖所有具体数值（1-10 评分、像素值、字号、Choose exactly N 等）。
- 必须覆盖原文中的列表项（即使有 10-30 条，全部列出）。
- 示例性表述（"like X"、"such as Y"）也抽出，但 kind 标 example。

【kind 分类】
- rule    : 硬性约束（must/never/ban）或软建议（prefer/avoid）
- enum    : 列表项（成员之一）— 如"允许的字体名：Inter / Satoshi / Geist"中的每个名字
- number  : 含具体数值的观点（含百分比、比例、像素、字号、计数）
- example : 示例性表述、举例说明，删了不影响规则本身

【输出 JSON 数组】
[
  {"id": 1, "statement": "一句话观点", "kind": "rule|enum|number|example", "source_quote": "原文最贴近的引用（≤ 80 字）"},
  ...
]

【硬性要求】
- 不要省略，不要用"等"、"诸如此类"
- statement 用陈述句，不要"建议"、"可能"等模糊词
- source_quote 必须是原文 verbatim 的一段（≤ 80 字，可以省略号截断）
- 一份典型 1000 行原文应抽出 50-150 条观点；少于 30 条说明你漏抽了

【原文（共 {char_count} 字符）】
{content}

请直接输出 JSON 数组。"""


def _extract_atomic_points(text: str, source_info: dict, cfg: dict) -> list[AtomicPoint]:
    """LLM Pass 1：从原文抽 atomic 观点清单。"""
    creds = resolve_llm_credentials(cfg)

    # 长文截断保护：审计 prompt 单次最多吃 60K 字符（≈20K tokens）
    # 超长文档（>60K 字符）按段截断；多数 SKILL.md 都在 30K 以内
    if len(text) > 60000:
        text = text[:60000] + "\n\n[... 原文过长，已截至 60K 字符。审计基于本段。]"

    user_prompt = (
        _POINTS_PROMPT_TEMPLATE
        .replace("{char_count}", str(len(text)))
        .replace("{content}", text)
    )

    response = _call_llm(creds, _AUDIT_SYSTEM_PROMPT, user_prompt, cfg)
    items = _parse_json_array(response)

    points: list[AtomicPoint] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        statement = str(item.get("statement", "")).strip()
        if not statement:
            continue
        kind = str(item.get("kind", "rule")).strip().lower()
        if kind not in ("rule", "enum", "number", "example"):
            kind = "rule"
        points.append(AtomicPoint(
            id=int(item.get("id", i + 1)) or (i + 1),
            statement=statement,
            kind=kind,
            source_quote=str(item.get("source_quote", "")).strip()[:200],
        ))
    return points


# ---------------------------------------------------------------------------
# Pass 2：逐条核对
# ---------------------------------------------------------------------------

_COMPARE_PROMPT_TEMPLATE = """【任务】
对每一条原文观点，判断它在"提取笔记内容"中的命中状态。

【判定规则】
- complete : 观点的核心信息在提取笔记中能找到，无明显信息损失（语义匹配即可，措辞可以不同）
- weak     : 出现了但简化 / 改写损失了精度（如数值丢失、长清单只保留示例、对称项弱势侧被砍）
- missing  : 提取笔记中完全找不到该观点

【输出 JSON 数组】
对每条观点输出一项，按 point_id 顺序：
[
  {"point_id": 1, "status": "complete|weak|missing", "evidence": "提取笔记里命中的片段（≤ 60 字，空字符串表示 missing）", "notes": "若 weak/missing，简述差异（≤ 40 字）"},
  ...
]

【硬性要求】
- 必须为每条观点都输出一项，不要遗漏
- 提取笔记可能是中文翻译的，语义匹配即可，不要因为措辞不同就判 missing
- evidence 必须是提取笔记里 verbatim 的片段

【原文观点清单】
{points_json}

【提取笔记内容（已发布到 vault）】
{extract_text}

请直接输出 JSON 数组。"""


def _compare_with_extracts(
    points: list[AtomicPoint],
    extract_text: str,
    cfg: dict,
) -> list[PointMatch]:
    """LLM Pass 2：逐条核对原文观点是否在提取笔记中保留。"""
    creds = resolve_llm_credentials(cfg)

    points_payload = [
        {"point_id": p.id, "statement": p.statement, "kind": p.kind}
        for p in points
    ]

    # 提取文本同样做截断保护
    if len(extract_text) > 60000:
        extract_text = extract_text[:60000] + "\n\n[... 提取笔记过长，已截至 60K 字符。]"

    user_prompt = (
        _COMPARE_PROMPT_TEMPLATE
        .replace("{points_json}", json.dumps(points_payload, ensure_ascii=False))
        .replace("{extract_text}", extract_text)
    )

    response = _call_llm(creds, _AUDIT_SYSTEM_PROMPT, user_prompt, cfg)
    items = _parse_json_array(response)

    # 按 point_id 建索引；LLM 可能漏判某条 → 默认 missing
    by_id: dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("point_id", -1))
        except (TypeError, ValueError):
            continue
        if pid > 0:
            by_id[pid] = item

    matches: list[PointMatch] = []
    for p in points:
        item = by_id.get(p.id, {})
        status = str(item.get("status", "missing")).strip().lower()
        if status not in ("complete", "weak", "missing"):
            status = "missing"
        matches.append(PointMatch(
            point_id=p.id,
            status=status,
            evidence=str(item.get("evidence", "")).strip()[:120],
            notes=str(item.get("notes", "")).strip()[:120],
        ))
    return matches


# ---------------------------------------------------------------------------
# 本地幻觉检测（数字 + wikilink）
# ---------------------------------------------------------------------------

# 匹配 "16px" / "120 px" / "4:1" / "1-10" / "10/10" / "clamp(...)" 等精确数值表达
_NUMBER_RE = re.compile(
    r'(\d+(?:\.\d+)?(?:px|rem|em|vw|vh|%|s|ms|fps)\b'  # 含单位的数字
    r'|\d+\s*:\s*\d+'                                   # 比例 4:1
    r'|\d+\s*-\s*\d+\s*px'                              # 范围 60-72px
    r'|clamp\([^)]*\d[^)]*\))'                          # clamp(...)
)

_WIKILINK_RE = re.compile(r'\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]')


def _normalize_for_hall_match(s: str) -> str:
    """归一化用于幻觉反查：小写 + 去空白。"""
    return re.sub(r'\s+', '', s.lower())


def _detect_hallucinations(extract_files: list[Path], raw_text: str) -> list[Hallucination]:
    """扫提取笔记里的精确数字 + wikilink，反查原文是否出现。"""
    halls: list[Hallucination] = []
    raw_norm = _normalize_for_hall_match(raw_text)

    for fp in extract_files:
        try:
            note_text = fp.read_text(encoding="utf-8")
        except Exception:
            continue

        # 1. 数字检测
        for m in _NUMBER_RE.finditer(note_text):
            value = m.group(0)
            value_norm = _normalize_for_hall_match(value)
            if value_norm in raw_norm:
                continue
            # 二次检查：数字本身（去单位）是否在原文出现 → 可能是单位差异
            num_only = re.search(r'\d+(?:\.\d+)?', value)
            if num_only and num_only.group(0) in raw_text:
                continue
            # 给出局部上下文
            ctx_start = max(0, m.start() - 30)
            ctx_end = min(len(note_text), m.end() + 30)
            context = note_text[ctx_start:ctx_end].replace("\n", " ").strip()
            halls.append(Hallucination(
                kind="number",
                value=value,
                found_in_extract=f"{fp.name} : {context}",
                note="原文未出现该精确数值",
            ))

        # 2. wikilink 检测
        for m in _WIKILINK_RE.finditer(note_text):
            target = m.group(1).strip()
            if not target:
                continue
            target_norm = _normalize_for_hall_match(target)
            # 跳过路径中带 / 的（明确指向 vault 路径，不算幻觉）
            if "/" in target:
                continue
            # 反查原文（语义匹配 + 字面匹配双重）
            if target_norm in raw_norm:
                continue
            # 跳过常见 vault 通用链接（避免误报）
            common_skips = ("anti-generic", "color discipline", "design system",
                           "key-concepts", "core-rules", "未命名笔记")
            if any(skip in target.lower() for skip in common_skips):
                halls.append(Hallucination(
                    kind="wikilink",
                    value=f"[[{target}]]",
                    found_in_extract=fp.name,
                    note="疑似为美化编造的关联链接（原文无对应概念）",
                ))
            else:
                halls.append(Hallucination(
                    kind="wikilink",
                    value=f"[[{target}]]",
                    found_in_extract=fp.name,
                    note="原文未提及该关联词",
                ))

    # 去重（同一文件同一 value 只报一次）
    seen: set[tuple[str, str, str]] = set()
    deduped: list[Hallucination] = []
    for h in halls:
        key = (h.kind, h.value, h.found_in_extract.split(" : ")[0])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)
    return deduped


# ---------------------------------------------------------------------------
# 渲染报告
# ---------------------------------------------------------------------------

def render_audit_report(report: AuditReport) -> str:
    """把 AuditReport 渲染为 markdown 报告。"""
    lines: list[str] = []
    lines.append(f"# SkillMind 覆盖率审计报告")
    lines.append("")
    lines.append(f"- **审计时间**：{report.audit_time}")
    lines.append(f"- **原文**：{report.source_title or '(无标题)'}")
    if report.source_path:
        lines.append(f"- **路径**：`{report.source_path}`")
    lines.append(f"- **source_hash**：`{report.source_hash[:16]}...`")
    lines.append(f"- **原文长度**：{report.original_chars:,} 字符")
    lines.append(f"- **提取笔记数**：{len(report.extract_files)} 篇")
    for fp in report.extract_files:
        lines.append(f"  - `{Path(fp).name}`")
    lines.append("")

    # 总览
    total = len(report.matches) or 1
    lines.append("## 📊 总览")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|---|---|")
    lines.append(f"| 原文 atomic 观点总数 | **{len(report.points)}** |")
    lines.append(f"| ✅ 完整保留 | {report.complete_count} |")
    lines.append(f"| 🟡 弱化 | {report.weak_count} |")
    lines.append(f"| ❌ 缺失 | {report.missing_count} |")
    lines.append(f"| **覆盖率** | **{report.coverage_rate * 100:.1f}%** |")
    lines.append(f"| 疑似幻觉条目 | {len(report.hallucinations)} |")
    lines.append("")

    grade = _grade_from_coverage(report.coverage_rate, len(report.hallucinations))
    lines.append(f"**保真度评级：{grade}**")
    lines.append("")

    # 按 kind 分类的覆盖率
    by_kind = _coverage_by_kind(report)
    if by_kind:
        lines.append("### 按观点类型分类")
        lines.append("")
        lines.append("| 类型 | 总数 | ✅ | 🟡 | ❌ | 覆盖率 |")
        lines.append("|---|---|---|---|---|---|")
        for kind, stats in by_kind.items():
            t = stats["total"]
            rate = (stats["complete"] + 0.5 * stats["weak"]) / t * 100 if t else 0
            lines.append(
                f"| {kind} | {t} | {stats['complete']} | "
                f"{stats['weak']} | {stats['missing']} | {rate:.1f}% |"
            )
        lines.append("")

    # 缺失观点
    missing = [(p, m) for p, m in zip(report.points, report.matches) if m.status == "missing"]
    if missing:
        lines.append(f"## ❌ 缺失观点（{len(missing)} 条）")
        lines.append("")
        for p, m in missing:
            lines.append(f"- **[{p.kind}]** {p.statement}")
            if p.source_quote:
                lines.append(f"  - 原文：> {p.source_quote}")
            if m.notes:
                lines.append(f"  - 备注：{m.notes}")
        lines.append("")

    # 弱化观点
    weak = [(p, m) for p, m in zip(report.points, report.matches) if m.status == "weak"]
    if weak:
        lines.append(f"## 🟡 弱化观点（{len(weak)} 条）")
        lines.append("")
        for p, m in weak:
            lines.append(f"- **[{p.kind}]** {p.statement}")
            if m.evidence:
                lines.append(f"  - 提取：{m.evidence}")
            if m.notes:
                lines.append(f"  - 差异：{m.notes}")
        lines.append("")

    # 幻觉
    if report.hallucinations:
        lines.append(f"## 🚨 疑似幻觉（{len(report.hallucinations)} 条）")
        lines.append("")
        for h in report.hallucinations:
            lines.append(f"- **[{h.kind}]** `{h.value}` — {h.note}")
            lines.append(f"  - 位置：{h.found_in_extract}")
        lines.append("")

    # 完整观点（仅在数量少时列出，避免报告爆炸）
    complete = [(p, m) for p, m in zip(report.points, report.matches) if m.status == "complete"]
    if complete and len(complete) <= 50:
        lines.append(f"## ✅ 完整保留观点（{len(complete)} 条）")
        lines.append("")
        for p, _ in complete:
            lines.append(f"- [{p.kind}] {p.statement}")
        lines.append("")
    elif complete:
        lines.append(f"## ✅ 完整保留观点（{len(complete)} 条，省略明细）")
        lines.append("")

    return "\n".join(lines)


def _grade_from_coverage(rate: float, hall_count: int) -> str:
    """覆盖率 + 幻觉数 → 评级。"""
    pct = rate * 100
    if pct >= 95 and hall_count == 0:
        return "A（极佳，可作权威复刻源）"
    if pct >= 90 and hall_count <= 1:
        return "A-（优秀）"
    if pct >= 85 and hall_count <= 3:
        return "B+（良好，够用于检索 / 参考）"
    if pct >= 75:
        return "B（合格，关键 enum/number 类有信息损失）"
    if pct >= 60:
        return "C（一般，建议改 prompt 重新提取）"
    return "D（差，必须 prompt 改造后重跑）"


def _coverage_by_kind(report: AuditReport) -> dict[str, dict[str, int]]:
    """按 kind 统计 complete/weak/missing。"""
    by_kind: dict[str, dict[str, int]] = {}
    by_id = {p.id: p for p in report.points}
    for m in report.matches:
        p = by_id.get(m.point_id)
        if not p:
            continue
        bucket = by_kind.setdefault(p.kind, {"total": 0, "complete": 0, "weak": 0, "missing": 0})
        bucket["total"] += 1
        bucket[m.status] += 1
    return by_kind


# ---------------------------------------------------------------------------
# 辅助：source 解析 / 文件 IO / LLM 调用
# ---------------------------------------------------------------------------

def _resolve_source(source_hash_prefix: str) -> tuple[str, dict]:
    """根据 source_hash 前缀找到完整 hash + source_info。"""
    from skillmind.collector import _load_hashes

    hashes = _load_hashes()
    matched = [sha for sha in hashes if sha.startswith(source_hash_prefix)]
    if not matched:
        # 兜底：试着按路径关键词匹配
        matched = [
            sha for sha, info in hashes.items()
            if source_hash_prefix in info.get("source_path", "")
            or source_hash_prefix in info.get("title", "")
        ]
    if not matched:
        raise RuntimeError(f"未找到 source_hash 前缀 / 路径关键词：{source_hash_prefix}")
    if len(matched) > 1:
        sample = "\n".join(f"  {sha[:16]}  {hashes[sha].get('title', '')[:60]}" for sha in matched[:5])
        raise RuntimeError(
            f"匹配到 {len(matched)} 个 source_hash，请用更长的前缀：\n{sample}"
        )
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
        # 兜底：按 hash 拼路径
        sha = source_info.get("source_hash", "")
        if sha:
            candidate = RAW_DIR / sha[:2] / f"{sha}.md"
            if candidate.exists():
                p = candidate
    if not p.exists():
        raise RuntimeError(f"原文不存在：{raw_path}")
    return p.read_text(encoding="utf-8", errors="replace")


def _find_extract_notes(
    source_hash: str,
    cfg: dict,
    *,
    max_n: int,
    vault_skills_override: str | Path | None = None,
) -> list[Path]:
    """在 vault 下扫所有 .md 笔记，匹配 frontmatter 的 source_hash。

    vault_skills_override 用于审计放在非默认 vault 路径下的笔记（如真正的 Obsidian vault）。
    """
    import yaml as _yaml

    if vault_skills_override:
        skills_dir = Path(vault_skills_override)
    else:
        vault_dir = get_vault_dir(cfg)
        skills_dir = vault_dir / "skills"
    if not skills_dir.exists():
        return []

    matched: list[Path] = []
    for fp in skills_dir.rglob("*.md"):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # 提取 frontmatter
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
        if fm_hash and (fm_hash == source_hash or source_hash.startswith(fm_hash) or fm_hash.startswith(source_hash[:7])):
            matched.append(fp)
            if len(matched) >= max_n:
                break
    return matched


def _concat_extracts(files: list[Path]) -> str:
    """拼接所有提取笔记的正文，每篇加分隔符。"""
    parts: list[str] = []
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # 去掉 frontmatter，保留正文（评审关心内容）
        if text.startswith("---"):
            end = text.find("\n---", 4)
            if end >= 0:
                text = text[end + 4:].lstrip()
        parts.append(f"=== 提取笔记：{fp.name} ===\n{text}")
    return "\n\n".join(parts)


def _call_llm(creds: dict, system: str, user: str, cfg: dict) -> str:
    """统一的 LLM 调用入口。失败按 max_retries 重试。"""
    from litellm import completion  # type: ignore

    timeout = int(cfg.get("llm", {}).get("timeout", 180))
    max_retries = int(cfg.get("llm", {}).get("max_retries", 2))

    kwargs: dict[str, Any] = {
        "model": creds["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "timeout": timeout,
        "api_key": creds["api_key"],
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
    raise RuntimeError(f"LLM 调用失败 {max_retries + 1} 次：{last_err}")


_CODEBLOCK_RE = re.compile(r"```(?:json|JSON)?\s*\n([\s\S]{1,200000}?)```")


def _parse_json_array(text: str) -> list[dict]:
    """从 LLM 响应中解析 JSON 数组（兼容代码块包裹 / 末尾解释）。"""
    text = text.strip()
    if len(text) > 200000:
        text = text[:200000]
    # 1. 代码块包裹
    m = _CODEBLOCK_RE.search(text)
    if m:
        text = m.group(1).strip()
    # 2. 截最外层 [...]
    if not text.startswith("["):
        idx = text.find("[")
        if idx >= 0:
            text = text[idx:]
    if not text.startswith("["):
        raise ValueError("LLM 响应不是 JSON 数组")
    last = text.rfind("]")
    if last >= 0:
        text = text[:last + 1]

    # 3. 解析（含截断修复）
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = json.loads(_fix_array_truncation(text))

    if not isinstance(data, list):
        raise ValueError("响应解析后不是数组")
    return [item for item in data if isinstance(item, dict)]


def _fix_array_truncation(text: str) -> str:
    """简化版的 JSON 数组截断修复：找最后一个完整 } 然后补 ]。"""
    last_brace = text.rfind("}")
    if last_brace < 0:
        return text
    truncated = text[:last_brace + 1]
    # 数 [ 和 ] 的不平衡
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
