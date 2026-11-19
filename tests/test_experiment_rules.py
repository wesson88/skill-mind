"""数据驱动实验规则。新增一条只加 JSONL，不新增测试函数。

这些断言是 mock / 夹具，不是真实模型效果。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from skillmind.auditor import PASS_COVERAGE_THRESHOLD
from skillmind.chunker import pack_chunks_for_prompt
from skillmind.cleaner import _html_to_markdown
from skillmind.cli import _ingest_counts
from skillmind.extractor import (
    _IncompleteJSONError,
    _load_extract_cache,
    _parse_json_response,
    _save_extract_cache,
    cache_param_token,
    combine_batch_results,
    extraction_cache_params,
    read_extract_cache_any,
)
from skillmind.fidelity import assess_faithfulness, quote_in_source
from skillmind.gates import (
    AuditChecklist,
    ChecklistItem,
    build_audit_summary,
    evaluate_completeness,
    evaluate_executability,
    find_critical_gaps,
    load_checklist,
    recover_critical_gaps,
    require_same_source,
    save_checklist,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "tests" / "fixtures" / "experiment_rules.jsonl"


def _load_rules() -> list[dict]:
    rules = []
    for line in RULES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rules.append(json.loads(line))
    ids = [rule["id"] for rule in rules]
    if len(ids) != len(set(ids)):
        raise AssertionError("experiment rule ids are not unique")
    return rules


def _item(case: dict) -> ChecklistItem:
    candidate = case.get("candidate") or {}
    anchors = candidate.get("anchors") or [case.get("source_excerpt") or ""]
    return ChecklistItem(
        id=case["id"],
        section=case.get("kind") or "fixture",
        kind="constraint",
        statement=str(candidate.get("statement") or anchors[0]),
        critical=True,
        anchors=[str(anchor) for anchor in anchors],
    )


def _check(case: dict) -> None:
    expect = case["expect"]
    candidate = case.get("candidate") or {}
    source = case.get("source_excerpt") or ""
    if "support" in expect:
        got = "sourced" if quote_in_source(candidate.get("source_quote") or "", source) else "unsupported"
        if got != expect["support"]:
            raise AssertionError(f"{case['id']}: support {got} != {expect['support']}")
    if "faithfulness" in expect and "critical_gap" not in expect:
        got = assess_faithfulness(candidate.get("action") or "", candidate.get("source_quote") or "")
        if got != expect["faithfulness"]:
            raise AssertionError(f"{case['id']}: faithfulness {got} != {expect['faithfulness']}")
    if "critical_gap" in expect:
        gaps = find_critical_gaps(source, candidate.get("units") or [])
        if bool(gaps) != bool(expect["critical_gap"]):
            raise AssertionError(f"{case['id']}: gaps={bool(gaps)} != {expect['critical_gap']} {[g.get('statement') for g in gaps]}")
        if "faithfulness" in expect:
            rule = ((candidate.get("units") or [{}])[0].get("rules") or [{}])[0]
            action = candidate.get("action") or rule.get("action") or ""
            quote = candidate.get("source_quote") or rule.get("source_quote") or ""
            got = assess_faithfulness(action, quote)
            if got != expect["faithfulness"]:
                raise AssertionError(f"{case['id']}: faithfulness {got} != {expect['faithfulness']}")
    if "item_status" in expect:
        item = _item(case)
        result = evaluate_completeness(
            [item],
            {},
            executed=True,
            extract_text=candidate.get("extract") or "",
            source_text=source,
            quotes=candidate.get("quotes") or [],
            source="fixture",
        )
        status = result["per_item"][0]["status"]
        if status != expect["item_status"]:
            raise AssertionError(f"{case['id']}: item {status} != {expect['item_status']}")
    if expect.get("placeholder"):
        from skillmind.fidelity import is_placeholder_only
        if not is_placeholder_only(candidate.get("statement") or source):
            raise AssertionError(f"{case['id']}: expected placeholder")
    if "verdict" in expect or "verdict_not" in expect or "quality_scope" in expect or "hallucination_checked" in expect:
        summary = build_audit_summary(
            source_hash=case.get("source_hash") or "fixture",
            source_title=case["id"],
            completeness={"status": candidate.get("completeness", "FAIL"), "coverage": 50, "per_item": [], "complete": 0, "weak": 0, "missing": 1, "total": 1, "critical_missing": [], "threshold": 90, "source": "fixture"},
            executability={"status": candidate.get("executability", "FAIL"), "structure": "UNKNOWN", "source_support": "UNKNOWN", "duplication": "UNKNOWN", "faithfulness": candidate.get("faithfulness", "UNKNOWN"), "reasons": []},
            checklist=None,
            checklist_source="fixture",
            prompt_version="extract_v10",
            schema_version="skill_unit_v2",
            quality_current=bool(candidate.get("quality_current", True)),
            hallucination_checked=bool(candidate.get("hallucination_checked", False)),
            hallucination_count=candidate.get("hallucination_count"),
        )
        if "verdict" in expect and summary["verdict"] != expect["verdict"]:
            raise AssertionError(f"{case['id']}: verdict {summary['verdict']}")
        if expect.get("verdict_not") and summary["verdict"] == expect["verdict_not"]:
            raise AssertionError(f"{case['id']}: verdict must not be {expect['verdict_not']}")
        if "quality_scope" in expect and summary["quality_current_scope"] != expect["quality_scope"]:
            raise AssertionError(case["id"])
        if "hallucination_checked" in expect:
            if summary["hallucination"]["checked"] != expect["hallucination_checked"]:
                raise AssertionError(case["id"])
            if expect.get("no_fake_zero"):
                if "hallucination_rate" in summary:
                    raise AssertionError(f"{case['id']}: unchecked hallucination must not invent a rate")
                if summary["hallucination"]["count"] == 0:
                    raise AssertionError(f"{case['id']}: unchecked count must not be 0")
        if "hallucination_count" in expect and summary["hallucination"]["count"] != expect["hallucination_count"]:
            raise AssertionError(case["id"])
    if case["id"] == "exp-recover-closes-gap":
        def _add(_source, _gaps):
            return [{"constraints": [{"statement": "每个请求检查授权，防止 IDOR", "source_quote": "Check authorization on every request (A01:2025, IDOR)."}]}]
        _merged, after = recover_critical_gaps(source, [], _add)
        if expect.get("gaps_before") and not find_critical_gaps(source, []):
            raise AssertionError(case["id"])
        if expect.get("gaps_after") is False and after:
            raise AssertionError(f"{case['id']}: still {after}")
    if case["id"] == "exp-recover-still-gap-not-complete":
        merged, after = recover_critical_gaps(source, [], None)
        if not after:
            raise AssertionError(case["id"])
        if merged:
            raise AssertionError(case["id"])
    if case["id"] == "exp-checklist-id-stable":
        items = [ChecklistItem(item_id, "s", "concept", f"statement {item_id} is long enough", False) for item_id in candidate["ids"]]
        checklist = AuditChecklist("chk-frozen", "abc123", "extract_v10", "skill_unit_v2", "audit_v2", items)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.json"
            save_checklist(path, checklist)
            loaded = load_checklist(path)
        if [item.id for item in loaded.items] != candidate["ids"] or loaded.total != expect["total"]:
            raise AssertionError(case["id"])
        if loaded.checklist_id != expect["checklist_id"]:
            raise AssertionError(case["id"])
    if case["id"] == "exp-checklist-hash-mismatch":
        checklist = AuditChecklist("chk-frozen", case["source_hash"], "extract_v10", "skill_unit_v2", "audit_v2", [])
        try:
            require_same_source(checklist, candidate["other_hash"])
        except ValueError:
            return
        raise AssertionError(case["id"])
    if case["id"] == "exp-cache-param-invalidates":
        import skillmind.extractor as extractor
        params = extraction_cache_params({"llm": {"model": candidate["model"], "chunk_size_tokens": 1000}})
        with tempfile.TemporaryDirectory() as tmp:
            old = extractor.EXTRACT_CACHE_DIR
            extractor.EXTRACT_CACHE_DIR = Path(tmp)
            try:
                _save_extract_cache("abc", "extract_v10", [{"meta": {"name": "A"}}], params, complete=True)
                other = dict(params, model=candidate["other_model"])
                if _load_extract_cache("abc", "extract_v10", other) is not None:
                    raise AssertionError(case["id"])
            finally:
                extractor.EXTRACT_CACHE_DIR = old
    if case["id"] == "exp-cache-truncated-not-reused":
        import skillmind.extractor as extractor
        params = extraction_cache_params({"llm": {"model": "mock/a"}})
        with tempfile.TemporaryDirectory() as tmp:
            old = extractor.EXTRACT_CACHE_DIR
            extractor.EXTRACT_CACHE_DIR = Path(tmp)
            try:
                _save_extract_cache("abc", "extract_v10", [{"meta": {"name": "A"}}], params, complete=True, truncated=True, failed_batches=[1])
                if _load_extract_cache("abc", "extract_v10", params) is not None:
                    raise AssertionError(case["id"])
            finally:
                extractor.EXTRACT_CACHE_DIR = old
    if case["id"] == "exp-cache-old-schema":
        import skillmind.extractor as extractor
        params = extraction_cache_params({"llm": {"model": "mock/a"}})
        with tempfile.TemporaryDirectory() as tmp:
            old = extractor.EXTRACT_CACHE_DIR
            extractor.EXTRACT_CACHE_DIR = Path(tmp)
            try:
                path = Path(tmp) / f"abc_extract_v10_{cache_param_token(params)}.json"
                path.write_text(json.dumps([{"meta": {"name": "Old"}}]), encoding="utf-8")
                record = read_extract_cache_any(path)
                if not record["legacy"] or record["complete"]:
                    raise AssertionError(case["id"])
                if _load_extract_cache("abc", "extract_v10", params) is not None:
                    raise AssertionError(case["id"])
            finally:
                extractor.EXTRACT_CACHE_DIR = old
    if case["id"] == "exp-batch-merge-unique-constraint":
        merged = combine_batch_results([
            [{"meta": {"name": "Safety Policy"}, "constraints": [{"statement": "Never blanket-approve"}], "rules": []}],
            [{"meta": {"name": "Safety Policy"}, "constraints": [{"statement": "token limit 8000"}], "rules": []}],
        ], doc_type="skill")
        statements = {item["statement"] for item in merged[0]["constraints"]}
        if "Never blanket-approve" not in statements or "token limit 8000" not in statements:
            raise AssertionError(case["id"])
    if case["id"] == "exp-chunk-no-omission-note":
        body = "intro\n\n```ts\nconst p: Promise<Task> = run();\n```\n\n" + ("tail " * 80)
        packed, leftover, dropped = pack_chunks_for_prompt(
            [{"chunk_id": 1, "content": body, "source_section": "Limits"}],
            80,
        )
        combined = packed + "\n" + "\n".join(chunk["content"] for chunk in leftover)
        if "Promise<Task>" not in combined or "已省略" in combined or dropped:
            raise AssertionError(case["id"])
    if case["id"] == "exp-json-truncated-not-success":
        raw = '[\n  {"meta": {"name": "Keep"}, "constraints": [{"statement": "token limit 8000"}]},\n  {"meta": {"name": "Tail"}, "rules": [{"condition": "later"'
        try:
            _parse_json_response(raw)
        except _IncompleteJSONError:
            return
        raise AssertionError(case["id"])
    if case["id"] == "exp-cli-skipped-missing-key":
        new_count, skipped = _ingest_counts(candidate["rows"])
        if new_count != expect["new"] or skipped != expect["skipped"]:
            raise AssertionError(case["id"])
    if case["id"] == "exp-high-coverage-critical-still-fails":
        items = [
            ChecklistItem(f"i-{index:04d}", "body", "concept", f"concept {index} stays translated here", False)
            for index in range(1, 20)
        ]
        items.append(ChecklistItem("i-0020", "safety", "safety_limit", "Never blanket-approve a tool call", True, ["Never blanket-approve a tool call"]))
        result = evaluate_completeness(
            items,
            {item.id: {"status": "complete"} for item in items},
            executed=True,
            extract_text="这是一段译文，没有安全句。",
            source="fixture",
        )
        if result["status"] != "FAIL" or result["coverage"] < PASS_COVERAGE_THRESHOLD:
            raise AssertionError(f"{case['id']}: {result['status']} {result['coverage']}")
    if case["id"] == "exp-slogan-not-executable":
        result = evaluate_executability(
            [{"key_concepts": [{"explanation": candidate["explanation"] * 8}], "rules": [], "constraints": []}],
            source,
            executed=True,
        )
        if result["status"] != "FAIL":
            raise AssertionError(case["id"])
    if case["id"] == "exp-unsourced-rule-fails":
        result = evaluate_executability(
            [{"rules": [candidate], "constraints": [], "key_concepts": []}],
            source,
            executed=True,
        )
        if result["status"] != "FAIL":
            raise AssertionError(f"{case['id']}: {result}")
    if "executability" in expect and case["id"] not in {"exp-slogan-not-executable", "exp-unsourced-rule-fails"}:
        return


class ExperimentRuleTests(unittest.TestCase):
    def test_catalog_is_unique_and_labeled(self) -> None:
        rules = _load_rules()
        self.assertGreaterEqual(len(rules), 40)
        for rule in rules:
            self.assertIn(rule["evidence"], {"mock_unit", "real_model"})
            self.assertNotEqual(rule["evidence"], "real_model")
            self.assertTrue(rule["id"].startswith("exp-"))
            self.assertIn("fix", rule)
            self.assertIn("kind", rule)

    def test_every_rule(self) -> None:
        failures = []
        for rule in _load_rules():
            try:
                _check(rule)
            except Exception as exc:
                failures.append(f"{rule['id']}: {exc}")
        if failures:
            self.fail("\n".join(failures))


if __name__ == "__main__":
    unittest.main()
