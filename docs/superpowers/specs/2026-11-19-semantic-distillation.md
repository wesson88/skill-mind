# Semantic distillation specification

Approved user goal: distill GitHub Skills into reusable knowledge, not chapter translations or copied prose. Preserve meaningful source content, including rationale, concepts, examples, exceptions, dependencies and executable details; do not invent claims. The user approved the source-backed code-review sample and requested implementation plus a real run on the same five Skills.

Use main as the baseline. Do not restore the discarded quality-gates branch. Keep legacy document formats readable and preserve non-Skill collectors. No commits, remote publication or production-vault writes without user authorization.

## Canonical contract (semantic_distillation_v1)

A new Skill draft has `schema_version: semantic_distillation_v1`, `extract_complete: bool`, `distillation: {source_spans, source_units, ignored_spans, sections, uncovered}` plus existing uuid/meta/source/status fields.

- source_spans: `{id, text, section, start_line, end_line}`. Program-generated immutable source spans, including frontmatter trigger descriptions and fenced examples. All substantive text must be retained in spans.
- source_units: `{id, kind, statement, span_ids}`. Closed kinds: purpose, concept, principle, procedure, decision, constraint, exception, pitfall, example, dependency. Source ids reference actual spans, not model-created quotations.
- ignored_spans: `{span_id, reason}`; source decoration can be recorded here, with substantive exclusions subject to audit.
- sections: `{id, title, items: [{id, text, source_ids}]}`. Natural Chinese synthesis can merge source units across chapters, retain commands/numbers where exactness matters, and use new organizational titles. No forced condition/action template or fixed word/length quota.
- uncovered: `{source_id, reason}`. Every unit must be linked from a content item or explicitly uncovered; every span must be referenced by a unit or explicitly ignored. Unknown ids, empty evidence, invalid enums and duplicate ids are errors. Evidence linkage is not proof of semantic correctness.

Coverage is semantic, not chapter-name/string matching. Audits separately assess source→output coverage, output→source support (including published names/intents/triggers), and synthesis quality (no translation/repetition masquerading as distillation). A separate source_gaps check compares original text to the inventory itself. Unknown/failed evaluations cannot PASS. No invented percentage or compression cutoff. A PASS requires complete semantic coverage, supported claims and a passing synthesis judgment; weak/missing/unverified content remains visible and requires review. Conceptual material is not forced into executable rules.

## Evidence and publication

Final Obsidian and Agent Skill bodies render the same canonical sections; no repeated raw English evidence. Publish an adjacent `.evidence.json` envelope `{artifact_sha256, draft}` for the actual Markdown artifact. Audit validates file hash and canonical source hash, then reads evidence directly; it must never guess evidence by parsing Markdown backticks. Missing/stale/edited evidence is UNKNOWN, never PASS.

## Generation and cache

Inventory source spans in bounded batches, then synthesize the complete inventory with source context. Treat finish_reason=length as incomplete even when JSON can be repaired. No heuristic fallback disguised as completed semantic output. Freeze inventory in successful extraction and reuse it during audits. Cache identity includes source/prompt/schema/model and generation parameters, and only complete valid results can be reused. Upgrade prompt identity beyond discarded extract_v10 to avoid cache collisions. Numeric request-size/timeout/output defaults inherit main's existing defaults, are configurable, and are resource budgets, not quality criteria.

## Verification

Meaningful regressions: preserve long fenced examples and final source spans; include non-procedural units; permit multi-source synthesis; reject dangling ids and uncovered units as complete; distinguish empty/truncated responses; invalidate model/parameter/schema caches; prevent Markdown editing or multi-line/backtick evidence from creating false verdicts; reject translations and unsupported claims even with nominal full coverage; preserve legacy renderer compatibility. Validate on the fixed five superpowers inputs with the same configured model, isolated data/output, original requests/responses and usage. Compare to archived main outputs and manually review representative source→synthesis mappings.
