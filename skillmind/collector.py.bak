"""采集器（Collector）- 从本地目录或 Git 仓库采集 SKILL.md 文件"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import yaml

from skillmind.config import (
    HASHES_FILE,
    RAW_DIR,
    REPOS_DIR,
    ensure_dirs,
    load_config,
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
            # 文件损坏时返回空字典，不崩溃
            return {}
    return {}


def _save_hashes(hashes: dict[str, dict]) -> None:
    """原子写：先写临时文件，再替换，防止中途崩溃导致文件损坏。"""
    tmp = HASHES_FILE.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.dump(hashes, f, allow_unicode=True)
    tmp.replace(HASHES_FILE)  # os.replace 语义，原子操作


def _safe_copy(src: Path, dest: Path) -> None:
    """
    安全复制文件：以二进制模式读写，避免编码转换导致内容损坏。
    同时处理 Windows 长路径（> 260 字符）问题。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dest)
    except OSError:
        # Windows 长路径兜底：直接读写二进制
        dest.write_bytes(src.read_bytes())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 本地目录扫描
# ---------------------------------------------------------------------------

def scan_local(base_dir: Path) -> Iterator[Path]:
    """递归扫描目录，返回所有 SKILL.md / *.skill.md 文件路径。"""
    patterns = ["SKILL.md", "*.skill.md", "skill.md"]
    seen: set[Path] = set()
    for pattern in patterns:
        for p in base_dir.rglob(pattern):
            if p not in seen:
                seen.add(p)
                yield p


# ---------------------------------------------------------------------------
# Git 克隆 / 拉取
# ---------------------------------------------------------------------------

def _slug_from_url(url: str) -> str:
    parsed = urlparse(url)
    slug = parsed.path.strip("/").replace("/", "__")
    return slug or "unknown"


def _parse_github_url(url: str) -> tuple[str, str | None]:
    """
    解析 GitHub URL，拆分出仓库根 URL 和子目录路径。

    支持以下格式：
      - https://github.com/owner/repo                      → repo_url, None
      - https://github.com/owner/repo/tree/main/some/dir  → repo_url, "some/dir"
      - https://github.com/owner/repo/blob/main/SKILL.md  → repo_url, None（单文件忽略子目录）

    返回: (clone_url, subdir_or_None)
    """
    parsed = urlparse(url)
    if parsed.netloc not in ("github.com", "www.github.com"):
        return url, None

    parts = parsed.path.strip("/").split("/")
    # 至少需要 owner/repo
    if len(parts) < 2:
        return url, None

    repo_root_url = f"https://github.com/{parts[0]}/{parts[1]}"

    # /tree/<branch>/sub/dir  →  subdir = sub/dir
    if len(parts) >= 4 and parts[2] == "tree":
        subdir = "/".join(parts[4:]) if len(parts) > 4 else None
        return repo_root_url, subdir or None

    # /blob/...  直接忽略子路径，克隆整个仓库
    return repo_root_url, None


def clone_or_pull(repo_url: str) -> Path:
    """克隆（首次）或拉取（已存在）远程 Git 仓库，返回本地路径。"""
    try:
        import git as gitpython  # type: ignore
    except ImportError:
        raise RuntimeError("请先安装 gitpython: pip install gitpython")

    slug = _slug_from_url(repo_url)
    local_path = REPOS_DIR / slug

    if local_path.exists():
        repo = gitpython.Repo(local_path)
        repo.remotes.origin.pull()
    else:
        gitpython.Repo.clone_from(repo_url, local_path, depth=1)

    return local_path


# ---------------------------------------------------------------------------
# 核心采集入口
# ---------------------------------------------------------------------------

def ingest(source: str, console=None) -> list[dict]:
    """
    采集入口：支持本地路径或 Git URL。

    返回已采集文件信息列表，每项包含:
      - raw_path: 缓存后的本地路径
      - source_path: 原始路径（相对于仓库根或绝对路径）
      - source_repo: 仓库 URL 或本地绝对路径
      - source_hash: SHA256
      - fetch_time: Unix 时间戳
      - skipped: 是否因哈希重复而跳过
    """
    ensure_dirs()
    hashes = _load_hashes()
    results: list[dict] = []

    # 判断是否为 URL
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https", "git"):
        # 解析 GitHub 子目录 URL
        clone_url, subdir = _parse_github_url(source)

        if console and clone_url != source:
            console.print(f"[dim]检测到子目录 URL，仓库地址:[/dim] {clone_url}")
            if subdir:
                console.print(f"[dim]仅扫描子目录:[/dim] {subdir}")

        if console:
            console.print(f"[cyan]克隆仓库:[/cyan] {clone_url}")
        base_dir = clone_or_pull(clone_url)
        source_repo = source  # 保留原始 URL 以便溯源
        commit_sha = _get_commit_sha(base_dir)

        # 若有子目录，则只扫描该子目录
        if subdir:
            scan_dir = base_dir / subdir
            if not scan_dir.exists():
                raise FileNotFoundError(
                    f"子目录不存在于仓库中: {subdir}\n"
                    f"请检查路径是否正确: {source}"
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

    hashes_dirty = False  # 标记是否有新增，批量写一次

    for skill_file in skill_files:
        sha = _sha256(skill_file)
        skipped = sha in hashes

        # 计算相对路径，统一用正斜杠（保证跨平台一致，且可直接拼接 GitHub URL）
        try:
            rel_path = skill_file.relative_to(base_dir).as_posix()
        except ValueError:
            rel_path = skill_file.as_posix()

        if not skipped:
            # 复制到 raw 缓存区
            raw_dest = RAW_DIR / sha[:2] / f"{sha}.md"
            _safe_copy(skill_file, raw_dest)

            hashes[sha] = {
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
            raw_dest = Path(hashes[sha]["raw_path"])
            # 缓存文件被手动删除时，重新复制并标记 dirty
            if not raw_dest.exists():
                _safe_copy(skill_file, raw_dest)
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
            "skipped": skipped,
        })

    # 批量写一次，避免频繁 IO
    if hashes_dirty:
        _save_hashes(hashes)

    return results


def list_cached() -> list[dict]:
    """返回所有已缓存的文件信息列表。"""
    hashes = _load_hashes()
    result = []
    for sha, info in hashes.items():
        info = info.copy()
        info["source_hash"] = sha
        result.append(info)
    return result


def _get_commit_sha(repo_path: Path) -> str | None:
    try:
        import git as gitpython  # type: ignore
        repo = gitpython.Repo(repo_path)
        return repo.head.commit.hexsha[:8]
    except Exception:
        return None
