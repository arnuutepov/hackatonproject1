"""Minimal jury-facing API: loopback service bearer token, no operator routes."""
from contextlib import asynccontextmanager
import hmac
import hashlib
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from support_copilot.knowledge import DATA, get_knowledge
from support_copilot.schemas import TriageRequest, TriageResponse
from support_copilot.service import triage


@asynccontextmanager
async def lifespan(app):
    token = os.environ.get('COPILOT_TRIAGE_TOKEN', '')
    if len(token) < 32 or not token.isascii():
        raise RuntimeError('COPILOT_TRIAGE_TOKEN должен содержать не менее 32 ASCII символов. '
                           'Создайте токен: python -c "import secrets; print(secrets.token_urlsafe(32))"')
    for name in ('rules.json', 'taxonomy.json'):
        path = DATA / name
        if not path.is_file():
            raise RuntimeError(f'Не найден {path}. Распакуйте handout в data/handout-2026-10-27/.')
    get_knowledge()
    yield


app = FastAPI(title='Oguzki Triage API', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse({'detail': [{'loc': list(error['loc']), 'type': error['type']}
                                    for error in exc.errors()]}, status_code=422)


@app.middleware('http')
async def authenticate(request: Request, call_next):
    token = os.environ.get('COPILOT_TRIAGE_TOKEN', '')
    supplied = request.headers.get('authorization', '')
    if len(token) < 32 or not token.isascii() or not supplied.isascii() or not hmac.compare_digest(supplied, 'Bearer ' + token):
        return JSONResponse({'detail': 'Invalid service token'}, status_code=401)
    return await call_next(request)


@app.get('/health')
def health():
    knowledge = get_knowledge()
    return {'status': 'ok', 'taxonomy_version': knowledge.taxonomy['version'], **knowledge.hashes}


@app.post('/triage', response_model=TriageResponse)
def triage_request(payload: TriageRequest):
    return triage(payload)


def main():
    token = os.environ.get('COPILOT_TRIAGE_TOKEN', '')
    if len(token) < 32 or not token.isascii():
        print('Укажите COPILOT_TRIAGE_TOKEN (не менее 32 ASCII символов). '
              'Создайте токен: python -c "import secrets; print(secrets.token_urlsafe(32))"', file=sys.stderr)
        return 2
    for name in ('rules.json', 'taxonomy.json'):
        path = DATA / name
        if not path.is_file():
            print(f'Не найден {path}. Распакуйте handout в data/handout-2026-10-27/.', file=sys.stderr)
            return 2
    manifest = Path(__file__).resolve().parent.parent / 'evaluation' / 'frozen-inputs.json'
    try:
        frozen = {Path(item['path']).name: item['sha256'] for item in json.loads(manifest.read_text(encoding='utf-8-sig'))}
        for name in ('rules.json', 'taxonomy.json'):
            if name in frozen and hashlib.sha256((DATA / name).read_bytes()).hexdigest() != frozen[name]:
                print(f'Предупреждение: SHA-256 {name} отличается от зафиксированного; проверьте источник.', file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print('Предупреждение: не удалось проверить манифест SHA-256.', file=sys.stderr)
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000, proxy_headers=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
