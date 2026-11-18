"""完整性与可执行性双门禁，以及可冻结的审计清单。

两道门都只有在真正跑完时才可能是 PASS。没有执行的门是 UNKNOWN，
不能当成通过。覆盖率数字单独不构成放行条件。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


# 下游（含 se-skill-distill）应继续从 skillmind.auditor 导入此常量。
# auditor 再从这里导出，避免两套阈值。
PASS_COVERAGE_THRESHOLD = 90.0

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
GATE_UNKNOWN = "UNKNOWN"

# 关键保留集。普通 concept / enum 不在其中，仍只参与 90% 覆盖率。
CRITICAL_KINDS = {
    "constraint",
    "threshold",
    "exception",
    "safety_limit",
    "safety",
    "abort",
    "halt",
    "limit",
}

_UNKNOWN_WORDS = {"", "unknown", "未知", "未说明", "未给出", "n/a", "na", "none", "null"}

_SLOGAN_ACTION = re.compile(
    r"^(be careful|follow best practices|do the right thing|ensure quality|"
    r"stay safe|注意|务必小心|保证质量|高质量|小心处理)$",
    re.IGNORECASE,
)

_CRITICAL_LINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("safety_limit", re.compile(r"blanket[-\s]?approv", re.IGNORECASE)),
    ("threshold", re.compile(
        r"(review|re-?review|复审|延期).{0,48}(\d{4}-\d{2}-\d{2}|date|日期)"
        r"|(\d{4}-\d{2}-\d{2}).{0,48}(review|复审|延期)",
        re.IGNORECASE,
    )),
    ("exception", re.compile(
        r"\b(regional policy|region|regional|eu-central|eu-west|us-east|us-west)\b|区域策略|区域",
        re.IGNORECASE,
    )),
    ("safety_limit", re.compile(
        r"\b(token|rate|recursion)\b.{0,32}(limit|cap|max|上限)"
        r"|(令牌|速率|递归).{0,16}(上限|限制|不超过)",
        re.IGNORECASE,
    )),
    ("abort", re.compile(
        r"\b(must not|must never|never|do not|don't|abort|halt)\b|禁止|不得|中止",
        re.IGNORECASE,
    )),
    ("threshold", re.compile(
        r"\b(at most|no more than|maximum|max)\b.{0,24}\d|不超过.{0,12}\d|上限.{0,12}\d",
        re.IGNORECASE,
    )),
)


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_unknown(value: object) -> bool:
    return norm_ws(str(value or "")).casefold() in _UNKNOWN_WORDS


def is_concrete(value: object) -> bool:
    return bool(norm_ws(str(value or ""))) and not is_unknown(value)


def is_critical_kind(kind: str) -> bool:
    return norm_ws(kind).casefold() in CRITICAL_KINDS


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------

@dataclass
class ChecklistItem:
    id: str
    section: str
    kind: str
    statement: str
    critical: bool = False
    anchors: list[str] = field(default_factory=list)

    @property
    def is_critical(self) -> bool:
        return bool(self.critical) or is_critical_kind(self.kind)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "section": self.section,
            "kind": self.kind,
            "statement": self.statement,
            "critical": self.is_critical,
            "anchors": list(self.anchors),
        }


@dataclass
class AuditChecklist:
    """冻结后的检查清单。再次加载不得重排、重编号或增删条目。"""

    checklist_id: str
    source_hash: str
    prompt_version: str
    schema_version: str
    audit_prompt_version: str
    items: list[ChecklistItem]
    created_at: str = ""

    @property
    def total(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict:
        return {
            "checklist_id": self.checklist_id,
            "source_hash": self.source_hash,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "audit_prompt_version": self.audit_prompt_version,
            "created_at": self.created_at,
            "total": self.total,
            "items": [item.to_dict() for item in self.items],
        }


def _item_from_raw(raw: dict, fallback_id: str) -> ChecklistItem:
    anchors = raw.get("anchors") or []
    if isinstance(anchors, str):
        anchors = [anchors]
    kind = str(raw.get("kind") or "concept")
    critical_flag = bool(raw.get("critical")) or is_critical_kind(kind)
    return ChecklistItem(
        id=str(raw.get("id") or fallback_id),
        section=str(raw.get("section") or ""),
        kind=kind,
        statement=str(raw.get("statement") or ""),
        critical=critical_flag,
        anchors=[str(a) for a in anchors if norm_ws(str(a))],
    )


def save_checklist(path: str | Path, checklist: AuditChecklist) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = checklist.to_dict()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dest)


def load_checklist(path: str | Path) -> AuditChecklist:
    """原样恢复 id、total 与身份字段，不重新抽条。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError(f"清单不是可复用的 JSON 对象: {path}")
    items = []
    for index, raw in enumerate(data["items"], start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"清单第 {index} 条不是对象")
        if not str(raw.get("id") or "").strip():
            raise ValueError(f"清单第 {index} 条缺少 id，拒绝重新编号")
        items.append(_item_from_raw(raw, ""))
    stored_total = data.get("total")
    if stored_total is not None and int(stored_total) != len(items):
        raise ValueError(
            f"清单 total={stored_total} 与条目数 {len(items)} 不一致，拒绝改写后继续"
        )
    checklist_id = str(data.get("checklist_id") or "").strip()
    if not checklist_id:
        raise ValueError("清单缺少 checklist_id")
    return AuditChecklist(
        checklist_id=checklist_id,
        source_hash=str(data.get("source_hash") or ""),
        prompt_version=str(data.get("prompt_version") or ""),
        schema_version=str(data.get("schema_version") or ""),
        audit_prompt_version=str(data.get("audit_prompt_version") or ""),
        items=items,
        created_at=str(data.get("created_at") or ""),
    )


def require_same_source(checklist: AuditChecklist, source_hash: str) -> None:
    if checklist.source_hash != source_hash:
        raise ValueError(
            "冻结清单的 source_hash 与当前原文不一致，不会换一份清单重抽。"
            f" checklist={checklist.source_hash} source={source_hash}"
        )


def _next_item_id(raw_items: list[dict]) -> str:
    nums = []
    for raw in raw_items:
        match = re.search(r"(\d+)$", str(raw.get("id") or ""))
        if match:
            nums.append(int(match.group(1)))
    return f"i-{max(nums, default=len(raw_items)) + 1:04d}"


def _kind_for_line(text: str) -> str:
    for kind, pattern in _CRITICAL_LINE_RULES:
        if pattern.search(text):
            return kind
    return ""


def scan_critical_anchors(source_text: str) -> list[dict]:
    """从原文确定性抽出关键约束行，供清单冻结前补进保留集。

    只在创建清单时调用。复审加载冻结清单时不再扫描，因此 id 与 total 不变。
    """
    found: list[dict] = []
    seen: set[str] = set()
    heading = "source"
    for line in source_text.splitlines():
        stripped = line.strip()
        heading_match = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading_match:
            heading = heading_match.group(1).strip()
            continue
        text = re.sub(r"^([-*+]|\d+[.)])\s+", "", stripped).strip()
        if len(text) < 12 or len(text) > 300:
            continue
        kind = _kind_for_line(text)
        if not kind:
            continue
        key = norm_ws(text).casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "section": heading,
            "kind": kind,
            "statement": text[:160],
            "critical": True,
            "anchors": [text[:240]],
        })
    return found


def merge_critical_items(raw_items: list[dict], source_text: str) -> list[dict]:
    """把扫描到、且清单里还没有的关键行追加到末尾。已有 id 保持不变。"""
    items = [dict(raw) for raw in raw_items if isinstance(raw, dict)]
    covered: list[str] = []
    for raw in items:
        if not raw.get("id"):
            raw["id"] = _next_item_id(items)
        anchors = raw.get("anchors") or []
        if isinstance(anchors, str):
            anchors = [anchors]
        for blob in (raw.get("statement"), *anchors):
            normalized = norm_ws(str(blob or "")).casefold()
            if len(normalized) >= 8:
                covered.append(normalized)
    for extra in scan_critical_anchors(source_text):
        anchor = norm_ws(extra["anchors"][0]).casefold()
        if any(anchor in have or have in anchor for have in covered):
            continue
        extra = dict(extra)
        extra["id"] = _next_item_id(items)
        items.append(extra)
        covered.append(anchor)
    return items


def checklist_from_items(
    raw_items: list[dict],
    *,
    source_hash: str,
    prompt_version: str,
    schema_version: str,
    audit_prompt_version: str,
    checklist_id: str = "",
    created_at: str = "",
) -> AuditChecklist:
    items = [
        _item_from_raw(raw, f"i-{index:04d}")
        for index, raw in enumerate(raw_items, start=1)
        if isinstance(raw, dict) and norm_ws(str(raw.get("statement") or ""))
    ]
    if not checklist_id:
        identity = "|".join(
            [source_hash, prompt_version, schema_version]
            + [f"{item.id}:{item.kind}:{item.statement}" for item in items]
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        checklist_id = f"chk-{digest}"
    return AuditChecklist(
        checklist_id=checklist_id,
        source_hash=source_hash,
        prompt_version=prompt_version,
        schema_version=schema_version,
        audit_prompt_version=audit_prompt_version,
        items=items,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# 完整性
# ---------------------------------------------------------------------------

def _anchor_status(item: ChecklistItem, extract_text: str | None) -> str | None:
    if not item.anchors or extract_text is None:
        return None
    haystack = norm_ws(extract_text).casefold()
    for anchor in item.anchors:
        needle = norm_ws(anchor).casefold()
        if len(needle) >= 8 and needle in haystack:
            return "complete"
    return "missing"


def resolve_item_status(
    item: ChecklistItem,
    reported: str | None,
    extract_text: str | None,
) -> str:
    """complete | weak | missing | unverified。

    关键条目如果带了原文锚点，而提取文里找不到锚点，不能被语义 complete 放行。
    """
    local = _anchor_status(item, extract_text)
    if item.is_critical and local == "missing":
        return "missing"
    if reported in ("complete", "weak", "missing"):
        return reported
    if local:
        return local
    return "unverified"


def evaluate_completeness(
    items: list[ChecklistItem],
    verify_by_id: dict,
    *,
    executed: bool,
    extract_text: str | None = None,
    threshold: float = PASS_COVERAGE_THRESHOLD,
    source: str = "not_run",
) -> dict:
    total = len(items)
    if not executed:
        return {
            "status": GATE_UNKNOWN,
            "coverage": None,
            "threshold": threshold,
            "critical_missing": [],
            "complete": 0,
            "weak": 0,
            "missing": 0,
            "unverified": total,
            "total": total,
            "source": source or "not_run",
            "per_item": [],
        }

    counts = {"complete": 0, "weak": 0, "missing": 0, "unverified": 0}
    per_item: list[dict] = []
    critical_missing: list[dict] = []
    for item in items:
        row = verify_by_id.get(item.id) or {}
        reported = str(row.get("status") or "").lower() or None
        status = resolve_item_status(item, reported, extract_text)
        counts[status] = counts.get(status, 0) + 1
        record = {
            "id": item.id,
            "section": item.section,
            "kind": item.kind,
            "statement": item.statement,
            "critical": item.is_critical,
            "status": status,
            "reason": str(row.get("notes") or ""),
        }
        per_item.append(record)
        if item.is_critical and status == "missing":
            critical_missing.append({
                "id": item.id,
                "section": item.section,
                "kind": item.kind,
                "statement": item.statement,
            })

    verified = counts["complete"] + counts["weak"] + counts["missing"]
    coverage = None
    if verified and counts["unverified"] == 0:
        coverage = (counts["complete"] + 0.5 * counts["weak"]) / verified * 100

    if critical_missing:
        status = GATE_FAIL
        if coverage is None and verified:
            coverage = (counts["complete"] + 0.5 * counts["weak"]) / verified * 100
    elif counts["unverified"]:
        status = GATE_UNKNOWN
        coverage = None
    elif coverage is None:
        status = GATE_PASS if total == 0 else GATE_UNKNOWN
    elif coverage < threshold:
        status = GATE_FAIL
    else:
        status = GATE_PASS

    return {
        "status": status,
        "coverage": None if coverage is None else round(coverage, 1),
        "threshold": threshold,
        "critical_missing": critical_missing,
        "complete": counts["complete"],
        "weak": counts["weak"],
        "missing": counts["missing"],
        "unverified": counts["unverified"],
        "total": total,
        "source": source,
        "per_item": per_item,
    }


# ---------------------------------------------------------------------------
# 可执行性、来源支持、重复
# ---------------------------------------------------------------------------

def quote_in_source(quote: str, source_text: str) -> bool:
    needle = norm_ws(quote)
    if len(needle) < 8 or not source_text:
        return False
    return needle in norm_ws(source_text)


def _rule_slots(rule: dict) -> dict[str, str]:
    return {
        name: str(rule.get(name) or "")
        for name in ("condition", "judgment", "action", "verification", "boundary")
    }


def rule_is_structured(rule: dict) -> bool:
    slots = _rule_slots(rule)
    if not is_concrete(slots["condition"]) or not is_concrete(slots["action"]):
        return False
    if _SLOGAN_ACTION.match(norm_ws(slots["action"])):
        return False
    for value in slots.values():
        if not is_concrete(value) and not is_unknown(value):
            return False
    return True


def _asserts_concrete(record: dict, fields: tuple[str, ...]) -> bool:
    return any(is_concrete(record.get(name)) for name in fields)


def _source_backed(record: dict, source_text: str) -> bool:
    quote = str(record.get("source_quote") or "")
    support = norm_ws(str(record.get("support") or "")).casefold()
    if support == "unknown" or is_unknown(quote):
        return False
    return quote_in_source(quote, source_text)


def _rule_signature(rule: dict) -> str:
    slots = _rule_slots(rule)
    return norm_ws(f"{slots['condition']} {slots['action']}").casefold()


def evaluate_executability(
    units: list[dict],
    source_text: str,
    *,
    executed: bool,
) -> dict:
    """独立于覆盖率。译文、口号、无来源规则不能通过。"""
    if not executed:
        return {
            "status": GATE_UNKNOWN,
            "structure": GATE_UNKNOWN,
            "source_support": GATE_UNKNOWN,
            "duplication": GATE_UNKNOWN,
            "reasons": ["executability gate was not run"],
        }

    rules: list[dict] = []
    constraints: list[dict] = []
    explanations: list[str] = []
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        for rule in unit.get("rules") or []:
            if isinstance(rule, dict):
                rules.append(rule)
        for constraint in unit.get("constraints") or []:
            if isinstance(constraint, dict):
                constraints.append(constraint)
            elif is_concrete(constraint):
                constraints.append({"statement": str(constraint), "source_quote": "unknown"})
        for concept in unit.get("key_concepts") or []:
            if isinstance(concept, dict) and concept.get("explanation"):
                explanations.append(str(concept["explanation"]))

    reasons: list[str] = []
    structured = [rule for rule in rules if rule_is_structured(rule)]
    sourced_rules = [rule for rule in structured if _source_backed(rule, source_text)]
    explanation_chars = sum(len(text) for text in explanations)

    if not source_text.strip():
        structure = GATE_UNKNOWN
        source_support = GATE_UNKNOWN
        reasons.append("source text missing; source support was not checked")
    elif explanation_chars >= 200 and not sourced_rules:
        structure = GATE_FAIL
        reasons.append("extract restates chapters and has no source-backed executable rule")
    elif not structured:
        structure = GATE_FAIL
        reasons.append("no executable rule with condition, judgment, action, verification, and boundary")
    else:
        structure = GATE_PASS

    unsupported = 0
    if source_text.strip():
        for rule in rules:
            if _asserts_concrete(rule, ("condition", "judgment", "action", "verification", "boundary")):
                if not _source_backed(rule, source_text):
                    unsupported += 1
        for constraint in constraints:
            if _asserts_concrete(constraint, ("statement", "value")):
                if not _source_backed(constraint, source_text):
                    unsupported += 1
        if unsupported:
            source_support = GATE_FAIL
            reasons.append(f"{unsupported} asserted rule(s) or constraint(s) have no source quote")
        elif not sourced_rules:
            source_support = GATE_FAIL
            reasons.append("no source-backed rule")
        else:
            source_support = GATE_PASS
    else:
        source_support = GATE_UNKNOWN

    seen: set[str] = set()
    duplicate = False
    for rule in rules:
        signature = _rule_signature(rule)
        if not signature or is_unknown(signature):
            continue
        if signature in seen:
            duplicate = True
            break
        seen.add(signature)
    duplication = GATE_FAIL if duplicate else GATE_PASS
    if duplicate:
        reasons.append("duplicate rules")

    parts = (structure, source_support, duplication)
    if GATE_FAIL in parts:
        status = GATE_FAIL
    elif GATE_UNKNOWN in parts:
        status = GATE_UNKNOWN
    else:
        status = GATE_PASS
    return {
        "status": status,
        "structure": structure,
        "source_support": source_support,
        "duplication": duplication,
        "reasons": reasons,
    }


def combine_gate_status(completeness: str, executability: str) -> str:
    """任一 FAIL 则整体 FAIL。没有 FAIL 时，任一 UNKNOWN 都不是 PASS。"""
    if completeness == GATE_FAIL or executability == GATE_FAIL:
        return GATE_FAIL
    if completeness == GATE_UNKNOWN or executability == GATE_UNKNOWN:
        return GATE_UNKNOWN
    if completeness == GATE_PASS and executability == GATE_PASS:
        return GATE_PASS
    return GATE_UNKNOWN


def build_audit_summary(
    *,
    source_hash: str,
    source_title: str,
    completeness: dict,
    executability: dict,
    checklist: AuditChecklist | None,
    checklist_source: str,
    prompt_version: str,
    schema_version: str,
    quality_current: bool,
) -> dict:
    """对外 JSON。保留 coverage / verdict / missing / needs_human_review。"""
    verdict = combine_gate_status(completeness.get("status", GATE_UNKNOWN), executability.get("status", GATE_UNKNOWN))
    quality_block = ""
    if verdict == GATE_PASS and not quality_current:
        verdict = GATE_FAIL
        quality_block = (
            "extract prompt/schema is not the current quality identity; "
            "refusing to mark PASS"
        )
    missing = []
    for row in completeness.get("per_item") or []:
        if row.get("status") != "missing":
            continue
        missing.append({
            "section": row.get("section", ""),
            "statement": row.get("statement", ""),
            "reason": row.get("reason") or "extract 未保留该条目",
        })
    checklist_block = {
        "id": checklist.checklist_id if checklist else "",
        "total": checklist.total if checklist else int(completeness.get("total") or 0),
        "source_hash": checklist.source_hash if checklist else source_hash,
        "prompt_version": checklist.prompt_version if checklist else prompt_version,
        "schema_version": checklist.schema_version if checklist else schema_version,
        "audit_prompt_version": checklist.audit_prompt_version if checklist else "",
    }
    counts = {
        "complete": int(completeness.get("complete") or 0),
        "weak": int(completeness.get("weak") or 0),
        "missing": int(completeness.get("missing") or 0),
        "total": int(completeness.get("total") or 0),
    }
    summary = {
        "source_hash": source_hash,
        "source_title": source_title,
        "coverage": completeness.get("coverage"),
        "verdict": verdict,
        "needs_human_review": verdict != GATE_PASS,
        "missing": missing,
        "gates": {
            "completeness": completeness.get("status", GATE_UNKNOWN),
            "executability": executability.get("status", GATE_UNKNOWN),
        },
        "completeness": {
            "status": completeness.get("status", GATE_UNKNOWN),
            "coverage": completeness.get("coverage"),
            "threshold": completeness.get("threshold", PASS_COVERAGE_THRESHOLD),
            "critical_missing": completeness.get("critical_missing") or [],
            "complete": counts["complete"],
            "weak": counts["weak"],
            "missing": counts["missing"],
            "total": counts["total"],
            "source": completeness.get("source") or checklist_source,
        },
        "executability": {
            "status": executability.get("status", GATE_UNKNOWN),
            "structure": executability.get("structure", GATE_UNKNOWN),
            "source_support": executability.get("source_support", GATE_UNKNOWN),
            "duplication": executability.get("duplication", GATE_UNKNOWN),
            "reasons": list(executability.get("reasons") or []),
        },
        "counts": counts,
        "checklist": checklist_block,
        "checklist_source": checklist_source,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "quality_current": bool(quality_current),
    }
    if quality_block:
        summary["quality_block"] = quality_block
    return summary


# ---------------------------------------------------------------------------
# 从已发布笔记回读结构，供可执行性门使用
# ---------------------------------------------------------------------------

def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            return text[end + 4:]
    return text


def read_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        import yaml
        data = yaml.safe_load(text[3:end])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _section_blocks(body: str) -> list[str]:
    parts = re.split(r"\n(?=## )", body)
    return [part.strip() for part in parts if part.strip()]


def _parse_constraint_block(block: str) -> list[dict]:
    items: list[dict] = []
    current: dict | None = None
    for line in block.splitlines()[1:]:
        match = re.match(r"^-\s+\*\*(.+?)\*\*\s+(.*)$", line.strip())
        if match:
            current = {
                "kind": match.group(1).strip(),
                "statement": match.group(2).strip(),
                "source_quote": "",
                "support": "",
            }
            items.append(current)
            continue
        plain = re.match(r"^-\s+(?!条件：)(.+)$", line.strip())
        if plain and current is None:
            current = {"kind": "constraint", "statement": plain.group(1).strip(), "source_quote": "", "support": ""}
            items.append(current)
            continue
        if current is None:
            continue
        quote = re.search(r"原文：`([^`]*)`", line)
        if quote:
            current["source_quote"] = quote.group(1)
        support = re.search(r"来源支持：(\S+)", line.strip())
        if support:
            current["support"] = support.group(1)
        value = re.search(r"取值：(.+)$", line.strip())
        if value:
            current["value"] = value.group(1).strip()
    return items


def _parse_rule_block(block: str) -> list[dict]:
    rules: list[dict] = []
    current: dict | None = None
    for line in block.splitlines()[1:]:
        start = re.match(r"^-\s+条件：(.+)$", line.strip())
        if start:
            current = {
                "condition": start.group(1).strip(),
                "judgment": "unknown",
                "action": "unknown",
                "verification": "unknown",
                "boundary": "unknown",
                "source_quote": "unknown",
                "support": "",
            }
            rules.append(current)
            continue
        if current is None:
            continue
        for label, key in (
            ("判定", "judgment"),
            ("动作", "action"),
            ("验证", "verification"),
            ("边界", "boundary"),
        ):
            match = re.search(rf"{label}：(.+)$", line.strip())
            if match:
                current[key] = match.group(1).strip()
        quote = re.search(r"原文：`([^`]*)`", line)
        if quote:
            current["source_quote"] = quote.group(1)
        support = re.search(r"来源支持：(\S+)", line.strip())
        if support:
            current["support"] = support.group(1)
    return rules


def _parse_concept_block(block: str) -> list[dict]:
    concepts: list[dict] = []
    title = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal title, buffer
        if title or buffer:
            concepts.append({"title": title, "explanation": "\n".join(buffer).strip()})
        title = ""
        buffer = []

    for line in block.splitlines()[1:]:
        if line.startswith("### "):
            flush()
            title = line[4:].strip()
            continue
        buffer.append(line)
    flush()
    return [item for item in concepts if item.get("explanation") or item.get("title")]


def units_from_extract_markdown(text: str) -> list[dict]:
    body = _strip_frontmatter(text)
    constraints: list[dict] = []
    rules: list[dict] = []
    concepts: list[dict] = []
    for block in _section_blocks(body):
        heading = block.splitlines()[0]
        if "关键约束" in heading or heading.startswith("## Constraints"):
            constraints.extend(_parse_constraint_block(block))
        elif "决策规则" in heading or heading.startswith("## Rules"):
            rules.extend(_parse_rule_block(block))
        elif "核心概念" in heading or heading.startswith("## Concepts"):
            concepts.extend(_parse_concept_block(block))
    frontmatter = read_frontmatter(text)
    return [{
        "constraints": constraints,
        "rules": rules,
        "key_concepts": concepts,
        "schema_version": str(frontmatter.get("schema_version") or ""),
        "prompt_version": str(frontmatter.get("prompt_version") or ""),
    }]


def notes_are_current(frontmatters: list[dict], *, schema_version: str, prompt_version: str) -> bool:
    if not frontmatters:
        return False
    prefix = prompt_version
    for fm in frontmatters:
        if str(fm.get("schema_version") or "") != schema_version:
            return False
        if not str(fm.get("prompt_version") or "").startswith(prefix):
            return False
        if fm.get("extract_complete") is not True:
            return False
    return True
