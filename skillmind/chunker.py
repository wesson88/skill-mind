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
_HEADING_RE = re.compile(r"\n#{1,6}\s+.+\n")
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
        """切分文本，返回 [{"chunk_id": int, "content": str, "estimated_tokens": int, "has_code_block": bool}, ...]"""
        if not text or not text.strip():
            return []

        if self.preserve_code_blocks:
            text_with_placeholders, code_blocks = self._extract_code_blocks(text)
        else:
            text_with_placeholders, code_blocks = text, []

        boundaries = self._find_semantic_boundaries(text_with_placeholders)
        raw_chunks = self._split_with_overlap(text_with_placeholders, boundaries)

        if self.preserve_code_blocks:
            raw_chunks = self._reinsert_code_blocks(raw_chunks, code_blocks)

        result: list[dict] = []
        for i, chunk_text in enumerate(raw_chunks):
            stripped = chunk_text.strip()
            if not stripped:
                continue
            result.append({
                "chunk_id": i,
                "content": stripped,
                "estimated_tokens": self.estimate_tokens(stripped),
                "has_code_block": "```" in stripped,
            })

        # chunk 列表为空时（极短输入），兜底返回原文为单 chunk
        if not result and text.strip():
            stripped = text.strip()
            result.append({
                "chunk_id": 0,
                "content": stripped,
                "estimated_tokens": self.estimate_tokens(stripped),
                "has_code_block": "```" in stripped,
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
        """把代码块还原到含占位符的 chunk 里。"""
        if not code_blocks:
            return chunks
        result = []
        for chunk in chunks:
            for block in code_blocks:
                placeholder = f"<<CODE_BLOCK_{block['index']}>>"
                if placeholder in chunk:
                    chunk = chunk.replace(placeholder, block["content"])
            result.append(chunk)
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

    def _split_with_overlap(self, text: str, boundaries: list[int]) -> list[str]:
        """在语义边界处切分，相邻 chunk 之间保留 overlap。"""
        target_size_chars = self.chunk_size * self.chars_per_token
        overlap_chars = self.chunk_overlap * self.chars_per_token
        min_size_chars = self.min_chunk_size * self.chars_per_token

        if len(text) <= target_size_chars:
            return [text] if text.strip() else []

        chunks: list[str] = []
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

            if chunk_text.strip() and (
                len(text) <= target_size_chars or len(chunk_text) >= min_size_chars
            ):
                chunks.append(chunk_text)

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

        return chunks


# ---------------------------------------------------------------------------
# 拼回工具：给 extractor 用
# ---------------------------------------------------------------------------

def join_chunks_for_prompt(
    chunks: list[dict],
    max_chars: int,
) -> tuple[str, bool]:
    """将 chunker.chunk() 输出拼成单一字符串，每个 chunk 前加 <<CHUNK N>> 标记。

    超过 max_chars 时截尾（按整 chunk 丢弃，不在 chunk 内部切），返回 (拼接文本, 是否截尾)。
    """
    if not chunks:
        return "", False

    parts: list[str] = []
    used_chars = 0
    truncated = False

    for ch in chunks:
        marker = f"<<CHUNK {ch['chunk_id']}>>\n"
        body = ch["content"]
        cost = len(marker) + len(body) + 2  # +2 给两个换行分隔

        if used_chars + cost > max_chars:
            truncated = True
            break

        parts.append(marker + body)
        used_chars += cost

    text = "\n\n".join(parts)
    if truncated:
        text += (
            f"\n\n... (剩余 chunk 因超过预算 {max_chars} 字符已省略，"
            f"已保留 {len(parts)}/{len(chunks)} 个 chunk)"
        )
    return text, truncated
