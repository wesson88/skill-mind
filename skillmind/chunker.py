"""文档分块（Chunker）v0.1

将原始 Markdown / 纯文本按语义边界切分，保护代码块完整性。

主要用途：
- extractor 把长文交给 LLM 前的"切分 + 拼回 + 加 chunk 标记"
- 为未来字段级引用回填（evidence.chunk_id / source_quote）打基础

设计参考：Skill_Seekers `src/skill_seekers/cli/rag_chunker.py`，
但 chars_per_token 默认 3（混合中英文），移除其 skill 目录扫描相关函数。

不在 W1 范围内的特性（推后）：
- 真实 token 计数（litellm.token_counter）
- 段落级 source span 还原
"""

from __future__ import annotations

import re


_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_PARA_BREAK_RE = re.compile(r"\n\n+")
_HEADING_RE = re.compile(r"(?:^|\n)#{1,6}\s+.+(?:\n|$)")
_LINE_BREAK_RE = re.compile(r"\n")


class Chunker:
    """语义切分 + 代码块占位保护。"""

    def __init__(
        self,
        chunk_size_tokens: int = 1500,
        chunk_overlap_tokens: int = 150,
        chars_per_token: int = 3,
        min_chunk_size_tokens: int = 100,
        preserve_code_blocks: bool = True,
        preserve_paragraphs: bool = True,
    ) -> None:
        self.chunk_size = chunk_size_tokens
        self.chunk_overlap = chunk_overlap_tokens
        self.chars_per_token = chars_per_token
        self.min_chunk_size = min_chunk_size_tokens
        self.preserve_code_blocks = preserve_code_blocks
        self.preserve_paragraphs = preserve_paragraphs

    # ── 公开 API ─────────────────────────────────────────────────────────

    def estimate_tokens(self, text: str) -> int:
        return len(text) // self.chars_per_token

    def chunk(self, text: str) -> list[dict]:
        """切分文本，返回 [{"chunk_id": int, "content": str, "estimated_tokens": int, "has_code_block": bool, "source_section": str}, ...]"""
        if not text or not text.strip():
            return []

        if self.preserve_code_blocks:
            text_with_placeholders, code_blocks = self._extract_code_blocks(text)
        else:
            text_with_placeholders, code_blocks = text, []

        boundaries = self._find_semantic_boundaries(text_with_placeholders)
        raw_chunks, start_positions = self._split_with_overlap(text_with_placeholders, boundaries)

        if self.preserve_code_blocks:
            raw_chunks = self._reinsert_code_blocks(raw_chunks, code_blocks)

        result: list[dict] = []
        for i, (chunk_text, start_pos) in enumerate(zip(raw_chunks, start_positions)):
            stripped = chunk_text.strip()
            if not stripped:
                continue
            # 找到 chunk 起始位置之前最近的标题行作为 source_section
            source_section = self._find_nearest_heading(text, start_pos)
            result.append({
                "chunk_id": i,
                "content": stripped,
                "estimated_tokens": self.estimate_tokens(stripped),
                "has_code_block": "```" in stripped,
                "source_section": source_section,
            })

        # chunk 列表为空时（极短输入），兜底返回原文为单 chunk
        if not result and text.strip():
            stripped = text.strip()
            result.append({
                "chunk_id": 0,
                "content": stripped,
                "estimated_tokens": self.estimate_tokens(stripped),
                "has_code_block": "```" in stripped,
                "source_section": self._find_nearest_heading(text, 0),
            })
        return result

    # ── 内部实现（移植自 Skill_Seekers rag_chunker） ─────────────────────

    def _extract_code_blocks(self, text: str) -> tuple[str, list[dict]]:
        """提取代码块为占位符，避免切分时把代码腰斩。"""
        code_blocks: list[dict] = []

        def replacer(match: re.Match) -> str:
            idx = len(code_blocks)
            code_blocks.append({"index": idx, "content": match.group(0)})
            return f"<<CODE_BLOCK_{idx}>>"

        return _CODE_BLOCK_RE.sub(replacer, text), code_blocks

    def _reinsert_code_blocks(self, chunks: list[str], code_blocks: list[dict]) -> list[str]:
        """把代码块还原到含占位符的 chunk 里。

        占位符若落在被丢掉的短片段里，或被硬切点拆开，整段代码追加回最近的 chunk，
        避免 ``` 内的泛型（如 Promise<Task>）消失。
        """
        if not code_blocks:
            return chunks
        result = []
        used: set[int] = set()
        for chunk in chunks:
            for block in code_blocks:
                placeholder = f"<<CODE_BLOCK_{block['index']}>>"
                if placeholder in chunk:
                    chunk = chunk.replace(placeholder, block["content"])
                    used.add(block["index"])
            chunk = re.sub(r"<<CODE_BLOCK_[^>\n]*>>?", "", chunk)
            result.append(chunk)
        missing = [block for block in code_blocks if block["index"] not in used]
        if missing:
            recovered = "\n\n".join(block["content"] for block in missing)
            if result:
                result[-1] = result[-1].rstrip() + "\n\n" + recovered
            else:
                result = [recovered]
        return result

    def _find_semantic_boundaries(self, text: str) -> list[int]:
        """三级语义边界：段落 > 标题 > 单换行。再按目标尺寸兜底补硬切点。"""
        boundaries = [0]

        if self.preserve_paragraphs:
            for m in _PARA_BREAK_RE.finditer(text):
                boundaries.append(m.end())

        for m in _HEADING_RE.finditer(text):
            boundaries.append(m.start())

        for m in _LINE_BREAK_RE.finditer(text):
            boundaries.append(m.start())

        target_size_chars = self.chunk_size * self.chars_per_token
        if len(text) > target_size_chars:
            expected_chunks = len(text) // target_size_chars
            if len(boundaries) < expected_chunks:
                for i in range(target_size_chars, len(text), target_size_chars):
                    boundaries.append(i)

        boundaries.append(len(text))
        return sorted(set(boundaries))

    def _split_with_overlap(self, text: str, boundaries: list[int]) -> tuple[list[str], list[int]]:
        """在语义边界处切分，相邻 chunk 之间保留 overlap。返回 (chunks, start_positions)。"""
        target_size_chars = self.chunk_size * self.chars_per_token
        overlap_chars = self.chunk_overlap * self.chars_per_token
        min_size_chars = self.min_chunk_size * self.chars_per_token

        if len(text) <= target_size_chars:
            return ([text] if text.strip() else [], [0])

        chunks: list[str] = []
        start_positions: list[int] = []
        i = 0
        while i < len(boundaries) - 1:
            start_pos = boundaries[i]
            j = i + 1
            while j < len(boundaries):
                potential_end = boundaries[j]
                potential_chunk = text[start_pos:potential_end]
                if len(potential_chunk) > target_size_chars:
                    if j > i + 1:
                        j -= 1
                    break
                j += 1

            if j == i + 1:
                j = min(i + 2, len(boundaries))

            end_pos = boundaries[min(j, len(boundaries) - 1)]
            chunk_text = text[start_pos:end_pos]

            # 含代码占位符的短片段不能按 min_chunk_size 丢掉，否则占位符无法还原。
            has_placeholder = "<<CODE_BLOCK_" in chunk_text
            if chunk_text.strip() and (
                len(text) <= target_size_chars
                or len(chunk_text) >= min_size_chars
                or has_placeholder
            ):
                chunks.append(chunk_text)
                start_positions.append(start_pos)

            if j < len(boundaries) - 1:
                overlap_start = max(start_pos, end_pos - overlap_chars)
                next_idx = min(j - 1, i + 1)
                for k in range(i + 1, j):
                    if boundaries[k] >= overlap_start:
                        next_idx = k
                        break
                i = next_idx if next_idx > i else i + 1
            else:
                break

        return chunks, start_positions

    def _find_nearest_heading(self, text: str, start_pos: int) -> str:
        """找到 start_pos 所在位置的标题（优先精确匹配），否则找最近的前置标题。"""
        headings: list[tuple[int, str]] = []
        for m in _HEADING_RE.finditer(text):
            headings.append((m.start(), m.group(0).strip().lstrip("#").strip()))

        if not headings:
            return "unknown"

        # 精确匹配：标题恰好在 chunk 起始处
        for h_start, h_text in headings:
            if h_start == start_pos:
                return h_text

        # 找 start_pos 之前最近的标题
        for h_start, h_text in reversed(headings):
            if h_start < start_pos:
                return h_text
        return "unknown"


# ---------------------------------------------------------------------------
# 拼回工具：给 extractor 用
# ---------------------------------------------------------------------------

def _render_chunk(ch: dict) -> str:
    section = ch.get("source_section", "unknown")
    return f"<<CHUNK {ch.get('chunk_id', 0)} — SOURCE_SECTION: {section}>>\n{ch.get('content', '')}"


_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _plain_pieces(text: str) -> list[str]:
    if not text:
        return []
    parts = re.split(r"(\n\n+)", text)
    grouped: list[str] = []
    buf = ""
    for part in parts:
        if not part:
            continue
        if buf and len(buf) + len(part) > 400 and not part.startswith("\n"):
            grouped.append(buf)
            buf = part
        else:
            buf += part
    if buf:
        grouped.append(buf)
    return grouped


def _atomic_segments(content: str) -> list[str]:
    """按代码围栏切段。围栏本身不再拆开，避免切坏泛型或代码。"""
    segments: list[str] = []
    pos = 0
    for match in _FENCE_RE.finditer(content):
        if match.start() > pos:
            segments.extend(_plain_pieces(content[pos:match.start()]))
        segments.append(match.group(0))
        pos = match.end()
    if pos < len(content):
        segments.extend(_plain_pieces(content[pos:]))
    return [segment for segment in segments if segment]


def _split_plain_to_budget(text: str, budget: int) -> list[str]:
    if len(text) <= budget:
        return [text] if text else []
    pieces: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if buf and len(buf) + len(line) > budget:
            pieces.append(buf)
            buf = ""
        if len(line) > budget:
            start = 0
            while start < len(line):
                pieces.append(line[start:start + budget])
                start += budget
            continue
        buf += line
    if buf:
        pieces.append(buf)
    return [piece for piece in pieces if piece]


def _split_chunk_to_fit(ch: dict, budget_chars: int) -> list[dict]:
    content = str(ch.get("content", ""))
    marker_len = len(_render_chunk({**ch, "content": ""}))
    budget = max(40, budget_chars - marker_len)
    if len(content) <= budget:
        return [dict(ch)]
    pieces: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            pieces.extend(_split_plain_to_budget(buf, budget))
            buf = ""

    for segment in _atomic_segments(content) or [content]:
        fenced = segment.startswith("```") and segment.endswith("```")
        if fenced:
            flush()
            pieces.append(segment)
            continue
        if not buf:
            buf = segment
            continue
        if len(buf) + len(segment) <= budget:
            buf += segment
        else:
            flush()
            buf = segment
    flush()
    return [
        {
            "chunk_id": ch.get("chunk_id", 0),
            "content": piece,
            "source_section": ch.get("source_section", "unknown"),
            "estimated_tokens": ch.get("estimated_tokens", 0),
            "has_code_block": "```" in piece,
        }
        for piece in pieces
    ] or [dict(ch)]


def pack_chunks_for_prompt(
    chunks: list[dict],
    max_chars: int,
) -> tuple[str, list[dict], bool]:
    """把 chunk 装进字符预算。装不下的整段留在 leftover，不换成短注记。

    返回 (text, leftover_chunks, content_dropped)。
    content_dropped 只在正文被裁掉且没有进入 leftover 时为 True。
    单个代码围栏即使超过预算也整段保留。
    """
    if not chunks:
        return "", [], False
    if max_chars <= 0:
        return "", list(chunks), True

    parts: list[str] = []
    used = 0
    for index, chunk in enumerate(chunks):
        rendered = _render_chunk(chunk)
        sep = 2 if parts else 0
        if used + sep + len(rendered) <= max_chars:
            parts.append(rendered)
            used += sep + len(rendered)
            continue

        room = max_chars - used - sep if parts else max_chars
        pieces = _split_chunk_to_fit(chunk, room)
        first_rendered = _render_chunk(pieces[0])
        rest = pieces[1:] + list(chunks[index + 1:])
        if used + sep + len(first_rendered) <= max_chars:
            parts.append(first_rendered)
            return "\n\n".join(parts), rest, False
        if not parts:
            return first_rendered, rest, False
        return "\n\n".join(parts), list(chunks[index:]), False
    return "\n\n".join(parts), [], False


def join_chunks_for_prompt(
    chunks: list[dict],
    max_chars: int,
) -> tuple[str, bool]:
    """将 chunk 拼成带 SOURCE_SECTION 标记的字符串。

    超过 max_chars 时不再丢弃整块、也不再只附加约 50 字的省略注记。
    返回文本包含全部输入正文。第二项为 True 表示超过预算，调用方应改用
    pack_chunks_for_prompt 分批发送，并且不得把“超预算”当成内容已经完整裁剪。
    """
    if not chunks:
        return "", False
    text = "\n\n".join(_render_chunk(chunk) for chunk in chunks)
    return text, len(text) > max_chars
