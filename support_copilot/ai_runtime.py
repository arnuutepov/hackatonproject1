"""Shared provider capabilities and an explicit, synthetic-only connectivity check."""
import os
from time import perf_counter

from support_copilot.draft_provider import DraftFailure, FAILURE_CODES, configured_provider


def provider_status():
    mode = os.environ.get('COPILOT_DRAFT_PROVIDER', 'template')
    result = dict(provider=mode, state='disabled', model=None, draft_configured=False,
                  assistant_configured=False, live_verified=False, error=None)
    if mode == 'template':
        return result
    try:
        provider = configured_provider()
        result.update(state='configured_unverified', model=provider.model, draft_configured=True,
                      assistant_configured=callable(getattr(provider, 'complete', None)))
    except DraftFailure as exc:
        result.update(state='configuration_error', error=str(exc) if str(exc) in FAILURE_CODES else 'draft_invalid_configuration')
    return result


def synthetic_check():
    """One user-triggered model plan, without stored tickets or operator dialogue."""
    from support_copilot.schemas import TriageRequest
    from support_copilot.service import triage
    status = provider_status()
    started = perf_counter()
    result = dict(provider=status['provider'], model=status['model'], ok=False,
                  synthetic_only=True, error=status['error'], usage=None, cost_usd=None)
    if not status['draft_configured']:
        result['error'] = status['error'] or 'draft_not_configured'
    else:
        request = TriageRequest(kind='report', messages=[{'text': 'Оплатил донат, баланс не изменился.'}])
        response = triage(request, draft_provider=configured_provider(), use_verified_templates=False)
        result.update(ok=response.draft_reply.provenance == 'validated_model_plan',
                      usage=response.generation.usage, cost_usd=response.generation.api_cost_usd)
        if not result['ok']:
            codes = [r for r in response.reasons if r in FAILURE_CODES]
            result['error'] = codes[-1] if codes else 'draft_provider_error'
    result['elapsed_ms'] = round((perf_counter() - started) * 1000, 2)
    return result


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Allow one synthetic model call; may incur cost')
    args = parser.parse_args()
    result = synthetic_check() if args.check else provider_status()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if (result.get('ok') if args.check else result['state'] != 'configuration_error') else 1


if __name__ == '__main__':
    raise SystemExit(main())
