# Semantic calibration implementation plan

> Use test-driven-development and requesting-code-review. User approved the preceding concrete repair scope; no commit/push authorization is implied.

**Goal:** Correct metadata/identity audit boundaries while retaining real source omissions and strengthening synthesis retention.

**Architecture:** Add a focused audit evidence helper; enrich audit payload with source frontmatter identity; validate typed negative witnesses; clarify extraction and audit prompts without changing canonical source schema or SE admission rules.

**Stack:** Existing Python, pytest, PyYAML, current SkillMind runtime and configured provider.

- [x] Independent review of the spec/plan against three archived failures.
- [x] Write and observe failing grounded-evidence/identity/metadata/real-omission regressions.
- [x] Implement audit evidence helper and v3 policy/evaluator; migrate old negative fixtures to genuine witnesses.
- [x] Update atomic-list/intent synthesis instructions, Skill-only prompt identity and local patch version; preserve non-Skill caches.
- [x] Run full SkillMind/SE tests and independent review; fix substantive findings.
- [x] Run controlled real audits on unmodified five-source artifacts; regenerate only the genuinely incomplete source, preserve all attempts and usage.
- [x] Verify evidence/body/SE compatibility and unchanged production data; publish a sanitized local report and update todos. Leave code uncommitted/unpushed.

Offline evidence: initial focused RED had eight failures; final SkillMind suite has 117 passes and SE has 123 passes (observed counts, not quality thresholds). Independent review caught malformed status types masking an existing valid FAIL; string guards and mixed-case regressions fix it. CLI confirms local version 2.5.1.

Live checks preserve a newly identified, source-backed but overly broad trigger keyword in diagnosing-bugs as FAIL. The actual SE any-keyword matcher admits an unrelated feedback-collection context. Keywords versus applicability policy requires separate discussion; no deletion, automatic PASS or repeated valid-negative audit is part of this repair. A malformed duplicate-key model response is retried only under the existing protocol retry policy and preserved in full.

Final measured evidence: nine real calls, 171223 tokens, 457.84 seconds. Original code-review and implement-spec pass without edits; original codebase-design still fails for unit, while its single regenerated candidate restores unit/component/service and passes all dimensions. Final candidates are four PASS and one blocked FAIL. Independent semantic review confirms the restored constraint and genuine cross-section synthesis. SE isolates four publications with all 65 items preserved; final-code replay of all six valid audit objects matches every verdict/dimension. Production 551 file hashes and all original test artifacts remain unchanged, no role gene changes. Counts are observed evidence, not quality thresholds. No commit/push.
