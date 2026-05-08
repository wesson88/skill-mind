"""提取引擎（Extractor）- 使用 LLM 将解析后的 Skill 转换为结构化 JSON"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from skillmind.config import (
    CURRENT_PROMPT_VERSION,
    DRAFTS_DIR,
    EXTRACT_CACHE_DIR,
    ensure_dirs,
    load_config,
)
from skillmind.parser import parse_skill_file


# ---------------------------------------------------------------------------
# JSON Schema（用于 LLM 输出校验）
# ---------------------------------------------------------------------------

SKILL_SCHEMA: dict = {
    "type": "object",
    "required": ["meta", "learning_enhancement"],
    "properties": {
        "meta": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "array", "items": {"type": "string"}},
                "trigger_keywords": {"type": "array", "items": {"type": "string"}},
                "intent": {"type": "string"},
                "os": {"type": "array", "items": {"type": "string"}},
                "tools_required": {"type": "array", "items": {"type": "string"}},
            },
        },
        "preconditions": {"type": "array", "items": {"type": "string"}},
        "procedure": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "seq": {"type": "integer"},
                    "action": {"type": "string"},
                    "command": {"type": "string"},
                },
            },
        },
        "decision_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "condition": {"type": "string"},
                    "then": {"type": "string"},
                    "else": {"type": "string"},
                },
            },
        },
        "halt_conditions": {"type": "array", "items": {"type": "string"}},
        "rollback_actions": {"type": "array", "items": {"type": "string"}},
        "cross_references": {"type": "array", "items": {"type": "string"}},
        "learning_enhancement": {
            "type": "object",
            "properties": {
                "pain_points": {"type": "array", "items": {"type": "string"}},
                "plain_summary": {"type": "string"},
                "knowledge_tags": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------------------------

EXTRACT_PROMPT_TEMPLATE = """\
你是一名专业的技术知识提炼专家。请仔细阅读以下 Skill 文档（可能为中文或英文），将其中的关键知识提炼为结构化 JSON。

## 输出要求
1. 首先判断 Skill 的类型（可多选）：command-oriented（命令型）、concept-explanation（概念解释型）、decision-tree（决策型）、troubleshooting（故障排查型）。
2. 根据类型提取对应字段：
   - 命令型：重点提取 procedure、command_snippets、preconditions、halt_conditions
   - 概念型：重点提取 plain_summary、knowledge_tags、pain_points（procedure 允许为空）
   - 决策型：重点提取 decision_points、cross_references
3. 严格按照下面的 JSON Schema 格式输出，不要包含任何额外解释文字，只输出纯 JSON。
4. **语言规则（重要）**：
   - 无论原始文档是中文还是英文，所有 JSON 字段的文字内容必须翻译/输出为中文。
   - 命令行指令、代码片段、专有名词（如工具名、库名、API 名）保持英文原样，不翻译。
   - 例如："Install dependencies" → "安装依赖"，但 `npm install` 保持不变。

## JSON Schema
```json
{schema}
```

## Skill 文档内容
{content}

## 要求
只输出合法 JSON，不要有 markdown 代码块包裹，不要有任何前缀说明。
"""


# ---------------------------------------------------------------------------
# 提取缓存
# ---------------------------------------------------------------------------

def _cache_key(source_hash: str, prompt_version: str) -> str:
    return hashlib.md5(f"{source_hash}:{prompt_version}".encode()).hexdigest()


def _get_from_cache(source_hash: str, prompt_version: str) -> dict | None:
    key = _cache_key(source_hash, prompt_version)
    cache_file = EXTRACT_CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        with cache_file.open("r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_to_cache(source_hash: str, prompt_version: str, data: dict) -> None:
    """原子写：防止崩溃损坏缓存 JSON。"""
    key = _cache_key(source_hash, prompt_version)
    cache_file = EXTRACT_CACHE_DIR / f"{key}.json"
    tmp = cache_file.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(cache_file)


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

def _call_llm(prompt: str, cfg: dict) -> str:
    """通过 litellm 调用 LLM，返回原始文本。带超时保护。"""
    try:
        from litellm import completion  # type: ignore
    except ImportError:
        raise RuntimeError("请先安装 litellm: pip install litellm")

    from skillmind.config import resolve_llm_credentials
    creds = resolve_llm_credentials(cfg)

    # 超时从配置读取，默认 120 秒，防止永久阻塞
    timeout = cfg.get("llm", {}).get("timeout", 120)

    kwargs: dict = {
        "model": creds["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "api_key": creds["api_key"],
        "timeout": timeout,
    }
    if "api_base" in creds:
        kwargs["api_base"] = creds["api_base"]

    response = completion(**kwargs)
    return response.choices[0].message.content or ""


def _parse_llm_json(raw: str) -> dict:
    """从 LLM 输出中提取 JSON，容忍 markdown 代码块包裹。"""
    raw = raw.strip()
    if not raw:
        raise ValueError("LLM 返回空响应")

    # 去除可能的 markdown 代码块
    if raw.startswith("```"):
        lines = raw.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        raw = "\n".join(inner).strip()

    # 尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 兜底：用正则从响应中找第一个 JSON 对象
    import re
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从 LLM 响应中解析 JSON，原始内容前100字符: {raw[:100]}")


# ---------------------------------------------------------------------------
# 核心提取
# ---------------------------------------------------------------------------

def extract_skill(
    raw_path: str,
    source_info: dict,
    cfg: dict | None = None,
    console=None,
) -> dict[str, Any]:
    """
    提取单个 SKILL.md 文件，返回完整 JSON 知识单元（草稿状态）。

    source_info 包含: source_repo, source_path, source_hash, commit_sha, fetch_time
    """
    ensure_dirs()
    cfg = cfg or load_config()
    source_hash = source_info.get("source_hash", "")
    prompt_version = CURRENT_PROMPT_VERSION

    # 检查提取缓存
    cached = _get_from_cache(source_hash, prompt_version)
    if cached:
        if console:
            console.print(f"  [dim]↩ 使用缓存结果[/dim]")
        return cached

    # 解析文件
    skill_file = Path(raw_path)
    parsed = parse_skill_file(skill_file)

    # 构建 prompt（token 截断长度从配置读取，默认 6000）
    max_chars = cfg.get("llm", {}).get("max_content_chars", 6000)
    content_for_llm = f"标题: {parsed['title']}\n\n{parsed['body'][:max_chars]}"
    prompt = EXTRACT_PROMPT_TEMPLATE.format(
        schema=json.dumps(SKILL_SCHEMA, ensure_ascii=False, indent=2),
        content=content_for_llm,
    )

    # 调用 LLM（带重试）
    if console:
        console.print(f"  [cyan]调用 LLM 提取:[/cyan] {source_info.get('source_path', raw_path)}")

    max_retries = cfg.get("llm", {}).get("max_retries", 2)
    extracted = None
    last_err = None

    for attempt in range(max_retries + 1):
        try:
            raw_response = _call_llm(prompt, cfg)
            extracted = _parse_llm_json(raw_response)
            break  # 成功则退出重试
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                wait = 2 ** attempt  # 指数退避：1s, 2s
                if console:
                    console.print(f"  [yellow]LLM 调用失败（第{attempt+1}次），{wait}s 后重试:[/yellow] {e}")
                time.sleep(wait)

    if extracted is None:
        if console:
            console.print(f"  [red]LLM 调用失败（已重试{max_retries}次）:[/red] {last_err}，使用启发式提取")
        extracted = _heuristic_extract(parsed)

    # 与本地正则提取结果合并（命令兜底）
    extracted = _merge_with_local(extracted, parsed)

    # 构建完整知识单元
    skill_id = f"skill-{uuid.uuid4().hex[:7]}"
    result: dict[str, Any] = {
        "uuid": skill_id,
        "source": {
            "repo_url": source_info.get("source_repo", ""),
            "file_path": source_info.get("source_path", ""),
            "author": parsed["front_matter"].get("author", ""),
            "updated_at": parsed["front_matter"].get("updated_at", ""),
            "commit_sha": source_info.get("commit_sha", ""),
            "source_hash": source_hash,
        },
        "meta": extracted.get("meta", {"name": parsed["title"], "type": [], "trigger_keywords": [], "intent": "", "os": [], "tools_required": []}),
        "preconditions": extracted.get("preconditions", []),
        "procedure": extracted.get("procedure", []),
        "decision_points": extracted.get("decision_points", []),
        "halt_conditions": extracted.get("halt_conditions", []),
        "rollback_actions": extracted.get("rollback_actions", []),
        "cross_references": extracted.get("cross_references", []),
        "learning_enhancement": extracted.get("learning_enhancement", {"pain_points": [], "plain_summary": "", "knowledge_tags": []}),
        "prompt_version": prompt_version,
        "status": "draft",
        "created_at": time.strftime("%Y-%m-%d"),
    }

    # 确保 meta.name 有值
    if not result["meta"].get("name"):
        result["meta"]["name"] = parsed["title"]

    # 保存提取缓存
    _save_to_cache(source_hash, prompt_version, result)

    # 保存到草稿目录（原子写）
    draft_file = DRAFTS_DIR / f"{skill_id}.json"
    tmp = draft_file.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    tmp.replace(draft_file)

    return result


# ---------------------------------------------------------------------------
# 启发式提取（LLM 失败时的兜底方案）
# ---------------------------------------------------------------------------

def _heuristic_extract(parsed: dict) -> dict:
    """当 LLM 不可用时，用规则从解析结果中构建基本结构。"""
    sections = parsed.get("sections", [])
    procedure = []
    preconditions = []
    halt_conditions = []
    pain_points = []
    summary_parts = []
    knowledge_tags = []

    for sec in sections:
        stype = sec["section_type"]
        heading = sec["heading"]
        content = sec["content"]
        lines = [l.strip().lstrip("-*•·123456789. ").strip() for l in content.splitlines() if l.strip()]

        if stype == "procedure":
            for i, line in enumerate(lines, 1):
                if line:
                    procedure.append({"seq": i, "action": line, "command": ""})
        elif stype == "preconditions":
            preconditions = [l for l in lines if l]
        elif stype == "halt_conditions":
            halt_conditions = [l for l in lines if l]
        elif stype in ("notes", "design"):
            pain_points.extend([l for l in lines if l][:3])
            knowledge_tags.append(heading)
        elif stype in ("overview", "other"):
            # 把所有区块的内容拼成摘要
            if content.strip():
                summary_parts.append(f"【{heading}】{content.strip()[:120]}")
            knowledge_tags.append(heading)

    plain_summary = "\n".join(summary_parts[:3]) if summary_parts else parsed["title"]

    return {
        "meta": {
            "name": parsed["title"],
            "type": ["concept-explanation"],
            "trigger_keywords": knowledge_tags[:6],
            "intent": parsed["title"],
            "os": [],
            "tools_required": [],
        },
        "preconditions": preconditions,
        "procedure": procedure,
        "decision_points": [],
        "halt_conditions": halt_conditions,
        "rollback_actions": [],
        "cross_references": [],
        "learning_enhancement": {
            "pain_points": pain_points[:5],
            "plain_summary": plain_summary,
            "knowledge_tags": list(dict.fromkeys(knowledge_tags))[:8],
        },
    }


def _merge_with_local(extracted: dict, parsed: dict) -> dict:
    """将本地正则提取的命令与 LLM 结果合并，LLM 优先。"""
    local_commands = parsed.get("commands", [])
    procedure = extracted.get("procedure", [])

    # 如果 procedure 为空但有本地命令，填充
    if not procedure and local_commands:
        for i, cmd in enumerate(local_commands[:20], 1):
            procedure.append({"seq": i, "action": cmd[:80], "command": cmd})
        extracted["procedure"] = procedure

    return extracted


# ---------------------------------------------------------------------------
# 批量提取
# ---------------------------------------------------------------------------

def extract_all(
    cached_files: list[dict],
    cfg: dict | None = None,
    console=None,
    qpm: int = 10,
) -> list[dict]:
    """
    批量提取所有缓存文件，返回草稿列表。

    限速策略：记录每次请求完成时间，下次请求前精确等待剩余间隔，
    避免 time.sleep(固定值) 导致实际 QPM 低于配置值。
    """
    cfg = cfg or load_config()
    results = []
    interval = 60.0 / max(qpm, 1)
    last_request_time: float = 0.0

    for i, info in enumerate(cached_files):
        # 精准限速：只等待真正剩余的时间
        if last_request_time > 0:
            elapsed = time.monotonic() - last_request_time
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)

        last_request_time = time.monotonic()
        try:
            draft = extract_skill(
                raw_path=info["raw_path"],
                source_info=info,
                cfg=cfg,
                console=console,
            )
            results.append(draft)
        except Exception as e:
            if console:
                console.print(f"  [red]✗ 提取失败:[/red] {info.get('source_path', '')} → {e}")

    return results
