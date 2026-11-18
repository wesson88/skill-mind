"""2.4.0 双门禁、冻结清单与保真修复。不调用真实 LLM。"""

from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

import skillmind
import skillmind.extractor as extractor
from skillmind import _version_from_pyproject
from skillmind.auditor import PASS_COVERAGE_THRESHOLD
from skillmind.chunker import Chunker, join_chunks_for_prompt, pack_chunks_for_prompt
from skillmind.cleaner import _html_to_markdown
from skillmind.config import AUDIT_PROMPT_VERSION, CURRENT_PROMPT_VERSION, SCHEMA_VERSION
from skillmind.extractor import (
    _IncompleteJSONError,
    _load_extract_cache,
    _normalize_unit,
    _parse_json_response,
    _save_extract_cache,
    cache_param_token,
    combine_batch_results,
    extraction_cache_params,
    read_extract_cache_any,
)
from skillmind.gates import (
    AuditChecklist,
    ChecklistItem,
    build_audit_summary,
    evaluate_completeness,
    evaluate_executability,
    load_checklist,
    merge_critical_items,
    read_frontmatter,
    require_same_source,
    save_checklist,
    scan_critical_anchors,
    units_from_extract_markdown,
)
from skillmind.renderer import render_to_markdown


def _sourced_rule() -> tuple[str, dict]:
    source = (
        "When the token limit is 8000, reject the request and record status 429. "
        "Health checks are exempt."
    )
    rule = {
        "condition": "token limit is 8000",
        "judgment": "over the published cap",
        "action": "reject the request",
        "verification": "record status 429",
        "boundary": "Health checks are exempt",
        "source_quote": "When the token limit is 8000, reject the request and record status 429.",
    }
    return source, rule


class GateTests(unittest.TestCase):
    def test_critical_missing_fails_even_when_coverage_is_high(self) -> None:
        items = [
            ChecklistItem(
                id=f"i-{index:04d}",
                section="body",
                kind="concept",
                statement=f"concept {index} stays in the translation",
                critical=False,
            )
            for index in range(1, 20)
        ]
        items.append(ChecklistItem(
            id="i-0020",
            section="safety",
            kind="safety_limit",
            statement="Never blanket-approve a tool call",
            critical=True,
            anchors=["Never blanket-approve a tool call"],
        ))
        verify = {item.id: {"status": "complete", "notes": ""} for item in items}
        extract = "这是一段按章节翻译的说明，覆盖率看起来很高。" * 40
        result = evaluate_completeness(
            items,
            verify,
            executed=True,
            extract_text=extract,
            source="frozen:chk-test",
        )
        self.assertEqual(result["coverage"], 95.0)
        self.assertGreaterEqual(result["coverage"], PASS_COVERAGE_THRESHOLD)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["critical_missing"][0]["id"], "i-0020")
        self.assertEqual(result["total"], 20)

        summary = build_audit_summary(
            source_hash="abc",
            source_title="safety",
            completeness=result,
            executability=evaluate_executability([], extract, executed=False),
            checklist=None,
            checklist_source="frozen:chk-test",
            prompt_version=CURRENT_PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            quality_current=True,
        )
        for key in ("source_hash", "coverage", "verdict", "needs_human_review", "missing"):
            self.assertIn(key, summary)
        self.assertEqual(summary["verdict"], "FAIL")
        self.assertEqual(summary["counts"]["complete"], 19)
        self.assertEqual(summary["counts"]["missing"], 1)
        self.assertEqual(summary["counts"]["total"], 20)
        self.assertTrue(summary["missing"])

    def test_translation_and_slogan_do_not_pass_executability(self) -> None:
        translated = evaluate_executability(
            [{
                "key_concepts": [{
                    "title": "Chapter 1",
                    "explanation": "这是对原文整节的逐句翻译，没有形成可执行决定。" * 20,
                }],
                "learning_enhancement": {"plain_summary": "务必小心，保证质量"},
                "rules": [],
                "constraints": [],
            }],
            "Never blanket-approve a tool call without an owner.",
            executed=True,
        )
        self.assertEqual(translated["status"], "FAIL")

        slogan = evaluate_executability(
            [{
                "rules": [{
                    "condition": "always",
                    "judgment": "quality matters",
                    "action": "be careful",
                    "verification": "unknown",
                    "boundary": "unknown",
                    "source_quote": "be careful and follow best practices",
                }],
                "constraints": [],
                "key_concepts": [],
            }],
            "be careful and follow best practices when you ship.",
            executed=True,
        )
        self.assertEqual(slogan["status"], "FAIL")
        self.assertEqual(slogan["structure"], "FAIL")

    def test_unsourced_rule_fails(self) -> None:
        result = evaluate_executability(
            [{
                "rules": [{
                    "condition": "request has no owner",
                    "judgment": "ownership is required",
                    "action": "reject the request",
                    "verification": "audit log contains reject",
                    "boundary": "health checks are exempt",
                    "source_quote": "this sentence is not in the source",
                }],
                "constraints": [],
                "key_concepts": [],
            }],
            "When the token limit is 8000, reject the request.",
            executed=True,
        )
        self.assertEqual(result["source_support"], "FAIL")
        self.assertEqual(result["status"], "FAIL")

    def test_sourced_rule_can_pass(self) -> None:
        source, rule = _sourced_rule()
        result = evaluate_executability(
            [{"rules": [rule], "constraints": [], "key_concepts": []}],
            source,
            executed=True,
        )
        self.assertEqual(result["status"], "PASS", result["reasons"])

    def test_duplicate_rules_fail(self) -> None:
        source, rule = _sourced_rule()
        result = evaluate_executability(
            [{"rules": [rule, dict(rule)], "constraints": [], "key_concepts": []}],
            source,
            executed=True,
        )
        self.assertEqual(result["duplication"], "FAIL")
        self.assertEqual(result["status"], "FAIL")

    def test_gates_not_run_are_unknown_not_pass(self) -> None:
        completeness = evaluate_completeness([], {}, executed=False)
        executability = evaluate_executability([], "source text is present", executed=False)
        summary = build_audit_summary(
            source_hash="abc",
            source_title="none",
            completeness=completeness,
            executability=executability,
            checklist=None,
            checklist_source="not_run",
            prompt_version=CURRENT_PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            quality_current=True,
        )
        self.assertEqual(completeness["status"], "UNKNOWN")
        self.assertEqual(executability["status"], "UNKNOWN")
        self.assertEqual(summary["verdict"], "UNKNOWN")
        self.assertNotEqual(summary["verdict"], "PASS")
        self.assertTrue(summary["needs_human_review"])
        self.assertIsNone(summary["coverage"])
        self.assertEqual(summary["gates"]["completeness"], "UNKNOWN")
        self.assertEqual(summary["gates"]["executability"], "UNKNOWN")

    def test_unknown_slots_are_not_invented(self) -> None:
        normalized = _normalize_unit(
            {"rules": [{"condition": "if over limit", "action": "stop"}]},
            "if over limit, stop the run immediately and page the owner",
        )
        rule = normalized["rules"][0]
        self.assertEqual(rule["judgment"], "unknown")
        self.assertEqual(rule["verification"], "unknown")
        self.assertEqual(rule["boundary"], "unknown")
        self.assertEqual(rule["source_quote"], "unknown")
        self.assertNotIn("page the owner", json.dumps(rule, ensure_ascii=False))


class ChecklistTests(unittest.TestCase):
    def test_frozen_checklist_keeps_id_and_total(self) -> None:
        checklist = AuditChecklist(
            checklist_id="chk-frozen",
            source_hash="abc123",
            prompt_version="extract_v10",
            schema_version="skill_unit_v2",
            audit_prompt_version="audit_v2",
            items=[
                ChecklistItem(
                    id="i-0007",
                    section="policy",
                    kind="concept",
                    statement="owners stay on the request",
                    critical=False,
                ),
                ChecklistItem(
                    id="i-0008",
                    section="safety",
                    kind="safety_limit",
                    statement="token limit is 8000",
                    critical=True,
                    anchors=["token limit is 8000"],
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checks.json"
            save_checklist(path, checklist)
            first = load_checklist(path)
            second = load_checklist(path)
        self.assertEqual([item.id for item in first.items], ["i-0007", "i-0008"])
        self.assertEqual([item.id for item in second.items], [item.id for item in first.items])
        self.assertEqual(first.checklist_id, "chk-frozen")
        self.assertEqual(second.total, first.total)
        self.assertEqual(first.total, 2)
        self.assertEqual(first.source_hash, "abc123")
        self.assertEqual(first.prompt_version, "extract_v10")
        self.assertEqual(first.schema_version, "skill_unit_v2")
        self.assertEqual(first.audit_prompt_version, "audit_v2")

        def summarize(status: str) -> dict:
            completeness = evaluate_completeness(
                first.items,
                {item.id: {"status": status} for item in first.items},
                executed=True,
                extract_text="token limit is 8000 remains in the note",
                source=f"frozen:{first.checklist_id}",
            )
            return build_audit_summary(
                source_hash="abc123",
                source_title="safety",
                completeness=completeness,
                executability=evaluate_executability([], "token limit is 8000", executed=False),
                checklist=first,
                checklist_source=f"frozen:{first.checklist_id}",
                prompt_version=CURRENT_PROMPT_VERSION,
                schema_version=SCHEMA_VERSION,
                quality_current=True,
            )

        earlier = summarize("complete")
        later = summarize("weak")
        self.assertEqual(earlier["checklist"]["id"], later["checklist"]["id"])
        self.assertEqual(earlier["checklist"]["total"], later["checklist"]["total"])
        self.assertEqual(earlier["counts"]["total"], later["counts"]["total"])
        self.assertEqual(earlier["checklist"]["total"], 2)
        self.assertEqual(earlier["completeness"]["source"], "frozen:chk-frozen")
        self.assertEqual(earlier["counts"]["complete"], 2)
        self.assertEqual(later["counts"]["weak"], 2)
        with self.assertRaises(ValueError):
            require_same_source(first, "different-hash")

    def test_critical_scan_is_stable_and_keeps_safety_lines(self) -> None:
        source = "\n".join([
            "# Safety",
            "Never blanket-approve a tool call.",
            "Deferred review date: 2026-12-01.",
            "Regional policy: EU requests stay in eu-central.",
            "Token limit is 8000 per request.",
            "Rate limit is 20 requests per minute.",
            "Recursion limit is 4.",
        ])
        first = merge_critical_items([], source)
        second = merge_critical_items([], source)
        self.assertEqual([item["id"] for item in first], [item["id"] for item in second])
        self.assertEqual(len(first), len(second))
        blob = " ".join(item["statement"] for item in first).casefold()
        for phrase in (
            "blanket-approve",
            "2026-12-01",
            "eu-central",
            "8000",
            "rate limit",
            "recursion limit",
        ):
            self.assertIn(phrase, blob)
        self.assertEqual(scan_critical_anchors(source)[0]["critical"], True)


class CompatibilityTests(unittest.TestCase):
    def test_legacy_cache_and_old_draft_do_not_claim_current_quality(self) -> None:
        params = extraction_cache_params({"llm": {"model": "mock/model", "chunk_size_tokens": 1500}})
        with tempfile.TemporaryDirectory() as tmp:
            original = extractor.EXTRACT_CACHE_DIR
            extractor.EXTRACT_CACHE_DIR = Path(tmp)
            try:
                legacy_path = Path(tmp) / f"abc_{CURRENT_PROMPT_VERSION}_{cache_param_token(params)}.json"
                legacy_path.write_text(
                    json.dumps([{"meta": {"name": "Old draft"}}]),
                    encoding="utf-8",
                )
                record = read_extract_cache_any(legacy_path)
                self.assertTrue(record["legacy"])
                self.assertFalse(record["complete"])
                self.assertEqual(record["schema_version"], "legacy")
                self.assertEqual(record["units"][0]["meta"]["name"], "Old draft")
                self.assertIsNone(_load_extract_cache("abc", CURRENT_PROMPT_VERSION, params))
            finally:
                extractor.EXTRACT_CACHE_DIR = original

        old = {
            "uuid": "skill-abc12345",
            "meta": {"name": "Legacy note", "intent": "keep the old shape"},
            "procedure": [{"seq": 1, "action": "Check", "command": ""}],
            "learning_enhancement": {"plain_summary": "An older extraction."},
            "source": {"source_hash": "abc12345", "doc_type": "skill"},
            "prompt_version": "extract_v9",
        }
        text = render_to_markdown(old, cfg={})
        self.assertIn("## 🧩 执行流程", text)
        self.assertNotIn("skill_unit_v2", text)

        source, rule = _sourced_rule()
        completeness = evaluate_completeness(
            [ChecklistItem("i-0001", "s", "concept", "keep the cap visible", False)],
            {"i-0001": {"status": "complete"}},
            executed=True,
            extract_text="keep the cap visible",
        )
        executability = evaluate_executability(
            [{"rules": [rule], "constraints": [], "key_concepts": []}],
            source,
            executed=True,
        )
        self.assertEqual(completeness["status"], "PASS")
        self.assertEqual(executability["status"], "PASS")
        blocked = build_audit_summary(
            source_hash="abc",
            source_title="legacy",
            completeness=completeness,
            executability=executability,
            checklist=None,
            checklist_source="generated",
            prompt_version="extract_v9",
            schema_version="legacy",
            quality_current=False,
        )
        self.assertEqual(blocked["verdict"], "FAIL")
        self.assertFalse(blocked["quality_current"])
        self.assertIn("quality_block", blocked)
        for key in ("coverage", "verdict", "missing", "needs_human_review"):
            self.assertIn(key, blocked)

    def test_versions_are_unified(self) -> None:
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], "2.4.0")
        self.assertEqual(_version_from_pyproject(), "2.4.0")
        self.assertEqual(skillmind.__version__, "2.4.0")
        self.assertEqual(CURRENT_PROMPT_VERSION, "extract_v10")
        self.assertEqual(SCHEMA_VERSION, "skill_unit_v2")
        self.assertEqual(AUDIT_PROMPT_VERSION, "audit_v2")
        self.assertEqual(PASS_COVERAGE_THRESHOLD, 90.0)


class FidelityTests(unittest.TestCase):
    def test_short_fragment_and_oversize_chunk_keep_generic(self) -> None:
        code = "```ts\nexport const p: Promise<Task> = null;\n```"
        text = (
            "Para A. " + ("alpha " * 80)
            + "\n\n" + code + "\n\n"
            + "Para B. " + ("beta " * 80)
        )
        chunker = Chunker(
            chunk_size_tokens=30,
            chunk_overlap_tokens=0,
            min_chunk_size_tokens=20,
            chars_per_token=4,
        )
        joined = "\n".join(chunk["content"] for chunk in chunker.chunk(text))
        self.assertIn("Promise<Task>", joined)
        self.assertNotIn("<<CODE_BLOCK_", joined)

        body = "intro\n\n```ts\nconst p: Promise<Task> = run();\n```\n\n" + ("tail " * 80)
        chunks = [{"chunk_id": 3, "content": body, "source_section": "Limits"}]
        packed_text, over = join_chunks_for_prompt(chunks, max_chars=80)
        self.assertIn("Promise<Task>", packed_text)
        self.assertNotIn("已省略", packed_text)
        self.assertTrue(over)
        packed, leftover, dropped = pack_chunks_for_prompt(chunks, max_chars=80)
        combined = packed + "\n" + "\n".join(chunk["content"] for chunk in leftover)
        self.assertIn("Promise<Task>", combined)
        self.assertNotIn("已省略", combined)
        self.assertFalse(dropped)

    def test_cleaner_keeps_generic_and_strips_real_tags(self) -> None:
        html = (
            "<article><h1>Limits</h1>"
            "<p>Return type is Promise<Task> and the cap is 8000 tokens.</p>"
            "<div>Hello<br>world</div></article>"
        )
        markdown = _html_to_markdown(html)
        self.assertIn("Promise<Task>", markdown)
        self.assertIn("8000", markdown)
        self.assertNotIn("<p>", markdown)
        self.assertNotIn("<div>", markdown)
        self.assertNotIn("<br>", markdown)
        self.assertNotIn("Promise ;", markdown)

    def test_cache_param_change_and_failed_cache_are_not_reused(self) -> None:
        params = extraction_cache_params({
            "llm": {"model": "mock/model", "chunk_size_tokens": 1000, "temperature": 0.2},
        })
        with tempfile.TemporaryDirectory() as tmp:
            original = extractor.EXTRACT_CACHE_DIR
            extractor.EXTRACT_CACHE_DIR = Path(tmp)
            try:
                _save_extract_cache(
                    "abc",
                    CURRENT_PROMPT_VERSION,
                    [{"meta": {"name": "A"}}],
                    params,
                    complete=True,
                )
                self.assertIsNotNone(_load_extract_cache("abc", CURRENT_PROMPT_VERSION, params))
                resized = dict(params, chunk_size_tokens=2000)
                self.assertIsNone(_load_extract_cache("abc", CURRENT_PROMPT_VERSION, resized))
                remodeled = dict(params, model="mock/other")
                self.assertIsNone(_load_extract_cache("abc", CURRENT_PROMPT_VERSION, remodeled))

                _save_extract_cache(
                    "failed",
                    CURRENT_PROMPT_VERSION,
                    [{"meta": {"name": "partial"}}],
                    params,
                    complete=False,
                    truncated=True,
                    failed_batches=[2],
                )
                self.assertIsNone(_load_extract_cache("failed", CURRENT_PROMPT_VERSION, params))

                forged = extractor._extract_cache_path(
                    "forged",
                    CURRENT_PROMPT_VERSION,
                    cache_param_token(params),
                )
                forged.write_text(json.dumps({
                    "cache_version": 2,
                    "complete": True,
                    "truncated": True,
                    "failed_batches": [],
                    "legacy": False,
                    "schema_version": params["schema_version"],
                    "model": params["model"],
                    "params": params,
                    "units": [{"meta": {"name": "cut"}}],
                }), encoding="utf-8")
                self.assertIsNone(_load_extract_cache("forged", CURRENT_PROMPT_VERSION, params))

                failed_batch = extractor._extract_cache_path(
                    "batch",
                    CURRENT_PROMPT_VERSION,
                    cache_param_token(params),
                )
                failed_batch.write_text(json.dumps({
                    "cache_version": 2,
                    "complete": True,
                    "truncated": False,
                    "failed_batches": [1],
                    "legacy": False,
                    "schema_version": params["schema_version"],
                    "model": params["model"],
                    "params": params,
                    "units": [{"meta": {"name": "batch"}}],
                }), encoding="utf-8")
                self.assertIsNone(_load_extract_cache("batch", CURRENT_PROMPT_VERSION, params))
            finally:
                extractor.EXTRACT_CACHE_DIR = original

    def test_same_name_batches_merge_constraints_and_rules(self) -> None:
        first = {
            "meta": {"name": "Safety Policy"},
            "constraints": [{"statement": "Never blanket-approve"}],
            "rules": [{"condition": "owned", "action": "allow"}],
        }
        second = {
            "meta": {"name": "Safety Policy"},
            "constraints": [{"statement": "token limit 8000"}],
            "rules": [{"condition": "over cap", "action": "reject"}],
        }
        merged = combine_batch_results([[first], [second]], doc_type="skill")
        self.assertEqual(len(merged), 1)
        statements = {item["statement"] for item in merged[0]["constraints"]}
        actions = {item["action"] for item in merged[0]["rules"]}
        self.assertIn("Never blanket-approve", statements)
        self.assertIn("token limit 8000", statements)
        self.assertIn("allow", actions)
        self.assertIn("reject", actions)

    def test_truncated_json_is_not_a_successful_parse(self) -> None:
        raw = """[
  {"meta": {"name": "Keep"}, "constraints": [{"statement": "token limit 8000"}]},
  {"meta": {"name": "Tail"}, "rules": [{"condition": "later"
"""
        with self.assertRaises(_IncompleteJSONError):
            _parse_json_response(raw)

    def test_rendered_rules_roundtrip_for_audit(self) -> None:
        draft = {
            "uuid": "skill-abc12345",
            "meta": {"name": "Safety"},
            "schema_version": "skill_unit_v2",
            "prompt_version": "extract_v10",
            "extract_complete": True,
            "constraints": [{
                "kind": "safety_limit",
                "statement": "token limit is 8000",
                "value": "8000",
                "source_quote": "token limit is 8000",
                "support": "sourced",
            }],
            "rules": [{
                "condition": "token limit is 8000",
                "judgment": "over the cap",
                "action": "reject the request",
                "verification": "status 429",
                "boundary": "health checks are exempt",
                "source_quote": "token limit is 8000",
                "support": "sourced",
            }],
            "source": {"source_hash": "abc12345", "doc_type": "skill"},
        }
        text = render_to_markdown(draft, cfg={})
        units = units_from_extract_markdown(text)
        self.assertEqual(units[0]["rules"][0]["action"], "reject the request")
        self.assertEqual(units[0]["rules"][0]["boundary"], "health checks are exempt")
        self.assertIn("token limit is 8000", units[0]["constraints"][0]["source_quote"])
        frontmatter = read_frontmatter(text)
        self.assertEqual(frontmatter.get("schema_version"), "skill_unit_v2")
        self.assertEqual(frontmatter.get("prompt_version"), "extract_v10")
        self.assertIs(frontmatter.get("extract_complete"), True)


if __name__ == "__main__":
    unittest.main()
