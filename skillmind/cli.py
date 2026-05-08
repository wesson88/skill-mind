"""SkillMind CLI 入口 - 基于 Typer + Rich"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich import box

app = typer.Typer(
    name="skillmind",
    help="🧠 SkillMind - 开源 Claude Code Skill 知识提炼系统",
    add_completion=False,
    rich_markup_mode="rich",
)

console = Console()


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

@app.command()
def ingest(
    source: str = typer.Argument(..., help="本地目录路径或 Git 仓库 URL"),
):
    """📥 将本地目录或 Git 仓库中的 Skill 文件导入缓存区"""
    from skillmind.collector import ingest as _ingest

    with console.status("[bold cyan]正在采集 Skill 文件...[/bold cyan]"):
        try:
            results = _ingest(source, console=console)
        except Exception as e:
            console.print(f"[bold red]采集失败:[/bold red] {e}")
            raise typer.Exit(1)

    new_count = sum(1 for r in results if not r["skipped"])
    skip_count = sum(1 for r in results if r["skipped"])
    console.print(
        Panel(
            f"✅ 采集完成\n"
            f"  新增文件: [green]{new_count}[/green]\n"
            f"  跳过(已存在): [yellow]{skip_count}[/yellow]\n"
            f"  合计: {len(results)}",
            title="[bold]采集结果[/bold]",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

@app.command()
def extract(
    skill: Optional[str] = typer.Option(None, "--skill", "-s", help="指定要提取的 source_hash 前缀或路径关键词"),
    auto_approve: bool = typer.Option(False, "--auto-approve", help="提取后自动批准草稿"),
):
    """🔬 对缓存内的 Skill 执行知识提取，生成草稿"""
    from skillmind.collector import list_cached
    from skillmind.extractor import extract_skill
    from skillmind.config import load_config

    cfg = load_config()
    cached = list_cached()

    if not cached:
        console.print("[yellow]缓存区为空，请先执行 skillmind ingest <path|url>[/yellow]")
        raise typer.Exit(0)

    # 过滤
    if skill:
        cached = [c for c in cached if skill in c.get("source_hash", "") or skill in c.get("source_path", "")]
        if not cached:
            console.print(f"[red]未找到匹配的缓存文件: {skill}[/red]")
            raise typer.Exit(1)

    console.print(f"[cyan]共发现 {len(cached)} 个文件待提取[/cyan]")
    qpm = cfg.get("llm", {}).get("qpm", 10)
    interval = 60.0 / max(qpm, 1)

    success = 0
    fail = 0
    last_request_time: float = 0.0  # 精准限速，避免固定 sleep 浪费时间

    for i, info in enumerate(cached):
        # 精准限速：仅等待真正剩余的间隔
        if last_request_time > 0:
            elapsed = time.monotonic() - last_request_time
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)
        last_request_time = time.monotonic()

        console.print(f"\n[bold][{i+1}/{len(cached)}][/bold] {info.get('source_path', info.get('source_hash', ''))[:60]}")
        try:
            draft = extract_skill(raw_path=info["raw_path"], source_info=info, cfg=cfg, console=console)
            if auto_approve:
                draft["status"] = "approved"
                from skillmind.reviewer import save_draft
                save_draft(draft)
            console.print(f"  [green]✓ 草稿已生成:[/green] {draft['uuid']}")
            success += 1
        except Exception as e:
            console.print(f"  [red]✗ 失败:[/red] {e}")
            fail += 1

    from skillmind.config import get_vault_dir
    vault_path = get_vault_dir(cfg)

    console.print(
        Panel(
            f"提取完成: 成功 [green]{success}[/green], 失败 [red]{fail}[/red]\n\n"
            f"📁 草稿保存于: [dim]~/.skillmind/drafts/[/dim]\n"
            f"📖 发布目标 Vault: [cyan]{vault_path}[/cyan]\n\n"
            f"[bold]下一步:[/bold]\n"
            f"  查看草稿列表  → [bold]skillmind review[/bold]\n"
            f"  直接全部发布  → [bold]skillmind publish --all[/bold]\n"
            f"  更换 Vault 路径 → [bold]skillmind config --vault <路径>[/bold]",
            title="[bold]提取结果[/bold]",
            border_style="cyan",
        )
    )


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@app.command()
def review(
    status: Optional[str] = typer.Option("draft", "--status", help="筛选状态: draft / published / all"),
):
    """📋 列出所有待审核的提取草稿"""
    from skillmind.reviewer import list_drafts

    filter_status = None if status == "all" else status
    drafts = list_drafts(status=filter_status)

    if not drafts:
        console.print(f"[yellow]没有状态为 '{status}' 的草稿[/yellow]")
        return

    table = Table(title=f"草稿列表 (status={status})", box=box.ROUNDED, show_lines=True)
    table.add_column("UUID", style="dim", width=18)
    table.add_column("名称", min_width=20)
    table.add_column("类型", width=20)
    table.add_column("状态", width=10)
    table.add_column("来源", width=30)
    table.add_column("创建日期", width=12)

    for d in drafts:
        meta = d.get("meta", {})
        uid = d.get("uuid", "")[-12:]
        name = meta.get("name", "?")[:40]
        skill_type = ", ".join(meta.get("type", []))[:20]
        st = d.get("status", "draft")
        source_path = d.get("source", {}).get("source_path", "")[:30]
        created = d.get("created_at", "")

        status_style = {"draft": "yellow", "approved": "cyan", "published": "green"}.get(st, "white")
        table.add_row(uid, name, skill_type, f"[{status_style}]{st}[/{status_style}]", source_path, created)

    console.print(table)
    console.print(f"\n共 [bold]{len(drafts)}[/bold] 条草稿。使用 [bold]skillmind edit <uuid>[/bold] 编辑，[bold]skillmind publish <uuid>[/bold] 发布。")


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

@app.command()
def edit(
    uid: str = typer.Argument(..., help="草稿 UUID（支持前缀）"),
):
    """✏️  使用编辑器手动修改某个草稿"""
    from skillmind.reviewer import open_in_editor, get_draft

    draft = get_draft(uid)
    if not draft:
        console.print(f"[red]未找到草稿: {uid}[/red]")
        raise typer.Exit(1)

    console.print(f"[cyan]打开草稿:[/cyan] {draft['uuid']}")
    open_in_editor(draft["uuid"])
    console.print("[green]编辑完成（保存时已自动写回文件）[/green]")


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------

@app.command()
def publish(
    uid: Optional[str] = typer.Argument(None, help="草稿 UUID，或使用 --all 批量发布"),
    all_drafts: bool = typer.Option(False, "--all", help="发布所有 draft/approved 状态的草稿"),
):
    """🚀 将审核通过的草稿发布到 Obsidian Vault"""
    from skillmind.reviewer import list_drafts, get_draft, save_draft
    from skillmind.renderer import publish_to_vault
    from skillmind.config import load_config

    cfg = load_config()

    if all_drafts:
        targets = list_drafts()
        targets = [d for d in targets if d.get("status") in ("draft", "approved")]
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

    # 尝试更新向量索引
    try:
        from skillmind.search import index_vault
        index_vault(console=console)
    except RuntimeError:
        pass  # chromadb 未安装，跳过
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
    """🔍 在知识库中进行语义搜索（需启用 Chroma）"""
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
    table.add_column("UUID", style="dim", width=16)
    table.add_column("相关度", width=8)
    table.add_column("摘要", min_width=40)
    table.add_column("文件路径")

    for i, r in enumerate(results, 1):
        table.add_row(
            str(i),
            r["uuid"][-12:],
            f"{r['score']:.3f}",
            r["snippet"][:60] + "...",
            r["file"],
        )

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
# status
# ---------------------------------------------------------------------------

@app.command()
def status():
    """📊 展示知识库统计信息与待更新提示"""
    from skillmind.reviewer import list_drafts
    from skillmind.collector import list_cached
    from skillmind.config import load_config, VAULT_DIR, CURRENT_PROMPT_VERSION, get_vault_dir

    cfg = load_config()
    cached = list_cached()
    all_drafts = list_drafts()

    draft_count = sum(1 for d in all_drafts if d.get("status") == "draft")
    published_count = sum(1 for d in all_drafts if d.get("status") == "published")
    approved_count = sum(1 for d in all_drafts if d.get("status") == "approved")

    # 检查旧 prompt 版本
    old_prompt = [d for d in all_drafts if d.get("prompt_version") != CURRENT_PROMPT_VERSION and d.get("status") == "published"]

    vault_dir = get_vault_dir(cfg)

    table = Table(title="SkillMind 状态概览", box=box.ROUNDED)
    table.add_column("指标", style="bold")
    table.add_column("值", justify="right")

    table.add_row("已缓存原始文件", str(len(cached)))
    table.add_row("草稿（待审核）", f"[yellow]{draft_count}[/yellow]")
    table.add_row("已批准（待发布）", f"[cyan]{approved_count}[/cyan]")
    table.add_row("已发布", f"[green]{published_count}[/green]")
    table.add_row("使用旧版 Prompt 的技能", f"[red]{len(old_prompt)}[/red]" if old_prompt else "0")
    table.add_row("Vault 路径", str(vault_dir))
    table.add_row("当前 Prompt 版本", CURRENT_PROMPT_VERSION)
    table.add_row("LLM 模型", cfg.get("llm", {}).get("model", "未配置"))

    console.print(table)

    if old_prompt:
        console.print(f"\n[yellow]⚠️  有 {len(old_prompt)} 个技能使用旧版 Prompt，建议执行 skillmind extract 重新提取[/yellow]")


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
            repos_seen[repo] = info.get("commit_sha", "")

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
                new_sha = g_repo.remotes.origin.refs[0].commit.hexsha[:8]
                if new_sha != old_sha:
                    console.print(f"  [yellow]⚠ 有更新:[/yellow] {old_sha} → {new_sha}，建议重新执行 ingest + extract")
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
    provider: Optional[str] = typer.Option(None, "--provider", help="指定 provider 存储 Key，如 anthropic / openai / deepseek / gemini / groq 等"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="直接设置 API Key（配合 --provider 可管理多个）"),
    api_key_env: Optional[str] = typer.Option(None, "--api-key-env", help="从环境变量读取 Key，如 ANTHROPIC_API_KEY"),
    api_base: Optional[str] = typer.Option(None, "--api-base", help="自定义 API 地址（可选）"),
    model: Optional[str] = typer.Option(None, "--model", help="设置 LLM 模型，如 anthropic/claude-3-5-haiku-20241022"),
    vault: Optional[str] = typer.Option(None, "--vault", help="设置 Obsidian Vault 目录"),
    qpm: Optional[int] = typer.Option(None, "--qpm", help="设置 LLM 每分钟请求数限制"),
    show: bool = typer.Option(False, "--show", help="查看当前配置"),
    list_providers: bool = typer.Option(False, "--list-providers", help="列出所有已配置的 provider"),
    test: bool = typer.Option(False, "--test", help="测试 LLM 连接是否正常"),
):
    """⚙️  查看或修改 SkillMind 配置"""
    from skillmind.config import load_config, save_config, _PROVIDER_ENV_MAP

    cfg = load_config()
    changed = False

    # --- 认证模式 ---
    if auth is not None:
        if auth not in ("api_key", "claude_code_max"):
            console.print("[red]--auth 只支持: api_key | claude_code_max[/red]")
            raise typer.Exit(1)
        cfg.setdefault("llm", {})["auth_mode"] = auth
        console.print(f"[green]✓ 认证模式:[/green] {auth}")
        changed = True

    if api_key is not None:
        if provider:
            # 存入多 provider 区
            cfg.setdefault("api_keys", {})[provider] = api_key
            console.print(f"[green]✓ 已保存 [{provider}] API Key[/green] (前8位: {api_key[:8]}...)")
        else:
            # 存入默认 key（当前 model 对应的 provider）
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
        console.print(f"[green]✓ QPM 限制:[/green] {qpm}")
        changed = True

    if changed:
        save_config(cfg)

    # --- 列出所有 provider ---
    if list_providers:
        table = Table(title="已配置的 API Key", box=box.ROUNDED)
        table.add_column("Provider", style="bold cyan")
        table.add_column("来源")
        table.add_column("Key 预览")
        table.add_column("对应环境变量")

        stored: dict = cfg.get("api_keys", {})
        # 当前 llm.api_key
        llm_key = cfg.get("llm", {}).get("api_key", "")
        if llm_key:
            masked = llm_key[:6] + "****" + llm_key[-4:]
            table.add_row("(默认)", "config.llm.api_key", masked, "-")

        for p, env in _PROVIDER_ENV_MAP.items():
            key_in_cfg = stored.get(p, "")
            key_in_env = os.environ.get(env, "")
            if key_in_cfg:
                masked = key_in_cfg[:6] + "****" + key_in_cfg[-4:]
                table.add_row(p, "config.api_keys", masked, env)
            elif key_in_env:
                masked = key_in_env[:6] + "****" + key_in_env[-4:]
                table.add_row(p, "[dim]环境变量[/dim]", masked, env)

        console.print(table)
        return

    # --- 测试连接 ---
    if test:
        console.print("\n[bold cyan]测试 LLM 连接...[/bold cyan]")
        try:
            from skillmind.config import resolve_llm_credentials
            from litellm import completion  # type: ignore

            creds = resolve_llm_credentials(cfg)
            console.print(f"  模式: [cyan]{cfg.get('llm',{}).get('auth_mode','api_key')}[/cyan]")
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
            console.print(f"[green]✅ 连接成功！模型回复:[/green] {reply.strip()}")
        except Exception as e:
            console.print(f"[red]❌ 连接失败:[/red] {e}")
        return

    # --- 展示配置 ---
    if show or not changed:
        import yaml as _yaml
        import os as _os
        display_cfg = _yaml.dump(cfg, allow_unicode=True)
        # 脱敏所有 Key
        for k, v in list(cfg.get("api_keys", {}).items()) + [("", cfg.get("llm", {}).get("api_key", ""))]:
            if v and len(v) > 10:
                display_cfg = display_cfg.replace(v, v[:6] + "****" + v[-4:])
        console.print(Panel(display_cfg, title="[bold]当前配置[/bold]", border_style="blue"))
        # 提示可用环境变量
        detected = [(env, _os.environ.get(env, "")) for env in _PROVIDER_ENV_MAP.values() if _os.environ.get(env)]
        if detected:
            console.print("\n[dim]检测到以下环境变量中的 Key（会自动使用）:[/dim]")
            for env, val in detected:
                console.print(f"  [green]{env}[/green] = {val[:6]}****{val[-4:]}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    app()


if __name__ == "__main__":
    main()
