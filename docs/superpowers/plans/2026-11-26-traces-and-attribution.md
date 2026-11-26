# Failure traces and attribution exclusion implementation plan

> Execute in this session with TDD and independent plan/code review. User approved structured failure trace propagation, explicit attribution exclusion, exclusion verification and targeted regressions. No commit/push, live model call or production ingestion is implied. Existing resource budgets are inherited, not new quality thresholds.

**Goal:** Diagnose failures from official records, and retain source-backed attribution exclusions without publishing them as method knowledge.

**Architecture:** A focused diagnostics module emits sanitized per-attempt/final failure events keyed by run/source/call/attempt. Semantic v2 adds an explicit excluded disposition. Existing source-inventory audit status incorporates exclusion and output-attribution checks; SE preserves events and requires all existing dimensions. Legacy semantic v1 remains readable under its original contract.

**Stack:** Existing Python, pytest, PyYAML, local editable runtime; offline provider-boundary fixtures only.

## Contracts

- `LEGACY_SCHEMA_VERSION='semantic_distillation_v1'`; new `SCHEMA_VERSION='semantic_distillation_v2'`; supported set contains both. `validate_distillation(payload, schema_version=None)` infers v1/v2 only for its legacy public payload-only API; actual draft admission always passes the declared version. V2 adds `excluded`; no silent rewriting of v1 omissions.
- Excluded row has exactly `source_id`, `reason_code`, `span_ids`, `source_quote`, `reason`. Only reason_code `attribution` is supported. IDs/quote must exist in that unit's original spans. Covered/excluded/uncovered are disjoint and exhaustive. Real uncovered still blocks complete candidates. Method/attribution mixed units must be split. Empty method output is never publishable.
- `renderable_sections` validates using declared schema and returns method sections only. Rendering, audit discovery and cache cleanup dispatch both supported versions. New generation uses extract_v13, semantic_audit_v4 and package 2.6.0; non-Skill prompt unchanged. SE package 0.3.0 supports both schemas; these version increments identify additive contracts, not score thresholds.
- V2 auditor expects exact `exclusions:[{source_id,status:justified|unjustified|unknown,reason,counterexample?}]` plus `attribution_check:{status:PASS|FAIL|UNKNOWN,reason,counterexample?}`. Existing v1 responses remain valid. Coverage evaluates method units with excluded count separate, never called body coverage; only fully justified exclusions can reduce the display denominator. If any exclusion is missing, unknown, invalid or unjustified, coverage_weighted remains null (persisted and in SE display), never an improved percentage. Invalid/missing exclusion judgments yield UNKNOWN; grounded unjustified exclusions/output attribution yield FAIL. All feed existing source_inventory_status and final verdict. Raw source always remains authoritative. Any valid FAIL dominates independent malformed/unknown dimensions.
- Diagnostics public API: `DistillationError(message, *, stage=None, category=None, validation_errors=None)` with `.diagnostic`; `diagnostic_run(run_id)` context manager; `TraceRecorder(cfg, source_hash)` with `.activate()`, `.start_call(stage)->call_id`, `.record_attempt(call_id,attempt,*,stage,response=None,error=None,category=None)`, `.failure(exc,stage=None)->safe error dict`. `current_trace()` returns active recorder or None. SE wraps run/standalone extraction in diagnostic_run; direct calls obtain a new run identity automatically.
- TraceRecorder uses `cfg['diagnostics_dir']` when provided, otherwise config.CACHE_DIR/diagnostics. Events are append-only unique artifacts; cache only stores final diagnostic link. No raw request headers/config/tracebacks persisted. Default records response finish/usage but not response text, with explicit unavailable_reason; local contract errors contain IDs/paths. Sanitize controlled and cause messages against credentials/auth/URLs/machine paths before all sinks. Unknown unavailable values stay null. Persistence failure must not replace original classification or leak raw exception text.
- Frozen diagnostic payload fields: trace_schema=`extraction_trace_v1`, event_type=`attempt|failure`, run_id, source_hash, call_id, attempt, stage, error_type, error_message, cause_chain, category (`provider|response|parse|contract|null`), finish_reason, usage, validation_errors, model, prompt_version, schema_version, parameters (effective resource whitelist), unavailable_reason. Failure return envelope adds artifact_ref relative to diagnostics root and artifact_sha256 of actual payload bytes; do not create a self-referential hash. Tests verify payload/ref/digest and that cache and SE queue carry the identical safe envelope.
- SE extract errors are structured objects, copied into audit_details.extraction_error with run identity. Unmeasured coverage/hallucination_rate stay null. Per-run JSONL archives coexist with current queue views; records have run/source links. No re-audit-to-PASS, automatic repair, new exclusion reasons or historical migration.

## Tasks

- [x] Baseline tests and plan review; preserve original worktrees/config and all unrelated vault edits.
- [x] RED: missing disposition ID, source mismatch, non-JSON/incomplete/provider errors, multiple attempts/runs, safe failure cache diagnostics.
- [x] Implement diagnostics helper + error API; integrate capture around source/inventory/synthesis/contract/cache stages; errors list specific missing/conflicting IDs.
- [x] RED: v2 valid attribution partition, forged source quote/IDs, mixed/unsupported dispositions, nonempty true uncovered, v1 compatibility. Implement validators, generation instructions and version/cache separation.
- [x] RED: v2 exact exclusion audit, unknown/malformed records, actual dependency exclusion FAIL, attribution in metadata/body FAIL, valid FAIL plus malformed other field. Implement auditor using existing inventory dimension.
- [x] RED: SE structured error forwarding, run archive preservation, null metrics, v2 staging/strict publication and v1 regression. Adapt schema dispatch and preflight atomically.
- [x] Full offline suites, exact saved-provider-response replay where available, independent code/security review; fix findings without loosening gates.
- [x] Update README/version/spec and sanitized vault record/TODO, verify no production input/queue changes, report actual tests and limits. Leave uncommitted/unpushed.

## Verification

Use skill-mind's `.venv` for both pytest suites, with isolated SKILLMIND_HOME/SE_VAULT/state. Diagnostics tests read persisted events and final cache/queue records, not only mocks. Source fixtures include credits sharing a frontmatter span with purpose and invocation flags; tests ensure a source quote alone does not certify semantic exclusion. Provider models are mocked only at completion boundary; no credentials read from production for these checks.

Cross-repo verification binds original hashes, complete candidate sets, actual rendered Markdown and sidecars, exact audit IDs and final admission. Negative/unknown extraction must never consume stale published drafts. Explicitly retain old v1 evidence semantics and queue history. All counts reported are observed test output, not new pass thresholds.

Measured completion evidence: baseline 117/123 tests; final 193/148 tests pass. Six archived real v1 evaluations replay identically through current dual-repo boundaries with zero new model calls; 551 business file hashes unchanged during replay. Independent plan and code/security reviews passed after findings were fixed. Local CLI reports 2.6.0, SE package 0.3.0. No live re-distillation, queue migration, final role ingestion, commit or push.

## Subsequent authorization

The paragraph above preserves the initial implementation checkpoint. The whole-unit exclusion follow-up advances the local products to 2.6.1/0.3.1, documented in the companion plan. The user subsequently authorized committing and pushing both products and updating the personal vault. Publication follows the agent branch and PR workflow; no package release or production skill ingestion is included.
