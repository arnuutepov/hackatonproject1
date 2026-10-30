from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from support_copilot.schemas import TriageRequest, TriageResponse
from support_copilot.service import triage

app = FastAPI(title="Oguzki Support Copilot", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Do not echo ticket contents or accidental secrets in validation responses.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()
    ]})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "wave_0_stub"}


@app.post("/triage", response_model=TriageResponse)
def triage_ticket(payload: TriageRequest) -> TriageResponse:
    return triage(payload)
