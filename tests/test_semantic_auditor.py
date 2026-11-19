"""Behavioral regressions for canonical, source-backed semantic audits."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def audit_case(tmp_path, monkeypatch):
    # Import inside fixture so the initial RED reports the missing implementation.
    semantic = importlib.import_module("skillmind.semantic_auditor")
    call_model = semantic._call_semantic_audit
    from skillmind.distillation import SCHEMA_VERSION, parse_source_spans
    raw_text = "# Evidence\n\nKnowledge is reusable when its assumptions are preserved.\n"
    raw = tmp_path / "source.md"
    raw.write_bytes(raw_text.encode("utf-8"))
    sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    spans = parse_source_spans(raw_text)
    draft = {
        "schema_version": SCHEMA_VERSION,
        "uuid": "skill-example",
        "extract_complete": True,
        "source": {"source_hash": sha, "doc_type": "skill"},
        "meta": {"name": "Reusable knowledge"},
        "distillation": {
            "source_spans": spans,
            "source_units": [{"id": "u1", "kind": "concept", "statement": "Preserve assumptions for reuse", "span_ids": [s["id"] for s in spans]}],
            "ignored_spans": [],
            "sections": [{"id": "s1", "title": "Reuse", "items": [{"id": "i1", "text": "保留适用前提，知识才能在新情境中复用。", "source_ids": ["u1"]}]}],
            "uncovered": [],
        },
    }
    note = tmp_path / "published.md"
    note.write_text("---\nsource_hash: " + sha + "\n---\n\n保留适用前提，知识才能在新情境中复用。\n", encoding="utf-8")
    response = {
        "coverage": [{"source_id": "skill-example:u1", "status": "complete", "reason": "The assumption requirement survives in the output item.", "item_ids": ["skill-example:i1"]}],
        "claims": [{"item_id": "skill-example:i1", "status": "supported", "reason": "Source concept explicitly ties preserved assumptions to reuse."},
                   {"item_id": "skill-example:meta.name", "status": "supported", "reason": "The title abstracts the source concept without adding capabilities."}],
        "synthesis": {"status": "PASS", "reason": "The concept is synthesized into one useful principle, without chapter restatement."},
        "source_gaps": [],
    }
    info = {"source_hash": sha, "raw_path": str(raw), "title": "Evidence", "doc_type": "skill"}
    monkeypatch.setattr("skillmind.auditor._resolve_source", lambda value: (sha, info))
    monkeypatch.setattr("skillmind.auditor._find_extract_notes", lambda *args, **kwargs: [note])
    monkeypatch.setattr(semantic, "_call_semantic_audit", lambda *args: copy.deepcopy(response))

    def save():
        note.with_suffix(".evidence.json").write_text(json.dumps({"artifact_sha256": hashlib.sha256(note.read_bytes()).hexdigest(), "draft": draft}, ensure_ascii=False), encoding="utf-8")

    def run():
        return semantic.audit_semantic_source(sha[:7], {"vault_dir": str(tmp_path)}, vault_skills_override=tmp_path)

    save()
    return SimpleNamespace(semantic=semantic, call_model=call_model, raw=raw, note=note, sha=sha, draft=draft, response=response, run=run, save=save)


def test_complete_supported_concept_can_pass_without_executable_slots(audit_case):
    report = audit_case.run()
    assert report.verdict == "PASS"
    assert report.coverage_weighted == 100.0
    assert not report.needs_human_review
    assert report.coverage_status == "PASS"
    assert report.support_status == "PASS"
    assert report.synthesis_status == "PASS"
    assert report.to_summary_dict()["coverage"] == 100.0
    assert report.to_detailed_dict()["semantic_evidence"]["source_gaps"] == []


@pytest.mark.parametrize("dimension", ["translation", "unsupported"])
def test_nominal_full_coverage_does_not_override_negative_judgment(audit_case, dimension):
    if dimension == "translation":
        audit_case.response["synthesis"] = {"status": "FAIL", "reason": "Chapter-by-chapter translation repeats the source instead of synthesis.", "counterexample": "skill-example:i1 restates source u1."}
    else:
        audit_case.response["claims"][0].update(status="unsupported", reason="The content adds a requirement absent from the source.", counterexample="skill-example:i1 adds a command absent from source u1.")
    report = audit_case.run()
    assert report.coverage_weighted == 100.0
    assert report.verdict == "FAIL"
    assert report.needs_human_review


@pytest.mark.parametrize("change", ["missing_coverage", "duplicate_coverage", "missing_claim", "duplicate_claim", "bad_status", "empty_reason", "unknown_reference", "no_synthesis", "no_source_gaps", "bad_source_gaps"])
def test_incomplete_or_malformed_evaluation_is_unknown(audit_case, change):
    response = audit_case.response
    if change == "missing_coverage":
        response["coverage"] = []
    elif change == "duplicate_coverage":
        response["coverage"] *= 2
    elif change == "missing_claim":
        response["claims"] = []
    elif change == "duplicate_claim":
        response["claims"] *= 2
    elif change == "bad_status":
        response["coverage"][0]["status"] = "looks-good"
    elif change == "empty_reason":
        response["claims"][0]["reason"] = ""
    elif change == "unknown_reference":
        response["coverage"][0]["item_ids"] = ["missing:i1"]
    elif change == "no_synthesis":
        response.pop("synthesis")
    elif change == "no_source_gaps":
        response.pop("source_gaps")
    else:
        response["source_gaps"] = [{"reason": "maybe something"}]
    assert audit_case.run().verdict == "UNKNOWN"


@pytest.mark.parametrize("change", ["missing", "edited", "bad_envelope", "wrong_source_hash", "forged_span", "missing_span", "incomplete_extract", "bad_schema"])
def test_invalid_publication_or_extraction_cannot_pass(audit_case, change, monkeypatch):
    if change == "missing":
        audit_case.note.with_suffix(".evidence.json").unlink()
    elif change == "edited":
        audit_case.note.write_text("Hand edited body with `invented evidence`.", encoding="utf-8")
    elif change == "bad_envelope":
        audit_case.note.with_suffix(".evidence.json").write_text("{}", encoding="utf-8")
    else:
        if change == "wrong_source_hash":
            audit_case.draft["source"]["source_hash"] = "0" * 64
        elif change == "forged_span":
            audit_case.draft["distillation"]["source_spans"][0]["text"] = "Model invented evidence"
        elif change == "missing_span":
            audit_case.draft["distillation"]["source_spans"].pop()
        elif change == "incomplete_extract":
            audit_case.draft["extract_complete"] = False
        else:
            audit_case.draft["schema_version"] = "legacy"
        audit_case.save()
    assert audit_case.run().verdict == "UNKNOWN"


def test_changed_actual_raw_source_is_unknown_even_when_metadata_matches(audit_case):
    audit_case.raw.write_text("tampered original", encoding="utf-8")
    assert audit_case.run().verdict == "UNKNOWN"


def test_explicit_weak_coverage_is_fail_and_legacy_weight_is_display_only(audit_case):
    audit_case.response["coverage"][0].update(status="weak", reason="The applicable assumption is oversimplified.", counterexample="skill-example:i1 omits the condition in source u1.")
    report = audit_case.run()
    assert report.coverage_weighted == 50.0
    assert report.verdict == "FAIL"
    assert report.to_detailed_dict()["coverage_weight_provenance"]
    assert report.missing[0]["source_id"] == "skill-example:u1"


def test_source_gap_fails_even_when_inventory_and_claims_look_complete(audit_case):
    # The inventory models only one concept; the full source contains an exception.
    from skillmind.distillation import parse_source_spans
    text = audit_case.raw.read_text(encoding="utf-8") + "\nException: assumptions may be dropped only for nonsemantic decoration.\n"
    audit_case.raw.write_text(text, encoding="utf-8")
    sha = hashlib.sha256(audit_case.raw.read_bytes()).hexdigest()
    audit_case.draft["source"]["source_hash"] = sha
    spans = parse_source_spans(text)
    audit_case.draft["distillation"]["source_spans"] = spans
    audit_case.draft["distillation"]["source_units"][0]["span_ids"] = [s["id"] for s in spans]
    audit_case.response["source_gaps"] = [{"span_id": "skill-example:" + spans[-1]["id"], "statement": "The exception limiting discarded assumptions is absent.", "reason": "The source's explicit exception has no output counterpart."}]
    with patch("skillmind.auditor._resolve_source", return_value=(sha, {"source_hash": sha, "raw_path": str(audit_case.raw), "title": "Evidence"})):
        audit_case.save()
        report = audit_case.run()
    assert report.verdict == "FAIL"
    assert report.coverage_weighted == 100.0
    assert report.source_inventory_status == "FAIL"


def test_model_failure_does_not_expose_credentials_in_report(audit_case, monkeypatch):
    def failure(*args):
        raise RuntimeError("provider error api_key=do-not-leak-this-secret")
    monkeypatch.setattr(audit_case.semantic, "_call_semantic_audit", failure)
    report = audit_case.run()
    assert report.verdict == "UNKNOWN"
    assert "do-not-leak" not in json.dumps(report.to_detailed_dict())


def test_multidraft_ids_are_namespaced(audit_case, monkeypatch):
    second = audit_case.note.with_name("second.md")
    second.write_text(audit_case.note.read_text(encoding="utf-8"), encoding="utf-8")
    draft = copy.deepcopy(audit_case.draft)
    draft["uuid"] = "skill-second"
    second.with_suffix(".evidence.json").write_text(json.dumps({"artifact_sha256": hashlib.sha256(second.read_bytes()).hexdigest(), "draft": draft}), encoding="utf-8")
    monkeypatch.setattr("skillmind.auditor._find_extract_notes", lambda *a, **kw: [audit_case.note, second])
    audit_case.response["coverage"].append({"source_id": "skill-second:u1", "status": "complete", "reason": "The second claim preserves the source.", "item_ids": ["skill-second:i1"]})
    audit_case.response["claims"].append({"item_id": "skill-second:i1", "status": "supported", "reason": "The second concept is supported."})
    audit_case.response["claims"].append({"item_id": "skill-second:meta.name", "status": "supported", "reason": "The title is supported."})
    assert audit_case.run().verdict == "PASS"


def test_same_artifact_discovered_twice_is_evaluated_once(audit_case, monkeypatch):
    monkeypatch.setattr("skillmind.auditor._find_extract_notes", lambda *a, **kw: [audit_case.note, audit_case.note])
    assert audit_case.run().verdict == "PASS"


def test_no_notes_is_unknown(audit_case, monkeypatch):
    audit_case.note.unlink()
    audit_case.note.with_suffix(".evidence.json").unlink()
    monkeypatch.setattr("skillmind.auditor._find_extract_notes", lambda *a, **kw: [])
    report = audit_case.run()
    assert report.verdict == "UNKNOWN"
    assert report.coverage_weighted is None


def test_llm_call_uses_audit_profile_and_refuses_truncation(audit_case):
    cfg = {"llm": {"model": "openai/base", "api_key": "original-secret", "max_retries": 0}, "llm_profiles": {"audit": {"model": "openai/auditor", "timeout": 12, "max_tokens": 456}}}
    truncated = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content=json.dumps(audit_case.response)))])
    with patch("litellm.completion", return_value=truncated) as completion:
        with pytest.raises(ValueError, match="incomplete"):
            audit_case.call_model("full source", [audit_case.draft], cfg)
    kwargs = completion.call_args.kwargs
    assert kwargs["api_key"] == "original-secret"
    assert kwargs["model"] == "openai/auditor"
    assert kwargs["timeout"] == 12
    assert kwargs["max_tokens"] == 456
    prompt = kwargs["messages"][1]["content"]
    assert "skill-example:u1" in prompt
    assert "skill-example:i1" in prompt
    assert "source_gaps" in prompt
    assert "full source" in prompt
    assert "original-secret" not in prompt


def test_negative_dimension_still_visible_when_another_evaluation_is_missing(audit_case):
    audit_case.response["claims"][0].update(status="unsupported", reason="The output adds a new condition.", counterexample="skill-example:i1 contains a condition absent from u1.")
    audit_case.response.pop("synthesis")
    report = audit_case.run()
    assert report.verdict == "FAIL"
    assert report.support_status == "FAIL"
    assert report.synthesis_status == "UNKNOWN"


def test_discovery_limit_cannot_hide_unaudited_artifacts(audit_case, monkeypatch):
    from skillmind.renderer import publish_skill
    publish_skill(audit_case.draft, audit_case.note.parent / "agent")
    report = audit_case.semantic.audit_semantic_source(audit_case.sha, {"vault_dir": str(audit_case.note.parent)}, max_extract_files=1, vault_skills_override=audit_case.note.parent)
    assert report.verdict == "UNKNOWN"
    assert "budget" in " ".join(report.integrity_errors).lower()


def test_model_call_preserves_multiline_and_backtick_source(audit_case):
    source = "# Example\n```python\nprint('`source`')\n```\nThe final exception survives."
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(audit_case.response)))])
    cfg = {"llm": {"model": "openai/test", "api_key": "secret", "max_retries": 0}}
    with patch("litellm.completion", return_value=response) as completion:
        assert audit_case.call_model(source, [audit_case.draft], cfg) == audit_case.response
    assert source in completion.call_args.kwargs["messages"][1]["content"]
    assert completion.call_args.kwargs["timeout"] == 180
    assert completion.call_args.kwargs["max_tokens"] == 32768


@pytest.mark.parametrize("content", ["", "[]", '{"coverage": []', '```json\n{}\n```'])
def test_model_call_does_not_repair_empty_or_invalid_json(audit_case, content):
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])
    cfg = {"llm": {"model": "openai/test", "api_key": "secret", "max_retries": 0}}
    with patch("litellm.completion", return_value=response):
        with pytest.raises(ValueError):
            audit_case.call_model("source", [audit_case.draft], cfg)


def test_duplicate_json_keys_are_not_silently_overwritten():
    from skillmind.semantic_auditor import _call_semantic_audit
    content = '{"coverage": [], "synthesis": {"status": "FAIL", "status": "PASS", "reason": "overwritten"}}'
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])
    cfg = {"llm": {"model": "openai/test", "api_key": "secret", "max_retries": 0}}
    with patch("litellm.completion", return_value=response):
        with pytest.raises(ValueError, match="duplicate"):
            _call_semantic_audit("source", [], cfg)


def test_malformed_protocol_retries_without_repairing_or_rerolling_verdict(audit_case):
    duplicate = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"reason":"first","reason":"second"}'))])
    valid = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(audit_case.response)))])
    cfg = {"llm": {"model": "openai/test", "api_key": "secret", "max_retries": 1}}
    with patch('litellm.completion', side_effect=[duplicate, valid]) as completion, patch('skillmind.semantic_auditor.time.sleep'):
        assert audit_case.call_model('source', [audit_case.draft], cfg) == audit_case.response
    assert completion.call_count == 2


def test_valid_negative_judgment_is_not_retried(audit_case):
    negative = copy.deepcopy(audit_case.response)
    negative['synthesis'] = {'status': 'FAIL', 'reason': 'The text restates each chapter rather than synthesizing it.', 'counterexample': 'Source chapter order is copied without a reusable structure.'}
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(negative)))])
    cfg = {'llm': {'model': 'openai/test', 'api_key': 'secret', 'max_retries': 2}}
    with patch('litellm.completion', return_value=response) as completion:
        assert audit_case.call_model('source', [audit_case.draft], cfg) == negative
    assert completion.call_count == 1


def test_extra_coverage_id_makes_coverage_dimension_unknown():
    from skillmind.auditor import SourceItem
    from skillmind.semantic_auditor import SemanticAuditReport, _evaluate
    report = SemanticAuditReport(source_hash="hash", source_title="Concept", source_path="source.md", items=[SourceItem(id="d:u", section="Concept", kind="concept", statement="Preserve rationale")])
    draft = {"uuid": "d", "distillation": {"source_spans": [{"id": "p"}], "sections": [{"items": [{"id": "i"}]}]}}
    result = {"coverage": [{"source_id": "d:u", "status": "complete", "reason": "Rationale is preserved", "item_ids": ["d:i"]}, {"source_id": "extra", "status": "complete", "reason": "Invented id", "item_ids": ["d:i"]}], "claims": [{"item_id": "d:i", "status": "supported", "reason": "The source explicitly states this rationale"}], "synthesis": {"status": "PASS", "reason": "Useful conceptual synthesis"}, "source_gaps": []}
    _evaluate(report, result, [draft])
    assert report.verdict == "UNKNOWN"
    assert report.coverage_status == "UNKNOWN"
    assert report.coverage_weighted is None


def test_namespaced_id_collisions_are_rejected(audit_case, monkeypatch):
    first = audit_case.draft
    first["distillation"]["source_units"][0]["id"] = "nested:u1"
    first["distillation"]["sections"][0]["items"][0]["source_ids"] = ["nested:u1"]
    audit_case.save()
    second = audit_case.note.with_name("second.md")
    second.write_bytes(audit_case.note.read_bytes())
    draft = copy.deepcopy(first)
    draft["uuid"] = "skill-example:nested"
    draft["distillation"]["source_units"][0]["id"] = "u1"
    draft["distillation"]["sections"][0]["items"][0]["source_ids"] = ["u1"]
    second.with_suffix(".evidence.json").write_text(json.dumps({"artifact_sha256": hashlib.sha256(second.read_bytes()).hexdigest(), "draft": draft}), encoding="utf-8")
    monkeypatch.setattr("skillmind.auditor._find_extract_notes", lambda *a, **kw: [audit_case.note, second])
    audit_case.response["coverage"][0]["source_id"] = "skill-example:nested:u1"
    audit_case.response["claims"].append({"item_id": "skill-example:nested:i1", "status": "supported", "reason": "Preserved concept"})
    report = audit_case.run()
    assert report.verdict == "UNKNOWN"
    assert "collision" in " ".join(report.integrity_errors).lower()


def test_bom_and_non_utf8_sources_use_the_extractor_decoder(audit_case):
    from skillmind.distillation import parse_source_spans
    from skillmind.parser import _read_text_auto
    text = "# 原理\r\n\r\n保留前提可以支持知识复用。\r\n"
    audit_case.raw.write_bytes(text.encode("gbk"))
    sha = hashlib.sha256(audit_case.raw.read_bytes()).hexdigest()
    audit_case.draft["source"]["source_hash"] = sha
    spans = parse_source_spans(_read_text_auto(audit_case.raw))
    audit_case.draft["distillation"]["source_spans"] = spans
    audit_case.draft["distillation"]["source_units"][0]["span_ids"] = [s["id"] for s in spans]
    audit_case.save()
    with patch("skillmind.auditor._resolve_source", return_value=(sha, {"source_hash": sha, "raw_path": str(audit_case.raw), "title": "Principle"})):
        assert audit_case.run().verdict == "PASS"


def test_failed_evaluation_counts_source_and_claims_as_unverified(audit_case, monkeypatch):
    def failure(*args):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(audit_case.semantic, "_call_semantic_audit", failure)
    report = audit_case.run()
    assert report.n_unverified == 1
    assert report.n_claims_unknown == 2
    assert report.coverage_weighted is None
    assert report.missing[0]["status"] == "unknown"


def test_nested_agent_skill_only_export_is_discovered(audit_case, monkeypatch):
    from skillmind.renderer import publish_skill
    root = audit_case.note.parent / "agent-only"
    artifact = publish_skill(audit_case.draft, root / "nested")
    monkeypatch.setattr("skillmind.auditor._find_extract_notes", lambda *a, **kw: [])
    report = audit_case.semantic.audit_semantic_source(audit_case.sha, {}, vault_skills_override=root)
    assert report.verdict == "PASS"
    assert report.extract_files == [str(artifact)]
    assert "source_hash:" not in artifact.read_text(encoding="utf-8").split("---")[1]


def test_identical_both_format_exports_are_verified_then_deduplicated(audit_case):
    from skillmind.renderer import publish_skill
    artifact = publish_skill(audit_case.draft, audit_case.note.parent / "agent")
    report = audit_case.run()
    assert report.verdict == "PASS"
    assert set(report.extract_files) == {str(audit_case.note), str(artifact)}
    assert len(report.items) == 1


@pytest.mark.parametrize("damage", ["edited_artifact", "missing_artifact", "missing_evidence", "invalid_evidence"])
def test_damaged_agent_sibling_cannot_be_hidden_by_valid_obsidian_export(audit_case, damage):
    from skillmind.renderer import publish_skill
    artifact = publish_skill(audit_case.draft, audit_case.note.parent / "agent")
    if damage == "edited_artifact":
        artifact.write_text(artifact.read_text(encoding="utf-8") + "\nUnsupported new capability.", encoding="utf-8")
    elif damage == "missing_artifact":
        artifact.unlink()
    elif damage == "missing_evidence":
        artifact.with_suffix(".evidence.json").unlink()
    else:
        artifact.with_suffix(".evidence.json").write_text("{broken-json", encoding="utf-8")
    assert audit_case.run().verdict == "UNKNOWN"


def test_conflicting_same_uuid_exports_are_unknown(audit_case):
    from skillmind.renderer import publish_skill
    conflicting = copy.deepcopy(audit_case.draft)
    conflicting["distillation"]["sections"][0]["items"][0]["text"] = "A conflicting claim for the same canonical identity."
    publish_skill(conflicting, audit_case.note.parent / "agent")
    report = audit_case.run()
    assert report.verdict == "UNKNOWN"
    assert "conflict" in " ".join(report.integrity_errors).lower()


def test_intent_only_change_is_a_required_source_support_claim(audit_case):
    before = audit_case.semantic._audit_payload([audit_case.draft])
    audit_case.draft["meta"]["intent"] = "Use when deploying a Kubernetes cluster automatically."
    audit_case.save()
    after = audit_case.semantic._audit_payload([audit_case.draft])
    assert before != after
    assert "Kubernetes" in json.dumps(after)
    # Existing content/name judgments do not cover this new metadata claim.
    report = audit_case.run()
    assert report.verdict == "UNKNOWN"
    assert report.support_status == "UNKNOWN"


def test_explicit_unsupported_intent_fails_with_full_body_coverage(audit_case):
    audit_case.draft["meta"]["intent"] = "Deploy Kubernetes clusters."
    audit_case.save()
    audit_case.response["claims"].append({"item_id": "skill-example:meta.intent", "status": "unsupported", "reason": "Source only explains knowledge reuse; no deployment capability exists.", "counterexample": "meta.intent adds Kubernetes deployment absent from u1."})
    report = audit_case.run()
    assert report.verdict == "FAIL"
    assert report.support_status == "FAIL"
    assert report.coverage_weighted == 100.0


def test_nonempty_metadata_claims_are_in_model_payload(audit_case):
    audit_case.draft["meta"].update(intent="Reuse knowledge with assumptions", trigger_keywords=["knowledge reuse", "assumptions"])
    payload = audit_case.semantic._audit_payload([audit_case.draft])[0]
    claims = {c["id"]: c for c in payload["metadata_claims"]}
    assert set(claims) == {"skill-example:meta.name", "skill-example:meta.intent", "skill-example:meta.trigger_keywords"}
    assert "assumptions" in claims["skill-example:meta.trigger_keywords"]["text"]


def test_metadata_claim_id_cannot_collide_with_content_id(audit_case):
    audit_case.draft["distillation"]["sections"][0]["items"][0]["id"] = "meta.name"
    audit_case.response["coverage"][0]["item_ids"] = ["skill-example:meta.name"]
    audit_case.response["claims"][0]["item_id"] = "skill-example:meta.name"
    audit_case.save()
    report = audit_case.run()
    assert report.verdict == "UNKNOWN"
    assert "collision" in " ".join(report.integrity_errors).lower()
