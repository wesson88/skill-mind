"""采集器（Collector）v2.2

将四类来源统一缓存到 ~/.skillmind/cache/raw/，并维护 hashes.yaml：

- ingest_skill : 本地目录 / Git 仓库 → SKILL.md            (doc_type=skill)
- ingest_rss   : RSS / Atom Feed → 批量博客文章             (doc_type=blog)
- ingest_url   : 单篇文章 / 博客 URL                        (doc_type=blog)
- ingest_forum : 论坛主题帖（Discourse / Reddit / HN 等）   (doc_type=forum_post)

设计要点：
- 文件级去重：SHA256 内容哈希一致即跳过。
- 原子写：hashes.yaml、raw 文件均走 tmp → replace。
- v1 旧 hashes.yaml 条目无 doc_type，list_cached 兜底为 "skill"。
"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import yaml

from skillmind.cleaner import fetch_and_clean, parse_rss_feed
from skillmind.config import (
    HASHES_FILE,
    RAW_DIR,
    REPOS_DIR,
    ensure_dirs,
)


# ---------------------------------------------------------------------------
# 哈希持久化（原子写，防止崩溃损坏）
# ---------------------------------------------------------------------------

def _load_hashes() -> dict[str, dict]:
    if HASHES_FILE.exists():
        try:
            with HASHES_FILE.open("r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except yaml.YAMLError:
            return {}
    return {}


def _save_hashes(hashes: dict[str, dict]) -> None:
    """先写 .tmp 再 replace，os.replace 语义保证原子性。"""
    tmp = HASHES_FILE.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.dump(hashes, f, allow_unicode=True)
    tmp.replace(HASHES_FILE)


# ---------------------------------------------------------------------------
# 文件 IO 工具
# ---------------------------------------------------------------------------

def _safe_copy(src: Path, dest: Path) -> None:
    """二进制复制，规避 Windows 长路径与编码转换问题。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dest)
    except OSError:
        dest.write_bytes(src.read_bytes())


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# 本地目录扫描
# ---------------------------------------------------------------------------

def scan_local(base_dir: Path) -> Iterator[Path]:
    """递归扫描，返回所有 SKILL.md / *.skill.md / skill.md。"""
    patterns = ["SKILL.md", "*.skill.md", "skill.md"]
    seen: set[Path] = set()
    for pattern in patterns:
        for p in base_dir.rglob(pattern):
            if p not in seen:
                seen.add(p)
                yield p


# ---------------------------------------------------------------------------
# Git 工具
# ---------------------------------------------------------------------------

def _slug_from_url(url: str) -> str:
    parsed = urlparse(url)
    slug = parsed.path.strip("/").replace("/", "__")
    return slug or "unknown"


def _parse_github_url(url: str) -> tuple[str, str | None]:
    """
    解析 GitHub URL，拆出 (clone_url, subdir|None)。

    支持：
      - https://github.com/owner/repo                       → repo_url, None
      - https://github.com/owner/repo/tree/<branch>/sub/dir → repo_url, "sub/dir"
      - https://github.com/owner/repo/blob/...              → repo_url, None
    """
    parsed = urlparse(url)
    if parsed.netloc not in ("github.com", "www.github.com"):
        return url, None

    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return url, None

    repo_root_url = f"https://github.com/{parts[0]}/{parts[1]}"

    if len(parts) >= 4 and parts[2] == "tree":
        subdir = "/".join(parts[4:]) if len(parts) > 4 else None
        return repo_root_url, subdir or None

    return repo_root_url, None


def clone_or_pull(repo_url: str) -> tuple[Path, str]:
    """克隆（首次）或拉取（已存在）远端仓库。

    返回 (local_path, default_branch)。
    clone 失败时清理半克隆目录，避免下次进入 "exists" 分支拿到损坏 repo。
    """
    try:
        import git as gitpython  # type: ignore
    except ImportError:
        raise RuntimeError("请先安装 gitpython: pip install gitpython")

    slug = _slug_from_url(repo_url)
    local_path = REPOS_DIR / slug

    if local_path.exists():
        try:
            repo = gitpython.Repo(local_path)
            repo.remotes.origin.pull()
        except Exception as e:
            # 网络中断、合并冲突等：保留本地缓存，不崩溃
            import warnings
            warnings.warn(f"Git pull 失败，使用本地缓存: {e}", stacklevel=2)
            repo = gitpython.Repo(local_path)
    else:
        try:
            gitpython.Repo.clone_from(repo_url, local_path, depth=1)
        except Exception:
            # 半 clone 目录会污染下次调用，必须彻底清理
            shutil.rmtree(local_path, ignore_errors=True)
            raise
        repo = gitpython.Repo(local_path)

    return local_path, _detect_default_branch(repo)


def _detect_default_branch(repo) -> str:
    """优先取 active_branch；detached HEAD 时降级 main/master。"""
    try:
        return repo.active_branch.name
    except Exception:
        try:
            ref_names = {r.name.split("/")[-1] for r in repo.refs}
            for fallback in ("main", "master"):
                if fallback in ref_names:
                    return fallback
        except Exception:
            pass
    return "main"


def _get_commit_sha(repo_path: Path) -> str | None:
    """读取 commit sha。允许从 repo 子目录定位 .git。"""
    try:
        import git as gitpython  # type: ignore
        repo = gitpython.Repo(repo_path, search_parent_directories=True)
        return repo.head.commit.hexsha[:8]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 列出已缓存条目
# ---------------------------------------------------------------------------

def list_cached() -> list[dict]:
    """返回所有已缓存条目；v1 旧条目自动兜底 doc_type='skill'。"""
    hashes = _load_hashes()
    result: list[dict] = []
    for sha, info in hashes.items():
        info = dict(info)
        info["source_hash"] = sha
        info.setdefault("doc_type", "skill")
        result.append(info)
    return result


# ---------------------------------------------------------------------------
# Web 文档缓存（rss / url / forum 共用）
# ---------------------------------------------------------------------------

def _cache_web_document(
    *,
    content: str,
    metadata: dict,
    doc_type: str,
    hashes: dict[str, dict],
) -> tuple[dict, bool]:
    """
    将一篇 web 来源的清洗后正文写入 RAW_DIR，并更新 hashes 字典。

    返回 (info_dict, is_new)。is_new=False 表示按内容哈希命中已存在条目。
    info_dict 直接可作为采集结果，含 raw_path / source_hash / doc_type /
    source_url / title / author / published_at / fetch_time / skipped。
    """
    sha = _sha256_text(content)
    skipped = sha in hashes
    raw_dest = RAW_DIR / sha[:2] / f"{sha}.md"

    if not skipped:
        _atomic_write_text(raw_dest, content)
        hashes[sha] = {
            "doc_type": doc_type,
            "source_url": metadata.get("source_url", ""),
            "title": metadata.get("title", ""),
            "author": metadata.get("author", ""),
            "published_at": metadata.get("published_at", ""),
            "fetch_time": time.time(),
            "raw_path": str(raw_dest),
        }
    else:
        # 旧条目；若文件被外部删了，重新落盘
        existing_path = Path(hashes[sha].get("raw_path", str(raw_dest)))
        if not existing_path.exists():
            _atomic_write_text(existing_path, content)

    info = dict(hashes[sha])
    info["source_hash"] = sha
    info["skipped"] = skipped
    return info, not skipped


# ---------------------------------------------------------------------------
# ingest_skill：本地目录 / Git 仓库
# ---------------------------------------------------------------------------

def ingest_skill(source: str, console=None) -> list[dict]:
    """采集 SKILL.md（doc_type=skill）。source 支持本地路径或 Git URL。"""
    ensure_dirs()
    hashes = _load_hashes()
    results: list[dict] = []

    parsed = urlparse(source)
    if parsed.scheme in ("http", "https", "git"):
        clone_url, subdir = _parse_github_url(source)
        if console and clone_url != source:
            console.print(f"[dim]检测到子目录 URL，仓库地址:[/dim] {clone_url}")
            if subdir:
                console.print(f"[dim]仅扫描子目录:[/dim] {subdir}")
        if console:
            console.print(f"[cyan]克隆仓库:[/cyan] {clone_url}")
        base_dir, branch = clone_or_pull(clone_url)
        source_repo = source  # 保留原始 URL，便于 trace 还原
        commit_sha = _get_commit_sha(base_dir)
        if subdir:
            scan_dir = base_dir / subdir
            if not scan_dir.exists():
                raise FileNotFoundError(
                    f"子目录不存在于仓库中: {subdir}\n请检查路径: {source}"
                )
            base_dir = scan_dir
    else:
        base_dir = Path(source).expanduser().resolve()
        if not base_dir.exists():
            raise FileNotFoundError(f"路径不存在: {base_dir}")
        source_repo = str(base_dir)
        commit_sha = None
        branch = ""  # 本地路径无分支概念

    skill_files = list(scan_local(base_dir))
    if console:
        console.print(f"[green]发现 {len(skill_files)} 个 SKILL 文件[/green]")

    hashes_dirty = False
    for skill_file in skill_files:
        sha = _sha256_file(skill_file)
        skipped = sha in hashes

        try:
            rel_path = skill_file.relative_to(base_dir).as_posix()
        except ValueError:
            rel_path = skill_file.as_posix()

        if not skipped:
            raw_dest = RAW_DIR / sha[:2] / f"{sha}.md"
            _safe_copy(skill_file, raw_dest)
            hashes[sha] = {
                "doc_type": "skill",
                "source_repo": source_repo,
                "source_path": rel_path,
                "commit_sha": commit_sha,
                "branch": branch,
                "fetch_time": time.time(),
                "raw_path": str(raw_dest),
            }
            hashes_dirty = True
            if console:
                console.print(f"  [green]✓[/green] 已缓存: {rel_path}")
        else:
            # 兼容旧条目：raw_path 字段可能缺失
            raw_dest = Path(hashes[sha].get("raw_path") or str(RAW_DIR / sha[:2] / f"{sha}.md"))
            if not raw_dest.exists():
                _safe_copy(skill_file, raw_dest)
                hashes[sha]["raw_path"] = str(raw_dest)
                hashes_dirty = True
                if console:
                    console.print(f"  [yellow]⚠ 缓存文件丢失，已重新复制:[/yellow] {rel_path}")
            else:
                if console:
                    console.print(f"  [yellow]⟳[/yellow] 跳过(已存在): {rel_path}")

        results.append({
            "raw_path": str(raw_dest),
            "source_path": rel_path,
            "source_repo": source_repo,
            "source_hash": sha,
            "commit_sha": commit_sha,
            "branch": hashes[sha].get("branch", branch),
            "fetch_time": hashes[sha].get("fetch_time", 0.0),
            "doc_type": "skill",
            "skipped": skipped,
            "title": hashes[sha].get("title", ""),
            "author": hashes[sha].get("author", ""),
            "published_at": hashes[sha].get("published_at", ""),
            "source_url": hashes[sha].get("source_url", ""),
        })

    if hashes_dirty:
        _save_hashes(hashes)

    return results


# ---------------------------------------------------------------------------
# ingest_url：单篇文章 / 博客
# ---------------------------------------------------------------------------

def ingest_url(article_url: str, console=None, doc_type: str = "blog") -> list[dict]:
    """抓取单篇 URL（默认 doc_type=blog；ingest_forum 复用并改为 forum_post）。"""
    ensure_dirs()
    hashes = _load_hashes()

    if console:
        console.print(f"[cyan]抓取:[/cyan] {article_url}")

    md, metadata = fetch_and_clean(article_url)
    if not md.strip():
        if console:
            console.print(f"[yellow]未能获取正文:[/yellow] {article_url}")
        return []

    metadata.setdefault("source_url", article_url)
    info, is_new = _cache_web_document(
        content=md, metadata=metadata, doc_type=doc_type, hashes=hashes
    )
    if is_new:
        _save_hashes(hashes)

    if console:
        title_short = (info.get("title") or article_url)[:60]
        if is_new:
            console.print(f"  [green]✓[/green] 已缓存: {title_short}")
        else:
            console.print(f"  [yellow]⟳[/yellow] 跳过(已存在): {title_short}")

    return [info]


# ---------------------------------------------------------------------------
# ingest_rss：批量抓取 RSS / Atom Feed
# ---------------------------------------------------------------------------

def ingest_rss(feed_url: str, console=None, max_items: int = 50) -> list[dict]:
    """解析 Feed → 逐条抓取正文 → 缓存（doc_type=blog）。"""
    ensure_dirs()
    hashes = _load_hashes()

    if console:
        console.print(f"[cyan]解析 RSS:[/cyan] {feed_url}")
    entries = parse_rss_feed(feed_url)
    if not entries:
        if console:
            console.print("[yellow]未解析到任何条目[/yellow]")
        return []
    entries = entries[:max_items]
    if console:
        console.print(f"[green]共 {len(entries)} 篇文章[/green]")

    results: list[dict] = []
    dirty = False
    for i, entry in enumerate(entries):
        title = entry.get("title", "") or "(no title)"
        url = entry.get("url", "")
        if not url:
            continue
        if console:
            console.print(f"  [{i+1}/{len(entries)}] {title[:60]}")

        md, metadata = fetch_and_clean(url)
        if not md.strip():
            if console:
                console.print("    [yellow]✗ 未能获取正文，跳过[/yellow]")
            continue

        # RSS 自带的元信息更可信，优先使用
        metadata = {
            "source_url": url,
            "title": entry.get("title") or metadata.get("title", ""),
            "author": entry.get("author") or metadata.get("author", ""),
            "published_at": entry.get("published_at") or metadata.get("published_at", ""),
        }
        info, is_new = _cache_web_document(
            content=md, metadata=metadata, doc_type="blog", hashes=hashes
        )
        dirty = dirty or is_new
        if console:
            tag = "[green]✓ 已缓存[/green]" if is_new else "[yellow]⟳ 跳过[/yellow]"
            console.print(f"    {tag}")
        results.append(info)

    if dirty:
        _save_hashes(hashes)
    return results


# ---------------------------------------------------------------------------
# ingest_forum：论坛主题帖
# ---------------------------------------------------------------------------

def ingest_forum(topic_url: str, console=None) -> list[dict]:
    """采集论坛主题帖（Discourse / Reddit / HN）。当前降级为通用抓取。"""
    return ingest_url(topic_url, console=console, doc_type="forum_post")
