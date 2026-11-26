"""Attribution disposition does not weaken the source ledger."""
import copy
import hashlib
import json
from unittest.mock import patch

import pytest

from skillmind import distillation as d

V1 = 'semantic_distillation_v1'
V2 = 'semantic_distillation_v2'
SOURCE = '---\nname: demo\ndescription: Diagnose failures.\ndisable-model-invocation: true\nmetadata:\n  credits: Thanks to upstream authors.\n---\n\n# Method\n\nPreserve a reproducible failure.\n'


def candidate():
    spans = d.parse_source_spans(SOURCE)
    return {'source_spans': spans, 'source_units': [
        {'id': 'u1', 'kind': 'purpose', 'statement': 'Diagnose failures', 'span_ids': [spans[0]['id']]},
        {'id': 'u2', 'kind': 'dependency', 'statement': 'Thanks to upstream authors', 'span_ids': [spans[0]['id']]},
        {'id': 'u3', 'kind': 'constraint', 'statement': 'Invocation disabled', 'span_ids': [spans[0]['id']]},
        {'id': 'u4', 'kind': 'procedure', 'statement': 'Preserve reproducibility', 'span_ids': [s['id'] for s in spans[1:]]},
    ], 'ignored_spans': [], 'sections': [{'id': 's1', 'title': 'Diagnosis', 'items': [
        {'id': 'i1', 'text': 'Diagnose reproducible failures; automatic invocation is disabled.', 'source_ids': ['u1', 'u3', 'u4']}
    ]}], 'uncovered': [], 'excluded': [{'source_id': 'u2', 'reason_code': 'attribution', 'span_ids': [spans[0]['id']],
        'source_quote': 'credits: Thanks to upstream authors.', 'reason': 'Source credits, not an operational dependency.'}]}


def test_v2_explicit_exclusion_closes_ledger_without_publishing_credits():
    assert d.validate_distillation(candidate(), schema_version=V2) == []


@pytest.mark.parametrize('change', ['reason_code', 'quote', 'unknown_span', 'unrelated_span', 'unknown_unit', 'overlap', 'missing', 'extra', 'duplicate'])
def test_forged_or_incomplete_exclusion_cannot_close_ledger(change):
    p = candidate(); row = p['excluded'][0]
    if change == 'reason_code': row['reason_code'] = 'anything'
    elif change == 'quote': row['source_quote'] = 'not in the original'
    elif change == 'unknown_span': row['span_ids'] = ['unknown']
    elif change == 'unrelated_span': row.update(span_ids=[p['source_spans'][-1]['id']], source_quote=p['source_spans'][-1]['text'])
    elif change == 'unknown_unit': row['source_id'] = 'unknown'
    elif change == 'overlap': p['sections'][0]['items'][0]['source_ids'].append('u2')
    elif change == 'missing': p['excluded'] = []
    elif change == 'extra': row['approved'] = True
    else: p['excluded'].append(copy.deepcopy(row))
    assert d.validate_distillation(p, schema_version=V2)


def test_v1_is_not_silently_reinterpreted_as_v2():
    p = candidate()
    assert d.validate_distillation(p, schema_version=V1)
    p.pop('excluded'); p['uncovered'] = [{'source_id': 'u2', 'reason': 'Not synthesized'}]
    assert d.validate_distillation(p, schema_version=V1) == []


def test_omitted_purpose_reports_exact_missing_unit():
    p = candidate(); p['sections'][0]['items'][0]['source_ids'].remove('u1')
    errors = d.validate_distillation(p, schema_version=V2)
    assert any('u1' in message and 'disposition' in message for message in errors)


@pytest.mark.parametrize('true_omission', [False, True])
def test_generation_exclusion_is_auditable_and_cacheable_but_not_a_pass(tmp_path, monkeypatch, true_omission):
    p = candidate(); raw = tmp_path/'SKILL.md'; raw.write_bytes(SOURCE.encode())
    info = {'source_hash': hashlib.sha256(raw.read_bytes()).hexdigest(), 'doc_type': 'skill'}
    inv = {'units': [{k:v for k,v in u.items() if k!='id'} for u in p['source_units']], 'ignored_spans': []}
    # Generator assigns stable u000N IDs.
    syn = {'meta': {'name': 'Diagnosis', 'intent': 'Diagnose failures'}, 'sections': copy.deepcopy(p['sections']), 'uncovered': [], 'excluded': copy.deepcopy(p['excluded'])}
    syn['sections'][0]['items'][0]['source_ids'] = ['u0001', 'u0003', 'u0004']
    syn['excluded'][0]['source_id'] = 'u0002'
    if true_omission:
        syn['sections'][0]['items'][0]['source_ids'].remove('u0003')
        syn['uncovered'] = [{'source_id': 'u0003', 'reason': 'Invocation constraint not synthesized'}]
    monkeypatch.setattr(d, 'EXTRACT_CACHE_DIR', tmp_path/'cache')
    cfg = {'llm': {'model': 'test/model'}, 'diagnostics_dir': str(tmp_path/'diagnostics')}
    with patch.object(d, '_call_json', side_effect=[inv, syn]), patch.object(d, 'save_draft'):
        draft = d.distill_skill(str(raw), info, cfg)[0]
    assert draft['schema_version'] == V2 and draft['extract_complete'] is (not true_omission)
    assert draft['distillation']['excluded'][0]['source_id'] == 'u0002'
    assert 'Thanks' not in ''.join(it['text'] for s in d.renderable_sections(draft) for it in s['items'])
    assert draft.get('status') != 'approved'
    assert d.has_reusable_distillation_cache(info['source_hash'], cfg) is (not true_omission)
