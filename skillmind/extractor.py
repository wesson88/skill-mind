"""提取引擎（Extractor）v2.2

职责：将单个已缓存的原始文档转成 1~N 张结构化草稿（一文多卡）。

设计要点（与 PRD §4.4 一致）：
- 动态 Prompt 路由：按 doc_type 选择提取重点。
- 一文多卡：LLM 可返回 JSON 数组，每项渲染为独立笔记。
- 可信度 / 过时风险：source_reliability / obsolescence_risk 由 LLM 标注。
- 提取缓存：同 (source_hash, prompt_version) 命中直接复用，避免重复消费 Token。
- LLM 失败兜底：超时/JSON 解析失败 → 指数退避重试 → 全部失败后走启发式规则。
- 凭证缺失（resolve_llm_credentials 抛出）属配置错误，向上抛，不静默降级。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from skillmind.config import (
    CURRENT_PROMPT_VERSION,
    EXTRACT_CACHE_DIR,
    ensure_dirs,
    resolve_llm_credentials,
)
from skillmind.parser import _read_text_auto, parse_skill_file
from skillmind.reviewer import save_draft


# ---------------------------------------------------------------------------
# Prompt 文本
# ---------------------------------------------------------------------------

_DOC_TYPE_LABEL = {
    "skill":      "Claude Code Skill 文档",
    "blog":       "技术博客文章",
    "forum_post": "论坛主题帖（含问答）",
    "webpage":    "通用网页",
}

_FOCUS_RULES: dict[str, str] = {
    "skill": (
        "- 着重提取严格的执行流程（procedure）、决策点（decision_points）、\n"
        "  命令片段（procedure[].command）、前置条件、中止条件、回滚步骤。\n"
        "- 保留作者写明的避坑提示（learning_enhancement.pain_points）。"
    ),
    "blog": (
        "- 提取核心步骤、避坑指南、关键命令片段；\n"
        "- 总结作者观点（learning_enhancement.plain_summary）；\n"
        "- 若长文涵盖多个独立主题，按主题拆成多张卡。"
    ),
    "forum_post": (
        "- 主帖：明确「问题描述」（写入 plain_summary）；\n"
        "- 多个回答：在 procedure 或 decision_points 中对比，标记最终采纳方案；\n"
        "- 不同方案适用条件写入 decision_points。"
    ),
    "webpage": (
        "- 抽取主要概念、关键信息点，写入 learning_enhancement.knowledge_tags；\n"
        "- 若文中含可执行命令则一并提取到 procedure。"
    ),
}

_SYSTEM_PROMPT = (
    "你是结构化知识提取助手。你的任务是从给定文档中抽取标准化知识单元，"
    "并以严格 JSON 输出。\n"
    "重要：直接输出 JSON，不要附带任何解释、不要使用 Markdown 代码块包裹。"
)

_USER_PROMPT_TEMPLATE = """【任务】
从以下{doc_type_label}中提取知识单元。

【提取重点】
{focus_rules}

【一文多卡】
若文档包含多个相对独立的主题，可返回 JSON 数组（每项一张笔记）；
否则返回单个 JSON 对象。

【字段定义】
{{
  "meta": {{
    "name": "笔记标题（简洁，<= 30 字）",
    "type": ["command-oriented" | "concept-explanation" | "decision-tree" | "troubleshooting"],
    "intent": "一句话目的描述",
    "trigger_keywords": ["关键词", "..."],
    "os": ["linux", "macos", "..."],
    "tools_required": ["工具名", "..."]
  }},
  "preconditions": ["前置条件", "..."],
  "procedure": [
    {{"seq": 1, "action": "动作描述", "command": "可选命令"}}
  ],
  "decision_points": [
    {{"condition": "判断条件", "then": "成立时", "else": "不成立时"}}
  ],
  "halt_conditions": ["停止条件", "..."],
  "rollback_actions": ["回滚动作", "..."],
  "cross_references": ["[[关联笔记名]]", "..."],
  "learning_enhancement": {{
    "pain_points": ["难点 / 避坑", "..."],
    "plain_summary": "白话一句话总结",
    "knowledge_tags": ["标签", "..."]
  }},
  "source_reliability": "high | medium | low",
  "obsolescence_risk": "low | medium | high"
}}

【可信度判断】
- high   : 官方文档、知名团队的 Skill 仓库
- medium : 大型技术博客、活跃维护的开源项目
- low    : 个人博客、论坛回答

【过时风险】
- high   : 涉及具体版本号、API 细节、易变命令
- medium : 涉及部署流程、第三方工具
- low    : 概念解释、设计原则

【文档元信息】
{metadata_json}

【文档正文（doc_type={doc_type}）】
{content}

请直接输出 JSON。"""


# ---------------------------------------------------------------------------
# 提取入口
# ---------------------------------------------------------------------------

def extract_skill(
    raw_path: str,
    source_info: dict,
    cfg: dict,
    console=None,
) -> list[dict]:
    """
    对单个已缓存文档执行提取，返回草稿列表（已写盘）。

    Parameters
    ----------
    raw_path : str
        缓存的 .md 文件路径。
    source_info : dict
        list_cached() 中的一条记录，含 source_hash / doc_type / 等。
    cfg : dict
        全局配置（load_config()）。
    console : rich.console.Console | None
        可选输出。
    """
    ensure_dirs()

    raw_p = Path(raw_path)
    if not raw_p.exists():
        raise FileNotFoundError(f"原始文件不存在: {raw_path}")

    source_hash = source_info.get("source_hash") or ""
    if not source_hash:
        raise ValueError("source_info 缺少 source_hash")

    doc_type = source_info.get("doc_type", "skill")
    prompt_version = CURRENT_PROMPT_VERSION

    # 1. 读取原文
    text = _read_text_auto(raw_p)

    # 2. 提取缓存命中（同一 hash + prompt 版本）
    cached_units = _load_extract_cache(source_hash, prompt_version)
    if cached_units is not None:
        if console:
            console.print("  [dim]提取缓存命中，跳过 LLM 调用[/dim]")
        units = cached_units
    else:
        # 3. 截断到 max_content_chars
        max_chars = int(cfg.get("llm", {}).get("max_content_chars", 6000))
        if len(text) > max_chars:
            text_for_llm = text[:max_chars] + f"\n\n... (内容已截断，仅前 {max_chars} 字符)"
        else:
            text_for_llm = text

        # 4. LLM 提取（凭证缺失向上抛，让用户配置；其他失败重试 → 启发式）
        try:
            units = _llm_extract(text_for_llm, doc_type, source_info, cfg, console=console)
        except _CredentialError:
            raise
        except Exception as e:
            if console:
                console.print(f"  [yellow]⚠ LLM 提取失败，降级启发式:[/yellow] {e}")
            units = [_heuristic_extract(text, raw_p, source_info)]

        # 5. 写入提取缓存（仅 LLM/启发式新结果）
        _save_extract_cache(source_hash, prompt_version, units)

    if not units:
        units = [_heuristic_extract(text, raw_p, source_info)]

    # 6. 组装并落盘草稿
    drafts: list[dict] = []
    total = len(units)
    for idx, unit in enumerate(units, start=1):
        draft = _assemble_draft(unit, source_info, idx=idx, total=total,
                                prompt_version=prompt_version)
        save_draft(draft)
        drafts.append(draft)

    return drafts


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

class _CredentialError(RuntimeError):
    """LLM 凭证缺失/无效；属配置错误，不应被启发式吃掉。"""


def _llm_extract(
    text: str,
    doc_type: str,
    source_info: dict,
    cfg: dict,
    console=None,
) -> list[dict]:
    """调用 LLM 并解析 JSON。失败按 max_retries 指数退避重试。"""
    try:
        creds = resolve_llm_credentials(cfg)
    except RuntimeError as e:
        # 凭证类错误明确包装，让上层不要降级
        raise _CredentialError(str(e))

    metadata = {
        "doc_type": doc_type,
        "title": source_info.get("title", ""),
        "author": source_info.get("author", ""),
        "source_url": source_info.get("source_url", ""),
        "source_repo": source_info.get("source_repo", ""),
        "source_path": source_info.get("source_path", ""),
        "published_at": source_info.get("published_at", ""),
    }

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        doc_type_label=_DOC_TYPE_LABEL.get(doc_type, "文档"),
        focus_rules=_FOCUS_RULES.get(doc_type, _FOCUS_RULES["webpage"]),
        metadata_json=json.dumps(metadata, ensure_ascii=False),
        doc_type=doc_type,
        content=text,
    )

    timeout = int(cfg.get("llm", {}).get("timeout", 120))
    max_retries = int(cfg.get("llm", {}).get("max_retries", 2))

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return _llm_call_once(creds, user_prompt, timeout=timeout)
        except _CredentialError:
            raise
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait = 1.0 * (2 ** attempt)  # 1s, 2s, 4s ...
                if console:
                    console.print(
                        f"  [yellow]LLM 失败（第 {attempt+1}/{max_retries+1} 次），"
                        f"{wait:.0f}s 后重试: {e}[/yellow]"
                    )
                time.sleep(wait)

    raise RuntimeError(f"LLM 调用 {max_retries+1} 次后仍失败: {last_err}")


def _llm_call_once(creds: dict, user_prompt: str, *, timeout: int) -> list[dict]:
    from litellm import completion  # type: ignore

    kwargs: dict[str, Any] = {
        "model": creds["model"],
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "api_key": creds["api_key"],
        "timeout": timeout,
    }
    if "api_base" in creds:
        kwargs["api_base"] = creds["api_base"]

    resp = completion(**kwargs)
    raw = (resp.choices[0].message.content or "").strip()
    return _parse_json_response(raw)


_CODEBLOCK_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)


def _parse_json_response(text: str) -> list[dict]:
    """剥离可能的 ``` 包裹，解析为 list[dict]。"""
    text = text.strip()
    # 1. 整体被 ``` 包裹
    m = _CODEBLOCK_RE.search(text)
    if m:
        text = m.group(1).strip()

    # 2. 兜底：截取最外层 [...] 或 {...}
    if not text.startswith(("{", "[")):
        idx_obj = text.find("{")
        idx_arr = text.find("[")
        candidates = [i for i in (idx_obj, idx_arr) if i >= 0]
        if not candidates:
            raise ValueError("响应中未找到 JSON 起始符")
        text = text[min(candidates):]
        last = max(text.rfind("}"), text.rfind("]"))
        if last >= 0:
            text = text[:last + 1]

    parsed = json.loads(text)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        units = [u for u in parsed if isinstance(u, dict)]
        if not units:
            raise ValueError("数组中没有有效对象")
        return units
    raise ValueError(f"LLM 响应不是 JSON 对象/数组: {type(parsed).__name__}")


# ---------------------------------------------------------------------------
# 启发式兜底（LLM 失败时使用 parser 提取最低限度信息）
# ---------------------------------------------------------------------------

def _extract_list_items(content: str) -> list[str]:
    """从 Markdown 片段提取列表项；无列表时返回前几行非空文本。"""
    items: list[str] = []
    for line in content.splitlines():
        s = line.strip()
        m = re.match(r"^[-*+]\s+(.*)", s) or re.match(r"^\d+[.、)]\s+(.*)", s)
        if m:
            t = m.group(1).strip()
            if t:
                items.append(t)
    if items:
        return items
    return [ln.strip() for ln in content.splitlines() if ln.strip()][:5]


def _heuristic_extract(text: str, raw_path: Path, source_info: dict) -> dict:
    """LLM 不可用时，按 parser 区块结果生成最小可发布草稿。"""
    parsed = parse_skill_file(raw_path)
    title = parsed.get("title") or raw_path.stem
    sections = parsed.get("sections", [])

    procedure: list[dict] = []
    preconditions: list[str] = []
    halt_conditions: list[str] = []
    rollback_actions: list[str] = []
    decision_points: list[dict] = []
    notes: list[str] = []
    summary = ""

    for sec in sections:
        st = sec.get("section_type", "other")
        content = sec.get("content", "")
        if not content.strip():
            continue
        if st == "procedure":
            for step in _extract_list_items(content):
                procedure.append({"seq": len(procedure) + 1, "action": step})
        elif st == "preconditions":
            preconditions.extend(_extract_list_items(content))
        elif st == "halt_conditions":
            halt_conditions.extend(_extract_list_items(content))
        elif st == "rollback":
            rollback_actions.extend(_extract_list_items(content))
        elif st == "decisions":
            decision_points.append({
                "condition": sec.get("heading", "")[:80],
                "then": content[:200],
            })
        elif st == "overview" and not summary:
            summary = content[:300]
        elif st == "notes":
            notes.extend(_extract_list_items(content)[:5])

    if not summary:
        for sec in sections:
            if sec.get("content", "").strip():
                summary = sec["content"][:200]
                break

    types = ["command-oriented"] if procedure else ["concept-explanation"]
    if decision_points:
        types.append("decision-tree")

    return {
        "meta": {
            "name": title[:60],
            "type": list(dict.fromkeys(types)),
            "intent": title[:80],
            "trigger_keywords": [],
            "os": [],
            "tools_required": [],
        },
        "preconditions": preconditions,
        "procedure": procedure,
        "decision_points": decision_points,
        "halt_conditions": halt_conditions,
        "rollback_actions": rollback_actions,
        "cross_references": [],
        "learning_enhancement": {
            "pain_points": notes,
            "plain_summary": summary or title,
            "knowledge_tags": [],
        },
        "source_reliability": "medium",
        "obsolescence_risk": "medium",
    }


# ---------------------------------------------------------------------------
# 草稿组装
# ---------------------------------------------------------------------------

def _assemble_draft(
    unit: dict,
    source_info: dict,
    *,
    idx: int,
    total: int,
    prompt_version: str,
) -> dict:
    """
    把 LLM/启发式输出的 unit 与 source_info 合并为完整草稿。

    多卡时 uuid 形如 'skill-<sha7>-2'；单卡 'skill-<sha7>'。
    顶层 doc_type 字段冗余存放，cli review 在 source 缺失时兜底使用。
    """
    source_hash = source_info["source_hash"]
    doc_type = source_info.get("doc_type", "skill")

    # uuid：优先使用 LLM 给出的；否则按 hash + 序号生成
    uuid = unit.get("uuid")
    if not uuid or not isinstance(uuid, str):
        suffix = f"-{idx}" if total > 1 else ""
        uuid = f"{doc_type}-{source_hash[:7]}{suffix}"

    # 仅当 source_repo 是 http(s) URL 时才作为 repo_url 暴露给 trace/renderer
    repo_url_candidate = source_info.get("source_repo", "")
    repo_url = repo_url_candidate if repo_url_candidate.startswith(("http://", "https://")) else ""

    source_block = {
        "doc_type": doc_type,
        "source_hash": source_hash,
        "source_repo": source_info.get("source_repo", ""),
        "source_path": source_info.get("source_path", ""),
        "file_path": source_info.get("source_path", ""),
        "source_url": source_info.get("source_url", ""),
        "repo_url": repo_url,
        "commit_sha": source_info.get("commit_sha", "") or "",
        "fetch_time": source_info.get("fetch_time", 0),
        "raw_path": source_info.get("raw_path", ""),
        "author": source_info.get("author", ""),
        "published_at": source_info.get("published_at", ""),
        "branch": "main",
    }

    if total > 1:
        source_block["card_index"] = idx
        source_block["card_total"] = total
        source_block["parent_source"] = (
            source_info.get("source_url")
            or source_info.get("source_repo")
            or ""
        )

    return {
        "uuid": uuid,
        "doc_type": doc_type,
        "source": source_block,
        "meta": unit.get("meta", {}),
        "preconditions": unit.get("preconditions", []),
        "procedure": unit.get("procedure", []),
        "decision_points": unit.get("decision_points", []),
        "halt_conditions": unit.get("halt_conditions", []),
        "rollback_actions": unit.get("rollback_actions", []),
        "cross_references": unit.get("cross_references", []),
        "learning_enhancement": unit.get("learning_enhancement", {}),
        "source_reliability": unit.get("source_reliability", "medium"),
        "obsolescence_risk": unit.get("obsolescence_risk", "medium"),
        "prompt_version": prompt_version,
        "status": "draft",
        "created_at": time.strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# 提取缓存
# ---------------------------------------------------------------------------

def _extract_cache_path(source_hash: str, prompt_version: str) -> Path:
    return EXTRACT_CACHE_DIR / f"{source_hash}_{prompt_version}.json"


def _load_extract_cache(source_hash: str, prompt_version: str) -> list[dict] | None:
    p = _extract_cache_path(source_hash, prompt_version)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [u for u in data if isinstance(u, dict)] or None
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _save_extract_cache(source_hash: str, prompt_version: str, units: list[dict]) -> None:
    p = _extract_cache_path(source_hash, prompt_version)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(units, f, ensure_ascii=False, indent=2)
    tmp.replace(p)
