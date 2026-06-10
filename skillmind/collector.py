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
from skillmind.parser import parse_sections, split_front_matter


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
    """递归扫描，返回所有 SKILL.md / *.skill.md / skill.md / CLAUDE.md / *.md（可配置）。"""
    patterns = [
        "SKILL.md", "*.skill.md", "skill.md",
        "CLAUDE.md",   # Claude Code / vibe-coding 风格仓库
        "AGENTS.md",   # OpenAI Codex Agent 风格
        "GEMINI.md",   # Gemini CLI 风格
        "DESIGN.md",   # 设计规范文档（如 taste-skill 等设计系统仓库）
    ]
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


def clone_or_pull(repo_url: str) -> Path:
    """克隆（首次）或拉取（已存在）远端仓库，返回本地路径。"""
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
    else:
        gitpython.Repo.clone_from(repo_url, local_path, depth=1)

    return local_path


def _get_commit_sha(repo_path: Path) -> str | None:
    try:
        import git as gitpython  # type: ignore
        repo = gitpython.Repo(repo_path)
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

def _ingest_single_raw_url(raw_url: str, *, source_url: str, source_repo: str,
                            hashes: dict, console=None) -> list[dict]:
    """下载单个 raw 文件（如 GitHub raw URL）并缓存为 skill 条目。"""
    import httpx as _httpx
    try:
        resp = _httpx.get(raw_url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"下载 raw 文件失败: {raw_url}\n{e}")

    content = resp.text
    sha = hashlib.sha256(content.encode()).hexdigest()

    if sha in hashes:
        if console:
            console.print(f"  [yellow]⟳ 已存在（内容未变），跳过:[/yellow] {raw_url.split('/')[-1]}")
        h = hashes[sha]
        raw_dest = Path(h.get("raw_path") or str(RAW_DIR / sha[:2] / f"{sha}.md"))
        return [{**h, "source_hash": sha, "raw_path": str(raw_dest)}]

    raw_dest = RAW_DIR / sha[:2] / f"{sha}.md"
    raw_dest.parent.mkdir(parents=True, exist_ok=True)
    raw_dest.write_text(content, encoding="utf-8")

    # 从 raw_url 解析 source_path
    # raw.githubusercontent.com/owner/repo/branch/path/SKILL.md
    path_parts = urlparse(raw_url).path.strip("/").split("/")
    source_path = "/".join(path_parts[3:]) if len(path_parts) > 3 else path_parts[-1]

    entry = {
        "doc_type": "skill",
        "source_repo": source_repo,
        "source_url": source_url,
        "source_path": source_path,
        "commit_sha": None,
        "fetch_time": time.time(),
        "raw_path": str(raw_dest),
        "title": source_path.split("/")[-1],
        "author": "",
        "published_at": "",
    }
    hashes[sha] = entry
    _save_hashes(hashes)

    if console:
        console.print(f"  [green]✓ 已缓存:[/green] {source_path}")
    return [{**entry, "source_hash": sha}]


def ingest_skill(source: str, console=None) -> list[dict]:
    """采集 SKILL.md（doc_type=skill）。source 支持本地路径或 Git URL。"""
    ensure_dirs()
    hashes = _load_hashes()
    results: list[dict] = []

    # GitHub blob URL（单文件）→ 转为 raw URL 直接下载，无需克隆仓库
    # https://github.com/owner/repo/blob/main/path/SKILL.md
    # → https://raw.githubusercontent.com/owner/repo/main/path/SKILL.md
    parsed_check = urlparse(source)
    if (parsed_check.scheme in ("http", "https")
            and "github.com" in parsed_check.netloc
            and "/blob/" in parsed_check.path):
        raw_url = (
            source
            .replace("github.com", "raw.githubusercontent.com", 1)
            .replace("/blob/", "/", 1)
        )
        if console:
            console.print(f"[dim]检测到 GitHub blob URL，转为 raw 下载:[/dim] {raw_url}")
        return _ingest_single_raw_url(raw_url, source_url=source,
                                      source_repo=source, hashes=hashes,
                                      console=console)

    parsed = urlparse(source)
    if parsed.scheme in ("http", "https", "git"):
        clone_url, subdir = _parse_github_url(source)
        if console and clone_url != source:
            console.print(f"[dim]检测到子目录 URL，仓库地址:[/dim] {clone_url}")
            if subdir:
                console.print(f"[dim]仅扫描子目录:[/dim] {subdir}")
        if console:
            console.print(f"[cyan]克隆仓库:[/cyan] {clone_url}")
        base_dir = clone_or_pull(clone_url)
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


# ---------------------------------------------------------------------------
# ingest_auto：自动识别输入类型并分派
# ---------------------------------------------------------------------------

def ingest_auto(
    target: str,
    *,
    kind_override: str | None = None,
    max_items: int = 50,
    console=None,
) -> tuple[str, list[dict]]:
    """
    自动识别 target 类型并调用对应采集器。

    返回 (kind, results)，kind 为实际使用的类型字符串：
      'skill' | 'design_system' | 'rss' | 'url' | 'forum'

    探测规则（可被 kind_override 覆盖）：
    1. 本地路径 / .git 结尾 / github.com 仓库根（无 feed/rss 关键词）→ skill
    2. 含 feed/rss/atom 关键词 或 .xml/.rss/.atom 后缀 → rss
    3. reddit.com / news.ycombinator.com / Discourse 模式 → forum
    4. DESIGN.md URL → design_system（文件名模式兜底）
    5. 其余 http(s) URL → url

    采集后自动识别：
    - 对 skill/url/forum 类型，采集后读取内容分析 section 结构，
      自动识别真正的 doc_type（skill / design_system / blog / forum_post）
    - 避免因文件名不规范导致错误分类
    """
    kind = kind_override or _detect_kind(target)

    if kind == "skill":
        results = ingest_skill(target, console=console)
    elif kind == "design_system":
        results = ingest_skill(target, console=console)
    elif kind == "rss":
        results = ingest_rss(target, console=console, max_items=max_items)
    elif kind == "forum":
        results = ingest_forum(target, console=console)
    else:
        kind = "url"
        results = ingest_url(target, console=console)

    # 采集后自动识别：根据内容结构确定最终 doc_type
    if kind in ("skill", "design_system", "url", "forum", "rss"):
        results = _auto_detect_doc_type(results, console=console)

    return kind, results


# ---------------------------------------------------------------------------
# 基于内容的 doc_type 自动识别（自举方案）
# ---------------------------------------------------------------------------

# section_type → doc_type 映射权重
# 根据真实文档统计得出：某种 section_type 出现在某种 doc_type 中的频率
_SECTION_TYPE_WEIGHTS: dict[str, dict[str, float]] = {
    # skill 文档特征：有明确的 procedure、preconditions、halt_conditions
    "skill": {
        "procedure":       3.0,   # 有操作步骤是 skill 的核心特征
        "preconditions":   2.5,   # 前置条件是 skill 的重要组成
        "halt_conditions": 2.0,   # 中止条件是 skill 的重要组成
        "rollback":        1.5,   # 回滚步骤是 skill 的补充组成
        "decisions":       1.0,   # 决策点在 skill 中出现
        "overview":        0.5,   # 概述在 skill 中可能出现
        "design":          0.3,   # design 在 skill 中很少出现（设计文档除外）
        "examples":        0.5,   # 示例在 skill 中可能出现
    },
    # design_system 文档特征：有 design、overview，少有 procedure
    "design_system": {
        "design":          3.0,   # 设计维度是 design_system 的核心
        "overview":        1.5,   # 概述/设计理念在 design_system 中常见
        "examples":        1.0,   # 示例在 design_system 中可能出现
        "procedure":       0.1,   # design_system 几乎没有操作步骤
        "preconditions":   0.1,
        "halt_conditions": 0.5,   # 铁律/禁区在 design_system 中可能出现
        "decisions":       0.5,   # 适用条件在 design_system 中可能出现
    },
    # blog 文档特征：overview + examples + notes，没有严格的 procedure
    "blog": {
        "overview":        2.0,   # 博客通常以概述/介绍开头
        "examples":        1.5,   # 博客常有示例
        "notes":           1.0,   # 博客常有提示/注意事项
        "design":          0.5,   # 技术博客可能涉及设计讨论
        "procedure":       0.5,   # 教程类博客有步骤，但不是严格格式
        "preconditions":   0.3,
    },
    # forum_post 文档特征：overview（问题描述）+ decisions（回答）
    "forum_post": {
        "overview":        2.0,   # 问题描述在论坛帖中
        "decisions":       2.0,   # 多个回答/方案在论坛帖中
        "notes":           1.0,   # 补充说明
        "examples":        0.5,
    },
}


def detect_doc_type_by_content(content: str) -> str:
    """
    根据文档内容结构自动识别 doc_type。

    阶梯式判断（优先级从高到低）：
    1. 有 procedure + (preconditions 或 halt_conditions) → skill
       （有明确操作步骤+执行约束，是技能文档的核心特征）
    2. 有 design + 无 procedure → design_system
       （有设计维度但无操作步骤，是设计系统文档）
    3. 有 decisions + (overview 为主) → forum_post
       （有决策/方案对比，论坛帖特征）
    4. 按权重计算：统计 section_type 频率，按权重加成，取最高分
    5. 兜底：无任何特征 → skill

    这种阶梯式判断避免了 blog 的 overview 累积分压制 procedure 信号的问题。
    """
    if not content or not content.strip():
        return "skill"

    _, body = split_front_matter(content)
    sections = parse_sections(body)

    # 统计 section_type 频率
    section_counts: dict[str, int] = {}
    for sec in sections:
        st = sec.get("section_type", "other")
        if st == "other":
            continue
        section_counts[st] = section_counts.get(st, 0) + 1

    if not section_counts:
        return "skill"

    # 阶梯式判断
    has_procedure = section_counts.get("procedure", 0) > 0
    has_preconditions = section_counts.get("preconditions", 0) > 0
    has_halt = section_counts.get("halt_conditions", 0) > 0
    has_design = section_counts.get("design", 0) > 0
    has_decisions = section_counts.get("decisions", 0) > 0
    has_overview = section_counts.get("overview", 0) > 0

    # 阶梯1：有操作步骤+执行约束 → skill
    if has_procedure and (has_preconditions or has_halt):
        return "skill"

    # 阶梯2：有操作步骤但无执行约束（教程类博客）→ blog
    # （这类内容 procedure 权重低，适合 blog 提取规则）

    # 阶梯3：有设计维度 + 无操作步骤 → design_system
    if has_design and not has_procedure:
        return "design_system"

    # 阶梯4：有决策对比 + overview 为主 → forum_post
    if has_decisions and has_overview and not has_procedure:
        return "forum_post"

    # 阶梯5：按权重计算（兜底）
    doc_type_scores: dict[str, float] = {}
    for doc_type, weights in _SECTION_TYPE_WEIGHTS.items():
        score = 0.0
        for section_type, count in section_counts.items():
            weight = weights.get(section_type, 0.0)
            score += weight * count
        doc_type_scores[doc_type] = score

    best = max(doc_type_scores, key=doc_type_scores.get)
    return best


def _auto_detect_doc_type(results: list[dict], console=None) -> list[dict]:
    """
    对采集结果逐一读取内容，自动识别真正的 doc_type 并覆盖。

    跳过条件：
    - 结果已明确是 rss/blog 等类型（它们的识别规则不同）
    - 没有 raw_path 或文件不存在
    """
    for r in results:
        raw_path = r.get("raw_path")
        if not raw_path:
            continue
        p = Path(raw_path)
        if not p.exists():
            continue

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        detected = detect_doc_type_by_content(content)
        original = r.get("doc_type", "skill")

        if detected != original:
            if console:
                console.print(f"  [dim]doc_type 自动识别：{original} → {detected}[/dim]")
            r["doc_type"] = detected

    return results


def _detect_kind(target: str) -> str:
    """根据 target 字符串启发式判断采集类型。"""
    import re as _re

    t = target.lower()

    # 本地路径
    if not t.startswith(("http://", "https://", "git@", "git://")):
        return "skill"

    # RSS/Atom Feed 特征
    rss_patterns = [
        r"/feed/?$", r"/rss/?$", r"/atom/?$",
        r"[?&]format=rss", r"[?&]feed=",
        r"\.xml$", r"\.rss$", r"\.atom$",
        r"/feeds?/", r"/rss2?/",
    ]
    if any(_re.search(p, t) for p in rss_patterns) or any(
        kw in t for kw in ("feed", "rss", "atom")
    ):
        return "rss"

    # 论坛特征
    forum_patterns = [
        r"reddit\.com/r/",
        r"news\.ycombinator\.com/item",
        r"/t/[^/]+/\d+",        # Discourse
        r"v2ex\.com/t/\d+",
        r"stackoverflow\.com/questions/",
        r"segmentfault\.com/q/",
    ]
    if any(_re.search(p, t) for p in forum_patterns):
        return "forum"

    # GitHub 仓库根（不含 /blob/ /raw/ ）→ skill
    if "github.com" in t and "/blob/" not in t and "/raw/" not in t:
        return "skill"

    # 任何 URL 路径末尾以 .md 结尾 → skill
    # 涵盖场景：
    #   github.com/blob/...  raw.githubusercontent.com/...  其他域名下的 .md 文件
    # 例：https://raw.githubusercontent.com/owner/repo/main/DESIGN.md
    #     https://example.com/docs/SKILL.md
    url_path = urlparse(target).path.lower().rstrip("/")
    if url_path.endswith(".md"):
        # DESIGN.md → 设计系统文档，单独路由
        if url_path.endswith("/design.md") or url_path == "design.md":
            return "design_system"
        return "skill"

    # 其余 URL → 通用网页
    return "url"
