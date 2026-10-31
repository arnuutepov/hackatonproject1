"""Explicit opt-in third-party chat transport. Never log credentials or ticket bodies."""
import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from support_copilot.draft_provider import DraftFailure, DraftPlan, SYSTEM

ENDPOINT = 'https://api.capybara-api.xyz/v1/chat/completions'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CapybaraProvider:
    def __init__(self, *, api_key, model='gemini-3.8-flash', timeout=10):
        if not api_key or model not in {'gemini-3.8-flash', 'qwen-3.8-flash', 'deepseek-v4-flash'} or not 0 < timeout <= 10:
            raise DraftFailure('draft_not_configured')
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self.last_usage, self.response_model = None, None

    def complete(self, system, messages, schema, max_tokens=900):
        self.last_usage, self.response_model = None, None
        from support_copilot.privacy import redact_tree
        messages = redact_tree(messages)
        body = {'model': self.model, 'messages': [{'role': 'system', 'content': system}, *messages],
                'max_tokens': max_tokens, 'stream': False,
                'response_format': {'type': 'json_schema', 'json_schema': {'name': 'support_response',
                                    'strict': True, 'schema': schema}}}
        raw = json.dumps(body, ensure_ascii=False).encode()
        # Hard bound before a third-party request; no automatic retries or tools.
        if len(raw) > 24000:
            raise DraftFailure('draft_provider_oversized')
        request = Request(ENDPOINT, data=raw, method='POST', headers={
            'Content-Type': 'application/json', 'User-Agent': 'Oguzki-MVP/0.10', 'Authorization': 'Bearer ' + self.api_key})
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=self.timeout) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise DraftFailure('draft_provider_oversized')
            value = json.loads(raw)
            usage = value.get('usage', {})
            counts = [usage.get('prompt_tokens'), usage.get('completion_tokens')]
            if all(type(n) is int and n >= 0 for n in counts):
                self.last_usage = dict(input_tokens=counts[0], output_tokens=counts[1], cached_tokens=0)
            self.response_model = value.get('model') if isinstance(value.get('model'), str) else None
            choices = value['choices']
            if not isinstance(choices, list) or len(choices) != 1:
                raise DraftFailure('draft_provider_invalid_envelope')
            if choices[0].get('finish_reason') != 'stop':
                raise DraftFailure('draft_provider_incomplete')
            message = choices[0]['message']
            if message.get('refusal'):
                raise DraftFailure('draft_provider_refusal')
            text = message.get('content')
            if not isinstance(text, str) or not text.strip():
                raise DraftFailure('draft_provider_invalid_envelope')
            return text
        except DraftFailure:
            raise
        except TimeoutError:
            raise DraftFailure('draft_provider_timeout') from None
        except URLError as exc:
            code = 'draft_provider_timeout' if isinstance(exc.reason, TimeoutError) else 'draft_provider_unavailable'
            raise DraftFailure(code) from None
        except OSError:
            raise DraftFailure('draft_provider_unavailable') from None
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise DraftFailure('draft_provider_invalid_envelope') from None

    def generate(self, context):
        return self.complete(SYSTEM, [{'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}],
                             DraftPlan.model_json_schema())


def configured_capybara():
    key = os.environ.get('CAPYBARA_API_KEY', '')
    location = os.environ.get('CAPYBARA_API_KEY_FILE', '')
    if location:
        try:
            key = Path(location).read_text().strip()
        except OSError:
            raise DraftFailure('draft_not_configured') from None
    return CapybaraProvider(api_key=key, model=os.environ.get('COPILOT_CAPYBARA_MODEL', 'gemini-3.8-flash'))
