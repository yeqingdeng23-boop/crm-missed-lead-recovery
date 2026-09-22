import hmac
import os
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from .engine import Conflict, Engine
from .llm import Ollama
from .workflow import TITLE, Intake, Processor


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0)
    reviewer: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,50}$")
    approve: bool


def create_app(db_path=None, model=None):
    keys = {k: os.getenv(k, "") for k in ("WEBHOOK_KEY", "OPERATOR_KEY", "APPROVAL_KEY")}
    if any(len(k) < 20 for k in keys.values()) or len(set(keys.values())) != 3:
        raise RuntimeError("Set three distinct WEBHOOK_KEY, OPERATOR_KEY, APPROVAL_KEY values (20+ chars).")
    store = Engine(db_path or os.getenv("DB_PATH", "var/workflow.sqlite3"), lambda _: None)
    store.processor = Processor(model or Ollama(), store)
    app = FastAPI(title=TITLE, description="SELF-BUILT TECHNICAL PROOF — NOT A CLIENT CASE STUDY")
    app.state.store = store

    def key_check(name):
        def check(x_api_key: str = Header(default="")):
            if not hmac.compare_digest(x_api_key, keys[name]):
                raise HTTPException(401, "invalid_key")
        return check
    operator, approver, intake_auth = (key_check(k) for k in ("OPERATOR_KEY", "APPROVAL_KEY", "WEBHOOK_KEY"))

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(KeyError)
    async def missing_handler(request, exc):
        return JSONResponse(status_code=404, content={"error": "not_found"})

    @app.middleware("http")
    async def size_limit(request: Request, call_next):
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 512_000:
                    return JSONResponse(status_code=413, content={"error": "payload_too_large"})
            request._body = bytes(body)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"ok": True, "project": TITLE, "sink": "local synthetic SQLite", "auto_send": False}

    @app.post("/webhooks/intake", dependencies=[Depends(intake_auth)], status_code=202)
    def intake(payload: Intake, idempotency_key: str = Header(min_length=1, max_length=100)):
        job_id, created = store.enqueue(idempotency_key, payload.model_dump(mode="json"))
        return {"job_id": job_id, "created": created, "state": store.get(job_id)["state"]}

    @app.get("/jobs/{job_id}", dependencies=[Depends(operator)])
    def get_job(job_id: str):
        return store.get(job_id)

    @app.post("/jobs/{job_id}/run", dependencies=[Depends(operator)])
    def run_job(job_id: str):
        return store.run(job_id)

    @app.get("/jobs/{job_id}/events", dependencies=[Depends(operator)])
    def events(job_id: str):
        return store.events(job_id)

    @app.post("/jobs/{job_id}/decision", dependencies=[Depends(approver)])
    def decide(job_id: str, decision: Decision):
        return store.approve(job_id, decision.version, decision.reviewer, decision.approve)

    @app.get("/records/{record_key}", dependencies=[Depends(operator)])
    def record(record_key: str):
        result = store.record(record_key)
        if result is None:
            raise HTTPException(404, "not_found")
        return result

    @app.post("/records/{record_key}/human-contact", dependencies=[Depends(approver)])
    def mark_contact(record_key: str, decision: Decision):
        if not decision.approve:
            raise HTTPException(422, "contact_confirmation_required")
        return store.human_contact(record_key, decision.version, decision.reviewer)

    return app
