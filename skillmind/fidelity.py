"""引用归一化、关键项证据关联、极性矛盾。

精确引文出现不能证明动作正确。对不上或跨语言无法核对时返回 UNKNOWN，
不能当成 PASS。代码围栏和行内代码里的 *、<> 保持原样。
"""

from __future__ import annotations

import re


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


_CODE_SPAN = re.compile(r"```[\s\S]*?```|`[^`\n]*`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_US = re.compile(r"__([^_\n]+)__")
# 只去掉包住一个词的斜体。`a * b`、`2*3`、比较运算不动。
_ITALIC = re.compile(r"(?<![\w*])\*([^*\s](?:[^*\n]*?[^*\s])?)\*(?![\w*])")
_ITALIC_US = re.compile(r"(?<![\w_])_([^_\s](?:[^_\n]*?[^_\s])?)_(?![\w_])")
_LIST_MARK = re.compile(r"(?m)^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
_OBLIGATION = re.compile(
    r"must\s+not|must\s+never|what must not|不得改变|不得变更|不得|禁止|绝不",
    re.IGNORECASE,
)
_PLACEHOLDER_LINE = re.compile(
    r"^\s*(?:\[[^\]]+\]|[^:\n]{0,40}:\s*\[\s*(?:\.{3}|…|[^\]]{0,12})\s*\])\s*$"
)
_NEG = re.compile(
    r"\b(never|must not|do not|don't|reject|deny|forbid|refuse)\b|禁止|不得|绝不|拒绝",
    re.IGNORECASE,
)
_POS = re.compile(
    r"\b(always allow|allow|approve|permit)\b|允许|批准|blanket-approve|全面批准",
    re.IGNORECASE,
)
_STOP = {
    "the", "a", "an", "to", "of", "and", "or", "is", "be", "for", "on", "in",
    "with", "that", "this",
}

# 通用风险类，不是某篇文档的词表。satisfy 必须带禁止或义务关系，单词出现不算覆盖。
CONCEPTS: tuple[dict, ...] = (
    {
        "id": "authz_per_request",
        "detect": re.compile(r"authoriz|\bIDOR\b|鉴权|每(?:个|一)?请求.{0,24}授权", re.IGNORECASE),
        "satisfy": re.compile(
            r"authoriz|\bIDOR\b|鉴权|每个请求.{0,20}授权|授权.{0,12}每个请求|"
            r"permitted on the specific resource|须拥有或获准",
            re.IGNORECASE,
        ),
    },
    {
        "id": "shape_not_authz",
        "detect": re.compile(
            r"shape check|well-formedness, not authorization|形状检查.{0,16}授权|"
            r"形状.{0,8}不证|形状.{0,8}不等于授权",
            re.IGNORECASE,
        ),
        "satisfy": re.compile(
            r"shape check|not authorization|well-formedness, not authorization|"
            r"形状检查|不证授权|不等于授权|不证明授权|只证格式",
            re.IGNORECASE,
        ),
    },
    {
        "id": "label_forbidden",
        "detect": re.compile(r"never a label|不得作为标签|禁止.{0,6}标签", re.IGNORECASE),
        "satisfy": re.compile(
            r"never a label|user_id|不得作为标签|禁止.{0,8}标签|无界",
            re.IGNORECASE,
        ),
    },
    {
        "id": "not_applicable_small",
        "detect": re.compile(r"when not to use|single-line fix|typo correction", re.IGNORECASE),
        "satisfy": re.compile(
            r"when not to use|single-line|typo|单行|拼写|不适用",
            re.IGNORECASE,
        ),
    },
    {
        "id": "skip_single_capability",
        "detect": re.compile(r"skip this phase", re.IGNORECASE),
        "satisfy": re.compile(r"skip this phase|跳过\s*Phase\s*0|单能力", re.IGNORECASE),
    },
    {
        "id": "stop_turn",
        "detect": re.compile(r"stop your turn|do not start phase 2|write code in this turn", re.IGNORECASE),
        "satisfy": re.compile(r"stop your turn|停止本轮|不得.{0,8}写代码|立即停止", re.IGNORECASE),
    },
    {
        "id": "tracing_before_app",
        "detect": re.compile(r"imported before anything else|must be imported before", re.IGNORECASE),
        "satisfy": re.compile(
            r"imported before|最先导入|其他任何.{0,6}之前|初始化顺序",
            re.IGNORECASE,
        ),
    },
    {
        "id": "blocks_merge",
        "detect": re.compile(r"blocks merge|阻断合并", re.IGNORECASE),
        "satisfy": re.compile(r"blocks merge|阻断合并|阻止合并", re.IGNORECASE),
    },
)


def _segments(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    pos = 0
    for match in _CODE_SPAN.finditer(text or ""):
        if match.start() > pos:
            parts.append(("prose", text[pos:match.start()]))
        parts.append(("code", match.group(0)))
        pos = match.end()
    if pos < len(text or ""):
        parts.append(("prose", (text or "")[pos:]))
    return parts or [("prose", text or "")]


def _soften_prose(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _BOLD.sub(r"\1", text)
        text = _BOLD_US.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _ITALIC_US.sub(r"\1", text)
    text = _LIST_MARK.sub("", text)
    text = text.replace("|", " ")
    text = text.replace('"', " ").replace("“", " ").replace("”", " ").replace("'", " ")
    return text


def _unwrap_code(segment: str) -> str:
    """去掉围栏和行内反引号，内部的 *、<> 原样保留。"""
    if segment.startswith("```"):
        inner = re.sub(r"^```[^\n]*\n?", "", segment)
        inner = re.sub(r"\n?```$", "", inner)
        return inner
    if len(segment) >= 2 and segment.startswith("`") and segment.endswith("`"):
        return segment[1:-1]
    return segment


def normalize_for_support(text: str) -> str:
    """比较引文用。代码内容保持原样，正文只去掉强调、列表前缀和表格竖线。"""
    chunks = []
    for kind, segment in _segments(text or ""):
        chunks.append(_unwrap_code(segment) if kind == "code" else _soften_prose(segment))
    return norm_ws("".join(chunks))


def quote_in_source(quote: str, source_text: str) -> bool:
    needle = norm_ws(quote)
    if len(needle) < 8 or not source_text:
        return False
    if needle in norm_ws(source_text):
        return True
    softened = normalize_for_support(quote)
    if len(softened) < 8:
        return False
    return softened in normalize_for_support(source_text)


def obligation_anchor(line: str) -> str | None:
    """占位符整行不是关键约束。行内若有必须/禁止，只保留该义务短语。"""
    text = (line or "").strip()
    if not text:
        return None
    found = _OBLIGATION.search(text)
    if found:
        start = max(0, found.start() - 48)
        end = min(len(text), found.end() + 48)
        return text[start:end].strip("[] ")
    if _PLACEHOLDER_LINE.match(text):
        return None
    if re.search(r":\s*\[\s*(?:\.{3}|…)\s*\]\s*$", text):
        return None
    return text


def is_placeholder_only(text: str) -> bool:
    return obligation_anchor(text) is None and bool((text or "").strip())


def _paragraphs(source: str) -> list[str]:
    return [part for part in re.split(r"\n\s*\n", source or "") if part.strip()]


_RISK_MARKERS = (
    "blanket", "idor", "authorization", "kebab-case", "no cycles",
    "skip this phase", "when not to use", "must not", "blocks merge",
    "never a label", "imported before", "three outcomes", "different body",
    "longest retry", "shape check", "not authorization",
    "provider module", "one change per delta", "untrusted",
)


def _quote_carries_anchor(anchor: str, quote: str) -> bool:
    """引文要带上锚点里的风险词。同段里另一句只有在锚点是短引导句时才算。"""
    anchor_norm = normalize_for_support(anchor)
    quote_norm = normalize_for_support(quote)
    if len(quote_norm) < 12:
        return False
    folded_anchor = anchor_norm.casefold()
    folded_quote = quote_norm.casefold()
    markers = [marker for marker in _RISK_MARKERS if marker in folded_anchor]
    if markers:
        return any(marker in folded_quote for marker in markers)
    if anchor_norm in quote_norm or quote_norm in anchor_norm:
        return len(quote_norm) >= 40 or len(quote_norm) >= 0.6 * max(len(anchor_norm), 1)
    return False


def sibling_quote_covers(anchor: str, source: str, quotes: list[str]) -> bool:
    """短引导句可以用同一段里的后一句证明已保留。带否定的锚点不能被无关半句带过。"""
    anchor_norm = normalize_for_support(anchor)
    if len(anchor_norm) < 8 or len(anchor_norm) > 90 or not source:
        return False
    if _NEG.search(anchor) and not any(_NEG.search(quote) for quote in quotes):
        return False
    for paragraph in _paragraphs(source):
        paragraph_norm = normalize_for_support(paragraph)
        if anchor_norm not in paragraph_norm:
            continue
        for quote in quotes:
            quote_norm = normalize_for_support(quote)
            if len(quote_norm) >= 40 and quote_norm in paragraph_norm and quote_norm != anchor_norm:
                return True
    return False


def concept_ids(text: str) -> list[str]:
    found = []
    for concept in CONCEPTS:
        if concept["detect"].search(text or ""):
            found.append(concept["id"])
    return found


def concept_satisfied(source_line: str, extract_text: str) -> bool:
    for concept in CONCEPTS:
        if concept["detect"].search(source_line or "") and concept["satisfy"].search(extract_text or ""):
            return True
    return False


def anchor_retained(
    anchor: str,
    *,
    source_text: str,
    extract_text: str,
    quotes: list[str],
) -> bool:
    if not anchor:
        return False
    if quote_in_source(anchor, extract_text or ""):
        return True
    if concept_satisfied(anchor, extract_text or ""):
        return True
    retained = [quote for quote in quotes if quote_in_source(quote, extract_text or "")]
    if any(_quote_carries_anchor(anchor, quote) for quote in retained):
        return True
    return sibling_quote_covers(anchor, source_text or "", retained)


def _content_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", (text or "").lower())
    return [token for token in tokens if token not in _STOP]


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _has_latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text or ""))


def _polarity_text(text: str) -> str:
    """允许列表不是“允许这个动作”。否定词后面的批准仍是否定。"""
    masked = re.sub(r"allowlists?|allowlisted|允许列表", " ", text or "", flags=re.IGNORECASE)
    masked = re.sub(r"(?:不要|别|勿|不).{0,16}(?:blanket[-\s]?)?(?:批准|允许)", " 禁止 ", masked, flags=re.IGNORECASE)
    return masked


def assess_faithfulness(action: str, quote: str) -> str:
    """PASS / FAIL / UNKNOWN。引文在原文里出现，不能单独证明动作方向正确。"""
    if not str(action or "").strip() or not str(quote or "").strip():
        return "UNKNOWN"
    if norm_ws(action).casefold() in {"unknown", "未知"} or norm_ws(quote).casefold() in {"unknown", "未知"}:
        return "UNKNOWN"
    action_view = _polarity_text(action)
    quote_view = _polarity_text(quote)
    quote_neg = bool(_NEG.search(quote_view))
    quote_pos = bool(_POS.search(quote_view))
    action_neg = bool(_NEG.search(action_view))
    action_pos = bool(_POS.search(action_view))
    if quote_neg and not quote_pos and action_pos and not action_neg:
        return "FAIL"
    if quote_pos and not quote_neg and action_neg and not action_pos:
        return "FAIL"
    action_tokens = _content_tokens(action)
    quote_tokens = set(_content_tokens(quote))
    if not action_tokens:
        return "UNKNOWN"
    overlap = sum(1 for token in action_tokens if token in quote_tokens) / len(action_tokens)
    cross_script = _has_cjk(action) != _has_cjk(quote) and (_has_latin(action) or _has_latin(quote))
    if cross_script and overlap < 0.5:
        return "UNKNOWN"
    if overlap >= 0.6 and not (quote_neg and action_pos and not action_neg):
        return "PASS"
    return "UNKNOWN"


def collect_quotes(units: list[dict]) -> list[str]:
    quotes: list[str] = []
    for unit in units or []:
        if not isinstance(unit, dict):
            continue
        for bucket in ("constraints", "rules"):
            for item in unit.get(bucket) or []:
                if isinstance(item, dict):
                    quote = str(item.get("source_quote") or "").strip()
                    if quote and quote.lower() != "unknown":
                        quotes.append(quote)
    return quotes


def extract_text_of(units: list[dict]) -> str:
    import json
    return json.dumps(units or [], ensure_ascii=False)


def gaps_for_lines(source_text: str, units: list[dict], lines: list[dict]) -> list[dict]:
    extract_text = extract_text_of(units)
    quotes = collect_quotes(units)
    gaps = []
    for line in lines:
        anchor = (line.get("anchors") or [""])[0]
        if obligation_anchor(anchor) is None:
            continue
        if anchor_retained(anchor, source_text=source_text, extract_text=extract_text, quotes=quotes):
            continue
        gaps.append(line)
    return gaps
