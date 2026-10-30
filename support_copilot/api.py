from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from support_copilot.schemas import TriageRequest, TriageResponse
from support_copilot.service import triage
from support_copilot.knowledge import get_knowledge
from support_copilot.operator import install_handlers, local_access, router as operator_router

app = FastAPI(title="Oguzki Support Copilot", version="0.6.0")
STATIC = Path(__file__).with_name('static')
app.include_router(operator_router)
install_handlers(app)


@app.middleware('http')
async def workspace_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == '/' or request.url.path.startswith(('/operator/', '/assets/')):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    return response


@app.get('/', include_in_schema=False, dependencies=[Depends(local_access)])
def workspace_page():
    return FileResponse(STATIC / 'index.html')


app.mount('/assets', StaticFiles(directory=STATIC), name='operator_assets')


@app.exception_handler(OSError)
@app.exception_handler(ValueError)
async def unavailable(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=503, content={'detail': 'Knowledge source unavailable or invalid'})


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Do not echo ticket contents or accidental secrets in validation responses.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()
    ]})


@app.get("/health")
def health() -> dict[str, str]:
    knowledge = get_knowledge()
    return {"status": "ok", "mode": "wave_5_measured_local",
            "taxonomy_version": knowledge.taxonomy['version'], **knowledge.hashes}


@app.post("/triage", response_model=TriageResponse)
def triage_ticket(payload: TriageRequest) -> TriageResponse:
    return triage(payload)
