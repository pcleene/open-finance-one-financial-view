"""Acme One Financial View — DC service layer (brief §5, §11 backends).

Composition root only (brief §6 SOLID refactor): lifespan, middleware, and the
versioned router. One process serves One View (Path A single read), consent
centre + the Open Finance platform link flow, the webhook receiver, PFM, transaction search,
underwriting, and scale-ops — each a thin controller in api/v1/routes/*
delegating to a service in services/* (DIP).

Run:  uv run uvicorn acme_ofv.api.app:app --port 8010
"""

import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from acme_ofv.api.v1.router import api_router
from acme_ofv.config import settings
from acme_ofv.db import make_async_client, ofv_db
from acme_ofv.query_log import get_log, reset_log


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = make_async_client(max_pool=100)
    app.state.db = ofv_db(app.state.client)
    app.state.cc_token = None       # cached OFP client-credentials token

    # consent events over MSK (brief §7): ensure the topic + start the producer
    # so link/revoke/suspend/reactivate/webhook + the storm/reauth sims all
    # publish to rcp.consent.events. direct mode constructs nothing.
    if settings().consent_event_transport == "kafka":
        from acme_ofv.streaming.msk_client import get_consent_producer
        from acme_ofv.streaming.topic_admin import ensure_consent_topic
        try:
            await ensure_consent_topic()
        except Exception as exc:  # topic may already exist / admin perms — non-fatal
            print(f"[lifespan] ensure_consent_topic: {exc}", flush=True)
        await get_consent_producer().start()

    yield

    if settings().consent_event_transport == "kafka":
        from acme_ofv.streaming.msk_client import get_consent_producer
        await get_consent_producer().stop()
    await app.state.client.close()


class QueryInspectorMiddleware:
    """Pure-ASGI middleware (NOT BaseHTTPMiddleware): runs the app in the same
    task/context, so the per-request query log (a contextvar) set here is
    visible to log_query in the endpoint AND readable back here. Buffers JSON
    responses to inject the captured MongoDB operations as `_queries`; streams
    (SSE) and non-JSON responses pass straight through."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        reset_log()
        state: dict = {"start": None, "json": False, "chunks": []}

        async def send_wrapper(message):
            mtype = message["type"]
            if mtype == "http.response.start":
                ct = b""
                for k, v in message.get("headers", []):
                    if k.lower() == b"content-type":
                        ct = v
                state["json"] = b"application/json" in ct
                if state["json"]:
                    state["start"] = message       # defer until body assembled
                else:
                    await send(message)
            elif mtype == "http.response.body" and state["json"]:
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(state["chunks"])
                queries = get_log()
                if queries:
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict):
                            data["_queries"] = queries
                            body = json.dumps(data).encode()
                    except Exception:
                        pass
                start = state["start"]
                headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                start["headers"] = headers
                await send(start)
                await send({"type": "http.response.body", "body": body, "more_body": False})
            else:
                await send(message)

        await self.app(scope, receive, send_wrapper)


def require_api_key(x_api_key: str | None = Header(default=None)):
    """Gate every DC API route. Fail-closed: when `auth_required` is set (any
    deployed/public env) a missing server-side `api_key` is a misconfiguration
    and gated requests are rejected (503) rather than served open. When
    `auth_required` is off and no key is configured (local dev), the gate is a
    no-op. /healthz is mounted outside this router so the ALB health check —
    which can't send custom headers — stays reachable."""
    s = settings()
    if s.auth_required and not s.api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "API auth required but API_KEY is not configured")
    if s.api_key and x_api_key != s.api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")


_cors_origins = [o.strip() for o in settings().cors_allow_origins.split(",") if o.strip()]

app = FastAPI(title="Acme One Financial View API", lifespan=lifespan)
app.add_middleware(QueryInspectorMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                   allow_methods=["GET", "POST"],
                   allow_headers=["X-API-Key", "Content-Type"])
app.include_router(api_router, dependencies=[Depends(require_api_key)])


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "acme-ofv-api"}
