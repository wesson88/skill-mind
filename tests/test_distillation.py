"""Behavioral regressions for source-backed synthesis, not LLM wording."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from skillmind import distillation as d


SOURCE = '# Policy\n\nReject unpaid orders.\n\n# Exception\n\nRefunds are exempt.\n'


def payload():
    spans = d.parse_source_spans(SOURCE)
    return {
        'source_spans': spans,
        'source_units': [
            {'id': 'u0001', 'kind': 'constraint', 'statement': '未付款订单不得受理', 'span_ids': [spans[0]['id'], spans[1]['id']]},
            {'id': 'u0002', 'kind': 'exception', 'statement': '退款业务例外', 'span_ids': [spans[2]['id'], spans[3]['id']]},
        ],
        'ignored_spans': [],
        'sections': [{'id': 's1', 'title': '受理边界', 'items': [
            {'id': 'k1', 'text': '受理前检查付款状态；未付款拒绝受理，退款业务不适用这一限制。', 'source_ids': ['u0001', 'u0002']},
        ]}],
        'uncovered': [],
    }


class DistillationContractTests(unittest.TestCase):
    def test_source_spans_preserve_frontmatter_fences_and_last_line(self):
        text = '---\nname: sample\ndescription: check before work\n---\n\n# Code\n\n```py\na = `x`\n\nb = 2\n```\n\nFinal exception'
        spans = d.parse_source_spans(text)
        combined = '\n'.join(x['text'] for x in spans)
        for line in text.splitlines():
            if line.strip(): self.assertIn(line, combined)
        code = [x for x in spans if '```py' in x['text']]
        self.assertEqual(len(code), 1)
        self.assertIn('b = 2\n```', code[0]['text'])
        self.assertEqual(spans[-1]['text'], 'Final exception')
        self.assertEqual(len({x['id'] for x in spans}), len(spans))

    def test_cross_chapter_synthesis_is_valid(self):
        self.assertEqual(d.validate_distillation(payload()), [])

    def test_dangling_references_and_duplicate_ids_rejected(self):
        p = payload()
        p['sections'][0]['items'][0]['source_ids'].append('invented')
        self.assertTrue(d.validate_distillation(p))
        p = payload(); p['source_units'][1]['id'] = 'u0001'
        self.assertTrue(d.validate_distillation(p))

    def test_non_procedural_knowledge_not_forced_into_condition_slots(self):
        p = payload(); p['source_units'][0]['kind'] = 'principle'
        self.assertEqual(d.validate_distillation(p), [])

    def test_undeclared_missing_content_is_rejected(self):
        p = payload(); p['sections'][0]['items'][0]['source_ids'] = ['u0001']
        self.assertTrue(d.validate_distillation(p))
        p['uncovered'] = [{'source_id': 'u0002', 'reason': '尚未纳入正文'}]
        self.assertEqual(d.validate_distillation(p), [])

    def test_invalid_kinds_and_ignored_meaningless_reasons_rejected(self):
        p = payload(); p['source_units'][0]['kind'] = 'anything'
        self.assertTrue(d.validate_distillation(p))

    def test_malformed_model_values_report_validation_errors(self):
        p = payload(); p['source_units'][0]['kind'] = ['principle']
        self.assertTrue(d.validate_distillation(p))
        p = payload(); p['source_units'][1]['span_ids'] = [p['source_spans'][2]['id']]
        p['ignored_spans'] = [{'span_id': p['source_spans'][3]['id'], 'reason': ''}]
        self.assertTrue(d.validate_distillation(p))


class PipelineTests(unittest.TestCase):
    def test_semantic_policy_version_does_not_invalidate_legacy_document_identity(self):
        from skillmind import config
        self.assertEqual(config.CURRENT_PROMPT_VERSION, 'extract_v11')
        self.assertEqual(d.CURRENT_PROMPT_VERSION, 'extract_v14')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / 'SKILL.md'; self.raw.write_text(SOURCE, encoding='utf-8')
        self.info = {'source_hash': hashlib.sha256(self.raw.read_bytes()).hexdigest(), 'doc_type': 'skill', 'source_path': 'SKILL.md'}
        self.cfg = {'llm': {'model': 'test/one'}}
        self.cache_patch = patch.object(d, 'EXTRACT_CACHE_DIR', self.root / 'cache')
        self.cache_patch.start(); self.addCleanup(self.cache_patch.stop)
        self.save_patch = patch.object(d, 'save_draft')
        self.save_patch.start(); self.addCleanup(self.save_patch.stop)

    def responses(self, uncovered=False):
        p = payload()
        inv = {'units': [{k: v for k, v in x.items() if k != 'id'} for x in p['source_units']], 'ignored_spans': []}
        syn = {'meta': {'name': '受理规则', 'intent': '检查受理边界'}, 'sections': p['sections'], 'uncovered': [], 'excluded': []}
        if uncovered:
            syn['sections'][0]['items'][0]['source_ids'] = ['u0001']
            syn['uncovered'] = [{'source_id': 'u0002', 'reason': '尚未提炼'}]
        return [inv, syn]

    def test_success_keeps_evidence_and_does_not_repeat_legacy_fields(self):
        with patch.object(d, '_call_json', side_effect=self.responses()) as call:
            drafts = d.distill_skill(str(self.raw), self.info, self.cfg)
        self.assertEqual(call.call_count, 2)
        self.assertTrue(drafts[0]['extract_complete'])
        self.assertEqual(drafts[0]['schema_version'], d.SCHEMA_VERSION)
        self.assertEqual(drafts[0]['distillation']['source_units'][1]['kind'], 'exception')
        self.assertFalse(drafts[0].get('key_concepts'))
        self.assertTrue(d.has_reusable_distillation_cache(self.info['source_hash'], self.cfg))
        changed = {'llm': {'model': 'test/two'}}
        self.assertFalse(d.has_reusable_distillation_cache(self.info['source_hash'], changed))

    def test_declared_uncovered_result_never_reuses_success_cache(self):
        with patch.object(d, '_call_json', side_effect=self.responses(uncovered=True)):
            drafts = d.distill_skill(str(self.raw), self.info, self.cfg)
        self.assertFalse(drafts[0]['extract_complete'])
        self.assertFalse(d.has_reusable_distillation_cache(self.info['source_hash'], self.cfg))

    def test_model_local_item_labels_are_replaced_without_losing_content(self):
        inv, syn = self.responses()
        syn['sections'].append({'id': 's1', 'title': '例外判断', 'items': [
            {'id': 'k1', 'text': '退款业务属于例外。', 'source_ids': ['u0002']},
        ]})
        with patch.object(d, '_call_json', side_effect=[inv, syn]):
            drafts = d.distill_skill(str(self.raw), self.info, self.cfg)
        sections = drafts[0]['distillation']['sections']
        self.assertEqual(len({s['id'] for s in sections}), len(sections))
        items = [x for s in sections for x in s['items']]
        self.assertEqual(len({x['id'] for x in items}), len(items))
        self.assertEqual(items[-1]['text'], '退款业务属于例外。')
        self.assertEqual(items[-1]['source_ids'], ['u0002'])

    def test_failure_never_becomes_heuristic_success(self):
        with patch.object(d, '_call_json', side_effect=RuntimeError('truncated response')):
            with self.assertRaises(RuntimeError): d.distill_skill(str(self.raw), self.info, self.cfg)
        self.assertFalse(d.has_reusable_distillation_cache(self.info['source_hash'], self.cfg))

    def test_force_ignores_previously_successful_cache(self):
        with patch.object(d, '_call_json', side_effect=self.responses()):
            d.distill_skill(str(self.raw), self.info, self.cfg)
        with patch.object(d, '_call_json', side_effect=RuntimeError('fresh call required')):
            with self.assertRaises(RuntimeError): d.distill_skill(str(self.raw), self.info, self.cfg, force=True)

    def test_closed_json_with_length_finish_is_not_success(self):
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='length', message=SimpleNamespace(content='{"units": [], "ignored_spans": []}'))])
        cfg = {'llm': {'model': 'test/one', 'max_retries': 0}}
        with patch('litellm.completion', return_value=response), patch.object(d, 'resolve_llm_credentials', return_value={'model': 'test/one', 'api_key': 'local-test-key'}):
            with self.assertRaises(d.DistillationError): d._call_json('system', 'input', cfg, 'inventory')

    def test_empty_or_array_response_is_not_a_semantic_object(self):
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='[]'))])
        cfg = {'llm': {'model': 'test/one', 'max_retries': 0}}
        with patch('litellm.completion', return_value=response), patch.object(d, 'resolve_llm_credentials', return_value={'model': 'test/one', 'api_key': 'local-test-key'}), patch.object(d, '_LAST_REQUEST_AT', 0):
            with self.assertRaises(d.DistillationError): d._call_json('system', 'input', cfg, 'inventory')


if __name__ == '__main__': unittest.main()
