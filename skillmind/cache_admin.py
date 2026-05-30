"""缓存管理（Cache Admin）v2.4c

负责 raw / extract_cache / drafts 三层缓存的清理与统计。

入口：
- cleanup_source(hash)            : 删该来源 raw + extract + 已发布草稿
- cleanup_after_publish(hash)     : publish 后自动调用；仅当无 pending 草稿才执行
- find_orphan_entries()           : hashes.yaml 有但 raw 不存在
- find_published_entries()        : hashes.yaml 中 published=true
- wipe_all_caches()               : 全清（raw + extract + drafts + hashes.yaml）

设计要点：
- 一文多卡场景：等同一 source_hash 的所有草稿都 status=published 才清 raw/extract
- 清理时 raw 文件 + extract_cache 文件直接删，hashes.yaml 条目**保留**并标记 published=true
  这样将来 re-ingest 同 SHA 仍能 dedup；如要重提取需 `cache clean --hash` 或重 ingest（自动清旗）
- 所有删除走 try/except，逐条容错，单条失败不影响整体
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

from skillmind.collector import _load_hashes, _save_hashes
from skillmind.config import (
    DRAFTS_DIR,
    EXTRACT_CACHE_DIR,
    HASHES_FILE,
    RAW_DIR,
    REPOS_DIR,
    ensure_dirs,
)
from skillmind.reviewer import list_drafts


# ---------------------------------------------------------------------------
# 单源清理
# ---------------------------------------------------------------------------

def cleanup_source(source_hash: str, console=None) -> dict:
    """删除某 source_hash 的 raw + extract_cache + 该来源的已发布草稿。

    hashes.yaml 中的条目保留并标记 published=true（不删条目，保留 dedup 能力）。

    返回统计 dict：{raw_deleted, extract_deleted, draft_deleted, hash_marked}
    """
    ensure_dirs()
    stats = {"raw_deleted": 0, "extract_deleted": 0, "draft_deleted": 0, "hash_marked": False}

    # 1. raw 文件
    raw_path = RAW_DIR / source_hash[:2] / f"{source_hash}.md"
    if raw_path.exists():
        try:
            raw_path.unlink()
            stats["raw_deleted"] = 1
        except OSError:
            pass

    # 2. extract_cache 各阶段 / 各版本
    for p in EXTRACT_CACHE_DIR.glob(f"{source_hash}_*.json"):
        try:
            p.unlink()
            stats["extract_deleted"] += 1
        except OSError:
            pass

    # 3. 已发布的草稿文件（status=published 才删；未发布的留着保险）
    for draft_file in DRAFTS_DIR.glob("*.json"):
        try:
            import json
            with draft_file.open("r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if (d.get("source", {}).get("source_hash") == source_hash
                and d.get("status") == "published"):
            try:
                draft_file.unlink()
                stats["draft_deleted"] += 1
            except OSError:
                pass

    # 4. hashes.yaml: 标记 published（保留条目用于 SHA dedup）
    hashes = _load_hashes()
    if source_hash in hashes:
        hashes[source_hash]["published"] = True
        _save_hashes(hashes)
        stats["hash_marked"] = True

    if console and any(v for v in stats.values() if isinstance(v, int) and v):
        console.print(
            f"  [dim]✓ 清理 {source_hash[:8]}：raw {stats['raw_deleted']} / "
            f"extract {stats['extract_deleted']} / drafts {stats['draft_deleted']}[/dim]"
        )

    return stats


def cleanup_after_publish(source_hash: str, console=None) -> dict:
    """publish 后自动调用：检查该来源是否还有未发布草稿，若无则触发 cleanup_source。

    返回 dict：{"cleaned": bool, "pending": int, **cleanup_stats}
    """
    pending = [
        d for d in list_drafts()
        if d.get("source", {}).get("source_hash") == source_hash
        and d.get("status") != "published"
    ]
    if pending:
        return {"cleaned": False, "pending": len(pending)}

    stats = cleanup_source(source_hash, console=console)
    return {"cleaned": True, "pending": 0, **stats}


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def find_orphan_entries() -> list[dict]:
    """hashes.yaml 中 raw_path 缺失或对应文件不存在的条目（孤儿）。"""
    hashes = _load_hashes()
    orphans: list[dict] = []
    for sha, info in hashes.items():
        raw_path = Path(info.get("raw_path", "")) if info.get("raw_path") else (
            RAW_DIR / sha[:2] / f"{sha}.md"
        )
        if not raw_path.exists() and not info.get("published"):
            orphans.append({"source_hash": sha, **info})
    return orphans


def find_published_entries() -> list[dict]:
    """hashes.yaml 中 published=true 的条目。"""
    hashes = _load_hashes()
    return [
        {"source_hash": sha, **info}
        for sha, info in hashes.items()
        if info.get("published")
    ]


def cache_stats() -> dict:
    """返回缓存全局统计。"""
    hashes = _load_hashes()
    raw_files = list(RAW_DIR.rglob("*.md"))
    extract_files = list(EXTRACT_CACHE_DIR.glob("*.json"))
    drafts = list(DRAFTS_DIR.glob("*.json"))
    repos = [p for p in REPOS_DIR.iterdir() if p.is_dir()] if REPOS_DIR.exists() else []

    orphans = find_orphan_entries()
    published = find_published_entries()

    raw_bytes = sum(p.stat().st_size for p in raw_files)
    extract_bytes = sum(p.stat().st_size for p in extract_files)

    return {
        "hashes_total": len(hashes),
        "hashes_active": len(hashes) - len(published),
        "hashes_published": len(published),
        "hashes_orphan": len(orphans),
        "raw_files": len(raw_files),
        "raw_bytes": raw_bytes,
        "extract_files": len(extract_files),
        "extract_bytes": extract_bytes,
        "drafts_total": len(drafts),
        "repos_clones": len(repos),
    }


# ---------------------------------------------------------------------------
# 批量 / 全量清理
# ---------------------------------------------------------------------------

def cleanup_many(source_hashes: Iterable[str], console=None) -> dict:
    """批量清理多个 source_hash。"""
    total = {"sources": 0, "raw_deleted": 0, "extract_deleted": 0, "draft_deleted": 0}
    for sha in source_hashes:
        s = cleanup_source(sha, console=console)
        total["sources"] += 1
        total["raw_deleted"] += s["raw_deleted"]
        total["extract_deleted"] += s["extract_deleted"]
        total["draft_deleted"] += s["draft_deleted"]
    return total


def cleanup_orphans(console=None) -> dict:
    """清理孤儿条目（raw 不存在）：从 hashes.yaml 删条目；不动 extract（也可能孤儿）。"""
    orphans = find_orphan_entries()
    hashes = _load_hashes()
    removed = 0
    for o in orphans:
        sha = o["source_hash"]
        if sha in hashes:
            del hashes[sha]
            removed += 1
            # 顺便清相应 extract_cache（孤儿条目的 extract 也无用）
            for p in EXTRACT_CACHE_DIR.glob(f"{sha}_*.json"):
                try:
                    p.unlink()
                except OSError:
                    pass
    if removed:
        _save_hashes(hashes)
    if console:
        console.print(f"  [dim]✓ 清理孤儿 {removed} 条[/dim]")
    return {"removed": removed}


def wipe_all_caches(console=None) -> dict:
    """全清：raw / extract_cache / drafts / hashes.yaml。repos/ 克隆保留（重 ingest 会复用）。"""
    ensure_dirs()
    stats = {"raw": 0, "extract": 0, "drafts": 0, "hashes": False}

    for p in RAW_DIR.rglob("*.md"):
        try:
            p.unlink()
            stats["raw"] += 1
        except OSError:
            pass
    # 清空空的 shard 目录
    if RAW_DIR.exists():
        for sub in RAW_DIR.iterdir():
            if sub.is_dir():
                try:
                    sub.rmdir()
                except OSError:
                    pass

    for p in EXTRACT_CACHE_DIR.glob("*.json"):
        try:
            p.unlink()
            stats["extract"] += 1
        except OSError:
            pass

    for p in DRAFTS_DIR.glob("*.json"):
        try:
            p.unlink()
            stats["drafts"] += 1
        except OSError:
            pass

    if HASHES_FILE.exists():
        try:
            HASHES_FILE.unlink()
            stats["hashes"] = True
        except OSError:
            pass

    if console:
        console.print(
            f"  [dim]✓ 全清：raw {stats['raw']} / extract {stats['extract']} / "
            f"drafts {stats['drafts']} / hashes.yaml {'✓' if stats['hashes'] else '−'}[/dim]"
        )
    return stats


def wipe_repos(console=None) -> dict:
    """额外：清理克隆缓存 ~/.skillmind/cache/repos/。需要单独触发，因为重复 clone 较贵。"""
    if not REPOS_DIR.exists():
        return {"removed": 0}
    removed = 0
    for sub in REPOS_DIR.iterdir():
        if sub.is_dir():
            try:
                shutil.rmtree(sub)
                removed += 1
            except OSError:
                pass
    if console:
        console.print(f"  [dim]✓ 清理克隆 {removed} 个仓库[/dim]")
    return {"removed": removed}
