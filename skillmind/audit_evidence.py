"""Authenticate audit witnesses; semantic truth remains the auditor's judgment."""
from __future__ import annotations

import re
import json

import yaml


def source_identity(draft: dict) -> dict:
    text = '\n\n'.join(s['text'] for s in draft['distillation']['source_spans'])
    match = re.match(r'\A---\s*\n(.*?)\n---(?:\s*\n|$)', text, re.S)
    try:
        metadata = yaml.safe_load(match.group(1)) if match else {}
    except yaml.YAMLError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    name = metadata.get('name')
    name = name if isinstance(name, str) else None
    retained = []
    for key in ('name', 'intent', 'trigger_keywords'):
        value = draft.get('meta', {}).get(key)
        values = value if isinstance(value, list) else [value]
        if name and any(isinstance(v, str) and name in v for v in values):
            retained.append('meta.' + key)
    # Raw spans remain authoritative. These fields do not imply preservation.
    # YAML scalars may include dates; normalize only this informational projection.
    return {'name': name, 'retained_in': retained,
            'other_frontmatter': json.loads(json.dumps({str(k): v for k, v in metadata.items() if k != 'name'}, default=str))}


def valid_counterexample(value, spans: dict[str, str], outputs: list[str], *,
                         allowed_spans: set[str] | None = None,
                         allow_empty_output: bool = False) -> bool:
    """Closed evidence shape, existing IDs, exact excerpts in their own language."""
    if not isinstance(value, dict) or set(value) != {
        'source_span_ids', 'source_quote', 'output_quote', 'explanation'
    }:
        return False
    ids = value['source_span_ids']
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in spans for i in ids):
        return False
    if len(set(ids)) != len(ids) or (allowed_spans is not None and not set(ids) <= allowed_spans):
        return False
    quote = value['source_quote']
    if not isinstance(quote, str) or not quote.strip() or not any(quote in spans[i] for i in ids):
        return False
    explanation = value['explanation']
    if not isinstance(explanation, str) or not explanation.strip():
        return False
    output = value['output_quote']
    return isinstance(output, str) and (
        (output == '' and allow_empty_output) or
        (bool(output.strip()) and any(output in candidate for candidate in outputs))
    )
