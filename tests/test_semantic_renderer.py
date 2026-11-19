"""Canonical rendering and artifact-bound evidence regressions."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from skillmind import renderer


RAW_EXAMPLE = 'Keep `literal` text.\n```python\nprint("``` and `")\n```\nFinal source line.\n'
SYNTHESIS = "证据关联能帮助追溯，但不能代替对语义支持关系的判断。"
COMMAND = '保留可执行示例：\n\n```sh\nprintf "`literal`\\n"\n```'


def semantic_draft() -> dict:
    return {
        "schema_version": "semantic_distillation_v1",
        "extract_complete": True,
        "uuid": "skill-semantic-abc12345",
        "status": "published",
        "meta": {"name": "Semantic review", "intent": "reviewing source-backed claims"},
        "source": {
            "doc_type": "skill",
            "source_hash": "source-hash",
            "source_url": "https://example.com/skills/review/SKILL.md",
            "raw_path": "C:/private/cache/source.md",
            "source_repo": "C:/private/checkout",
        },
        # Stale legacy projections must not reintroduce repeated content.
        "learning_enhancement": {"plain_summary": SYNTHESIS},
        "procedure": [{"seq": 1, "action": "STALE LEGACY ACTION"}],
        "key_concepts": [{"title": "STALE LEGACY CONCEPT", "explanation": SYNTHESIS}],
        "distillation": {
            "source_spans": [{"id": "span-1", "text": RAW_EXAMPLE, "section": "Evidence", "start_line": 1, "end_line": 6}],
            "source_units": [
                {"id": "unit-1", "kind": "concept", "statement": "Linkage is not semantic proof.", "span_ids": ["span-1"]},
                {"id": "unit-2", "kind": "example", "statement": "Retain literal examples.", "span_ids": ["span-1"]},
            ],
            "ignored_spans": [],
            "sections": [
                {"id": "section-1", "title": "证据与判断", "items": [{"id": "item-1", "text": SYNTHESIS, "source_ids": ["unit-1", "unit-2"]}]},
                {"id": "section-2", "title": "示例", "items": [{"id": "item-2", "text": COMMAND, "source_ids": ["unit-2"]}]},
            ],
            "uncovered": [],
        },
    }


@pytest.mark.parametrize("render", [renderer.render_to_markdown, renderer.render_skill_markdown])
def test_canonical_body_renders_sections_once_without_legacy_or_raw_evidence(render):
    text = render(semantic_draft())
    body = text.split("---", 2)[2]
    assert body.count(SYNTHESIS) == 1
    assert body.count("## 证据与判断") == 1
    assert COMMAND in body
    assert "STALE LEGACY" not in body
    assert RAW_EXAMPLE not in body
    assert "Linkage is not semantic proof." not in body
    assert "## Steps" not in body
    assert "No executable steps" not in body


@pytest.mark.parametrize("format", ["obsidian", "agent"])
def test_published_artifacts_have_exact_sanitized_json_evidence(tmp_path, format):
    draft = semantic_draft()
    before = copy.deepcopy(draft)
    if format == "obsidian":
        path = renderer.publish_to_vault(draft, {"vault_dir": str(tmp_path)}, output_dir=tmp_path)
    else:
        path = renderer.publish_skill(draft, tmp_path)
    evidence_path = path.with_suffix(".evidence.json")
    assert evidence_path.is_file()
    envelope = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert set(envelope) == {"artifact_sha256", "draft"}
    assert envelope["artifact_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert envelope["draft"]["distillation"] == draft["distillation"]
    assert "raw_path" not in envelope["draft"]["source"]
    assert "source_repo" not in envelope["draft"]["source"]
    assert draft == before
    assert renderer.load_artifact_evidence(path) == envelope
    assert not list(tmp_path.rglob("*.tmp"))


def test_evidence_preserves_remote_repository(tmp_path):
    draft = semantic_draft()
    draft["source"]["source_repo"] = "https://github.com/example/skills"
    path = renderer.publish_skill(draft, tmp_path)
    assert path.with_suffix(".evidence.json").exists()
    envelope = renderer.load_artifact_evidence(path)
    assert envelope["draft"]["source"]["source_repo"] == draft["source"]["source_repo"]


def test_artifact_edit_invalidates_evidence(tmp_path):
    path = renderer.publish_skill(semantic_draft(), tmp_path)
    path.write_bytes(path.read_bytes() + b"\nEdited claim.\n")
    assert hasattr(renderer, "load_artifact_evidence")
    with pytest.raises(ValueError, match="(?i)hash"):
        renderer.load_artifact_evidence(path)


@pytest.mark.parametrize("evidence", [None, "not JSON", "{}", '{"artifact_sha256": "bad", "draft": []}'])
def test_missing_or_invalid_evidence_is_a_clear_error(tmp_path, evidence):
    path = tmp_path / "SKILL.md"
    path.write_text("# Unverified", encoding="utf-8")
    if evidence is not None:
        path.with_suffix(".evidence.json").write_text(evidence, encoding="utf-8")
    assert hasattr(renderer, "load_artifact_evidence")
    with pytest.raises(ValueError, match="(?i)evidence"):
        renderer.load_artifact_evidence(path)


def test_renamed_skill_retires_only_owned_evidence(tmp_path):
    draft = semantic_draft()
    first = renderer.publish_skill(draft, tmp_path)
    evidence_path = first.with_suffix(".evidence.json")
    assert evidence_path.exists()
    unrelated = first.parent / "notes.txt"
    unrelated.write_text("Leave this file", encoding="utf-8")
    other = copy.deepcopy(draft)
    other["uuid"] = "different-owner"
    other["meta"]["name"] = "Other"
    other_path = renderer.publish_skill(other, tmp_path)
    draft["meta"]["name"] = "Renamed review"
    renamed = renderer.publish_skill(draft, tmp_path)
    assert not first.exists()
    assert not evidence_path.exists()
    assert unrelated.exists()
    assert other_path.exists()
    assert other_path.with_suffix(".evidence.json").exists()
    assert renderer.load_artifact_evidence(renamed)["draft"]["uuid"] == draft["uuid"]


def test_renaming_does_not_delete_unowned_evidence(tmp_path):
    draft = semantic_draft()
    first = renderer.publish_skill(draft, tmp_path)
    sidecar = first.with_suffix(".evidence.json")
    foreign_evidence = {"artifact_sha256": "foreign", "draft": {"uuid": "another-owner"}}
    sidecar.write_text(json.dumps(foreign_evidence), encoding="utf-8")
    draft["meta"]["name"] = "Renamed review"
    renderer.publish_skill(draft, tmp_path)
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == foreign_evidence


def publish_obsidian(draft, root):
    return renderer.publish_to_vault(draft, {"vault_dir": str(root)}, output_dir=root)


def test_obsidian_republish_and_rename_leave_one_audit_discoverable_artifact(tmp_path):
    from skillmind.auditor import _find_extract_notes

    draft = semantic_draft()
    first = publish_obsidian(draft, tmp_path)
    draft["distillation"]["sections"][0]["items"][0]["text"] = "更新后的内容。"
    again = publish_obsidian(draft, tmp_path)
    assert again == first
    assert "更新后的内容。" in first.read_text(encoding="utf-8")
    assert renderer.load_artifact_evidence(first)["draft"]["distillation"] == draft["distillation"]
    draft["meta"]["name"] = "Renamed review"
    renamed = publish_obsidian(draft, tmp_path)
    assert renamed != first
    assert not first.exists()
    assert not first.with_suffix(".evidence.json").exists()
    assert _find_extract_notes("source-hash", tmp_path) == [renamed]


def test_obsidian_collisions_preserve_foreign_artifacts_and_orphan_evidence(tmp_path):
    draft = semantic_draft()
    foreign = tmp_path / "Semantic review.md"
    foreign.write_text("User-authored note", encoding="utf-8")
    suffixed = tmp_path / f"Semantic review_{draft['uuid'][-7:]}.md"
    suffixed.write_text("Another user-authored note", encoding="utf-8")
    orphan = tmp_path / "Renamed review.evidence.json"
    orphan.write_text("Do not overwrite orphan evidence", encoding="utf-8")
    published = publish_obsidian(draft, tmp_path)
    assert published not in (foreign, suffixed)
    assert foreign.read_text(encoding="utf-8") == "User-authored note"
    assert suffixed.read_text(encoding="utf-8") == "Another user-authored note"
    assert publish_obsidian(draft, tmp_path) == published
    draft["meta"]["name"] = "Renamed review"
    renamed = publish_obsidian(draft, tmp_path)
    assert renamed != orphan.with_suffix(".md")
    assert orphan.read_text(encoding="utf-8") == "Do not overwrite orphan evidence"
    assert not published.exists()


def test_obsidian_preserves_edited_old_artifact_on_republish_and_rename(tmp_path):
    draft = semantic_draft()
    first = publish_obsidian(draft, tmp_path)
    first.write_bytes(first.read_bytes() + b"\nUser changes\n")
    edited_bytes = first.read_bytes()
    old_sidecar = first.with_suffix(".evidence.json").read_bytes()
    again = publish_obsidian(draft, tmp_path)
    assert again != first
    draft["meta"]["name"] = "Renamed review"
    renamed = publish_obsidian(draft, tmp_path)
    assert first.read_bytes() == edited_bytes
    assert first.with_suffix(".evidence.json").read_bytes() == old_sidecar
    assert not again.exists()
    assert renderer.load_artifact_evidence(renamed)["draft"]["uuid"] == draft["uuid"]


def test_obsidian_rename_failure_preserves_previous_owned_artifact(tmp_path, monkeypatch):
    draft = semantic_draft()
    first = publish_obsidian(draft, tmp_path)
    first_bytes = first.read_bytes()
    first_sidecar = first.with_suffix(".evidence.json").read_bytes()
    draft["meta"]["name"] = "Renamed review"

    def fail_evidence_write(*args):
        raise OSError("simulated evidence write failure")

    monkeypatch.setattr(renderer, "_write_artifact_evidence", fail_evidence_write)
    with pytest.raises(OSError, match="simulated"):
        publish_obsidian(draft, tmp_path)
    assert first.read_bytes() == first_bytes
    assert first.with_suffix(".evidence.json").read_bytes() == first_sidecar


@pytest.mark.parametrize("semantic", [False, True])
def test_obsidian_frontmatter_quotes_link_scalars(semantic):
    draft = semantic_draft()
    if not semantic:
        draft.pop("schema_version")
    draft["source"]["repo_url"] = "https://example.com/skills"
    draft["source"]["parent_source"] = "https://example.com/parent"
    text = renderer.render_to_markdown(draft)
    frontmatter = text.split("---", 2)[1]
    nodes = yaml.compose(frontmatter)
    values = {key.value: value for key, value in nodes.value}
    for field in ("source_url", "source_repo", "parent_source"):
        assert values[field].style in ('"', "'")
    assert values["name"].style is None


def test_legacy_obsidian_collision_behavior_stays_unchanged(tmp_path):
    draft = semantic_draft()
    draft.pop("schema_version")
    first = publish_obsidian(draft, tmp_path)
    again = publish_obsidian(draft, tmp_path)
    assert again != first
    assert first.exists()
    assert again.exists()
