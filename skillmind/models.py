"""SkillMind 核心数据模型"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class RawDocument:
    """
    采集器统一输出的原始文档结构。
    所有 Collector 均返回此类型，下游 Extractor 无需感知来源差异。
    """
    content: str        # 清洗后的 Markdown 正文
    metadata: dict      # 来源、作者、时间、URL、doc_type 等
    content_hash: str   # SHA256（基于 content），用于去重
    doc_type: str       # "skill" / "blog" / "forum_post" / "webpage"

    @classmethod
    def from_content(
        cls,
        content: str,
        metadata: dict,
        doc_type: str,
    ) -> "RawDocument":
        """从内容字符串构建 RawDocument，自动计算 SHA256。
        surrogate 字符用 replace 模式处理，防止 UnicodeEncodeError。
        """
        safe_content = content.encode("utf-8", errors="replace").decode("utf-8")
        h = hashlib.sha256(safe_content.encode("utf-8")).hexdigest()
        return cls(content=safe_content, metadata=metadata, content_hash=h, doc_type=doc_type)

    # ── 便捷属性 ───────────────────────────────────────────────────────────

    @property
    def source_url(self) -> str:
        return self.metadata.get("source_url", "")

    @property
    def author(self) -> str:
        return self.metadata.get("author", "")

    @property
    def title(self) -> str:
        return self.metadata.get("title", "")

    @property
    def published_at(self) -> str:
        return self.metadata.get("published_at", "")
