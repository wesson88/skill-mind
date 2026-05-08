"""解析器（Parser）- 将 SKILL.md 解析为结构化的内部数据"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# 编码自动检测
# ---------------------------------------------------------------------------

def _read_text_auto(path: Path) -> str:
    """
    自动检测文件编码并读取文本。
    优先尝试 UTF-8（含 BOM），再尝试常见编码，最后 latin-1 兜底（不会报错）。
    """
    # 优先尝试带 BOM 的 UTF-8
    raw = path.read_bytes()

    # UTF-8 BOM 检测
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")

    # 依次尝试常见编码
    for enc in ("utf-8", "gbk", "gb2312", "gb18030", "big5", "shift_jis", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    # 最终兜底：UTF-8 替换模式，不会抛异常，乱码字符用 ? 替代
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# YAML front matter 分离
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def split_front_matter(text: str) -> tuple[dict, str]:
    """分离 YAML front matter 与 Markdown 正文。"""
    m = _FM_RE.match(text)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        body = text[m.end():]
    else:
        fm = {}
        body = text
    return fm, body


# ---------------------------------------------------------------------------
# 标题区块识别
# ---------------------------------------------------------------------------

# 常见 Skill 区块标题关键词映射
_SECTION_KEYWORDS: dict[str, list[str]] = {
    "overview": [
        "overview", "summary", "description", "about", "intro", "background",
        "概述", "简介", "描述", "背景", "介绍", "总览", "说明", "设计思维", "思维", "目标",
    ],
    "procedure": [
        "procedure", "steps", "process", "instructions", "usage", "workflow", "how to",
        "流程", "步骤", "操作", "使用", "执行", "实现", "方法", "指南", "实施", "操作方法",
    ],
    "decisions": [
        "decision", "when to use", "conditions", "if", "strategy",
        "决策", "条件", "何时", "选择", "策略", "判断", "方向",
    ],
    "preconditions": [
        "preconditions", "prerequisites", "requirements", "setup", "before",
        "前提", "前置条件", "要求", "准备", "依赖", "环境",
    ],
    "halt_conditions": [
        "halt", "stop", "abort", "avoid", "forbidden", "禁忌",
        "中止", "停止", "禁止", "避免", "不要", "反例",
    ],
    "rollback": [
        "rollback", "revert", "undo", "fallback",
        "回滚", "撤销", "恢复", "降级",
    ],
    "examples": [
        "example", "sample", "demo", "case",
        "示例", "样例", "案例", "举例", "实例",
    ],
    "references": [
        "reference", "see also", "related", "resources",
        "参考", "相关", "关联", "延伸", "资源", "链接",
    ],
    "notes": [
        "note", "warning", "caution", "tip", "best practice", "guideline",
        "注意", "警告", "提示", "建议", "规范", "原则", "指南", "总结",
    ],
    "design": [
        "design", "aesthetic", "style", "theme", "color", "typography", "layout", "animation",
        "设计", "美学", "风格", "主题", "色彩", "字体", "排版", "版式", "动效", "交互",
        "视觉", "空间", "背景", "细节", "可访问性", "accessibility",
    ],
}


def _classify_section(heading: str) -> str:
    lower = heading.lower().strip()
    for section_type, keywords in _SECTION_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return section_type
    return "other"


# ---------------------------------------------------------------------------
# Markdown 区块分割
# ---------------------------------------------------------------------------

def parse_sections(body: str) -> list[dict[str, Any]]:
    """
    将 Markdown 正文分割为区块列表。
    每个区块: {heading, level, section_type, content}
    """
    lines = body.splitlines()
    sections: list[dict[str, Any]] = []
    current_heading = "intro"
    current_level = 0
    current_lines: list[str] = []

    heading_re = re.compile(r"^(#{1,6})\s+(.*)")

    def flush():
        nonlocal current_heading, current_level, current_lines
        content = "\n".join(current_lines).strip()
        if content or current_heading != "intro":
            sections.append({
                "heading": current_heading,
                "level": current_level,
                "section_type": _classify_section(current_heading),
                "content": content,
            })
        current_lines = []

    for line in lines:
        m = heading_re.match(line)
        if m:
            flush()
            current_level = len(m.group(1))
            current_heading = m.group(2).strip()
        else:
            current_lines.append(line)

    flush()
    return sections


# ---------------------------------------------------------------------------
# 命令片段提取（辅助正则，供提取引擎兜底）
# ---------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def extract_commands(text: str) -> list[str]:
    """从 Markdown 文本中提取命令代码片段。"""
    commands: list[str] = []
    for m in _CODE_BLOCK_RE.finditer(text):
        block = m.group(1).strip()
        if block:
            commands.extend(block.splitlines())
    # 行内代码，若像命令则纳入
    for m in _INLINE_CODE_RE.finditer(text):
        code = m.group(1).strip()
        if len(code) > 3 and (" " in code or code.startswith(("$", "//", "kubectl", "git", "docker", "npm", "pip", "apt"))):
            commands.append(code)
    return list(dict.fromkeys(commands))  # 去重保序


# ---------------------------------------------------------------------------
# 整体解析入口
# ---------------------------------------------------------------------------

def parse_skill_file(path: Path) -> dict[str, Any]:
    """
    解析单个 SKILL.md 文件，返回:
    {
        front_matter: dict,
        title: str,
        sections: list[dict],
        full_text: str,
        commands: list[str],
    }
    """
    text = _read_text_auto(path)
    fm, body = split_front_matter(text)
    sections = parse_sections(body)

    # 尝试从第一个标题或 front matter name 获取标题
    title = fm.get("name", fm.get("title", ""))
    if not title:
        for sec in sections:
            if sec["level"] == 1:
                title = sec["heading"]
                break
    if not title:
        title = path.stem

    commands = extract_commands(body)

    return {
        "front_matter": fm,
        "title": title,
        "sections": sections,
        "full_text": text,
        "body": body,
        "commands": commands,
    }
