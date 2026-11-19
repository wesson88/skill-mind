# Semantic Distillation Implementation Plan

> For agentic workers: implement in this session using the approved specification, TDD and independent review. No automatic commits or pushes; user authorization is required for those operations.

**Goal:** replace Skill chapter restatement with source-backed synthesis and verify real output on the fixed five sources.

**Architecture:** add a canonical distillation module for source spans, source inventory, synthesized sections, schema validation and generation/cache state. Render that structure directly with hashed evidence sidecars. A semantic auditor consumes canonical evidence and performs bidirectional source/claim and synthesis checks. Existing non-Skill extraction and legacy rendering remain compatible.

**Tech stack:** Python, existing LiteLLM/Typer/Rich/PyYAML, unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-11-19-semantic-distillation.md`.

## Task 1 — Canonical distillation and generation (root implementer)

Files: create `skillmind/distillation.py`, `tests/test_distillation.py`; modify `skillmind/extractor.py`, `skillmind/config.py`.

1. Write tests for the specified canonical record, source-span preservation, invalid/missing references, complete-vs-partial state and model-dependent cache identity. Run `python -m pytest tests/test_distillation.py -q` and observe failures before implementation.
2. Implement source spans with stable ids and line/section context, bounded packing without discarding prose or fenced examples, closed schema validation and coverage ledger. Canonical API: `SCHEMA_VERSION`, `parse_source_spans(text)`, `validate_distillation(payload) -> list[str]`, `renderable_sections(draft) -> list[dict]`, `distill_skill(raw_path, source_info, cfg, console=None) -> list[dict]`, `has_reusable_distillation_cache(source_hash,cfg) -> bool`.
3. Implement inventory→synthesis via a patchable `_call_json(system,user,cfg,stage)` that uses existing credentials/profiles and rejects truncated/empty/invalid JSON. Preserve full source context and meaningful examples, no arbitrary content caps. Store complete/failed state, prompt/schema/parameters and provenance; do not cache failure as success. Retry behavior follows main's configured retry budget.
4. Route only `doc_type=skill` to the new pipeline in extract_skill; keep existing non-Skill behavior. Update CURRENT_PROMPT_VERSION to extract_v11. Tests cover semantic no-fallback and stale caches.
5. Run focused tests then existing tests. Save failures and fixes. No commit.

## Task 2 — Canonical rendering and evidence sidecars (separate implementer)

Files: modify `skillmind/renderer.py`; create `tests/test_semantic_renderer.py`. Own these files only.

1. Write failing tests using the exact canonical contract: both formats include synthesized text once; do not leak source spans into body; concepts render without fake executable steps; multi-line/backtick evidence survives as JSON; artifact edits invalidate sidecar hash; legacy tests remain valid.
2. Implement direct section rendering when schema is semantic_distillation_v1. Evidence path is `artifact_path.with_suffix('.evidence.json')` (also for SKILL.md); envelope contains SHA256 of artifact bytes and the full canonical draft. Sidecar writes are atomic. Source evidence belongs in JSON, not repeated English inline quotes.
3. Keep legacy render path and same-uuid publication behavior. Clean up only owned obsolete sidecars when an owned renamed Skill is retired. No unrelated filesystem deletions.
4. Expose `load_artifact_evidence(path) -> dict` with clear errors for hash mismatch or missing/invalid envelopes. Auditor will consume it.
5. Run new failing→passing tests and `tests/test_skill_renderer.py`. No commit.

## Task 3 — Canonical semantic audit (separate implementer)

Files: create `skillmind/semantic_auditor.py`, `tests/test_semantic_auditor.py`. Own these files only; root wires existing auditor/CLI.

1. Write failing tests demonstrating that nominal full coverage cannot PASS for translation/restatement or unsupported claims; absent/duplicate ids and incomplete responses cannot silently pass; missing/edited evidence is UNKNOWN; non-procedural concepts do not require executable slots.
2. Provide `audit_semantic_source(source_hash,cfg,*,console=None,max_extract_files=20,vault_skills_override=None)` with a report compatible with CLI attributes/methods (`to_summary_dict`, `to_detailed_dict`, coverage_weighted, verdict, missing, needs_human_review, source_title, original_chars, extract_files, pass1_elapsed, pass2_elapsed). Resolve input via existing collector hashes and published Markdown notes via existing auditor helper. Lazy imports avoid circular references.
3. Load and verify `.evidence.json` directly, validate current schema/source hash and source-span fidelity. Expose patchable `_call_semantic_audit(source_text,drafts,cfg)` using the audit credential profile. Ask for `coverage:[{source_id,status:complete|weak|missing,reason,item_ids}]`, `claims:[{item_id,status:supported|unsupported|unknown,reason}]`, `synthesis:{status:PASS|FAIL|UNKNOWN,reason}`. Validate exact inventory/content id sets; duplicates/omissions become unverified/UNKNOWN, not defaults to success. Coverage source ids are namespaced with draft uuid when multiple drafts exist.
4. PASS only when all source units are complete, all claims supported, synthesis PASS, extraction complete, no unresolved meaningful source spans. FAIL for explicit missing/weak/unsupported/synthesis FAIL. UNKNOWN for unexecuted or malformed evaluations. Show per-dimension statuses and counts. Weighted coverage retains legacy 0.5 weak weighting for display only (document provenance); no coverage-percentage threshold grants PASS.
5. Save detailed evidence rather than backtick reparsing. Run focused RED→GREEN tests. No commit.

## Task 4 — Integration, docs, regression and review (root)

Files: `skillmind/auditor.py`, `skillmind/cli.py`, `skillmind/reviewer.py`, `pyproject.toml`, `README.md`, integration tests as needed.

1. Wire Skill source audits to semantic auditor; old Skill notes without canonical evidence become UNKNOWN; existing non-Skill legacy audits remain callable.
2. Default extraction checks reusable semantic cache instead of file existence; --all really bypasses semantic reuse. Incomplete drafts cannot auto-approve. CLI summary shows coverage unmeasured when None and separate coverage/support/synthesis statuses; preserve JSON fields used by consumers and add explicit dimensions.
3. Document schema/version and cache transition, canonical sidecars, current model budgets and no-fabrication/no-copy semantics. Choose release version 2.5.0 to avoid reusing archived/discarded 2.4.0 identity. New numeric defaults must be explicitly inherited from main or documented as initial resource budgets.
4. Run full suite once integrations stabilize. Independently review code and fix meaningful findings with regressions. Do not commit/push.

## Task 5 — Real workflow and artifact inspection (root)

1. New isolated run directory using fixed five original inputs and configured model; never production vault. Preserve raw prompts/responses, stage, finish_reason, usage, generated JSON and both formats. Do not reuse earlier translation or failed caches.
2. Start with requesting-code-review as the shortest approved style reference; inspect actual body, inventory mappings and audit results. Resolve real defects, then process remaining four and retain original attempts. Resume from completed stages if interrupted.
3. Validate output hash/sidecar linkage and source maps; manually inspect examples, exceptions, principle/reason retention and actual unsupported/weak findings. Distinguish mock tests, model judgments and manual conclusions.
4. Compare against archived main on same sources/model, report actual model usage, complete state and semantic judgments without claiming perfect quality from self-scored coverage. Record phenomenon/root cause/fix/risks in vault project records, redact tracked docs and leave nonurgent items in private todo.

## Review focus

- Source spans at the end, frontmatter descriptions and fenced examples must not disappear during packing.
- Different source/claim ids, truncation, model failures, stale sidecars and incomplete evaluations must never produce PASS or reusable successful cache.
- Provenance linkage does not itself prove semantic support; model audit and manual checks remain necessary.
- Rendering must neither duplicate the canonical body nor reintroduce translated chapter lists/raw English quotes.
- Approved source-specific conditions/exceptions/numbers must survive synthesis without invented hard requirements.
