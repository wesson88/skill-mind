"""预处理 & 清洗（Cleaner）

职责：将任意网页 URL 或本地 HTML 文件转换为干净的 Markdown 正文。
优先使用 crawl4ai（可选依赖），降级使用 httpx + 简单正则，最终兜底返回原始 HTML。

设计原则：
- 全部同步调用，无多线程，无 asyncio，避免死锁与线程安全问题。
- crawl4ai 的 async API 通过 asyncio.run() 在单独事件循环中执行，
  绝不与外部事件循环混用。
- 任何异常都有降级路径，不会崩溃整个流程。
"""

from __future__ import annotations

import html as html_lib
import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def fetch_and_clean(url: str, timeout: int = 30) -> tuple[str, dict]:
    """
    从 URL 抓取正文 Markdown。

    返回: (markdown_content, metadata)
    metadata 包含: title, author, published_at, source_url
    """
    # 优先尝试 crawl4ai（功能最强）
    result = _try_crawl4ai(url, timeout)
    if result:
        return result

    # 降级：httpx + 启发式提取
    result = _try_httpx(url, timeout)
    if result:
        return result

    # 最终兜底：返回空内容+元信息
    return "", {"source_url": url, "title": "", "author": "", "published_at": ""}


def clean_local_html(file_path: str | Path) -> tuple[str, dict]:
    """
    读取本地 HTML 文件并转换为 Markdown 正文。

    返回: (markdown_content, metadata)
    metadata 包含: title, author, published_at, source_path
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    # 尝试多种编码读取
    html = ""
    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            html = p.read_text(encoding=enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not html:
        html = p.read_bytes().decode("utf-8", errors="replace")

    # 防止超大文件
    if len(html) > 8 * 1024 * 1024:
        html = html[:8 * 1024 * 1024]

    md = _html_to_markdown(html)

    metadata = {
        "source_path": str(p),
        "title": _extract_title(html) or p.stem,
        "author": _extract_meta(html, "author"),
        "published_at": (
            _extract_meta(html, "article:published_time")
            or _extract_meta(html, "date")
        ),
    }
    return md, metadata


# ---------------------------------------------------------------------------
# crawl4ai 路径（可选依赖）
# ---------------------------------------------------------------------------

def _try_crawl4ai(url: str, timeout: int) -> Optional[tuple[str, dict]]:
    """通过 crawl4ai 提取正文。同步包装 async API，使用独立事件循环。"""
    try:
        import asyncio
        from crawl4ai import AsyncWebCrawler  # type: ignore
    except ImportError:
        return None

    async def _crawl():
        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await crawler.arun(
                url=url,
                word_count_threshold=10,
                exclude_external_links=False,
                remove_overlay_elements=True,
                timeout=timeout,
            )
            return result

    try:
        # 使用 asyncio.run 创建独立事件循环，与调用方完全隔离
        result = asyncio.run(_crawl())
    except Exception:
        return None

    if not result or not result.success:
        return None

    md = result.markdown or result.fit_markdown or ""
    if len(md.strip()) < 50:
        return None

    metadata = {
        "source_url": url,
        "title": getattr(result, "title", "") or "",
        "author": "",
        "published_at": "",
    }

    # 尝试从 metadata 字段补充
    if hasattr(result, "metadata") and isinstance(result.metadata, dict):
        m = result.metadata
        metadata["author"] = m.get("author", "")
        metadata["published_at"] = m.get("date", "") or m.get("published_time", "")
        if not metadata["title"]:
            metadata["title"] = m.get("title", "")

    return md, metadata


# ---------------------------------------------------------------------------
# httpx 降级路径
# ---------------------------------------------------------------------------

def _try_httpx(url: str, timeout: int) -> Optional[tuple[str, dict]]:
    """用 httpx 获取 HTML，再用启发式规则提取正文。"""
    try:
        import httpx
    except ImportError:
        return None

    from skillmind import __version__

    headers = {
        "User-Agent": (
            f"Mozilla/5.0 (compatible; SkillMind/{__version__}; +https://github.com/wesson88/skill-mind)"
        )
    }

    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        # 防止超大页面撑爆内存（限制 8 MB）
        if len(resp.content) > 8 * 1024 * 1024:
            html = resp.text[:8 * 1024 * 1024]
        else:
            html = resp.text
    except Exception:
        return None

    md = _html_to_markdown(html)
    if len(md.strip()) < 50:
        return None

    metadata = {
        "source_url": url,
        "title": _extract_title(html),
        "author": _extract_meta(html, "author"),
        "published_at": _extract_meta(html, "article:published_time") or _extract_meta(html, "date"),
    }
    return md, metadata


# ---------------------------------------------------------------------------
# 简单 HTML → Markdown 工具函数
# ---------------------------------------------------------------------------

_NOISE_TAGS = re.compile(
    r"<(script|style|nav|header|footer|aside|form|iframe|noscript|svg)"
    r"[^>]*>[\s\S]{0,50000}?</\1>",
    re.IGNORECASE,
)
# 只去掉真实 HTML 标签。`Promise<Task>` 这类泛型不是标签，不能被 `<[^>]+>` 洗成 `Promise `。
_HTML_TAG_NAMES = (
    "a|abbr|address|article|aside|audio|b|base|bdi|bdo|blockquote|body|br|button|"
    "canvas|caption|cite|code|col|colgroup|data|datalist|dd|del|details|dfn|dialog|div|dl|dt|"
    "em|embed|fieldset|figcaption|figure|footer|form|h1|h2|h3|h4|h5|h6|head|header|hr|html|"
    "i|iframe|img|input|ins|kbd|label|legend|li|link|main|map|mark|meta|meter|nav|noscript|"
    "object|ol|optgroup|option|output|p|picture|pre|progress|q|rp|rt|ruby|s|samp|script|"
    "section|select|small|source|span|strong|style|sub|summary|sup|svg|table|tbody|td|"
    "template|textarea|tfoot|th|thead|time|title|tr|track|u|ul|var|video|wbr"
)
_HTML_TAGS = re.compile(
    r"<!--[\s\S]*?-->|<!DOCTYPE[^>]*>|"
    rf"</?(?:{_HTML_TAG_NAMES})\b[^<>]*?/?>",
    re.IGNORECASE,
)
_CODE_SPAN_RE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")


def _strip_html_tags(text: str, replacement: str = "") -> str:
    return _HTML_TAGS.sub(replacement, text)


def _clean_embedded_html(raw: str) -> str:
    """先去掉格式化标签，再反转义，避免把刚还原的 Promise<Task> 再洗掉。"""
    return html_lib.unescape(_strip_html_tags(raw, "")).strip()


def _mask_code_spans(text: str) -> tuple[str, list[str]]:
    saved: list[str] = []

    def repl(match: re.Match) -> str:
        saved.append(match.group(0))
        return f"\x00CODE{len(saved) - 1}\x00"

    return _CODE_SPAN_RE.sub(repl, text), saved


def _unmask_code_spans(text: str, saved: list[str]) -> str:
    for index, block in enumerate(saved):
        text = text.replace(f"\x00CODE{index}\x00", block)
    return text
_MULTI_BLANK = re.compile(r"\n{3,}")
_CODE_BLOCK = re.compile(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", re.DOTALL | re.IGNORECASE)
_INLINE_CODE = re.compile(r"<code[^>]*>(.*?)</code>", re.DOTALL | re.IGNORECASE)
_H_TAG = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.DOTALL | re.IGNORECASE)
_P_TAG = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)
_LI_TAG = re.compile(r"<li[^>]*>(.*?)</li>", re.DOTALL | re.IGNORECASE)
_A_TAG = re.compile(r"<a[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)


def _html_to_markdown(html: str) -> str:
    """极简 HTML → Markdown，保留代码块/标题/列表结构。"""
    # 删除噪声区块
    text = _NOISE_TAGS.sub("", html)

    # 代码块优先处理（防止后续被清洗）。泛型留在代码里。
    def replace_code_block(m: re.Match) -> str:
        inner = _clean_embedded_html(m.group(1))
        return f"\n```\n{inner}\n```\n"

    text = _CODE_BLOCK.sub(replace_code_block, text)
    text = _INLINE_CODE.sub(lambda m: f"`{_clean_embedded_html(m.group(1))}`", text)
    text, masked_code = _mask_code_spans(text)

    # 标题
    def replace_heading(m: re.Match) -> str:
        level = int(m.group(1))
        content = _strip_html_tags(m.group(2)).strip()
        return f"\n{'#' * level} {content}\n"

    text = _H_TAG.sub(replace_heading, text)

    # 段落
    text = _P_TAG.sub(lambda m: f"\n{_strip_html_tags(m.group(1)).strip()}\n", text)

    # 列表
    text = _LI_TAG.sub(lambda m: f"\n- {_strip_html_tags(m.group(1)).strip()}", text)

    # 链接
    text = _A_TAG.sub(
        lambda m: f"[{_strip_html_tags(m.group(2)).strip()}]({m.group(1)})",
        text,
    )

    # 剩余真实 HTML 标签。未知的 <Task> 不是标签。
    text = _strip_html_tags(text, " ")
    text = html_lib.unescape(text)
    text = _unmask_code_spans(text, masked_code)

    # 多余空行压缩
    text = _MULTI_BLANK.sub("\n\n", text)

    return text.strip()


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        return _strip_html_tags(m.group(1)).strip()
    return ""


def _extract_meta(html: str, prop: str) -> str:
    # Pattern 1: property="og:xxx" content="value"
    m = re.search(
        rf'<meta[^>]+property=["\']og:{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    # Pattern 2: name="xxx" content="value"
    m = re.search(
        rf'<meta[^>]+name=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    # Pattern 3: content="value" name="xxx"（属性顺序颠倒）
    m = re.search(
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(prop)}["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()  # group(1) 始终是 content 值

    return ""


# ---------------------------------------------------------------------------
# RSS Feed 解析
# ---------------------------------------------------------------------------

def parse_rss_feed(feed_url: str, timeout: int = 30) -> list[dict]:
    """
    解析 RSS/Atom Feed，返回文章列表。
    每项包含: title, url, published_at, author, summary

    优先 feedparser，降级用 httpx + 正则。
    """
    # 优先 feedparser
    try:
        import feedparser  # type: ignore
        feed = feedparser.parse(feed_url)
        entries = []
        for entry in feed.entries:
            entries.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "published_at": entry.get("published", "") or entry.get("updated", ""),
                "author": entry.get("author", ""),
                "summary": entry.get("summary", ""),
            })
        return entries
    except ImportError:
        pass
    except Exception:
        return []

    # 降级：httpx + 正则提取 <item> 或 <entry>
    try:
        import httpx
        resp = httpx.get(feed_url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        xml = resp.text
    except Exception:
        return []

    entries = []
    items = re.findall(r"<(?:item|entry)(.*?)</(?:item|entry)>", xml, re.DOTALL | re.IGNORECASE)
    for item_xml in items:
        # 用显式参数传入 item_xml，避免闭包延迟绑定歧义
        def _tag(tag: str, _xml: str = item_xml) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", _xml, re.DOTALL | re.IGNORECASE)
            return _strip_html_tags(m.group(1)).strip() if m else ""

        link = _tag("link") or _tag("id")
        entries.append({
            "title": _tag("title"),
            "url": link,
            "published_at": _tag("pubDate") or _tag("published") or _tag("updated"),
            "author": _tag("author"),
            "summary": _tag("description") or _tag("summary"),
        })

    return entries
