# Whole-unit attribution exclusion repair

> User approved continuing inventory separation and whole-unit exclusion verification after the real pr false-positive. Use TDD and independent plan/code review. No commit/push or production ingestion. Preserve the first-run files and its automatic PASS.

**Goal:** Keep purpose/operational knowledge separate from credits before stable unit IDs exist; require audit evidence about the entire excluded unit.

**Architecture:** Clarify inventory and synthesis prompts without automatic unit rewriting. Version the semantic audit response to require explicit whole-unit scope and content classification. Fold decisions into existing inventory status. Add a shared, no-model approval validator so the consumer cannot reuse an old v2 exclusion approval missing the new evidence.

## Fixed contracts

- Canonical semantic v1/v2 unchanged. Extract prompt becomes extract_v14; auditor becomes semantic_audit_v5; SkillMind patch 2.6.1. SE patch 0.3.1 denotes strict evidence-consumption compatibility, not independent semantic evaluation.
- Inventory creates separate method/purpose/flags/dependency and pure-attribution units, even when sharing a raw span. Pure credits stay explicit for later exclusion; not hidden in ignored_spans or fused with source purpose. Synthesis never splits/renumbers source units or uses partial quote as whole-unit exclusion.
- V2 exclusion judgment closed fields: source_id, status, scope, unit_content, reason, optional counterexample. scope must be whole_unit. unit_content enum attribution_only/mixed/method/unknown. A justified decision is usable only for attribution_only + whole_unit. A mixed/method justified response is inconsistent and UNKNOWN, never PASS. Unjustified still needs grounded negative evidence; valid FAIL dominates unknown. Unknown classifications never reduce coverage denominator.
- Whole-unit semantic instructions explicitly inspect the unit's full statement and full-source method context. source_quote proves original evidence only, does not narrow scope. Purpose appearing in another unit or meta does not excuse mixed-unit exclusion. kind is a clue, not an admission rule.
- New `validate_exclusion_approval(drafts, semantic_evidence)` performs only protocol checks (no model): exact namespaced excluded IDs, exact allowed fields, whole_unit scope, attribution_only classification and justified status, nonempty reason. Empty v2 exclusions still require explicit empty decisions; attribution_check must be PASS. Legacy v1 unchanged. Raise controlled ValueError on absent/partial evidence. Auditor evaluation and SE approval checks share this contract; old v2 decisions lacking fields cannot authorize new writes. Do not edit their original historical verdicts.
- SE final publication guard verifies this protocol for v2 alongside all existing source/draft/staging/hash checks and four-dimensional PASS. Add consumer regression only; no extra model, score or source-kind allowlist.

## Steps

- [x] Independently review this plan against first PR original/candidate/audit evidence.
- [x] RED tests for archived false-positive response, explicit quoted-fragment scope, mixed+justified inconsistency, unknown classification, pure attribution with misleading kind, and consumer rejecting old/incomplete approvals.
- [x] Implement mandatory scope/content checks, prompts, shared approval validator and prompt/patch identities; update positive fixtures to provide required evidence.
- [x] Full SkillMind/SE suites and independent review; preserve v1 and positive pure-credit paths. Observed 200/161 passed, not quality thresholds; independent reviewer reproduced old real approval rejection.
- [x] One real audit of the unchanged old mixed-unit draft, then one fresh inventory+synthesis+audit on the same pinned PR source/model/resource settings. Observed 4 calls / 72665 tokens, no retries/rerolls. Old result UNKNOWN (invalid negative witness rejected), fresh PASS. Old first-run manifest preserved.
- [x] Read source/ledger/body and both audit outcomes; independent review confirms purpose retained, one pure-attribution unit excluded, no clear method omissions. Final bytes/sidecar/engine recall/full payload/tamper and malformed-approval probes verified. Formal 551 files/role directories unchanged. Sanitized project report and TODO written; uncommitted/unpushed. Deep synthesis quality remains a separate limitation, not claimed solved.

Numeric identities are versions or observed source/test records; no new quality percentage, word-count or retry threshold. The existing provider retry budget stays unchanged. Live calls reuse authorized in-memory configuration and isolated storage.

## Subsequent publication authorization

After the validation checkpoint above, the user explicitly requested commits/pushes for both products and a vault update. Fresh full suites again passed (200 SkillMind, 161 SE). Publish the reviewed code through agent branches and PRs, preserving historical validation outcomes. This does not authorize package publishing, automatic main merges or production skill ingestion.
