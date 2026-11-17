"""Renderer tests for the optional Agent Skill SKILL.md output."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from skillmind.renderer import (
    publish_skill,
    render_skill_markdown,
    render_to_markdown,
    skill_slug,
)


def _draft(**overrides) -> dict:
    data = {
        "uuid": "skill-abc12345",
        "meta": {
            "name": "Zero-downtime PostgreSQL Upgrade",
            "intent": "upgrading PostgreSQL major versions without downtime",
            "trigger_keywords": ["postgresql", "pg_upgrade"],
        },
        "preconditions": ["A current backup exists"],
        "procedure": [
            {"seq": 1, "action": "Check the cluster", "command": "pg_upgrade --check"},
            {"seq": 2, "action": "Run the upgrade", "command": ""},
        ],
        "decision_points": [
            {"condition": "version >= 16", "then": "use pg_upgrade", "else": "dump and restore"},
        ],
        "halt_conditions": ["pg_upgrade --check fails"],
        "rollback_actions": ["Start the old instance"],
        "learning_enhancement": {
            "plain_summary": "Upgrade PostgreSQL with pg_upgrade while the app stays up.",
        },
        "source": {
            "source_url": "https://example.com/skills/pg/SKILL.md",
            "file_path": "skills/pg/SKILL.md",
            "source_hash": "abc12345",
        },
    }
    data.update(overrides)
    return data


class SkillMarkdownTests(unittest.TestCase):
    def test_slug_is_kebab_case(self) -> None:
        self.assertEqual(
            skill_slug("Zero-downtime PostgreSQL Upgrade"),
            "zero-downtime-postgresql-upgrade",
        )
        self.assertEqual(skill_slug("零停机升级", fallback="skill-abc12345"), "skill-abc12345")
        self.assertLessEqual(len(skill_slug("A" * 200)), 64)

    def test_skill_markdown_shape(self) -> None:
        text = render_skill_markdown(_draft())
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: zero-downtime-postgresql-upgrade\n", text)
        self.assertIn("description:", text)
        self.assertIn("Use when: upgrading PostgreSQL major versions without downtime. Upgrade", text)
        desc_lines = [line for line in text.splitlines() if line.startswith("description:")]
        self.assertEqual(len(desc_lines), 1)
        self.assertIn("## Preconditions", text)
        self.assertIn("- A current backup exists", text)
        self.assertIn("## Steps", text)
        self.assertIn("pg_upgrade --check", text)
        self.assertIn("## Halt conditions", text)
        self.assertIn("pg_upgrade --check fails", text)
        self.assertIn("## Source", text)
        self.assertIn("https://example.com/skills/pg/SKILL.md", text)
        self.assertIn("<!-- skillmind-uuid: skill-abc12345 -->", text)
        self.assertNotIn("📌", text)

    def test_obsidian_note_still_carries_source_hash(self) -> None:
        text = render_to_markdown(_draft(), cfg={})
        self.assertIn("source_hash: abc12345", text)
        self.assertIn("## 🧩 执行流程", text)

    def test_publish_is_idempotent_and_collision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = publish_skill(_draft(), root)
            self.assertEqual(
                first,
                root / "zero-downtime-postgresql-upgrade" / "SKILL.md",
            )
            updated = _draft()
            updated["procedure"] = [{"seq": 1, "action": "Recheck", "command": "pg_upgrade --check"}]
            again = publish_skill(updated, root)
            self.assertEqual(again, first)
            self.assertIn("Recheck", first.read_text(encoding="utf-8"))
            self.assertEqual(len(list(root.glob("*/SKILL.md"))), 1)

            other = _draft(uuid="skill-zzz99999")
            other_path = publish_skill(other, root)
            self.assertNotEqual(other_path, first)
            self.assertTrue(other_path.name == "SKILL.md")
            self.assertIn("zzz99999", other_path.parent.name)
            self.assertIn("skill-abc12345", first.read_text(encoding="utf-8"))
            self.assertIn("skill-zzz99999", other_path.read_text(encoding="utf-8"))

            renamed = _draft()
            renamed["meta"] = dict(renamed["meta"], name="Postgres major upgrade")
            moved = publish_skill(renamed, root)
            self.assertEqual(moved.parent.name, "postgres-major-upgrade")
            self.assertFalse(first.exists())
            self.assertTrue(other_path.exists())


if __name__ == "__main__":
    unittest.main()
