"""SkillMind CLI 入口 - 基于 Typer + Rich  (v2.2)"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

app = typer.Typer(
    name="skillmind",
    help="🧠 SkillMind - 全源知识提炼系统（Skill / 博客 / 论坛 → Obsidian）",
    add_completion=False,
    rich_markup_mode="rich",
)

# ingest 子命令组
ingest_app = typer.Typer(
    name="ingest",
    help="📥 采集知识来源（skill / rss / url / forum）",
    add_completion=False,
    rich_markup_mode="rich",
)
app.add_typer(ingest_app, name="ingest")

console = Console()


# ---------------------------------------------------------------------------
# ingest skill
# ---------------------------------------------------------------------------

@ingest_app.command("skill")
def ingest_skill_cmd(
    source: str = typer.Argument(..., help="本地目录路径或 Git 仓库 URL（支持 GitHub 子目录）"),
):
    """📋 采集本地目录或 Git 仓库中的 SKILL.md 文件"""
    from skillmind.collector import ingest_skill

    try:
        results = ingest_skill(source, console=console)
    except Exception as e:
        console.print(f"[bold red]采集失败:[/bold red] {e}")
        raise typer.Exit(1)

    new_count = sum(1 for r in results if not r["skipped"])
    skip_count = sum(1 for r in results if r["skipped"])
    console.print(
        Panel(
            f"✅ 采集完成\n"
            f"  新增: [green]{new_count}[/green]　跳过: [yellow]{skip_count}[/yellow]　合计: {len(results)}",
            title="[bold]Skill 采集结果[/bold]", border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# ingest rss
# ---------------------------------------------------------------------------

@ingest_app.command("rss")
def ingest_rss_cmd(
    feed_url: str = typer.Argument(..., help="RSS / Atom Feed URL"),
    max_items: int = typer.Option(50, "--max", "-n", help="最多抓取条目数"),
):
    """📡 订阅博客 RSS Feed，批量抓取文章正文"""
    from skillmind.collector import ingest_rss

    try:
        results = ingest_rss(feed_url, console=console, max_items=max_items)
    except Exception as e:
        console.print(f"[bold red]采集失败:[/bold red] {e}")
        raise typer.Exit(1)

    new_count = sum(1 for r in results if not r["skipped"])
    skip_count = sum(1 for r in results if r["skipped"])
    console.print(
        Panel(
            f"✅ RSS 采集完成\n"
            f"  新增: [green]{new_count}[/green]　跳过: [yellow]{skip_count}[/yellow]　合计: {len(results)}",
            title="[bold]RSS 采集结果[/bold]", border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# ingest url
# ---------------------------------------------------------------------------

@ingest_app.command("url")
def ingest_url_cmd(
    article_url: str = typer.Argument(..., help="单篇文章或博客 URL"),
):
    """🔗 抓取单篇文章 / 博客页面正文"""
    from skillmind.collector import ingest_url

    try:
        results = ingest_url(article_url, console=console)
    except Exception as e:
        console.print(f"[bold red]采集失败:[/bold red] {e}")
        raise typer.Exit(1)

    if results:
        console.print(f"[green]✓ 已缓存:[/green] {results[0].get('title', article_url)[:60]}")
    else:
        console.print("[yellow]未能获取有效内容[/yellow]")


# ---------------------------------------------------------------------------
# ingest forum
# ---------------------------------------------------------------------------

@ingest_app.command("forum")
def ingest_forum_cmd(
    topic_url: str = typer.Argument(..., help="论坛主题帖 URL（支持 Discourse / Reddit / HN）"),
):
    """💬 采集论坛主题帖（Discourse API 优先，降级通用抓取）"""
    from skillmind.collector import ingest_forum

    try:
        results = ingest_forum(topic_url, console=console)
    except Exception as e:
        console.print(f"[bold red]采集失败:[/bold red] {e}")
        raise typer.Exit(1)

    if results:
        console.print(f"[green]✓ 已缓存:[/green] {results[0].get('title', topic_url)[:60]}")
    else:
        console.print("[yellow]未能获取有效内容[/yellow]")


# ---------------------------------------------------------------------------
# ingest file（本地 HTML / TXT）
# ---------------------------------------------------------------------------

@ingest_app.command("file")
def ingest_file_cmd(
    file_path: str = typer.Argument(..., help="本地文件路径（支持 .html / .htm / .mhtml / .txt）"),
    doc_type: Optional[str] = typer.Option(None, "--type", "-t", help="指定文档类型: blog / webpage / skill（默认自动识别）"),
):
    """📄 采集本地 HTML / TXT 文件，转换为 Markdown 后入库"""
    from skillmind.collector import ingest_local_file

    dt = doc_type or "webpage"
    try:
        results = ingest_local_file(file_path, console=console, doc_type=dt)
    except Exception as e:
        console.print(f"[bold red]采集失败:[/bold red] {e}")
        raise typer.Exit(1)

    if results:
        console.print(f"[green]✓ 已缓存:[/green] {results[0].get('title', file_path)[:60]}")
    else:
        console.print("[yellow]未能获取有效内容[/yellow]")


# ---------------------------------------------------------------------------
# ingest auto
# ---------------------------------------------------------------------------

@ingest_app.command("auto")
def ingest_auto_cmd(
    target: str = typer.Argument(..., help="本地路径、Git 仓库 URL、文章 URL、RSS Feed URL 等"),
    kind: Optional[str] = typer.Option(None, "--type", "-t", help="强制指定类型：skill / rss / url / forum"),
    max_items: int = typer.Option(50, "--max", "-n", help="RSS 模式最多抓取条目数"),
):
    """🤖 自动识别来源类型并采集（skill / rss / url / forum 智能路由）"""
    from skillmind.collector import ingest_auto

    try:
        detected_kind, results = ingest_auto(
            target, kind_override=kind, max_items=max_items, console=console
        )
    except Exception as e:
        console.print(f"[bold red]采集失败:[/bold red] {e}")
        raise typer.Exit(1)

    kind_label = {
        "skill":      "📋 skill",
        "rss":        "📡 rss",
        "url":        "🔗 url",
        "forum":      "💬 forum",
        "local_file": "📄 local file",
    }.get(detected_kind, detected_kind)
    console.print(f"  [dim]识别类型:[/dim] {kind_label}")

    if results:
        for r in results[:3]:
            console.print(f"  [green]✓ 已缓存:[/green] {r.get('title', target)[:60]}")
        if len(results) > 3:
            console.print(f"  [dim]... 共 {len(results)} 条[/dim]")
    else:
        console.print("[yellow]未能获取有效内容[/yellow]")


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

@app.command()
@app.command()
def extract(
    skill: Optional[str] = typer.Option(None, "--skill", "-s", help="指定 source_hash 前缀或路径关键词"),
    doc_type: Optional[str] = typer.Option(None, "--type", "-t", help="只处理指定类型: skill / blog / forum_post / webpage"),
    auto_approve: bool = typer.Option(False, "--auto-approve", help="提取后自动批准草稿"),
    rerun_all: bool = typer.Option(False, "--all", help="强制重新提取所有文档（忽略 extract_cache）"),
):
    """🔬 对缓存内的文档执行 LLM 知识提取，生成草稿（默认只处理未提取过的新文档）"""
    from skillmind.collector import list_cached
    from skillmind.extractor import extract_skill as _extract, _extract_cache_path
    from skillmind.config import load_config, get_vault_dir, CURRENT_PROMPT_VERSION

    cfg = load_config()
    cached = list_cached()

    if not cached:
        console.print("[yellow]缓存区为空，请先执行 skillmind ingest ...[/yellow]")
        raise typer.Exit(0)

    # 过滤
    if skill:
        cached = [
            c for c in cached
            if skill in c.get("source_hash", "") or skill in c.get("source_path", "")
        ]
    if doc_type:
        cached = [c for c in cached if c.get("doc_type", "skill") == doc_type]

    if not cached:
        console.print(f"[red]未找到匹配的缓存文件[/red]")
        raise typer.Exit(1)

    # 默认只处理没有当前版本 extract_cache 的文档
    if not rerun_all and not skill:
        pending = [
            c for c in cached
            if not _extract_cache_path(c.get("source_hash", ""), CURRENT_PROMPT_VERSION).exists()
        ]
        skipped = len(cached) - len(pending)
        if skipped:
            console.print(f"[dim]已跳过 {skipped} 个文档（extract_cache 命中），如需重跑全部请加 --all[/dim]")
        cached = pending

    if not cached:
        console.print("[green]✓ 所有文档已是最新提取结果，无需重新提取[/green]")
        raise typer.Exit(0)

    console.print(f"[cyan]共 {len(cached)} 个文件待提取[/cyan]")
    qpm = cfg.get("llm", {}).get("qpm", 10)
    interval = 60.0 / max(qpm, 1)
    last_request_time: float = 0.0
    success = 0
    fail = 0
    total_cards = 0

    for i, info in enumerate(cached):
        if last_request_time > 0:
            elapsed = time.monotonic() - last_request_time
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)
        last_request_time = time.monotonic()

        label = info.get("source_path") or info.get("source_hash", "")[:16]
        dtype = info.get("doc_type", "skill")
        console.print(f"\n[bold][{i+1}/{len(cached)}][/bold] [{dtype}] {label[:60]}")

        try:
            raw_path = info.get("raw_path", "")
            if not raw_path or not Path(raw_path).exists():
                console.print(f"  [red]✗ 缓存文件不存在，请重新 ingest:[/red] {label[:50]}")
                fail += 1
                continue
            drafts = _extract(raw_path=raw_path, source_info=info, cfg=cfg, console=console)
            total_cards += len(drafts)
            if auto_approve:
                from skillmind.reviewer import save_draft
                for d in drafts:
                    d["status"] = "approved"
                    save_draft(d)
            for d in drafts:
                console.print(f"  [green]✓ 草稿:[/green] {d['uuid']} - {d['meta'].get('name','')[:40]}")
            success += 1
        except Exception as e:
            console.print(f"  [red]✗ 失败:[/red] {e}")
            fail += 1

    vault_path = get_vault_dir(cfg)
    console.print(
        Panel(
            f"提取完成: 成功 [green]{success}[/green] 个文件 / 失败 [red]{fail}[/red]\n"
            f"共生成笔记: [magenta]{total_cards}[/magenta] 张（含一文多卡）\n\n"
            f"📁 草稿: [dim]~/.skillmind/drafts/[/dim]\n"
            f"📖 Vault: [cyan]{vault_path}[/cyan]\n\n"
            f"[bold]下一步:[/bold]  skillmind review  →  skillmind publish --all",
            title="[bold]提取结果[/bold]", border_style="cyan",
        )
    )


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@app.command()
def review(
    status: Optional[str] = typer.Option("draft", "--status", help="筛选状态: draft / approved / published / all"),
):
    """📋 列出草稿，展示来源类型与可信度"""
    from skillmind.reviewer import list_drafts

    filter_status = None if status == "all" else status
    drafts = list_drafts(status=filter_status)

    if not drafts:
        console.print(f"[yellow]没有状态为 '{status}' 的草稿[/yellow]")
        return

    table = Table(title=f"草稿列表 (status={status})", box=box.ROUNDED, show_lines=True)
    table.add_column("UUID", style="dim", width=14)
    table.add_column("名称", min_width=18)
    table.add_column("类型", width=8)
    table.add_column("可信度", width=10)
    table.add_column("时效性", width=10)
    table.add_column("状态", width=8)
    table.add_column("来源", width=28)
    table.add_column("日期", width=11)

    _rel_style = {"high": "green", "medium": "yellow", "low": "red"}
    _obs_style = {"low": "green", "medium": "yellow", "high": "red"}

    for d in drafts:
        meta = d.get("meta", {})
        source = d.get("source", {})
        uid = d.get("uuid", "")[-12:]
        name = meta.get("name", "?")[:30]
        dtype = source.get("doc_type", d.get("doc_type", "skill"))[:6]
        rel = d.get("source_reliability", "medium")
        obs = d.get("obsolescence_risk", "medium")
        st = d.get("status", "draft")
        src_path = (source.get("file_path") or source.get("source_path") or source.get("source_url", ""))[:28]
        created = d.get("created_at", "")

        status_color = {"draft": "yellow", "approved": "cyan", "published": "green"}.get(st, "white")
        table.add_row(
            uid, name, dtype,
            f"[{_rel_style.get(rel,'white')}]{rel}[/]",
            f"[{_obs_style.get(obs,'white')}]{obs}[/]",
            f"[{status_color}]{st}[/]",
            src_path, created,
        )

    console.print(table)
    console.print(f"\n共 [bold]{len(drafts)}[/bold] 条。"
                  f"  [bold]skillmind edit <uuid>[/bold] 编辑  "
                  f"  [bold]skillmind publish <uuid|--all>[/bold] 发布")


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

@app.command()
def edit(
    uid: str = typer.Argument(..., help="草稿 UUID（支持前缀）"),
):
    """✏️  使用系统编辑器手动修改草稿"""
    from skillmind.reviewer import open_in_editor, get_draft

    draft = get_draft(uid)
    if not draft:
        console.print(f"[red]未找到草稿: {uid}[/red]")
        raise typer.Exit(1)

    console.print(f"[cyan]打开草稿:[/cyan] {draft['uuid']}")
    open_in_editor(draft["uuid"])
    console.print("[green]编辑完成[/green]")


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------

@app.command()
def publish(
    uid: Optional[str] = typer.Argument(None, help="草稿 UUID"),
    all_drafts: bool = typer.Option(False, "--all", help="发布所有 draft/approved 状态草稿"),
    output_dir: Optional[str] = typer.Option(None, "--output-dir", "-o", help="指定输出目录（绝对路径或相对于 vault/skills/ 的子目录）"),
    prefix: Optional[str] = typer.Option(None, "--prefix", "-p", help="文件名前缀，如 SM_ 或 技能_"),
):
    """🚀 将审核通过的草稿发布到 Obsidian Vault"""
    from skillmind.reviewer import list_drafts, get_draft, save_draft
    from skillmind.renderer import publish_to_vault
    from skillmind.config import load_config

    cfg = load_config()

    if all_drafts:
        targets = [d for d in list_drafts() if d.get("status") in ("draft", "approved")]
    elif uid:
        draft = get_draft(uid)
        if not draft:
            console.print(f"[red]未找到草稿: {uid}[/red]")
            raise typer.Exit(1)
        targets = [draft]
    else:
        console.print("[yellow]请指定 uuid 或使用 --all[/yellow]")
        raise typer.Exit(1)

    if not targets:
        console.print("[yellow]没有待发布的草稿[/yellow]")
        return

    success = 0
    for draft in targets:
        try:
            draft["status"] = "published"
            dest = publish_to_vault(draft, cfg=cfg, output_dir=output_dir, prefix=prefix)
            save_draft(draft)
            console.print(f"[green]✓ 已发布:[/green] {draft.get('meta',{}).get('name','')} → {dest}")
            success += 1
        except Exception as e:
            console.print(f"[red]✗ 发布失败:[/red] {draft.get('uuid','')} → {e}")

    console.print(f"\n[bold green]发布完成: {success}/{len(targets)}[/bold green]")

    try:
        from skillmind.search import index_vault
        index_vault(console=console)
    except RuntimeError:
        pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@app.command()
def search(
    query: str = typer.Argument(..., help="搜索关键词（支持自然语言）"),
    top_k: int = typer.Option(5, "--top-k", "-k", help="返回结果数量"),
):
    """🔍 在知识库中进行语义搜索（需安装 chromadb）"""
    from skillmind.search import semantic_search

    try:
        results = semantic_search(query, top_k=top_k)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if not results:
        console.print("[yellow]未找到相关结果[/yellow]")
        return

    table = Table(title=f'搜索: "{query}"', box=box.ROUNDED, show_lines=True)
    table.add_column("排名", width=4)
    table.add_column("UUID", style="dim", width=14)
    table.add_column("相关度", width=8)
    table.add_column("摘要", min_width=40)
    table.add_column("文件路径")

    for i, r in enumerate(results, 1):
        table.add_row(str(i), r["uuid"][-12:], f"{r['score']:.3f}", r["snippet"][:60] + "...", r["file"])

    console.print(table)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

cache_app = typer.Typer(name="cache", help="🗂️ 缓存管理（孤儿清理 / 统计 / 全清）", invoke_without_command=True)
app.add_typer(cache_app, name="cache")


@cache_app.command("clean")
def cache_clean_cmd(
    orphans: bool = typer.Option(False, "--orphans", help="清理孤儿条目（hashes.yaml 有记录但 raw 文件已丢失）"),
    hash_prefix: Optional[str] = typer.Option(None, "--hash", help="清理指定 source_hash（前缀匹配）"),
    all_caches: bool = typer.Option(False, "--all", help="⚠️  全清所有缓存（raw + extract + drafts + hashes.yaml）"),
    stale: bool = typer.Option(False, "--stale", help="清理旧命名格式的 extract_cache 文件（*_structure.json 等）"),
    repos: bool = typer.Option(False, "--repos", help="同时清理克隆的 Git 仓库（--all 时也不默认清）"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认提示"),
):
    """🧹 清理缓存：孤儿条目 / 指定 hash / 旧格式文件 / 全清"""
    from skillmind.cache_admin import (
        cleanup_orphans, cleanup_source, wipe_all_caches, wipe_repos,
        find_orphan_entries, cache_stats,
    )
    from skillmind.collector import _load_hashes
    from skillmind.extractor import clean_stale_extract_caches

    if not orphans and not hash_prefix and not all_caches and not stale:
        # 默认：显示孤儿列表
        orphan_list = find_orphan_entries()
        if not orphan_list:
            console.print("[green]✓ 没有孤儿缓存条目[/green]")
        else:
            console.print(f"[yellow]发现 {len(orphan_list)} 个孤儿条目（hashes.yaml 有记录但 raw 文件不存在）：[/yellow]")
            for o in orphan_list:
                console.print(f"  [dim]{o['source_hash'][:16]}[/dim]  {o.get('title','')[:60]}")
            console.print(f"\n运行 [bold]skillmind cache clean --orphans[/bold] 删除这些条目")
        return

    if stale:
        n = clean_stale_extract_caches(console=console)
        if n:
            console.print(f"[green]✓ 已删除 {n} 个旧格式 extract_cache 文件[/green]")
        else:
            console.print("[green]✓ 没有旧格式 extract_cache 文件[/green]")
        return

    if all_caches:
        if not yes:
            confirm = typer.confirm("⚠️  将清除全部 raw / extract / drafts / hashes.yaml，确认？")
            if not confirm:
                raise typer.Abort()
        stats = wipe_all_caches(console=console)
        console.print(f"[bold green]全清完成：[/bold green]raw {stats['raw']} / extract {stats['extract']} / drafts {stats['drafts']}")
        if repos:
            r = wipe_repos(console=console)
            console.print(f"[green]克隆仓库已清理：{r['removed']} 个[/green]")
        return

    if orphans:
        orphan_list = find_orphan_entries()
        if not orphan_list:
            console.print("[green]✓ 没有孤儿缓存条目[/green]")
            return
        console.print(f"[yellow]清理 {len(orphan_list)} 个孤儿条目...[/yellow]")
        result = cleanup_orphans(console=console)
        console.print(f"[green]✓ 已清理 {result['removed']} 个孤儿条目[/green]")
        return

    if hash_prefix:
        hashes = _load_hashes()
        matched = [sha for sha in hashes if sha.startswith(hash_prefix)]
        if not matched:
            console.print(f"[red]未找到匹配的 hash：{hash_prefix}[/red]")
            raise typer.Exit(1)
        if len(matched) > 1 and not yes:
            console.print(f"[yellow]匹配到 {len(matched)} 个 hash：[/yellow]")
            for sha in matched:
                console.print(f"  {sha[:16]}  {hashes[sha].get('title','')[:50]}")
            confirm = typer.confirm("确认全部清理？")
            if not confirm:
                raise typer.Abort()
        for sha in matched:
            cleanup_source(sha, console=console)
        console.print(f"[green]✓ 已清理 {len(matched)} 个 hash[/green]")


@cache_app.command("stats")
def cache_stats_cmd():
    """📊 显示缓存统计（各层文件数 / 占用大小 / 孤儿数）"""
    from skillmind.cache_admin import cache_stats
    from rich.table import Table
    from rich import box

    s = cache_stats()

    def fmt_bytes(b: int) -> str:
        if b < 1024:
            return f"{b} B"
        if b < 1024 ** 2:
            return f"{b/1024:.1f} KB"
        return f"{b/1024**2:.1f} MB"

    table = Table(title="缓存统计", box=box.ROUNDED)
    table.add_column("项目", style="bold cyan")
    table.add_column("数量 / 大小", justify="right")
    table.add_row("hashes.yaml 总条目", str(s["hashes_total"]))
    table.add_row("  └ 活跃（未发布）", str(s["hashes_active"]))
    table.add_row("  └ 已发布", str(s["hashes_published"]))
    table.add_row("  └ ⚠️  孤儿（raw 缺失）", f"[yellow]{s['hashes_orphan']}[/yellow]" if s["hashes_orphan"] else "0")
    table.add_row("raw 文件", f"{s['raw_files']}  ({fmt_bytes(s['raw_bytes'])})")
    table.add_row("extract_cache 文件", f"{s['extract_files']}  ({fmt_bytes(s['extract_bytes'])})")
    table.add_row("drafts 文件", str(s["drafts_total"]))
    table.add_row("克隆仓库", str(s["repos_clones"]))
    console.print(table)

    if s["hashes_orphan"]:
        console.print(f"\n[yellow]💡 有 {s['hashes_orphan']} 个孤儿条目，运行:[/yellow] skillmind cache clean --orphans")


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

@app.command()
def sync():
    """🔄 重新扫描 Vault，更新向量索引"""
    from skillmind.search import index_vault

    try:
        with console.status("[cyan]正在扫描 Vault 并重建索引...[/cyan]"):
            count = index_vault(console=console)
        console.print(f"[green]✓ 同步完成，共索引 {count} 个技能卡片[/green]")
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------

@app.command()
def trace(
    uid: str = typer.Argument(..., help="草稿或已发布笔记的 UUID（支持前缀）"),
):
    """🔎 溯源：打开笔记对应的原始网页或本地文件"""
    from skillmind.reviewer import get_draft

    draft = get_draft(uid)
    if not draft:
        console.print(f"[red]未找到草稿: {uid}[/red]")
        raise typer.Exit(1)

    source = draft.get("source", {})
    source_url = source.get("source_url", "") or source.get("repo_url", "")
    doc_type = source.get("doc_type", "skill")

    if source_url and doc_type in ("blog", "forum_post", "webpage"):
        console.print(f"[cyan]打开原文:[/cyan] {source_url}")
        _open_url(source_url)
        return

    # Git 仓库来源：拼接 blob URL
    repo_url = source.get("repo_url", "")
    file_path = source.get("file_path", "") or source.get("source_path", "")
    if repo_url and file_path:
        branch = source.get("branch", "main")
        file_path_posix = file_path.replace("\\", "/")
        url = f"{repo_url.rstrip('/')}/blob/{branch}/{file_path_posix}"
        console.print(f"[cyan]打开原始 Skill:[/cyan] {url}")
        _open_url(url)
        return

    # 本地文件
    raw_path = source.get("raw_path", "")
    if raw_path and Path(raw_path).exists():
        console.print(f"[cyan]打开本地文件:[/cyan] {raw_path}")
        _open_local(raw_path)
        return

    console.print("[yellow]未能找到有效的来源链接[/yellow]")


def _open_url(url: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.call(["open", url])
        else:
            subprocess.call(["xdg-open", url])
    except Exception:
        console.print(f"[yellow]无法自动打开链接，请手动访问:[/yellow] {url}")


def _open_local(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.call(["open", path])
        else:
            subprocess.call(["xdg-open", path])
    except Exception:
        console.print(f"[yellow]无法自动打开文件，请手动访问:[/yellow] {path}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status():
    """📊 展示知识库统计信息"""
    from skillmind.reviewer import list_drafts
    from skillmind.collector import list_cached
    from skillmind.config import load_config, CURRENT_PROMPT_VERSION, get_vault_dir

    cfg = load_config()
    cached = list_cached()
    all_drafts = list_drafts()

    draft_count = sum(1 for d in all_drafts if d.get("status") == "draft")
    published_count = sum(1 for d in all_drafts if d.get("status") == "published")
    approved_count = sum(1 for d in all_drafts if d.get("status") == "approved")
    old_prompt = [d for d in all_drafts if d.get("status") == "published"
                  and not d.get("prompt_version", "").startswith(CURRENT_PROMPT_VERSION)]

    # 按 doc_type 统计缓存
    type_counts: dict[str, int] = {}
    for c in cached:
        t = c.get("doc_type", "skill")
        type_counts[t] = type_counts.get(t, 0) + 1

    vault_dir = get_vault_dir(cfg)
    table = Table(title="SkillMind 状态概览", box=box.ROUNDED)
    table.add_column("指标", style="bold")
    table.add_column("值", justify="right")

    table.add_row("已缓存原始文件", str(len(cached)))
    for dtype, cnt in sorted(type_counts.items()):
        table.add_row(f"  └ {dtype}", str(cnt))
    table.add_row("草稿（待审核）", f"[yellow]{draft_count}[/yellow]")
    table.add_row("已批准（待发布）", f"[cyan]{approved_count}[/cyan]")
    table.add_row("已发布", f"[green]{published_count}[/green]")
    table.add_row("旧版 Prompt 笔记", f"[red]{len(old_prompt)}[/red]" if old_prompt else "0")
    table.add_row("Vault 路径", str(vault_dir))
    table.add_row("当前 Prompt 版本", CURRENT_PROMPT_VERSION)
    table.add_row("LLM 模型", cfg.get("llm", {}).get("model", "未配置"))

    console.print(table)
    if old_prompt:
        console.print(f"\n[yellow]⚠️  有 {len(old_prompt)} 个笔记使用旧版 Prompt，建议重新执行 extract[/yellow]")


# ---------------------------------------------------------------------------
# update --check
# ---------------------------------------------------------------------------

@app.command()
def update(
    check: bool = typer.Option(False, "--check", help="检查上游 Skill 仓库是否有更新"),
):
    """🔃 检查上游 Skill 仓库是否有更新"""
    if not check:
        console.print("[yellow]请使用 --check 参数[/yellow]")
        return

    from skillmind.collector import list_cached, _get_commit_sha
    from skillmind.config import REPOS_DIR

    cached = list_cached()
    repos_seen: dict[str, str] = {}
    for info in cached:
        repo = info.get("source_repo", "")
        if repo.startswith("http") and repo not in repos_seen:
            repos_seen[repo] = info.get("commit_sha", "") or ""

    if not repos_seen:
        console.print("[yellow]没有通过 Git URL 采集的仓库[/yellow]")
        return

    for repo_url, old_sha in repos_seen.items():
        console.print(f"[cyan]检查:[/cyan] {repo_url}")
        try:
            import git as gitpython  # type: ignore
            from skillmind.collector import _slug_from_url
            local_path = REPOS_DIR / _slug_from_url(repo_url)
            if local_path.exists():
                g_repo = gitpython.Repo(local_path)
                g_repo.remotes.origin.fetch()
                refs = g_repo.remotes.origin.refs
                if not refs:
                    console.print(f"  [yellow]⚠ 无法获取远程 refs[/yellow]")
                    continue
                new_sha = refs[0].commit.hexsha[:8]
                if new_sha != old_sha:
                    console.print(f"  [yellow]⚠ 有更新:[/yellow] {old_sha} → {new_sha}，建议重新 ingest + extract")
                else:
                    console.print(f"  [green]✓ 已是最新[/green] ({new_sha})")
        except Exception as e:
            console.print(f"  [red]检查失败:[/red] {e}")


# ---------------------------------------------------------------------------
# audit — 覆盖率审计
# ---------------------------------------------------------------------------

@app.command()
def audit(
    source: str = typer.Argument(..., help="source_hash 前缀（≥ 7 位）或路径关键词"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="写 JSON 结果到文件（默认精简:覆盖率+verdict+缺失项）"),
    json_output: bool = typer.Option(False, "--json", help="以 JSON 格式打印精简 AuditReport"),
    json_detailed: bool = typer.Option(False, "--json-detailed", help="以 JSON 格式打印完整 AuditReport(含 items / verify 详细)"),
    max_extracts: int = typer.Option(20, "--max-extracts", help="最多审计的提取笔记数（一文多卡时）"),
    vault_skills: Optional[str] = typer.Option(None, "--vault-skills", help="覆盖 vault skills 目录路径"),
):
    """🔬 覆盖率审计:原文 vs vault 提取卡片(逐条 verbatim 对齐)

    流程:
        Pass 1 — LLM 从原文按章节抽出可验证条目(items)
        Pass 2 — LLM 对每条 vs 抽取卡片,标 complete / weak / missing + verbatim
        规则 — 数字 / wikilink 反查(幻觉)+ 跨卡重复(多卡时)

    verdict:coverage ≥ 90% → PASS(自动放行) / < 90% → FAIL(需人工 review)

    示例:
      skillmind audit a1b2c3d                  # 终端 summary
      skillmind audit brandkit --json          # 精简 JSON
      skillmind audit a1b2c3d -o result.json   # 写精简 JSON
      skillmind audit brandkit --json-detailed # 完整 JSON(调试用)
    """
    from skillmind.auditor import audit_source
    from skillmind.config import load_config

    cfg = load_config()

    try:
        with console.status(f"[cyan]正在审计 {source}...[/cyan]", spinner="dots"):
            report = audit_source(
                source, cfg, console=console,
                max_extract_files=max_extracts,
                vault_skills_override=vault_skills,
            )
    except Exception as e:
        console.print(f"[red]✗ 审计失败:[/red] {e}")
        raise typer.Exit(1)

    # ---- 写文件 ----
    if output:
        import json as _json
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            _json.dumps(report.to_summary_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.print(f"[green]✓ JSON 已写入:[/green] {out_path}")
        return

    # ---- 终端 JSON 完整输出 ----
    if json_detailed:
        import json as _json
        console.print(_json.dumps(report.to_detailed_dict(), ensure_ascii=False, indent=2))
        return

    if json_output:
        import json as _json
        console.print(_json.dumps(report.to_summary_dict(), ensure_ascii=False, indent=2))
        return

    # ---- 终端 summary ----
    verdict_label = {
        "PASS": "[bold green]✅ PASS[/bold green]",
        "FAIL": "[bold red]❌ FAIL[/bold red]",
    }.get(report.verdict, report.verdict)
    color = {"PASS": "green", "FAIL": "red"}.get(report.verdict, "white")

    summary = (
        f"原文: {report.source_title[:60]}\n"
        f"extract 卡片: {len(report.extract_files)} 张  原文: {report.original_chars:,} 字符  hash: {report.source_hash[:12]}...\n"
        f"Pass 1: {report.pass1_elapsed:.0f}s  Pass 2: {report.pass2_elapsed:.0f}s\n\n"
        f"覆盖率: [bold {color}]{report.coverage_weighted:.1f}%[/bold {color}]  "
        f"missing: {len(report.missing)} 条\n"
        f"verdict: {verdict_label}\n"
        f"needs_human_review: {report.needs_human_review}"
    )
    console.print(Panel(summary, title="[bold]覆盖率审计[/bold]", border_style=color))

    if report.missing:
        console.print(f"\n[bold red]缺失项 ({len(report.missing)}):[/bold red]")
        for i, m in enumerate(report.missing[:10], 1):
            sec = m.get("section", "?")
            stmt = m.get("statement", "?")
            reason = m.get("reason", "")
            console.print(f"  {i}. [dim]\\[{sec}\\[/dim]] {stmt[:80]}")
            if reason:
                console.print(f"     [dim]原因:{reason[:100]}[/dim]")
        if len(report.missing) > 10:
            console.print(f"  ... 还有 {len(report.missing) - 10} 条")

    console.print(f"\n[dim]精简 JSON 加 --json / 写文件 -o result.json / 详细 --json-detailed[/dim]")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@app.command(name="config")
def config_cmd(
    auth: Optional[str] = typer.Option(None, "--auth", help="认证模式: api_key | claude_code_max"),
    provider: Optional[str] = typer.Option(None, "--provider", help="provider: anthropic / openai / deepseek 等"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="API Key"),
    api_key_env: Optional[str] = typer.Option(None, "--api-key-env", help="从环境变量读取 Key 的变量名"),
    api_base: Optional[str] = typer.Option(None, "--api-base", help="自定义 API 地址"),
    model: Optional[str] = typer.Option(None, "--model", help="LLM 模型，如 anthropic/claude-3-5-haiku-20241022"),
    vault: Optional[str] = typer.Option(None, "--vault", help="Obsidian Vault 目录"),
    output_prefix: Optional[str] = typer.Option(None, "--prefix", help="默认输出文件名前缀，如 SM_ 或 技能_"),
    qpm: Optional[int] = typer.Option(None, "--qpm", help="每分钟 LLM 请求数限制"),
    profile: Optional[str] = typer.Option(None, "--profile", help="为指定指令设置独立 LLM（如 extract / audit），需配合 --model 使用"),
    show: bool = typer.Option(False, "--show", help="查看当前配置"),
    list_providers: bool = typer.Option(False, "--list-providers", help="列出所有已配置的 provider"),
    test: bool = typer.Option(False, "--test", help="测试 LLM 连接"),
):
    """⚙️  查看或修改 SkillMind 配置

    示例：
      skillmind config --model anthropic/claude-sonnet-4-20250514    # 全局模型
      skillmind config --profile extract --model deepseek/deepseek-chat  # extract 用 deepseek
      skillmind config --profile audit --model openai/gpt-4o             # audit 用 gpt-4o
      skillmind config --show                                            # 查看当前配置
    """
    from skillmind.config import load_config, save_config, _PROVIDER_ENV_MAP

    cfg = load_config()
    changed = False

    if auth is not None:
        if auth not in ("api_key", "claude_code_max"):
            console.print("[red]--auth 只支持: api_key | claude_code_max[/red]")
            raise typer.Exit(1)
        cfg.setdefault("llm", {})["auth_mode"] = auth
        console.print(f"[green]✓ 认证模式:[/green] {auth}")
        changed = True

    if api_key is not None:
        if provider:
            cfg.setdefault("api_keys", {})[provider] = api_key
            console.print(f"[green]✓ [{provider}] API Key 已保存[/green] (前8位: {api_key[:8]}...)")
        else:
            cfg.setdefault("llm", {})["api_key"] = api_key
            console.print(f"[green]✓ API Key 已保存[/green] (前8位: {api_key[:8]}...)")
        cfg.setdefault("llm", {})["auth_mode"] = "api_key"
        changed = True

    if api_key_env is not None:
        cfg.setdefault("llm", {})["api_key_env"] = api_key_env
        cfg.setdefault("llm", {})["auth_mode"] = "api_key"
        console.print(f"[green]✓ API Key 环境变量:[/green] {api_key_env}")
        changed = True

    if api_base is not None:
        cfg.setdefault("llm", {})["api_base"] = api_base
        console.print(f"[green]✓ API Base:[/green] {api_base}")
        changed = True

    if model:
        if profile:
            # 设置指定指令的独立 LLM 模型
            profiles = cfg.setdefault("llm_profiles", {})
            p_cfg = profiles.setdefault(profile, {})
            p_cfg["model"] = model
            # 同时把 api_key / api_base 也写入 profile（若用户一起指定了的话）
            if api_key is not None:
                p_cfg["api_key"] = api_key
            if api_base is not None:
                p_cfg["api_base"] = api_base
            console.print(f"[green]✓ [{profile}] 独立模型:[/green] {model}")
        else:
            cfg.setdefault("llm", {})["model"] = model
            console.print(f"[green]✓ LLM 模型:[/green] {model}")
        changed = True

    if vault:
        cfg["vault_dir"] = vault
        console.print(f"[green]✓ Vault 目录:[/green] {vault}")
        changed = True

    if output_prefix is not None:
        cfg["output_prefix"] = output_prefix
        label = repr(output_prefix) if output_prefix else "（无前缀）"
        console.print(f"[green]✓ 输出文件名前缀:[/green] {label}")
        changed = True

    if qpm is not None:
        cfg.setdefault("llm", {})["qpm"] = qpm
        console.print(f"[green]✓ QPM:[/green] {qpm}")
        changed = True

    if changed:
        save_config(cfg)

    if list_providers:
        table = Table(title="已配置的 API Key", box=box.ROUNDED)
        table.add_column("Provider", style="bold cyan")
        table.add_column("来源")
        table.add_column("Key 预览")
        table.add_column("环境变量")
        stored: dict = cfg.get("api_keys", {})
        llm_key = cfg.get("llm", {}).get("api_key", "")
        if llm_key:
            table.add_row("(默认)", "config.llm.api_key", llm_key[:6] + "****" + llm_key[-4:], "-")
        for p, env in _PROVIDER_ENV_MAP.items():
            key_cfg = stored.get(p, "")
            key_env = os.environ.get(env, "")
            if key_cfg:
                table.add_row(p, "config.api_keys", key_cfg[:6] + "****" + key_cfg[-4:], env)
            elif key_env:
                table.add_row(p, "[dim]环境变量[/dim]", key_env[:6] + "****" + key_env[-4:], env)
        console.print(table)
        return

    if test:
        console.print("\n[bold cyan]测试 LLM 连接...[/bold cyan]")
        try:
            from skillmind.config import resolve_llm_credentials
            from litellm import completion  # type: ignore
            creds = resolve_llm_credentials(cfg)
            console.print(f"  模型: [cyan]{creds['model']}[/cyan]")
            kwargs: dict = {
                "model": creds["model"],
                "messages": [{"role": "user", "content": "Reply with: OK"}],
                "temperature": 0,
                "max_tokens": 10,
                "api_key": creds["api_key"],
            }
            if "api_base" in creds:
                kwargs["api_base"] = creds["api_base"]
            resp = completion(**kwargs)
            reply = resp.choices[0].message.content or ""
            console.print(f"[green]✅ 连接成功！回复:[/green] {reply.strip()}")
        except Exception as e:
            console.print(f"[red]❌ 连接失败:[/red] {e}")
        return

    if show or not changed:
        import yaml as _yaml
        display_cfg = _yaml.dump(cfg, allow_unicode=True)
        for k, v in list(cfg.get("api_keys", {}).items()) + [("", cfg.get("llm", {}).get("api_key", ""))]:
            if v and len(v) > 10:
                display_cfg = display_cfg.replace(v, v[:6] + "****" + v[-4:])
        console.print(Panel(display_cfg, title="[bold]当前配置[/bold]", border_style="blue"))
        detected = [(e, os.environ.get(e, "")) for e in _PROVIDER_ENV_MAP.values() if os.environ.get(e)]
        if detected:
            console.print("\n[dim]检测到环境变量 Key:[/dim]")
            for env, val in detected:
                console.print(f"  [green]{env}[/green] = {val[:6]}****{val[-4:]}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    app()


if __name__ == "__main__":
    main()
