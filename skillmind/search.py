"""搜索与同步模块（可选，依赖 chromadb）"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skillmind.config import SKILLMIND_HOME, get_vault_dir, load_config


def _chroma_db_dir() -> Path:
    """动态返回 Chroma DB 路径，跟随 SKILLMIND_HOME 环境变量变化。"""
    return SKILLMIND_HOME / "chroma"


def _get_chroma_collection():
    try:
        import chromadb  # type: ignore
    except ImportError:
        raise RuntimeError(
            "语义搜索需要安装 chromadb: pip install 'skillmind[search]' 或 pip install chromadb"
        )
    db_dir = _chroma_db_dir()
    db_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(db_dir))
    return client.get_or_create_collection("skillmind_skills")


# ---------------------------------------------------------------------------
# 索引构建
# ---------------------------------------------------------------------------

def _skill_doc_text(data: dict) -> str:
    """将技能卡片转为用于嵌入的文本。"""
    meta = data.get("meta", {})
    le = data.get("learning_enhancement", {})
    parts = [
        meta.get("name", ""),
        meta.get("intent", ""),
        le.get("plain_summary", ""),
        " ".join(le.get("knowledge_tags", [])),
        " ".join(meta.get("trigger_keywords", [])),
    ]
    return " ".join(p for p in parts if p)


def index_vault(console=None) -> int:
    """扫描 Vault 中所有已发布的 Markdown，批量更新 Chroma 索引。返回已索引数量。"""
    cfg = load_config()
    vault_dir = get_vault_dir(cfg)
    published_dir = vault_dir / "skills"

    if not published_dir.exists():
        if console:
            console.print("[yellow]Vault/skills 目录不存在，请先发布技能卡片[/yellow]")
        return 0

    collection = _get_chroma_collection()

    # 批量收集，一次 upsert，减少 Chroma 写入次数
    BATCH_SIZE = 50
    ids_batch, docs_batch, metas_batch = [], [], []
    count = 0

    def _flush():
        if ids_batch:
            collection.upsert(ids=ids_batch, documents=docs_batch, metadatas=metas_batch)
            ids_batch.clear()
            docs_batch.clear()
            metas_batch.clear()

    for md_file in published_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        uid = _extract_uuid_from_md(text) or md_file.stem
        doc_text = _extract_searchable_text(text)
        ids_batch.append(uid)
        docs_batch.append(doc_text)
        metas_batch.append({"file": str(md_file)})
        count += 1
        if len(ids_batch) >= BATCH_SIZE:
            _flush()

    _flush()  # 剩余部分

    if console:
        console.print(f"[green]已索引 {count} 个技能卡片[/green]")
    return count


def _extract_uuid_from_md(text: str) -> str:
    import re
    m = re.search(r"uuid:\s*([^\n]+)", text)
    return m.group(1).strip() if m else ""


def _extract_searchable_text(md_text: str) -> str:
    """从 Markdown 提取可搜索文本（去除 YAML front matter 符号）。"""
    import re
    text = re.sub(r"^---.*?---\s*", "", md_text, flags=re.DOTALL)
    text = re.sub(r"[#`*_\[\]|]", " ", text)
    return " ".join(text.split())[:2000]


# ---------------------------------------------------------------------------
# 语义搜索
# ---------------------------------------------------------------------------

def semantic_search(query: str, top_k: int = 5) -> list[dict]:
    """在 Chroma 中执行语义搜索，返回结果列表。"""
    collection = _get_chroma_collection()
    results = collection.query(query_texts=[query], n_results=top_k)
    output = []
    ids = results.get("ids", [[]])[0]
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]
    for uid, doc, meta, dist in zip(ids, docs, metas, distances):
        output.append({
            "uuid": uid,
            "file": meta.get("file", ""),
            "snippet": doc[:200],
            "score": round(1 - dist, 4) if dist is not None else 0,
        })
    return output
