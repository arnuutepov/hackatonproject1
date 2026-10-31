"""Gemini plans: bounded transport, token-count budget preflight, no automatic retries."""
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from support_copilot.costing import estimate_cost, provider_prices
from support_copilot.draft_provider import DraftFailure, DraftPlan, SYSTEM, draft_timeout
from support_copilot.privacy import redact_tree

ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/models/'
MAX_REQUEST_BYTES = 24000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured_key():
    location = os.environ.get('GEMINI_API_KEY_FILE')
    try:
        if location:
            with Path(location).open('r', encoding='utf-8-sig') as stream:
                key = stream.read(4097).strip()
        else:
            key = os.environ.get('GEMINI_API_KEY', '').strip()
    except (OSError, UnicodeError, ValueError):
        raise DraftFailure('draft_not_configured') from None
    if not key or len(key) > 4096 or not key.isascii() or any(c.isspace() for c in key):
        raise DraftFailure('draft_not_configured')
    return key


class GeminiProvider:
    def __init__(self, api_key, model, timeout, budget_usd=0.002):
        if not api_key or not re.fullmatch(r'gemini-[a-z0-9.-]{1,80}', model):
            raise DraftFailure('draft_not_configured')
        if not math.isfinite(timeout) or not 0 < timeout <= 10 or not math.isfinite(budget_usd) or not 0 < budget_usd <= 0.002:
            raise DraftFailure('draft_invalid_configuration')
        self.api_key, self.model, self.timeout, self.budget_usd = api_key, model, timeout, budget_usd
        self.last_usage = self.response_model = self.preflight_cost_usd = None
        self.generation_started = False

    def generate(self, context):
        return self.complete(SYSTEM, [{'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}],
                             DraftPlan.model_json_schema())

    def _post(self, operation, payload, deadline, limit=65536):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DraftFailure('draft_provider_timeout')
        request = Request(ENDPOINT + self.model + ':' + operation,
                          data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), method='POST',
                          headers={'Content-Type': 'application/json', 'x-goog-api-key': self.api_key})
        try:
            with build_opener(NoRedirect()).open(request, timeout=remaining) as response:
                raw = response.read(limit + 1)
            if len(raw) > limit:
                raise DraftFailure('draft_provider_oversized')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError('envelope')
            return result
        except HTTPError as exc:
            code = {401: 'draft_provider_authentication', 403: 'draft_provider_authentication',
                    429: 'draft_provider_rate_limited', 404: 'draft_provider_model_unavailable'}.get(exc.code)
            raise DraftFailure(code or 'draft_provider_unavailable') from None
        except TimeoutError:
            raise DraftFailure('draft_provider_timeout') from None
        except URLError as exc:
            code = 'draft_provider_timeout' if isinstance(exc.reason, TimeoutError) else 'draft_provider_unavailable'
            raise DraftFailure(code) from None
        except OSError:
            raise DraftFailure('draft_provider_unavailable') from None
        except (ValueError, TypeError, UnicodeError):
            raise DraftFailure('draft_provider_invalid_envelope') from None

    def complete(self, system, messages, schema, max_tokens=900):
        self.last_usage = self.response_model = self.preflight_cost_usd = None
        self.generation_started = False
        if type(max_tokens) is not int or not 1 <= max_tokens <= 900:
            raise DraftFailure('draft_invalid_configuration')
        config = {'maxOutputTokens': max_tokens, 'candidateCount': 1,
                  'responseMimeType': 'application/json', 'responseJsonSchema': schema}
        if self.model == 'gemini-2.5-flash-lite':
            config['thinkingConfig'] = {'thinkingBudget': 0}
        payload = {'systemInstruction': {'parts': [{'text': system}]},
                   'contents': [{'role': 'user', 'parts': [{'text': json.dumps(redact_tree(messages), ensure_ascii=False)}]}],
                   'generationConfig': config}
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > MAX_REQUEST_BYTES:
            raise DraftFailure('draft_provider_oversized')
        prices = provider_prices(self.model)
        if estimate_cost(dict(input_tokens=1, output_tokens=max_tokens, cached_tokens=0), self.model, prices) is None:
            raise DraftFailure('draft_budget_unverified')
        deadline = time.monotonic() + self.timeout
        # Full request includes schema and system instructions, not just the player's text.
        count = self._post('countTokens', {'generateContentRequest': {'model': 'models/' + self.model, **payload}}, deadline, 8192)
        tokens = count.get('totalTokens')
        if type(tokens) is not int or tokens <= 0:
            raise DraftFailure('draft_token_count_unavailable')
        # Allow for count/generation framing differences; no tools, retries or extra candidates.
        upper = estimate_cost(dict(input_tokens=tokens + 512, output_tokens=max_tokens, cached_tokens=0), self.model, prices)
        self.preflight_cost_usd = upper
        if upper is None or upper > self.budget_usd:
            raise DraftFailure('draft_budget_exceeded')
        if time.monotonic() >= deadline:
            raise DraftFailure('draft_provider_timeout')
        self.generation_started = True
        value = self._post('generateContent', payload, deadline)
        try:
            usage = value.get('usageMetadata', {})
            inp, out, thinking = usage.get('promptTokenCount'), usage.get('candidatesTokenCount'), usage.get('thoughtsTokenCount', 0)
            if all(type(x) is int and x >= 0 for x in (inp, out, thinking)):
                # Cache discounts are deliberately not assumed; charge full input for the estimate.
                self.last_usage = {'input_tokens': inp, 'output_tokens': out + thinking, 'cached_tokens': 0}
            self.response_model = self.model
            candidates = value['candidates']
            if not isinstance(candidates, list) or len(candidates) != 1:
                raise ValueError('candidate count')
            candidate = candidates[0]
            if candidate.get('finishReason') != 'STOP':
                raise DraftFailure('draft_provider_incomplete')
            texts = [p['text'] for p in candidate['content']['parts'] if isinstance(p.get('text'), str) and not p.get('thought')]
            if len(texts) != 1 or not texts[0].strip():
                raise ValueError('text')
            return texts[0]
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise DraftFailure('draft_provider_invalid_envelope') from None


def configured_gemini():
    try:
        return GeminiProvider(configured_key(), os.environ.get('COPILOT_GEMINI_MODEL', 'gemini-2.5-flash-lite'),
                              draft_timeout(), float(os.environ.get('COPILOT_MAX_REQUEST_USD', '0.002')))
    except ValueError:
        raise DraftFailure('draft_invalid_configuration') from None
