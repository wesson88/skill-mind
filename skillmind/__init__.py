"""SkillMind - 开源 Claude Code Skill 知识提炼系统"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _dist_version


def _resolve_version() -> str:
    """Return the distribution version.

    The only declared number is ``[project].version`` in ``pyproject.toml``.
    An installed copy reads that value from package metadata. A source tree
    that has not been installed yet reads the same file directly.
    """
    try:
        return _dist_version("skillmind")
    except PackageNotFoundError:
        return _version_from_pyproject()


def _version_from_pyproject() -> str:
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


__version__ = _resolve_version()
