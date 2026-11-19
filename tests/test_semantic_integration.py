"""Entry-point and real publication safety boundaries for semantic extraction."""
import hashlib
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner
from skillmind import auditor, collector, extractor
from skillmind.cli import app


class SemanticIntegrationTests(unittest.TestCase):
    def test_machine_audit_json_is_unwrapped_and_has_no_progress_or_markup(self):
        summary = {'source_hash': 'a' * 64, 'source_title': '[red] literal ' + 'long title ' * 30, 'verdict': 'PASS', 'coverage': 100.0}
        report = SimpleNamespace(to_summary_dict=lambda: summary)
        with patch('skillmind.auditor.audit_source', return_value=report) as call, patch('skillmind.config.load_config', return_value={'vault_dir': 'unused'}):
            result = CliRunner().invoke(app, ['audit', 'abc', '--json'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output), summary)
        self.assertIsNone(call.call_args.kwargs['console'])

    def test_extractor_routes_skill_and_force_to_semantic_pipeline(self):
        with patch('skillmind.distillation.distill_skill', return_value=['canonical']) as call:
            result = extractor.extract_skill('source.md', {'doc_type': 'skill'}, {}, force=True)
        self.assertEqual(result, ['canonical'])
        self.assertTrue(call.call_args.kwargs['force'])

    def test_skill_audit_routes_to_semantic_not_legacy_coverage(self):
        marker = object()
        with patch.object(auditor, '_resolve_source', return_value=('abc', {'doc_type': 'skill'})), patch('skillmind.semantic_auditor.audit_semantic_source', return_value=marker):
            self.assertIs(auditor.audit_source('abc', {}), marker)

    def test_url_ingest_hash_matches_bytes_and_reports_skip_state(self):
        with tempfile.TemporaryDirectory() as temp:
            response = Mock(); response.text = '# Skill\n\nNever omit exceptions.\n'
            with patch('httpx.get', return_value=response), patch.object(collector, 'RAW_DIR', Path(temp)), patch.object(collector, '_save_hashes'):
                hashes = {}
                info = collector._ingest_single_raw_url('https://raw.githubusercontent.com/a/b/main/SKILL.md', source_url='https://github.com/a/b/blob/main/SKILL.md', source_repo='https://github.com/a/b', hashes=hashes)[0]
                self.assertIs(info.get('skipped'), False)
                self.assertEqual(hashlib.sha256(Path(info['raw_path']).read_bytes()).hexdigest(), info['source_hash'])
                again = collector._ingest_single_raw_url('https://raw.githubusercontent.com/a/b/main/SKILL.md', source_url='https://github.com/a/b/blob/main/SKILL.md', source_repo='https://github.com/a/b', hashes=hashes)[0]
                self.assertIs(again.get('skipped'), True)

    def test_atomic_source_write_keeps_hash_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            text = 'first\nsecond\n'
            path = Path(temp) / 'raw.md'; collector._atomic_write_text(path, text)
            self.assertEqual(path.read_bytes(), text.encode('utf-8'))

    def test_source_cleanup_removes_semantic_cache_only_for_target_source(self):
        from skillmind import cache_admin
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); cache = root / 'extract'; cache.mkdir()
            target = cache / 'fingerprint.semantic.json'; target.write_text(json.dumps({'source_hash': 'abc', 'complete': True}), encoding='utf-8')
            other = cache / 'other.semantic.json'; other.write_text(json.dumps({'source_hash': 'other', 'complete': True}), encoding='utf-8')
            drafts = root / 'drafts'; drafts.mkdir()
            with patch.object(cache_admin, 'EXTRACT_CACHE_DIR', cache), patch.object(cache_admin, 'RAW_DIR', root / 'raw'), patch.object(cache_admin, 'DRAFTS_DIR', drafts), patch.object(cache_admin, '_load_hashes', return_value={}):
                stats = cache_admin.cleanup_source('abc')
            self.assertFalse(target.exists())
            self.assertTrue(other.exists())
            self.assertEqual(stats['extract_deleted'], 1)

    def test_cli_all_forces_fresh_semantic_call_and_never_autoapproves_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / 'SKILL.md'; raw.write_text('Source', encoding='utf-8')
            info = {'doc_type': 'skill', 'source_hash': 'abc', 'raw_path': str(raw)}
            draft = {'uuid': 'skill-abc', 'meta': {'name': 'partial'}, 'schema_version': 'semantic_distillation_v1', 'extract_complete': False, 'status': 'draft'}
            cfg = {'llm': {'qpm': 10}, 'vault_dir': temp}
            with patch('skillmind.collector.list_cached', return_value=[info]), patch('skillmind.config.load_config', return_value=cfg), patch('skillmind.extractor.extract_skill', return_value=[draft]) as call, patch('skillmind.reviewer.save_draft') as save:
                result = CliRunner().invoke(app, ['extract', '--all', '--auto-approve'])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(call.call_args.kwargs.get('force'))
            self.assertEqual(draft['status'], 'draft')
            save.assert_not_called()


if __name__ == '__main__': unittest.main()
