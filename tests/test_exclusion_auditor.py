"""V2 exclusions are candidates that require independent semantic verification."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from skillmind import semantic_auditor as semantic
from skillmind.distillation import SCHEMA_VERSION, parse_source_spans


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    call_model = semantic._call_semantic_audit
    text = ("---\nname: evidence\npurpose: Reuse knowledge with assumptions\n"
            "user-invocable: false\ncredits: Example Authors\n---\n\n"
            "Knowledge is reusable when its assumptions are preserved.\n")
    raw = tmp_path / "source.md"
    raw.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    spans = parse_source_spans(text)
    front, body = spans[0], spans[-1]
    draft = {"schema_version": SCHEMA_VERSION, "uuid": "d", "extract_complete": True,
             "source": {"source_hash": sha, "doc_type": "skill"},
             "meta": {"name": "Knowledge reuse", "intent": "Reuse with assumptions",
                      "trigger_keywords": ["knowledge reuse"]},
             "distillation": {
                 "source_spans": spans, "ignored_spans": [], "uncovered": [],
                 "source_units": [
                     {"id": "method", "kind": "concept", "statement": "Preserve assumptions",
                      "span_ids": [body["id"]]},
                     {"id": "flags", "kind": "constraint", "statement": "Purpose and invocation flag",
                      "span_ids": [front["id"]]},
                     {"id": "credit", "kind": "concept", "statement": "Attribution to Example Authors",
                      "span_ids": [front["id"]]}],
                 "sections": [{"id": "s", "title": "Reuse", "items": [
                     {"id": "i", "text": "Preserve assumptions for reuse; user-invocable: false.",
                      "source_ids": ["method", "flags"]}]}],
                 "excluded": [{"source_id": "credit", "reason_code": "attribution",
                               "span_ids": [front["id"]], "source_quote": "credits: Example Authors",
                               "reason": "Upstream attribution only."}]} }
    response = {
        "coverage": [{"source_id": "d:" + key, "status": "complete",
                      "reason": "The method and operational flag survive.", "item_ids": ["d:i"]}
                     for key in ("method", "flags")],
        "claims": [{"item_id": "d:" + key, "status": "supported",
                    "reason": "Checked against original method and purpose."}
                   for key in ("i", "meta.name", "meta.intent", "meta.trigger_keywords")],
        "synthesis": {"status": "PASS", "reason": "Reusable principle preserves the constraint."},
        "source_gaps": [],
        "exclusions": [{"source_id": "d:credit", "status": "justified",
                        "scope": "whole_unit", "unit_content": "attribution_only",
                        "reason": "Credits alone identify upstream authors; purpose and flag are retained."}],
        "attribution_check": {"status": "PASS", "reason": "Checked title, intent, triggers, headings and content for credits."}}
    artifact = tmp_path / "published.md"
    artifact.write_text("Canonical content", encoding="utf-8")
    def save():
        artifact.with_suffix(".evidence.json").write_text(json.dumps({
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "draft": draft}), encoding="utf-8")
    def run():
        save()
        return semantic.audit_semantic_source(sha, {"llm": {
            "model": "openai/test", "api_key": "fixture", "max_retries": 0}},
            vault_skills_override=tmp_path)
    monkeypatch.setattr("skillmind.auditor._resolve_source", lambda _: (sha, {"raw_path": str(raw)}))
    monkeypatch.setattr("skillmind.auditor._find_extract_notes", lambda *a, **kw: [artifact])
    monkeypatch.setattr("litellm.completion", lambda **kw: SimpleNamespace(choices=[
        SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(response)))]))
    return SimpleNamespace(draft=draft, response=response, run=run, text=text, call_model=call_model,
                           front=front, body=body)


def proof(case, output="", quote="credits: Example Authors", span=None):
    return {"source_span_ids": ["d:" + (span or case.front)["id"]],
            "source_quote": quote, "output_quote": output,
            "explanation": "Concrete source/output discrepancy."}


def test_justified_exclusion_preserves_mixed_frontmatter_method_and_counts_separately(case):
    report = case.run()
    assert report.verdict == "PASS"
    assert report.coverage_weighted == 100.0
    assert report.n_complete == 2
    assert report.n_excluded == 1
    assert report.to_summary_dict()["n_excluded"] == 1
    assert {item.id for item in report.items} == {"d:method", "d:flags"}


@pytest.mark.parametrize("defect", ["missing", "unknown", "bad_status", "wrong_id", "duplicate", "extra_id", "extra_field", "malformed"])
def test_unverified_exclusions_never_shrink_coverage_denominator(case, defect):
    rows = case.response["exclusions"]
    if defect == "missing":
        case.response.pop("exclusions")
    elif defect == "unknown":
        rows[0]["status"] = "unknown"
    elif defect == "bad_status":
        rows[0]["status"] = []
    elif defect == "wrong_id":
        rows[0]["source_id"] = "credit"
    elif defect == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif defect == "extra_id":
        rows.append({**rows[0], "source_id": "d:invented"})
    elif defect == "extra_field":
        rows[0]["confidence"] = 1
    else:
        case.response["exclusions"] = "justified"
    report = case.run()
    assert report.verdict == "UNKNOWN"
    assert report.source_inventory_status == "UNKNOWN"
    assert report.coverage_weighted is None
    assert report.to_summary_dict()["coverage"] is None


def test_excluding_an_operational_dependency_is_a_grounded_failure(case):
    payload = case.draft["distillation"]
    payload["sections"][0]["items"][0]["source_ids"] = ["method"]
    payload["sections"][0]["items"][0]["text"] = "Preserve assumptions for reuse."
    payload["excluded"].append({"source_id": "flags", "reason_code": "attribution",
                                "span_ids": [case.front["id"]], "source_quote": "user-invocable: false",
                                "reason": "Claimed decoration."})
    case.response["coverage"] = case.response["coverage"][:1]
    case.response["exclusions"].append({"source_id": "d:flags", "status": "unjustified",
                                        "scope": "whole_unit", "unit_content": "method",
                                        "reason": "Invocation flag constrains behavior, not attribution.",
                                        "counterexample": proof(case, quote="user-invocable: false")})
    report = case.run()
    assert report.verdict == "FAIL"
    assert report.source_inventory_status == "FAIL"
    assert report.coverage_weighted is None


@pytest.mark.parametrize("field", ["name", "intent", "trigger_keywords", "section", "body"])
def test_reintroduced_attribution_in_any_published_field_fails(case, field):
    if field == "trigger_keywords":
        case.draft["meta"][field].append("Example Authors")
    elif field in {"name", "intent"}:
        case.draft["meta"][field] += " by Example Authors"
    elif field == "section":
        case.draft["distillation"]["sections"][0]["title"] += " by Example Authors"
    else:
        case.draft["distillation"]["sections"][0]["items"][0]["text"] += " Credits: Example Authors."
    case.response["attribution_check"] = {"status": "FAIL", "reason": "Credits reappear in published output.",
                                           "counterexample": proof(case, "Example Authors")}
    report = case.run()
    assert report.verdict == "FAIL"
    assert report.source_inventory_status == "FAIL"


@pytest.mark.parametrize("defect", ["missing", "bad_status", "extra_field", "empty_quote", "forged_quote"])
def test_attribution_check_requires_closed_shape_and_grounded_negative(case, defect):
    check = case.response["attribution_check"]
    if defect == "missing":
        case.response.pop("attribution_check")
    elif defect == "bad_status":
        check["status"] = []
    elif defect == "extra_field":
        check["confidence"] = 1
    else:
        check.update(status="FAIL", counterexample=proof(case, "" if defect == "empty_quote" else "Invented credit"))
    report = case.run()
    assert report.verdict == "UNKNOWN"
    assert report.source_inventory_status == "UNKNOWN"


def test_grounded_inventory_failure_survives_other_malformed_judgments(case):
    case.response["source_gaps"] = [{"span_id": "d:" + case.front["id"],
                                    "statement": "An invocation condition is absent.", "reason": "Actual omission.",
                                    "counterexample": proof(case, quote="user-invocable: false")}]
    case.response["exclusions"] = None
    case.response["attribution_check"] = []
    case.response["synthesis"] = None
    report = case.run()
    assert report.verdict == "FAIL"
    assert report.source_inventory_status == "FAIL"
    assert report.coverage_weighted is None


def test_ungrounded_unjustified_exclusion_is_unknown(case):
    case.response["exclusions"][0].update(status="unjustified", counterexample=proof(case, quote="invented source"))
    report = case.run()
    assert report.verdict == "UNKNOWN"
    assert report.coverage_weighted is None


def test_grounded_exclusion_failure_dominates_malformed_other_dimensions(case):
    case.response["exclusions"][0].update(
        status="unjustified", reason="Candidate credit unit hides a real invocation constraint.",
        counterexample=proof(case, quote="user-invocable: false"))
    case.response["attribution_check"] = []
    case.response["source_gaps"] = None
    case.response["claims"] = None
    case.response["synthesis"] = None
    report = case.run()
    assert report.verdict == "FAIL"
    assert report.source_inventory_status == "FAIL"
    assert report.support_status == "UNKNOWN"
    assert report.coverage_weighted is None


def test_v2_without_exclusions_still_requires_explicit_empty_decisions(case):
    payload = case.draft["distillation"]
    payload["excluded"] = []
    payload["source_units"] = payload["source_units"][:2]
    case.response["exclusions"] = []
    assert case.run().verdict == "PASS"
    case.response.pop("exclusions")
    report = case.run()
    assert report.verdict == "UNKNOWN"
    assert report.coverage_weighted is None


def test_v2_audit_prompt_namespaces_exclusions_and_keeps_original(case):
    payload = semantic._audit_payload([case.draft])[0]
    assert payload["excluded"][0]["source_id"] == "d:credit"
    assert payload["excluded"][0]["span_ids"] == ["d:" + case.front["id"]]
    cfg = {"llm": {"model": "openai/test", "api_key": "fixture", "max_retries": 0}}
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(case.response)))])
    with patch("litellm.completion", return_value=response) as completion:
        case.call_model(case.text, [case.draft], cfg)
    prompt = completion.call_args.kwargs["messages"][1]["content"]
    assert case.text in prompt
    assert "Expected exclusion ids: [\"d:credit\"]" in prompt
    assert "Expected source ids: [\"d:method\", \"d:flags\"]" in prompt
    assert "attribution_check" in prompt
    assert semantic.PROMPT_VERSION == "semantic_audit_v5"


@pytest.mark.parametrize("defect", ["legacy", "fragment", "mixed", "method", "unknown"])
def test_entire_excluded_unit_requires_explicit_pure_attribution_evidence(case, defect):
    row = case.response["exclusions"][0]
    row.update(scope="whole_unit", unit_content="attribution_only")
    if defect == "legacy":
        row.pop("scope"); row.pop("unit_content")
    elif defect == "fragment":
        row["scope"] = "source_quote"
    else:
        row["unit_content"] = defect
    report = case.run()
    assert report.verdict == "UNKNOWN"
    assert report.source_inventory_status == "UNKNOWN"
    assert report.coverage_weighted is None
    with pytest.raises(ValueError, match="exclusion"):
        semantic.validate_exclusion_approval([case.draft], case.response)


def test_shared_approval_checks_whole_unit_without_using_kind_as_allowlist(case):
    case.response["exclusions"][0].update(scope="whole_unit", unit_content="attribution_only")
    assert case.draft["distillation"]["source_units"][-1]["kind"] == "concept"
    semantic.validate_exclusion_approval([case.draft], case.response)
    assert case.run().verdict == "PASS"


def test_inventory_prompt_separates_purpose_and_credits_before_stable_ids(case):
    from skillmind import distillation
    response = {"units": [{"kind": "purpose", "statement": "Purpose and invocation flag",
                           "span_ids": [case.front["id"]]},
                          {"kind": "concept", "statement": "Upstream credits",
                           "span_ids": [case.front["id"]]}], "ignored_spans": []}
    captured = []
    def provider(**kw):
        captured.append(kw["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(response)))])
    with patch("litellm.completion", side_effect=provider):
        units, ignored = distillation._inventory([case.front], {"llm": {"model":"openai/test", "api_key":"fixture", "max_retries":0}})
    prompt = " ".join(m["content"] for m in captured[0])
    assert "separate source units" in prompt
    assert "purpose" in prompt and "credits" in prompt and "ignored_spans" in prompt
    assert len(units) == 2 and not ignored
    assert units[0]["span_ids"] == units[1]["span_ids"]
