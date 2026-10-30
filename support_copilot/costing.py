"""Explicit usage-based estimates. Missing usage/prices never means free inference."""
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path


def parse_usage(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    details = value.get('input_tokens_details')
    if not isinstance(details, dict):
        return None
    counts = {'input_tokens':value.get('input_tokens'), 'output_tokens':value.get('output_tokens'),
              'cached_tokens':details.get('cached_tokens'), 'cache_write_tokens':details.get('cache_write_tokens',0)}
    if not all(type(item) is int and item >= 0 for item in counts.values()):
        return None
    if counts['cached_tokens'] + counts['cache_write_tokens'] > counts['input_tokens']:
        return None
    return counts


def estimate_cost(usage: dict | None, model: str | None, prices: dict | None) -> float | None:
    if not usage or not prices or not model or prices.get('model') != model:
        return None
    if any(type(usage.get(key)) is not int or usage[key] < 0 for key in ('input_tokens','output_tokens','cached_tokens')):
        return None
    if usage['cached_tokens'] > usage['input_tokens']:
        return None
    # Cache-write billing has provider/model-specific semantics; do not guess it.
    if usage.get('cache_write_tokens',0):
        return None
    try:
        rates = [Decimal(str(prices[key])) for key in ('input_per_million','cached_input_per_million','output_per_million')]
        if not all(rate.is_finite() and rate >= 0 for rate in rates):
            return None
        if not prices.get('source_url') or not prices.get('checked_at'):
            return None
        value = ((usage['input_tokens']-usage['cached_tokens'])*rates[0] +
                 usage['cached_tokens']*rates[1] + usage['output_tokens']*rates[2])/Decimal(1_000_000)
        return float(value)
    except (KeyError,TypeError,ValueError,InvalidOperation):
        return None


def configured_prices() -> dict | None:
    location = os.environ.get('COPILOT_PRICING_FILE')
    if not location:
        return None
    try:
        value = json.loads(Path(location).read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else None
    except (OSError,ValueError):
        return None
