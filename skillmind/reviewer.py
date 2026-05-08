"""审核器（Reviewer）- 草稿管理、列表展示、编辑、发布"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from skillmind.config import DRAFTS_DIR, ensure_dirs


# ---------------------------------------------------------------------------
# 草稿 CRUD
# ---------------------------------------------------------------------------

def list_drafts(status: str | None = None) -> list[dict]:
    """列出所有草稿，可按 status 过滤（draft / published）。"""
    ensure_dirs()
    drafts = []
    for f in sorted(DRAFTS_DIR.glob("*.json")):
        try:
            with f.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            if status is None or data.get("status") == status:
                drafts.append(data)
        except (json.JSONDecodeError, KeyError):
            pass
    return drafts


def get_draft(uid: str) -> dict | None:
    """根据 uuid 获取草稿（支持前缀匹配）。"""
    ensure_dirs()
    for f in DRAFTS_DIR.glob("*.json"):
        if f.stem == uid or f.stem.startswith(uid):
            with f.open("r", encoding="utf-8") as fp:
                return json.load(fp)
    return None


def save_draft(data: dict) -> Path:
    """原子写草稿到文件，防止崩溃损坏，返回文件路径。"""
    ensure_dirs()
    uid = data.get("uuid", "unknown")
    draft_file = DRAFTS_DIR / f"{uid}.json"
    tmp = draft_file.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(draft_file)
    return draft_file


def delete_draft(uid: str) -> bool:
    for f in DRAFTS_DIR.glob("*.json"):
        if f.stem == uid or f.stem.startswith(uid):
            f.unlink()
            return True
    return False


# ---------------------------------------------------------------------------
# 编辑草稿（用系统编辑器打开）
# ---------------------------------------------------------------------------

def open_in_editor(uid: str) -> bool:
    """用系统默认编辑器打开草稿 JSON。优先级：EDITOR 环境变量 > 配置文件 > 平台默认。"""
    from skillmind.config import load_config
    draft = get_draft(uid)
    if not draft:
        return False
    draft_file = DRAFTS_DIR / f"{draft['uuid']}.json"

    # 编辑器查找优先级
    cfg = load_config()
    editor = (
        os.environ.get("SKILLMIND_EDITOR")        # 专属环境变量（最高优先）
        or cfg.get("editor", "")                   # 配置文件
        or os.environ.get("EDITOR")                # 通用 EDITOR 变量
        or os.environ.get("VISUAL")                # 通用 VISUAL 变量
    )
    if not editor:
        # 平台默认兜底
        if sys.platform == "win32":
            editor = "notepad"
        elif sys.platform == "darwin":
            editor = "open -e"  # TextEdit
        else:
            editor = "nano"

    try:
        subprocess.call(editor.split() + [str(draft_file)])
    except FileNotFoundError:
        # 编辑器不存在时，降级用 os.startfile（Windows）或 xdg-open
        if sys.platform == "win32":
            os.startfile(str(draft_file))
        else:
            subprocess.call(["xdg-open", str(draft_file)])
    return True


# ---------------------------------------------------------------------------
# Schema 校验
# ---------------------------------------------------------------------------

def validate_draft(data: dict) -> list[str]:
    """简单校验必填字段，返回错误列表（空列表表示通过）。"""
    errors = []
    if not data.get("uuid"):
        errors.append("缺少 uuid")
    if not data.get("meta", {}).get("name"):
        errors.append("缺少 meta.name")
    if not data.get("source", {}).get("source_hash"):
        errors.append("缺少 source.source_hash")
    return errors
