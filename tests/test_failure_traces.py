"""Extraction failures retain stage and identifiers through official artifacts."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from skillmind import distillation as d


def setup_source(tmp_path):
    raw = tmp_path/'SKILL.md'; raw.write_text('Diagnose failures.\n', encoding='utf-8')
    source = {'source_hash': hashlib.sha256(raw.read_bytes()).hexdigest(), 'doc_type': 'skill'}
    cfg = {'llm': {'model': 'test/model', 'max_retries': 0}, 'diagnostics_dir': str(tmp_path/'diagnostics')}
    return raw, source, cfg


def response(value, finish='stop'):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=value))],
        usage=SimpleNamespace(model_dump=lambda: {'prompt_tokens': 12, 'completion_tokens': 4, 'total_tokens': 16}))


def test_missing_disposition_error_cache_contains_exact_unit_and_response_finish(tmp_path, monkeypatch):
    raw, source, cfg = setup_source(tmp_path)
    monkeypatch.setattr(d, 'EXTRACT_CACHE_DIR', tmp_path/'cache')
    inv = {'units': [{'kind':'purpose', 'statement':'Diagnose failures', 'span_ids':['p0001']},
                     {'kind':'principle','statement':'Use evidence', 'span_ids':['p0001']}], 'ignored_spans': []}
    syn = {'meta':{'name':'Diagnosis','intent':'Diagnose failures'},'sections':[{'id':'s','title':'Method','items':[
        {'id':'i','text':'Use evidence','source_ids':['u0002']}]}], 'excluded':[], 'uncovered':[]}
    with patch('litellm.completion', side_effect=[response(json.dumps(inv)),response(json.dumps(syn))]), \
         patch.object(d,'resolve_llm_credentials',return_value={'model':'test/model','api_key':'local-test-key'}), \
         patch.object(d.time,'sleep'), patch.object(d,'save_draft') as save:
        with pytest.raises(d.DistillationError) as caught:
            d.distill_skill(str(raw),source,cfg)
    assert 'u0001' in str(caught.value)
    error = caught.value.diagnostic
    assert error['stage']=='synthesis-validate' and error['category']=='contract'
    assert error['finish_reason']=='stop'
    assert any(row.get('unit_ids')==['u0001'] for row in error['validation_errors'])
    cache=json.loads(d._cache_path(source['source_hash'],cfg).read_text(encoding='utf-8'))
    assert cache['complete'] is False and cache['error']==error
    artifact=tmp_path/'diagnostics'/error['artifact_ref']
    assert hashlib.sha256(artifact.read_bytes()).hexdigest()==error['artifact_sha256']
    assert not save.called


@pytest.mark.parametrize('failure,category,finish', [('not-json','parse','stop'), ('truncated','response','length'), ('provider','provider',None)])
def test_failure_kinds_survive_unchanged_retries(tmp_path, monkeypatch, failure,category,finish):
    raw, source, cfg=setup_source(tmp_path)
    monkeypatch.setattr(d,'EXTRACT_CACHE_DIR',tmp_path/'cache')
    reply=response('not valid JSON',finish or 'stop')
    kwargs={'side_effect':RuntimeError('provider unavailable')} if failure=='provider' else {'return_value':reply}
    with patch('litellm.completion',**kwargs),patch.object(d,'resolve_llm_credentials',return_value={'model':'test/model','api_key':'local-test-key'}),patch.object(d.time,'sleep'):
        with pytest.raises(d.DistillationError) as caught:d.distill_skill(str(raw),source,cfg)
    error=caught.value.diagnostic
    assert error['category']==category and error['finish_reason']==finish
    assert error['stage']=='inventory-1'
    assert not d.has_reusable_distillation_cache(source['source_hash'],cfg)


def test_source_validation_failure_is_traced_before_model_call(tmp_path, monkeypatch):
    raw,source,cfg=setup_source(tmp_path);raw.write_text('changed',encoding='utf-8')
    monkeypatch.setattr(d,'EXTRACT_CACHE_DIR',tmp_path/'cache')
    with patch('litellm.completion') as call:
        with pytest.raises(d.DistillationError) as caught:d.distill_skill(str(raw),source,cfg)
    assert not call.called
    assert caught.value.diagnostic['stage']=='source-validate'


def test_resolved_provider_credentials_are_redacted_even_when_not_in_config(tmp_path, monkeypatch):
    raw,source,cfg=setup_source(tmp_path)
    secret='opaque-resolved-credential'
    monkeypatch.setattr(d,'EXTRACT_CACHE_DIR',tmp_path/'cache')
    with patch('litellm.completion',side_effect=RuntimeError('request failed for '+secret)), \
         patch.object(d,'resolve_llm_credentials',return_value={'model':'test/model','api_key':secret}),patch.object(d.time,'sleep'):
        with pytest.raises(d.DistillationError) as caught:d.distill_skill(str(raw),source,cfg)
    assert secret not in json.dumps(caught.value.diagnostic)
    for path in (tmp_path/'diagnostics').rglob('*.json'):
        assert secret not in path.read_text(encoding='utf-8')
    assert secret not in d._cache_path(source['source_hash'],cfg).read_text(encoding='utf-8')


def test_cache_hit_draft_write_failure_keeps_actual_stage(tmp_path, monkeypatch):
    raw,source,cfg=setup_source(tmp_path)
    monkeypatch.setattr(d,'EXTRACT_CACHE_DIR',tmp_path/'cache')
    with patch.object(d,'_cached_drafts',return_value=[{'uuid':'cached'}]),patch.object(d,'save_draft',side_effect=OSError('write failed')):
        with pytest.raises(d.DistillationError) as caught:d.distill_skill(str(raw),source,cfg)
    assert caught.value.diagnostic['stage']=='draft-write'


def test_unknown_inventory_reference_is_identified(tmp_path, monkeypatch):
    raw,source,cfg=setup_source(tmp_path)
    monkeypatch.setattr(d,'EXTRACT_CACHE_DIR',tmp_path/'cache')
    inv={'units':[{'kind':'purpose','statement':'Method','span_ids':['pUNKNOWN']}],'ignored_spans':[]}
    with patch.object(d,'_call_json',return_value=inv):
        with pytest.raises(d.DistillationError) as caught:d.distill_skill(str(raw),source,cfg)
    assert 'pUNKNOWN' in str(caught.value)
    assert any('pUNKNOWN' in row.get('span_ids',[]) for row in caught.value.diagnostic['validation_errors'])


def test_second_call_credentials_failure_does_not_reuse_inventory_response(tmp_path, monkeypatch):
    raw,source,cfg=setup_source(tmp_path)
    monkeypatch.setattr(d,'EXTRACT_CACHE_DIR',tmp_path/'cache')
    inv={'units':[{'kind':'purpose','statement':'Method','span_ids':['p0001']}],'ignored_spans':[]}
    with patch('litellm.completion',return_value=response(json.dumps(inv))),patch.object(d,'resolve_llm_credentials',
        side_effect=[{'model':'test/model','api_key':'fixture'},RuntimeError('credentials unavailable')]),patch.object(d.time,'sleep'):
        with pytest.raises(d.DistillationError) as caught:d.distill_skill(str(raw),source,cfg)
    error=caught.value.diagnostic
    assert error['stage']=='synthesis'
    assert error['finish_reason'] is None and error['usage'] is None


def test_public_exception_is_sanitized_before_console_rendering(tmp_path, monkeypatch):
    raw,source,cfg=setup_source(tmp_path);cfg['llm']['api_key']='fixture-secret'
    monkeypatch.setattr(d,'EXTRACT_CACHE_DIR',tmp_path/'cache')
    inv={'units':[{'kind':'purpose','statement':'Method','span_ids':['/private-machine/project fixture-secret']}],'ignored_spans':[]}
    with patch.object(d,'_call_json',return_value=inv):
        with pytest.raises(d.DistillationError) as caught:d.distill_skill(str(raw),source,cfg)
    assert 'fixture-secret' not in str(caught.value)
    assert 'private-machine' not in str(caught.value)


def test_generation_requires_explicit_v2_excluded_array(tmp_path, monkeypatch):
    raw,source,cfg=setup_source(tmp_path)
    monkeypatch.setattr(d,'EXTRACT_CACHE_DIR',tmp_path/'cache')
    inv={'units':[{'kind':'purpose','statement':'Method','span_ids':['p0001']}],'ignored_spans':[]}
    syn={'meta':{'name':'Method','intent':'Method'},'sections':[{'title':'Method','items':[{'text':'Method','source_ids':['u0001']}]}],'uncovered':[]}
    with patch.object(d,'_call_json',side_effect=[inv,syn]),patch.object(d,'save_draft') as save:
        with pytest.raises(d.DistillationError) as caught:d.distill_skill(str(raw),source,cfg)
    assert 'excluded' in str(caught.value) and not save.called


@pytest.mark.parametrize('value', ['invalid',0,float('nan')])
def test_invalid_resource_settings_have_explicit_preflight_classification(tmp_path, monkeypatch,value):
    raw,source,cfg=setup_source(tmp_path);cfg['llm']['qpm']=value
    monkeypatch.setattr(d,'EXTRACT_CACHE_DIR',tmp_path/'cache')
    with patch('litellm.completion') as call:
        with pytest.raises(d.DistillationError) as caught:d.distill_skill(str(raw),source,cfg)
    assert not call.called
    assert caught.value.diagnostic['stage']=='configuration-validate'
    assert caught.value.diagnostic['category']=='contract'
