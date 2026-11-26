"""Offline trace artifacts: identity, safe metadata and original failure semantics."""
import importlib.util
import copy
import hashlib
import json
from types import SimpleNamespace
import pytest


def diagnostics():
    assert importlib.util.find_spec('skillmind.diagnostics') is not None
    from skillmind import diagnostics as module
    return module


def completion():
    return SimpleNamespace(
        model='provider/returned',
        choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='PRIVATE RESPONSE BODY'))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18,
                              secret='PRIVATE USAGE EXTRA'),
        headers={'authorization': 'Bearer PRIVATE RESPONSE HEADER'},
    )


def saved_events(root):
    return [json.loads(path.read_text(encoding='utf-8')) for path in root.rglob('*.json')]


def test_error_carries_controlled_fields_without_inventing_diagnostic():
    d = diagnostics()
    error = d.DistillationError('missing unit u2', stage='contract', category='contract',
                                validation_errors=['u2 missing'])
    assert str(error) == 'missing unit u2'
    assert error.stage == 'contract'
    assert error.category == 'contract'
    assert error.validation_errors == ['u2 missing']
    assert error.diagnostic is None


def test_context_is_inherited_and_restored(tmp_path):
    d = diagnostics()
    assert d.current_trace() is None
    with d.diagnostic_run('outer-run'):
        first = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source-a')
        with first.activate():
            assert d.current_trace() is first
            with d.diagnostic_run('inner-run'):
                second = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source-b')
                with second.activate():
                    assert d.current_trace() is second
                    result = second.failure(d.DistillationError('bad', category='contract'))
                assert d.current_trace() is first
        assert result['run_id'] == 'outer-run'
    assert d.current_trace() is None
    standalone = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source-a')
    assert standalone.failure(ValueError('bad'))['run_id'] != 'outer-run'


def test_real_artifacts_preserve_all_attempts_calls_and_runs(tmp_path):
    d = diagnostics()
    recorder = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source-a')
    first = recorder.start_call('inventory')
    recorder.record_attempt(first, 0, stage='inventory', error=RuntimeError('offline'), category='provider')
    recorder.record_attempt(first, 1, stage='inventory', response=completion())
    second = recorder.start_call('synthesis')
    recorder.record_attempt(second, 0, stage='synthesis', response=completion())
    error = d.DistillationError('missing u2', stage='contract', category='contract',
                                validation_errors=['missing u2'])
    result = recorder.failure(error)
    other = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source-a')
    other.failure(ValueError('other'))
    events = saved_events(tmp_path)
    assert len(events) == 5
    assert len({p.as_posix() for p in tmp_path.rglob('*.json')}) == 5
    assert first != second
    assert len({event['run_id'] for event in events}) == 2
    assert result['call_id'] == second
    assert result['attempt'] == 0
    assert result['finish_reason'] == 'stop'
    assert result['usage'] == {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}
    assert result['validation_errors'] == ['missing u2']
    assert result['trace_schema'] == 'extraction_trace_v1'
    assert result['stage'] == 'contract'
    assert result['category'] == 'contract'
    assert result['error_type'] == 'DistillationError'
    assert error.diagnostic == result
    artifact = tmp_path / result['artifact_ref']
    raw = artifact.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == result['artifact_sha256']
    stored = json.loads(raw)
    assert 'artifact_sha256' not in stored
    assert stored['error_message'] == 'missing u2'
    assert stored['event_type'] == 'failure'
    assert 'PRIVATE RESPONSE' not in raw.decode()
    assert 'PRIVATE USAGE EXTRA' not in raw.decode()


def test_same_attempt_events_never_overwrite_each_other(tmp_path):
    d = diagnostics()
    recorder = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source')
    call = recorder.start_call('inventory')
    recorder.record_attempt(call, 0, stage='inventory', response=completion())
    recorder.record_attempt(call, 0, stage='inventory', error=ValueError('parse'), category='parse')
    recorder.failure(d.DistillationError('parse', category='parse'))
    assert len(saved_events(tmp_path)) == 3


def test_requested_metadata_uses_effective_profile_and_never_mutates_inputs(tmp_path):
    d = diagnostics()
    cfg = {'diagnostics_dir': str(tmp_path),
           'llm': {'model': 'base/model', 'temperature': 0.3},
           'llm_profiles': {'extract': {'model': 'requested/model', 'max_tokens': 91,
                                        'api_key': 'PROFILE KEY SECRET'}},
           'extra': 'UNWHITELISTED CONFIG'}
    before = copy.deepcopy(cfg)
    response = completion()
    response_before = copy.deepcopy(response)
    recorder = d.TraceRecorder(cfg, 'source')
    call = recorder.start_call('inventory')
    recorder.record_attempt(call, 0, stage='inventory', response=response)
    result = recorder.failure(d.DistillationError('bad', category='contract'))
    assert result['model'] == 'requested/model'
    assert result['returned_model'] == 'provider/returned'
    assert result['parameters']['max_tokens'] == 91
    assert result['parameters']['temperature'] == 0.3
    assert result['prompt_version']
    assert result['schema_version']
    assert cfg == before
    assert response == response_before
    dumped = json.dumps(result) + ''.join(p.read_text(encoding='utf-8') for p in tmp_path.rglob('*.json'))
    assert 'PROFILE KEY SECRET' not in dumped
    assert 'UNWHITELISTED CONFIG' not in dumped


def test_errors_and_root_causes_are_sanitized_before_any_sink(tmp_path, monkeypatch):
    d = diagnostics()
    monkeypatch.setenv('DIAGNOSTIC_TEST_API_KEY', 'ENV_SECRET_VALUE')
    cfg = {'diagnostics_dir': tmp_path, 'api_keys': {'openai': 'CONFIG_SECRET_VALUE'},
           'llm': {'api_key_env': 'DIAGNOSTIC_TEST_API_KEY'},
           'llm_profiles': {'extract': {'api_key': 'PROFILE_SECRET_VALUE'}}}
    unsafe = ('CONFIG_SECRET_VALUE PROFILE_SECRET_VALUE ENV_SECRET_VALUE '
              'Authorization: Bearer auth-secret x-api-key=header-secret '
              'sk-ant-oauth-not-in-config '
              'https://user:password@private.example/api?token=query-secret '
              'http://10.1.2.3/private '
              'C:\\Users\\private-user\\secret.txt /home/private-user/key.json')
    cause = RuntimeError(unsafe)
    error = d.DistillationError(unsafe, category='provider', validation_errors=[unsafe])
    error.__cause__ = cause
    recorder = d.TraceRecorder(cfg, 'source')
    call = recorder.start_call('inventory')
    recorder.record_attempt(call, 0, stage='inventory', error=error, category='provider')
    result = recorder.failure(error)
    dumped = json.dumps(result) + ''.join(p.read_text(encoding='utf-8') for p in tmp_path.rglob('*.json'))
    for secret in ['CONFIG_SECRET_VALUE', 'PROFILE_SECRET_VALUE', 'ENV_SECRET_VALUE',
                   'auth-secret', 'header-secret', 'sk-ant-oauth-not-in-config',
                   'user:password', 'query-secret', '10.1.2.3',
                   'private-user', 'secret.txt', 'key.json']:
        assert secret not in dumped
    assert result['cause_chain'][0]['error_type'] == 'RuntimeError'
    assert '[REDACTED' in result['error_message']


def test_missing_provider_fields_remain_null_with_reasons(tmp_path):
    d = diagnostics()
    recorder = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source')
    call = recorder.start_call('inventory')
    recorder.record_attempt(call, 0, stage='inventory', error=TimeoutError('timed out'), category='provider')
    result = recorder.failure(d.DistillationError('timed out', category='provider'))
    for field in ('finish_reason', 'usage', 'returned_model', 'cause_chain'):
        assert result[field] is None
        assert result['unavailable_reason'][field]
    assert result['unavailable_reason']['response_content'] == 'not_recorded_by_policy'
    assert result['category'] == 'provider'


def test_new_call_never_borrows_metadata_from_previous_call(tmp_path):
    d = diagnostics()
    recorder = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source')
    first = recorder.start_call('inventory')
    recorder.record_attempt(first, 0, stage='inventory', response=completion())
    second = recorder.start_call('synthesis')
    recorder.record_attempt(second, 0, stage='synthesis', error=TimeoutError('timed out'), category='provider')
    result = recorder.failure(d.DistillationError('failed', category='provider'))
    assert result['call_id'] == second
    assert result['finish_reason'] is None
    assert result['usage'] is None


def test_dict_provider_shape_and_usage_extra_fields_are_safe(tmp_path):
    d = diagnostics()
    recorder = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source')
    call = recorder.start_call('inventory')
    recorder.record_attempt(call, 0, stage='inventory', response={
        'choices': [{'finish_reason': 'length', 'message': {'content': 'SECRET CONTENT'}}],
        'model': 'dict/model',
        'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5,
                  'prompt_tokens_details': {'cached_tokens': 1, 'api_key': 'SECRET DETAIL'}}})
    result = recorder.failure(d.DistillationError('incomplete', category='response'))
    assert result['finish_reason'] == 'length'
    assert result['usage']['prompt_tokens_details'] == {'cached_tokens': 1}
    assert 'SECRET' not in json.dumps(result)


def test_persistence_failure_preserves_original_error_without_leaking_path(tmp_path):
    d = diagnostics()
    blocked = tmp_path / 'private-machine-file'
    blocked.write_text('already a file', encoding='utf-8')
    recorder = d.TraceRecorder({'diagnostics_dir': blocked}, 'source')
    error = d.DistillationError('missing u2', stage='contract', category='contract')
    result = recorder.failure(error)
    assert error.diagnostic == result
    assert result['category'] == 'contract'
    assert result['stage'] == 'contract'
    assert result['error_message'] == 'missing u2'
    assert result['artifact_ref'] is None
    assert result['artifact_sha256'] is None
    assert result['unavailable_reason']['artifact'] == 'persistence_failed:FileExistsError'
    assert 'private-machine-file' not in json.dumps(result)


def test_default_root_uses_current_cache_directory(tmp_path, monkeypatch):
    d = diagnostics()
    from skillmind import config
    monkeypatch.setattr(config, 'CACHE_DIR', tmp_path)
    result = d.TraceRecorder({}, 'source').failure(ValueError('bad'))
    assert (tmp_path / 'diagnostics' / result['artifact_ref']).is_file()


def test_failure_before_call_has_no_invented_attempt(tmp_path):
    d = diagnostics()
    result = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source').failure(ValueError('bad'))
    assert result['attempt'] is None
    assert result['call_id'] is None
    assert result['category'] is None
    assert result['stage'] is None
    for field in ('attempt', 'call_id', 'category', 'stage'):
        assert result['unavailable_reason'][field]


def test_run_and_source_ids_cannot_escape_artifact_root(tmp_path):
    d = diagnostics()
    with d.diagnostic_run('../../outside'):
        result = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'C:\\Users\\private\\file').failure(ValueError('bad'))
    artifact = (tmp_path / result['artifact_ref']).resolve()
    assert artifact.is_relative_to(tmp_path.resolve())
    assert 'private' not in json.dumps(result)


def test_diagnostics_public_module_exists():
    assert importlib.util.find_spec('skillmind.diagnostics') is not None


def test_structured_validation_errors_keep_machine_readable_ids_and_paths(tmp_path):
    d = diagnostics()
    details = [{'code': 'missing_unit', 'path': '$.uncovered', 'unit_ids': ['u2'],
                'span_ids': ['p3'], 'message': 'Missing u2; sk-private-token'}]
    error = d.DistillationError('bad', category='contract', validation_errors=details)
    result = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source').failure(error)
    stored = json.loads((tmp_path / result['artifact_ref']).read_text(encoding='utf-8'))
    assert stored['validation_errors'][0]['unit_ids'] == ['u2']
    assert stored['validation_errors'][0]['span_ids'] == ['p3']
    assert stored['validation_errors'][0]['path'] == '$.uncovered'
    assert 'sk-private-token' not in json.dumps(stored)
    assert details[0]['message'] == 'Missing u2; sk-private-token'


def test_error_only_retry_does_not_reuse_previous_attempt_response(tmp_path):
    d = diagnostics()
    recorder = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source')
    call = recorder.start_call('inventory')
    recorder.record_attempt(call, 0, stage='inventory', response=completion())
    result = recorder.record_attempt(call, 1, stage='inventory', error=TimeoutError('timeout'), category='provider')
    assert result['finish_reason'] is None
    assert result['usage'] is None
    final = recorder.failure(d.DistillationError('timeout', category='provider'))
    assert final['finish_reason'] is None
    assert final['usage'] is None


def test_same_attempt_parse_error_retains_response_metadata(tmp_path):
    d = diagnostics()
    recorder = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source')
    call = recorder.start_call('inventory')
    recorder.record_attempt(call, 0, stage='inventory', response=completion())
    result = recorder.record_attempt(call, 0, stage='inventory', error=ValueError('invalid json'), category='parse')
    assert result['finish_reason'] == 'stop'
    assert result['usage']['total_tokens'] == 18


def test_private_ipv6_and_generic_token_assignments_are_sanitized(tmp_path):
    d = diagnostics()
    error = ValueError('fd00::1234 token=unconfigured-secret https://example.com/#access_token=fragment-secret')
    result = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source').failure(error)
    assert 'fd00::1234' not in json.dumps(result)
    assert 'unconfigured-secret' not in json.dumps(result)
    assert 'fragment-secret' not in json.dumps(result)


def test_machine_paths_outside_common_home_directories_are_sanitized(tmp_path):
    d = diagnostics()
    error = ValueError('read /custom-machine/private-project/data.json or ~/private-project/key')
    result = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source').failure(error)
    assert 'private-project' not in json.dumps(result)


def test_error_urls_do_not_reveal_unpublished_hosts(tmp_path):
    d = diagnostics()
    result = d.TraceRecorder({'diagnostics_dir': tmp_path}, 'source').failure(
        RuntimeError('request failed at https://internal.company.example/api'))
    assert 'internal.company.example' not in json.dumps(result)


@pytest.mark.parametrize('path', [r'C:\Example Folder\secret-project\file.json','/example folder/secret-project/file.json'])
def test_quoted_machine_paths_with_spaces_are_fully_redacted(tmp_path,path):
    d=diagnostics()
    result=d.TraceRecorder({'diagnostics_dir':tmp_path},'source').failure(ValueError("Could not read '"+path+"'"))
    assert 'secret-project' not in json.dumps(result)
    artifact=json.loads((tmp_path/result['artifact_ref']).read_text(encoding='utf-8'))
    assert 'secret-project' not in json.dumps(artifact)


def test_resolved_request_model_and_endpoint_replace_configured_defaults(tmp_path):
    d=diagnostics()
    trace=d.TraceRecorder({'diagnostics_dir':tmp_path,'llm':{'model':'configured/default'}},'source')
    trace.protect_credentials({'model':'resolved/model','api_base':'https://private.example/api','api_key':'opaque-secret'})
    result=trace.failure(ValueError('request failed'))
    assert result['model']=='resolved/model'
    assert result['parameters']['endpoint_hash']==hashlib.sha256(b'https://private.example/api').hexdigest()
    assert 'private.example' not in json.dumps(result)
