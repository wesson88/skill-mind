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


def witness(case, output=None):
    span = case.draft['distillation']['source_spans'][-1]
    return {'source_span_ids': ['skill-example:' + span['id']],
            'source_quote': span['text'],
            'output_quote': output if output is not None else case.draft['distillation']['sections'][0]['items'][0]['text'],
            'explanation': 'Concrete source/output discrepancy.'}


@pytest.mark.parametrize('defect', ['absent', 'string', 'source_quote', 'output_quote', 'span', 'extra_field'])
def test_ungrounded_negative_is_unknown_not_fail_or_pass(audit_case, defect):
    row = audit_case.response['claims'][0]
    row.update(status='unsupported', reason='An alleged extra requirement.')
    proof = witness(audit_case)
    if defect == 'string':
        proof = 'No source supports this.'
    elif defect == 'source_quote':
        proof['source_quote'] = 'fabricated source sentence'
    elif defect == 'output_quote':
        proof['output_quote'] = 'not present in this claim'
    elif defect == 'span':
        proof['source_span_ids'] = ['skill-example:missing']
    elif defect == 'extra_field':
        proof['confidence'] = 1
    if defect != 'absent':
        row['counterexample'] = proof
    report = audit_case.run()
    assert report.verdict == 'UNKNOWN'
    assert report.support_status == 'UNKNOWN'
    assert report.evaluation_errors


def test_negative_proof_cannot_quote_another_claim(audit_case):
    audit_case.response['claims'][0].update(status='unsupported', reason='Wrong claim cited.',
                                          counterexample=witness(audit_case, 'Reusable knowledge'))
    assert audit_case.run().verdict == 'UNKNOWN'


def test_identity_is_explicit_without_inventing_inventory_units(audit_case):
    from skillmind.distillation import parse_source_spans
    draft = audit_case.draft
    draft['distillation']['source_spans'] = parse_source_spans('---\nname: implement-spec\ndisable-model-invocation: true\n---\n\nDo the task.\n')
    draft['meta']['trigger_keywords'] = ['implement-spec']
    identity = audit_case.semantic._audit_payload([draft])[0]['source_identity']
    assert identity['name'] == 'implement-spec'
    assert 'meta.trigger_keywords' in identity['retained_in']
    assert identity['other_frontmatter']['disable-model-invocation'] is True
    assert len(draft['distillation']['source_units']) == 1


def test_referenced_unit_does_not_hide_one_missing_forbidden_term(audit_case):
    from skillmind.distillation import parse_source_spans
    source = 'For Module, avoid unit, component, service.\n'
    audit_case.raw.write_bytes(source.encode())
    digest = hashlib.sha256(audit_case.raw.read_bytes()).hexdigest()
    payload = audit_case.draft['distillation']
    payload['source_spans'] = parse_source_spans(source)
    payload['source_units'][0].update(statement=source.strip(), span_ids=['p0001'])
    payload['sections'][0]['items'][0]['text'] = 'For Module, avoid component and service.'
    audit_case.draft['source']['source_hash'] = digest
    audit_case.response['coverage'][0].update(status='weak', reason='Module also forbids unit; that member is missing.', counterexample=witness(audit_case))
    audit_case.save()
    with patch('skillmind.auditor._resolve_source', return_value=(digest, {'source_hash': digest, 'raw_path': str(audit_case.raw), 'doc_type': 'skill'})):
        report = audit_case.run()
    assert report.coverage_status == 'FAIL'
    assert report.verdict == 'FAIL'


def test_coverage_witness_must_belong_to_its_unit(audit_case):
    # Both span IDs and excerpts exist, but the title does not ground this unit.
    audit_case.draft['distillation']['source_units'][0]['span_ids'] = [audit_case.draft['distillation']['source_spans'][-1]['id']]
    audit_case.draft['distillation']['ignored_spans'] = [{'span_id': audit_case.draft['distillation']['source_spans'][0]['id'], 'reason': 'Heading only.'}]
    proof = witness(audit_case)
    first = audit_case.draft['distillation']['source_spans'][0]
    proof.update(source_span_ids=['skill-example:' + first['id']], source_quote=first['text'])
    audit_case.response['coverage'][0].update(status='weak', reason='Wrong source span cited.', counterexample=proof)
    audit_case.save()
    assert audit_case.run().coverage_status == 'UNKNOWN'


def test_valid_inventory_failure_dominates_another_malformed_gap(audit_case):
    gap = {'span_id': witness(audit_case)['source_span_ids'][0], 'statement': 'Assumptions not inventoried.',
           'reason': 'Explicit source requirement.', 'counterexample': witness(audit_case, '')}
    audit_case.response['source_gaps'] = [{'reason': 'Malformed'}, gap]
    report = audit_case.run()
    assert report.source_inventory_status == 'FAIL'
    assert report.verdict == 'FAIL'
    assert report.evaluation_errors


@pytest.mark.parametrize('dimension', ['coverage', 'claims', 'synthesis'])
def test_malformed_status_cannot_erase_an_independent_grounded_failure(audit_case, dimension):
    audit_case.draft['meta']['intent'] = 'Deploy Kubernetes clusters.'
    audit_case.save()
    audit_case.response['claims'].append({'item_id': 'skill-example:meta.intent', 'status': 'unsupported',
        'reason': 'Deployment is an unsupported new capability.', 'counterexample': witness(audit_case, 'Deploy Kubernetes clusters.')})
    row = audit_case.response[dimension] if dimension == 'synthesis' else audit_case.response[dimension][0]
    row['status'] = []
    report = audit_case.run()
    assert report.verdict == 'FAIL'
    assert report.support_status == 'FAIL'


@pytest.mark.parametrize('dimension', ['coverage', 'synthesis', 'source_gaps'])
def test_every_negative_dimension_requires_a_grounded_counterexample(audit_case, dimension):
    if dimension == 'coverage':
        audit_case.response['coverage'][0].update(status='weak', reason='Alleged omission without evidence.')
    elif dimension == 'synthesis':
        audit_case.response['synthesis'].update(status='FAIL', reason='Alleged translation without evidence.')
    else:
        span = audit_case.draft['distillation']['source_spans'][-1]
        audit_case.response['source_gaps'] = [{'span_id': 'skill-example:' + span['id'],
            'statement': 'Alleged missing concept.', 'reason': 'No grounded excerpt.'}]
    assert audit_case.run().verdict == 'UNKNOWN'


def test_real_source_audience_restriction_cannot_be_discarded_as_editorial(audit_case):
    from skillmind.distillation import parse_source_spans
    source = 'This method applies only to licensed operators.\n'
    audit_case.raw.write_bytes(source.encode())
    digest = hashlib.sha256(audit_case.raw.read_bytes()).hexdigest()
    payload = audit_case.draft['distillation']
    payload['source_spans'] = parse_source_spans(source)
    payload['source_units'][0].update(statement=source.strip(), span_ids=['p0001'])
    audit_case.draft['source']['source_hash'] = digest
    audit_case.draft['meta']['intent'] = 'For all learners without qualification.'
    audit_case.response['claims'].append({'item_id': 'skill-example:meta.intent', 'status': 'unsupported',
        'reason': 'The source restricts the audience to licensed operators.',
        'counterexample': witness(audit_case, 'For all learners without qualification.')})
    audit_case.save()
    with patch('skillmind.auditor._resolve_source', return_value=(digest, {'source_hash': digest, 'raw_path': str(audit_case.raw), 'doc_type': 'skill'})):
        assert audit_case.run().support_status == 'FAIL'


@pytest.mark.parametrize("dimension", ["translation", "unsupported"])
def test_nominal_full_coverage_does_not_override_negative_judgment(audit_case, dimension):
    if dimension == "translation":
        audit_case.response["synthesis"] = {"status": "FAIL", "reason": "Chapter-by-chapter translation repeats the source instead of synthesis.", "counterexample": witness(audit_case)}
    else:
        audit_case.draft['distillation']['sections'][0]['items'][0]['text'] += ' Run deploy now.'
        audit_case.save()
        audit_case.response["claims"][0].update(status="unsupported", reason="The content adds a requirement absent from the source.", counterexample=witness(audit_case, 'Run deploy now.'))
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
    audit_case.draft['distillation']['sections'][0]['items'][0]['text'] = 'Knowledge can always be reused.'
    audit_case.save()
    audit_case.response["coverage"][0].update(status="weak", reason="The applicable assumption is oversimplified.", counterexample=witness(audit_case))
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
    audit_case.response["source_gaps"] = [{"span_id": "skill-example:" + spans[-1]["id"], "statement": "The exception limiting discarded assumptions is absent.", "reason": "The source's explicit exception has no output counterpart.", 'counterexample': witness(audit_case, '')}]
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
    audit_case.draft['distillation']['sections'][0]['items'][0]['text'] += ' Only reuse after deployment.'
    audit_case.save()
    audit_case.response["claims"][0].update(status="unsupported", reason="The output adds a new condition.", counterexample=witness(audit_case, 'Only reuse after deployment.'))
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
    draft = {"uuid": "d", "distillation": {"source_spans": [{"id": "p", "text": "Preserve rationale"}], "source_units": [{'id': 'u', 'span_ids': ['p']}], 'ignored_spans': [], 'uncovered': [], "sections": [{'id': 's', 'title': 'Concept', "items": [{"id": "i", 'text': 'Preserve rationale', 'source_ids': ['u']}]}]}}
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
    audit_case.response["claims"].append({"item_id": "skill-example:meta.intent", "status": "unsupported", "reason": "Source only explains knowledge reuse; no deployment capability exists.", "counterexample": witness(audit_case, 'Deploy Kubernetes clusters.')})
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
