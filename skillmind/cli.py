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
# extract
# ---------------------------------------------------------------------------

@app.command()
def extract(
    skill: Optional[str] = typer.Option(None, "--skill", "-s", help="指定 source_hash 前缀或路径关键词"),
    doc_type: Optional[str] = typer.Option(None, "--type", "-t", help="只处理指定类型: skill / blog / forum_post / webpage"),
    auto_approve: bool = typer.Option(False, "--auto-approve", help="提取后自动批准草稿"),
):
    """🔬 对缓存内的文档执行 LLM 知识提取，生成草稿（支持一文多卡）"""
    from skillmind.collector import list_cached
    from skillmind.extractor import extract_skill as _extract
    from skillmind.config import load_config, get_vault_dir

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
            dest = publish_to_vault(draft, cfg=cfg)
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
    qpm: Optional[int] = typer.Option(None, "--qpm", help="每分钟 LLM 请求数限制"),
    show: bool = typer.Option(False, "--show", help="查看当前配置"),
    list_providers: bool = typer.Option(False, "--list-providers", help="列出所有已配置的 provider"),
    test: bool = typer.Option(False, "--test", help="测试 LLM 连接"),
):
    """⚙️  查看或修改 SkillMind 配置"""
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
        cfg.setdefault("llm", {})["model"] = model
        console.print(f"[green]✓ LLM 模型:[/green] {model}")
        changed = True

    if vault:
        cfg["vault_dir"] = vault
        console.print(f"[green]✓ Vault 目录:[/green] {vault}")
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
