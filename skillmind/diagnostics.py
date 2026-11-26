"""Sanitized, append-only extraction traces; never serialize provider objects.

The closed ``extraction_trace_v1`` event consists of identity, stage/error,
configured request metadata and provider finish/usage metadata. Response bodies,
request headers and stack traces are deliberately unavailable. Artifact digests
belong to the returned envelope, since an event cannot include its own hash.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit
import uuid


TRACE_SCHEMA = 'extraction_trace_v1'
_CATEGORIES = frozenset(('provider', 'response', 'parse', 'contract'))
_PARAMETERS = ('batch_chars', 'max_tokens', 'timeout', 'max_retries',
               'temperature', 'qpm', 'auth_mode', 'endpoint_hash', 'template_hash')
_TOKEN_COUNTS = ('prompt_tokens', 'completion_tokens', 'total_tokens',
                 'input_tokens', 'output_tokens')
_TOKEN_DETAILS = ('cached_tokens', 'audio_tokens', 'reasoning_tokens',
                  'accepted_prediction_tokens', 'rejected_prediction_tokens',
                  'text_tokens', 'image_tokens')
_SECRET_NAME = re.compile(r'api.?key|token|secret|password|credential|authorization', re.I)
_URL = re.compile(r'https?://[^\s<>"\']+', re.I)
_RUN_ID: ContextVar[str | None] = ContextVar('skillmind_diagnostic_run', default=None)
_TRACE: ContextVar[TraceRecorder | None] = ContextVar('skillmind_trace', default=None)


class DistillationError(RuntimeError):
    """An extraction failure with explicit classification and a safe trace link."""

    def __init__(self, message, *, stage=None, category=None, validation_errors=None):
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.validation_errors = list(validation_errors) if validation_errors is not None else None
        self.diagnostic = None


@contextmanager
def diagnostic_run(run_id):
    """Share an outer orchestration identity without replacing an existing run."""
    token = _RUN_ID.set(_RUN_ID.get() or str(run_id))
    try:
        yield _RUN_ID.get()
    finally:
        _RUN_ID.reset(token)


def current_trace():
    return _TRACE.get()


def _get(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    try:
        return getattr(value, key, default)
    except Exception:
        return default


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _configured_secrets(cfg):
    """Copy configured secrets, including explicitly named credential variables."""
    result = set()

    def walk(value, secret=False):
        if isinstance(value, dict):
            for key, item in value.items():
                name = str(key)
                if name.endswith('_env') and _SECRET_NAME.search(name) and isinstance(item, str):
                    resolved = os.environ.get(item)
                    if resolved:
                        result.add(resolved)
                    continue
                walk(item, secret or bool(_SECRET_NAME.search(name)))
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, secret)
        elif secret and isinstance(value, str) and value:
            result.add(value)

    walk(cfg)
    return sorted(result, key=len, reverse=True)


class _Sanitizer:
    def __init__(self, cfg):
        self.secrets = _configured_secrets(cfg)

    def text(self, value):
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, '[REDACTED]')

        def safe_url(match):
            url = match.group()
            try:
                parsed = urlsplit(url)
                if parsed.username is not None or parsed.password is not None:
                    return '[REDACTED_URL]'
                if any(_SECRET_NAME.search(key) or key.lower() in ('key', 'auth', 'signature', 'sig')
                       for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
                       + parse_qsl(parsed.fragment, keep_blank_values=True)):
                    return '[REDACTED_URL]'
                try:
                    address = ipaddress.ip_address(parsed.hostname or '')
                except ValueError:
                    address = None
                if address is not None and not address.is_global:
                    return '[REDACTED_URL]'
                if parsed.hostname in ('localhost',) or (parsed.hostname or '').endswith('.local'):
                    return '[REDACTED_URL]'
            except ValueError:
                return '[REDACTED_URL]'
            # Exception URLs are not needed to reproduce extraction; the
            # configured endpoint fingerprint is already in request metadata.
            return '[REDACTED_URL]'

        text = _URL.sub(safe_url, text)
        text = re.sub(r"""(['"])(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/])[^'"\r\n]*\1""",
                      '[REDACTED_PATH]', text)
        text = re.sub(r'(?i)\b(?:authorization|proxy-authorization)\s*[\"\']?\s*[:=]\s*[\"\']?\s*(?:bearer\s+|basic\s+)?[^\s,;\"\'\}\]]+',
                      '[REDACTED_AUTH]', text)
        text = re.sub(r'(?i)\bbearer\s+[^\s,;\"\'\}\]]+', '[REDACTED_AUTH]', text)
        text = re.sub(r'(?i)\b(?:x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|client[_-]?secret)\s*[\"\']?\s*[:=]\s*[\"\']?[^\s,;\"\'\}\]]+',
                      '[REDACTED_SECRET]', text)
        text = re.sub(r'\b(?:sk-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+|xox[baprs]-[\w-]+|AIza[\w-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)',
                      '[REDACTED_TOKEN]', text)
        text = re.sub(r'(?i)(?<!\w)[a-z]:[\\/][^\s<>\"\'\)\]\}]+', '[REDACTED_PATH]', text)
        text = re.sub(r'\\\\[^\s<>\"\'\)\]\}]+', '[REDACTED_PATH]', text)
        text = re.sub(r'(?<![\w:/])(?:~[\\/]|/)[^\s<>\"\'\)\]\}]+',
                      '[REDACTED_PATH]', text)

        def safe_ip(match):
            try:
                address = ipaddress.ip_address(match.group())
                return match.group() if address.is_global else '[REDACTED_IP]'
            except ValueError:
                return match.group()

        text = re.sub(r'(?<![\w.])(?:\d+\.){3}\d+(?![\w.])', safe_ip, text)
        return re.sub(r'(?<![\w:])[0-9A-Fa-f]*(?::[0-9A-Fa-f]*){2,}(?:%[\w-]+)?(?![\w:])', safe_ip, text)

    def scalar(self, value):
        if value is None or type(value) is bool or _number(value):
            return value
        if isinstance(value, str):
            return self.text(value)
        return None

    def json_value(self, value, seen=None):
        """Retain structured validation evidence without object repr or cycles."""
        seen = set() if seen is None else seen
        if isinstance(value, (dict, list, tuple)):
            if id(value) in seen:
                return None
            seen.add(id(value))
            try:
                if isinstance(value, dict):
                    return {self.text(key): ('[REDACTED]' if _SECRET_NAME.search(str(key))
                                             else self.json_value(item, seen))
                            for key, item in value.items() if isinstance(key, str)}
                return [self.json_value(item, seen) for item in value]
            finally:
                seen.remove(id(value))
        return self.scalar(value)

    def identity(self, value):
        raw = str(value)
        safe = self.text(raw)
        if safe == raw and re.fullmatch(r'[A-Za-z0-9_-]+', raw):
            return raw
        # The digest is an opaque identity, never a user-controlled path segment.
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _cause_chain(exc, sanitizer):
    result = []
    seen = {id(exc)}
    current = exc
    while True:
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        result.append({'error_type': sanitizer.text(type(cause).__name__),
                       'error_message': sanitizer.text(cause)})
        current = cause
    return result or None


class TraceRecorder:
    def __init__(self, cfg, source_hash):
        from skillmind import config, distillation

        self._sanitizer = _Sanitizer(cfg)
        self.root = Path(cfg.get('diagnostics_dir') or config.CACHE_DIR / 'diagnostics')
        self.run_id = self._sanitizer.identity(_RUN_ID.get() or uuid.uuid4().hex)
        self.source_hash = self._sanitizer.identity(source_hash)
        self._calls = {}
        self._last_call = None
        self._metadata_unavailable = None
        try:
            settings = distillation._settings(cfg)
        except Exception as exc:
            # A bad configuration is itself diagnosable; do not replace its error.
            settings = {}
            self._metadata_unavailable = 'settings_unavailable:' + type(exc).__name__
        self.model = self._sanitizer.scalar(settings.get('model'))
        self.prompt_version = self._sanitizer.scalar(settings.get('prompt', config.SKILL_PROMPT_VERSION))
        self.schema_version = self._sanitizer.scalar(settings.get('schema', distillation.SCHEMA_VERSION))
        self.parameters = {key: self._sanitizer.scalar(settings[key]) for key in _PARAMETERS if key in settings}
        unavailable = [key for key,value in self.parameters.items() if value is None]
        if unavailable:
            self._metadata_unavailable = 'non_json_parameter_values:' + ','.join(unavailable)

    @contextmanager
    def activate(self):
        token = _TRACE.set(self)
        try:
            yield self
        finally:
            _TRACE.reset(token)

    def start_call(self, stage):
        call_id = uuid.uuid4().hex
        self._calls[call_id] = {'stage': self._sanitizer.scalar(stage), 'attempt': None,
                                'finish_reason': None, 'usage': None, 'returned_model': None}
        self._last_call = call_id
        return call_id

    def protect_credentials(self, credentials):
        """Bind resolved env/subscription secrets without persisting them."""
        values = _configured_secrets(credentials)
        self._sanitizer.secrets = sorted(set(self._sanitizer.secrets).union(values), key=len, reverse=True)
        if isinstance(credentials.get('model'), str):
            self.model = self._sanitizer.text(credentials['model'])
        if isinstance(credentials.get('api_base'), str):
            self.parameters['endpoint_hash'] = hashlib.sha256(credentials['api_base'].encode('utf-8')).hexdigest()

    def _response_metadata(self, response):
        choices = _get(response, 'choices')
        choice = choices[0] if isinstance(choices, (list, tuple)) and choices else None
        usage = _get(response, 'usage')
        counts = {key: _get(usage, key) for key in _TOKEN_COUNTS if _number(_get(usage, key))}
        for key in ('prompt_tokens_details', 'completion_tokens_details',
                    'input_tokens_details', 'output_tokens_details'):
            details = _get(usage, key)
            safe = {name: _get(details, name) for name in _TOKEN_DETAILS if _number(_get(details, name))}
            if safe:
                counts[key] = safe
        return {'finish_reason': self._sanitizer.scalar(_get(choice, 'finish_reason')),
                'usage': counts or None,
                'returned_model': self._sanitizer.scalar(_get(response, 'model'))}

    def _event(self, event_type, call_id, *, stage=None, error=None, category=None):
        state = self._calls.get(call_id, {})
        stage = stage if stage is not None else _get(error, 'stage')
        if stage is None:
            stage = state.get('stage')
        category = category if category is not None else _get(error, 'category')
        category = category if isinstance(category, str) and category in _CATEGORIES else None
        validation = _get(error, 'validation_errors')
        validation = (self._sanitizer.json_value(validation)
                      if isinstance(validation, (list, tuple)) else None)
        event = {
            'trace_schema': TRACE_SCHEMA, 'event_type': event_type, 'event_id': uuid.uuid4().hex,
            'run_id': self.run_id, 'source_hash': self.source_hash,
            'call_id': call_id, 'attempt': state.get('attempt'),
            'stage': self._sanitizer.scalar(stage), 'category': category,
            'error_type': self._sanitizer.text(type(error).__name__) if error is not None else None,
            'error_message': self._sanitizer.text(error) if error is not None else None,
            'cause_chain': _cause_chain(error, self._sanitizer) if error is not None else None,
            'validation_errors': validation,
            'model': self.model, 'returned_model': state.get('returned_model'),
            'prompt_version': self.prompt_version, 'schema_version': self.schema_version,
            'parameters': dict(self.parameters),
            'finish_reason': state.get('finish_reason'), 'usage': state.get('usage'),
            'unavailable_reason': {'response_content': 'not_recorded_by_policy'},
        }
        for key, value in event.items():
            if value is None:
                event['unavailable_reason'][key] = 'not_observed' if error is not None else 'not_applicable_or_not_observed'
        if self._metadata_unavailable:
            event['unavailable_reason']['parameters'] = self._metadata_unavailable
        return event

    def _persist(self, event):
        envelope = dict(event)
        envelope['unavailable_reason'] = dict(event['unavailable_reason'])
        envelope['artifact_ref'] = None
        envelope['artifact_sha256'] = None
        # Full identities live in the immutable payload. A short, opaque path
        # also lets consumers read artifacts without Windows extended prefixes.
        run_directory = hashlib.sha256(self.run_id.encode('utf-8')).hexdigest()
        relative = Path(run_directory) / (event['event_id'] + '.json')
        try:
            path = self.root / relative
            if os.name == 'nt':
                # Full run/source/call identity can exceed legacy MAX_PATH even
                # for a normal cache directory. The extended prefix is local only.
                absolute = str(path.absolute())
                if not absolute.startswith('\\\\?\\'):
                    absolute = ('\\\\?\\UNC\\' + absolute[2:] if absolute.startswith('\\\\')
                                else '\\\\?\\' + absolute)
                path = Path(absolute)
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = (json.dumps(event, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
            # Exclusive creation makes accidental identity reuse fail safely.
            with path.open('xb') as stream:
                stream.write(raw)
            envelope['artifact_ref'] = relative.as_posix()
            envelope['artifact_sha256'] = hashlib.sha256(raw).hexdigest()
        except Exception as exc:
            # Never stringify filesystem errors: they commonly include private paths.
            envelope['unavailable_reason']['artifact'] = 'persistence_failed:' + type(exc).__name__
        return envelope

    def record_attempt(self, call_id, attempt, *, stage, response=None, error=None, category=None):
        if call_id not in self._calls:
            raise ValueError('unknown diagnostic call identity')
        state = self._calls[call_id]
        if state['attempt'] != attempt:
            # A response from a previous retry is evidence for that retry only.
            state.update({'finish_reason': None, 'usage': None, 'returned_model': None})
        state['attempt'] = attempt if type(attempt) is int else None
        state['stage'] = self._sanitizer.scalar(stage)
        if response is not None:
            state.update(self._response_metadata(response))
        self._last_call = call_id
        return self._persist(self._event('attempt', call_id, stage=stage, error=error, category=category))

    def failure(self, exc, stage=None):
        result = self._persist(self._event('failure', self._last_call, stage=stage, error=exc))
        try:
            exc.diagnostic = result
        except (AttributeError, TypeError):
            pass
        return result
