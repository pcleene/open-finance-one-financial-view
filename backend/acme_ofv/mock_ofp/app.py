"""Open Finance Platform mock — Authorization Server + Resource Server (spec v1.2.2 shaped).

Stands in for the Open Finance platform + every DP. Backed by the `ofp_mock` database on the same
Atlas cluster (inspectable in Compass, clearly separated from the DC-side
`acme_ofv` serving layer).

Faithfulness targets (brief §6.1): endpoint paths/params, R/O/C fields, enums,
consent lifecycle (§13) incl. 5-minute authorization timeout, duplicate-renewal
protocol, EOD expiry validation, cursor-only pagination (page_size 10..500,
newest-first + stable secondary sort), 429 + Retry-After, webhook events on
DP/platform-initiated changes, chaos toggles.

Crypto envelope (JWS/JWE) is feature-flagged and OFF by default: payloads are
plain JSON with correct structure and headers so demos stay readable.

Run:  uv run uvicorn acme_ofv.mock_ofp.app:app --port 8100
"""

import asyncio
import base64
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from acme_ofv.config import settings
from acme_ofv.db import make_async_client, mock_db
from acme_ofv.ofp_models.enums import (
    CONSENT_TYPE,
    ACTIVE_CONSENT_STATES,
    ConsentStatus,
)

MYT = timezone(timedelta(hours=8))
S = settings()


def require_api_key(x_api_key: str | None = Header(default=None)):
    """Gate the mock's operator-only /admin/* routes (chaos + DP-side actions).
    Same fail-closed contract as the DC API: when `auth_required` is set, a
    missing server-side key rejects (503); otherwise the gate enforces only when
    a key is configured (no-op for local dev). The OAuth/resource endpoints stay
    open so the DC service and the browser authorize flow keep working."""
    if S.auth_required and not S.api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "API auth required but API_KEY is not configured")
    if S.api_key and x_api_key != S.api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")

# --------------------------------------------------------------------------
# app + state
# --------------------------------------------------------------------------

client = None
db = None

# chaos toggles (admin-set, in-memory)
chaos = {
    "rate_storm_dps": set(),       # always-429 these DPs
    "rate_storm_retry_after": True,
    "slow_dps": set(),             # 6 s delay (spec timeout is 5 s)
    "blocked_accounts": set(),     # 403 Consent.AccountTemporarilyBlocked
    "offline_dps": set(),
}

# per-DP rolling 60 s request window (spec: 200 req/min per DC per DP)
_rate_window: dict[str, list[float]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, db
    client = make_async_client(max_pool=30)
    db = mock_db(client)
    yield
    await client.close()


app = FastAPI(title="Open Finance Platform mock (AS + RS)", version="1.2.2", lifespan=lifespan)


def err(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"errors": [{"code": code, "message": message}]},
        headers=headers or {},
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fapi_headers(x_fapi_interaction_id: str | None) -> dict:
    return {"x-fapi-interaction-id": x_fapi_interaction_id or str(uuid.uuid4())}


# --------------------------------------------------------------------------
# consent lifecycle helpers (§13)
# --------------------------------------------------------------------------

async def fire_webhook(consent: dict) -> None:
    """POST {DC_URL}/v1/consents/events for DP-/platform-initiated changes."""
    payload = {"event_type": "consent_status_updated", "consent": public_consent(consent)}
    try:
        async with httpx.AsyncClient(timeout=5) as hc:
            await hc.post(f"{S.dc_base_url}/v1/consents/events", json=payload)
    except Exception:
        pass  # webhook delivery is best-effort in the mock


def public_consent(doc: dict) -> dict:
    """Account Access Consent Object — verbatim fields only, JSON-safe."""
    out = {
        "consent_id": doc["consent_id"],
        "dc_id": doc["dc_id"],
        "dp_id": doc["dp_id"],
        "id_type": doc.get("id_type"),
        "hashed_id_number": doc.get("hashed_id_number"),
        "consent_type": doc.get("consent_type", CONSENT_TYPE),
        "consent_purpose": doc["consent_purpose"],
        "permissions": doc["permissions"],
        "expiration_datetime": dt_iso_myt(doc["expiration_datetime"]),
        "status": doc["status"],
        "status_reason": doc.get("status_reason"),
        "accounts": doc.get("accounts"),
        "created_at": dt_iso_myt(doc["created_at"]),
        "updated_at": dt_iso_myt(doc.get("updated_at")) if doc.get("updated_at") else None,
        "updated_by": doc.get("updated_by"),
    }
    return out


def dt_iso_myt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MYT).isoformat(timespec="seconds")


async def apply_lazy_transitions(doc: dict) -> dict:
    """5-min awaiting_authorization -> failed; past-expiry authorized/suspended -> expired."""
    if doc is None:
        return doc
    status = doc["status"]
    now = now_utc()
    created = doc["created_at"].replace(tzinfo=timezone.utc)
    exp = doc["expiration_datetime"].replace(tzinfo=timezone.utc)

    if status == "awaiting_authorization" and now - created > timedelta(minutes=5):
        return await transition(doc, "failed",
                                {"reason_code": "timeout",
                                 "reason_description": "authorization not completed within 5 minutes"},
                                "ofp", webhook=False)
    if status in ("authorized", "suspended") and now > exp:
        return await transition(doc, "expired", None, "ofp", webhook=True)
    return doc


async def transition(doc: dict, new_status: str, status_reason: dict | None,
                     updated_by: str, webhook: bool) -> dict:
    update = {
        "status": new_status,
        "status_reason": status_reason,
        "updated_at": now_utc(),
        "updated_by": updated_by,
        "_version": doc.get("_version", 0) + 1,
    }
    if new_status == "authorized":
        update["status_reason"] = None  # must be cleared on reactivation
    await db.consents.update_one({"consent_id": doc["consent_id"]}, {"$set": update})
    doc = {**doc, **update}
    if webhook:
        asyncio.ensure_future(fire_webhook(doc))
    return doc


async def enforce_duplicate_rule(new_consent: dict) -> None:
    """Platform safeguard: one active consent per (hashed_id, dp, dc, purpose) —
    auto-revoke predecessors with reason_code=duplicate, webhook to DC."""
    cursor = db.consents.find({
        "hashed_id_number": new_consent["hashed_id_number"],
        "dp_id": new_consent["dp_id"],
        "dc_id": new_consent["dc_id"],
        "consent_purpose": new_consent["consent_purpose"],
        "consent_id": {"$ne": new_consent["consent_id"]},
        "status": {"$in": [s.value for s in ACTIVE_CONSENT_STATES]},
    })
    async for old in cursor:
        await transition(
            old, "revoked",
            {"reason_code": "duplicate",
             "reason_description": f"superseded by {new_consent['consent_id']}"},
            "ofp", webhook=True)


def validate_eod_expiry(expiration: datetime) -> bool:
    """Spec: expiration MUST be 23:59:59 on the final validity day, +08:00."""
    myt = expiration.astimezone(MYT)
    return (myt.hour, myt.minute, myt.second) == (23, 59, 59)


# --------------------------------------------------------------------------
# auth plumbing (tokens are opaque; spec envelope feature-flagged off)
# --------------------------------------------------------------------------

async def bearer(request: Request) -> dict | None:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    return await db.tokens.find_one({"token": h[7:]})


def unsigned_jwt(claims: dict) -> str:
    """JWT-shaped unsigned token (crypto flag off) — readable in demos."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return f"{header.decode()}.{body.decode()}."


# --------------------------------------------------------------------------
# chaos + rate limiting middleware-ish helper for RS data endpoints
# --------------------------------------------------------------------------

async def rs_gate(dp_id: str) -> JSONResponse | None:
    if dp_id in chaos["offline_dps"]:
        return err(503, "Upstream.InvalidResponse", "provider offline")
    if dp_id in chaos["slow_dps"]:
        await asyncio.sleep(6)  # > 5 s spec sync timeout
        return err(504, "Upstream.Timeout", "data provider did not respond within 5s")
    if dp_id in chaos["rate_storm_dps"]:
        headers = {"Retry-After": "5"} if chaos["rate_storm_retry_after"] else {}
        return err(429, "Request.RateLimited", "rate limit exceeded (chaos storm)", headers)
    # rolling 60 s window per DP
    win = _rate_window.setdefault(dp_id, [])
    now = time.monotonic()
    while win and now - win[0] > 60:
        win.pop(0)
    if len(win) >= S.ofp_rate_limit_per_min:
        return err(429, "Request.RateLimited", "200 req/min per DC per DP exceeded",
                   {"Retry-After": "5"})
    win.append(now)
    return None


# ==========================================================================
# AS — Authorization Server
# ==========================================================================

@app.get("/.well-known/openid-configuration")
async def well_known():
    base = S.ofp_base_url
    return {
        "issuer": base,
        "pushed_authorization_request_endpoint": f"{base}/v1/oauth/par",
        "authorization_endpoint": f"{base}/v1/oauth/authorize",
        "token_endpoint": f"{base}/v1/oauth/token",
        "introspection_endpoint": f"{base}/v1/oauth/introspect",
        "revocation_endpoint": f"{base}/v1/oauth/revoke",
        "userinfo_endpoint": f"{base}/v1/oauth/userinfo",
        "jwks_uri": f"{base}/v1/oauth/jwks/ofp",
    }


@app.get("/v1/oauth/jwks/ofp")
@app.get("/v1/oauth/jwks/{dp_id}")
async def jwks(dp_id: str = "ofp"):
    return {"keys": []}  # crypto flag off — populated when OFP_CRYPTO=on


@app.post("/v1/oauth/par")
async def par(request: Request):
    """Pushed Authorization Request. Body (JAR-equivalent, crypto off): JSON with
    client_id, redirect_uri, state, login_hint (hashed_id_number + id_type),
    authorization_details {dp_id, consent_purpose, permissions, expiration_datetime}."""
    body = await request.json()
    ad = body.get("authorization_details") or {}
    dp_id, purpose = ad.get("dp_id"), ad.get("consent_purpose")
    permissions = ad.get("permissions") or []
    if purpose not in ("pfm", "credit_underwriting"):
        return err(400, "Request.InvalidParameter", f"invalid consent_purpose {purpose!r}")
    if not permissions or not set(permissions) <= {"read_accounts", "read_balances", "read_transactions"}:
        return err(400, "Request.InvalidParameter", "invalid permissions")
    provider = await db.providers.find_one({"provider_id": dp_id})
    if not provider:
        return err(400, "Request.InvalidParameter", f"unknown dp_id {dp_id!r}")
    try:
        expiration = datetime.fromisoformat(ad["expiration_datetime"])
    except Exception:
        return err(400, "Request.InvalidParameter", "expiration_datetime unparseable")
    if not validate_eod_expiry(expiration):
        return err(400, "Consent.ValidationError",
                   "expiration_datetime must be EOD 23:59:59 +08:00 on the final validity day")

    # consent_id must be URL-safe (it appears in the LCM path); keep only
    # alphanumerics from the provider short name (e.g. "National Provident Fund" -> "NPFEP").
    short = "".join(ch for ch in provider["display"]["short_name"].upper() if ch.isalnum())[:6]
    consent_id = f"CONS-{short}-" \
                 f"{'PFM' if purpose == 'pfm' else 'CU'}-{secrets.token_hex(3).upper()}"
    consent = {
        "consent_id": consent_id,
        "dc_id": body.get("client_id", S.dc_id),
        "dp_id": dp_id,
        "id_type": (body.get("login_hint") or {}).get("id_type"),
        "hashed_id_number": (body.get("login_hint") or {}).get("hashed_id_number"),
        "consent_type": CONSENT_TYPE,
        "consent_purpose": purpose,
        "permissions": permissions,
        "expiration_datetime": expiration.astimezone(timezone.utc),
        "status": "awaiting_authorization",
        "status_reason": None,
        "accounts": None,
        "created_at": now_utc(),
        "updated_at": None,
        "updated_by": None,
        "_version": 1,
    }
    await db.consents.insert_one(consent)

    request_uri = f"urn:ietf:params:oauth:request_uri:{uuid.uuid4()}"
    await db.par_requests.insert_one({
        "request_uri": request_uri,
        "consent_id": consent_id,
        "redirect_uri": body.get("redirect_uri"),
        "state": body.get("state"),
        "created_at": now_utc(),
        "used": False,
    })
    return JSONResponse(status_code=201, content={"request_uri": request_uri, "expires_in": 600})


PICKER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>{dp_name} — Account Access</title>
<style>
 body{{font-family:-apple-system,'Segoe UI',sans-serif;background:#f4f5f7;margin:0;padding:0}}
 .bar{{background:{brand};color:#fff;padding:14px 22px;font-weight:700;font-size:18px}}
 .card{{max-width:520px;margin:28px auto;background:#fff;border-radius:14px;
        box-shadow:0 4px 18px rgba(0,0,0,.08);padding:26px}}
 h2{{margin:0 0 4px;font-size:19px}} p{{color:#555;font-size:14px;line-height:1.5}}
 .acc{{display:flex;align-items:center;gap:12px;border:1px solid #e3e5e8;border-radius:10px;
       padding:12px 14px;margin:8px 0}}
 .acc b{{font-size:14px}} .acc span{{color:#777;font-size:12px;display:block}}
 .perm{{display:inline-block;background:#eef2ff;color:#3b5bdb;border-radius:6px;
        padding:2px 8px;font-size:11px;margin:2px 3px 0 0}}
 .row{{display:flex;gap:10px;margin-top:20px}}
 button{{flex:1;padding:12px;border-radius:10px;border:0;font-size:15px;font-weight:600;cursor:pointer}}
 .ok{{background:{brand};color:#fff}} .no{{background:#e9ecef;color:#333}}
</style></head><body>
<div class="bar">{dp_name}</div>
<form class="card" method="post" action="/v1/oauth/authorize/decision">
 <h2>Acme requests access to your accounts</h2>
 <p>Purpose: <b>{purpose}</b> &middot; valid until <b>{expiry}</b><br>{perms}</p>
 {accounts}
 <input type="hidden" name="request_uri" value="{request_uri}">
 <div class="row">
  <button class="no" name="decision" value="reject">Reject</button>
  <button class="ok" name="decision" value="approve">Allow access</button>
 </div>
</form></body></html>"""


@app.get("/v1/oauth/authorize")
async def authorize(request_uri: str = Query(...)):
    par_doc = await db.par_requests.find_one({"request_uri": request_uri, "used": False})
    if not par_doc or (now_utc() - par_doc["created_at"].replace(tzinfo=timezone.utc)) > timedelta(seconds=600):
        return err(400, "Request.InvalidParameter", "unknown or expired request_uri")
    consent = await db.consents.find_one({"consent_id": par_doc["consent_id"]})
    consent = await apply_lazy_transitions(consent)
    if consent["status"] != "awaiting_authorization":
        return err(400, "Consent.Invalid", f"consent is {consent['status']}")
    provider = await db.providers.find_one({"provider_id": consent["dp_id"]})
    accounts = await db.accounts.find({
        "dp_id": consent["dp_id"],
        "hashed_id_number": consent["hashed_id_number"],
    }).to_list(None)
    if not accounts:
        return err(400, "Consent.Invalid", "user holds no accounts at this provider")

    rows = "".join(
        f'<label class="acc"><input type="checkbox" name="account_ids" value="{a["account_id"]}" checked>'
        f'<div><b>{a["account_name"]}</b><span>{a["account_number_display"]} · {a["type"]}/{a["subtype"]}</span></div></label>'
        for a in accounts
    )
    perms = "".join(f'<span class="perm">{p}</span>' for p in consent["permissions"])
    html = PICKER_HTML.format(
        dp_name=provider["name"], brand=provider["display"]["brand_color"],
        purpose=consent["consent_purpose"], perms=perms,
        expiry=dt_iso_myt(consent["expiration_datetime"])[:10],
        accounts=rows, request_uri=request_uri,
    )
    return HTMLResponse(html)


def mask_for_consent(acc: dict) -> str:
    """account_number masked first6+last4 for credit types (spec rule)."""
    n = acc["account_number"]
    if acc["type"] == "credit" and len(n) > 10:
        return n[:6] + "*" * (len(n) - 10) + n[-4:]
    return n


@app.post("/v1/oauth/authorize/decision")
async def authorize_decision(request_uri: str = Form(...), decision: str = Form(...),
                             account_ids: list[str] = Form(default=[])):
    par_doc = await db.par_requests.find_one({"request_uri": request_uri, "used": False})
    if not par_doc:
        return err(400, "Request.InvalidParameter", "unknown or expired request_uri")
    consent = await db.consents.find_one({"consent_id": par_doc["consent_id"]})
    await db.par_requests.update_one({"_id": par_doc["_id"]}, {"$set": {"used": True}})

    sep = "&" if "?" in (par_doc.get("redirect_uri") or "") else "?"
    if decision != "approve" or not account_ids:
        await transition(consent, "rejected",
                         {"reason_code": "not_allowed", "reason_description": "user rejected"},
                         "data_provider_user", webhook=False)
        return RedirectResponse(
            f"{par_doc['redirect_uri']}{sep}error=access_denied&state={par_doc['state']}", 303)

    accounts = await db.accounts.find({"account_id": {"$in": account_ids}}).to_list(None)
    consented = [
        {"account_id": a["account_id"], "account_number": mask_for_consent(a),
         "account_name": a["account_name"]}
        for a in accounts
    ]
    await db.consents.update_one(
        {"consent_id": consent["consent_id"]}, {"$set": {"accounts": consented}})
    consent = await db.consents.find_one({"consent_id": consent["consent_id"]})
    consent = await transition(consent, "authorized", None, "data_consumer_user", webhook=False)
    await enforce_duplicate_rule(consent)

    code = secrets.token_urlsafe(24)
    await db.auth_codes.insert_one({
        "code": code, "consent_id": consent["consent_id"],
        "created_at": now_utc(), "used": False,
    })
    return RedirectResponse(
        f"{par_doc['redirect_uri']}{sep}code={code}&state={par_doc['state']}&iss={S.ofp_base_url}", 303)


@app.post("/v1/oauth/token")
async def token(request: Request):
    form = dict(await request.form()) if "form" in (request.headers.get("content-type") or "") \
        else await request.json()
    grant = form.get("grant_type")

    if grant == "client_credentials":
        tok = f"cc_{secrets.token_urlsafe(24)}"
        await db.tokens.insert_one({
            "token": tok, "type": "client", "dc_id": form.get("client_id", S.dc_id),
            "created_at": now_utc(),
        })
        return {"access_token": tok, "token_type": "Bearer", "expires_in": 3600,
                "scope": "consents providers"}

    if grant == "authorization_code":
        code_doc = await db.auth_codes.find_one({"code": form.get("code"), "used": False})
        if not code_doc or (now_utc() - code_doc["created_at"].replace(tzinfo=timezone.utc)) > timedelta(seconds=60):
            return err(400, "Request.InvalidParameter", "invalid or expired code (60s single-use)")
        await db.auth_codes.update_one({"_id": code_doc["_id"]}, {"$set": {"used": True}})
        consent = await db.consents.find_one({"consent_id": code_doc["consent_id"]})
        tok, rtok = f"at_{secrets.token_urlsafe(24)}", f"rt_{secrets.token_urlsafe(24)}"
        await db.tokens.insert_one({
            "token": tok, "type": "user", "consent_id": consent["consent_id"],
            "hashed_id_number": consent["hashed_id_number"], "created_at": now_utc(),
        })
        id_token = unsigned_jwt({
            "iss": S.ofp_base_url, "sub": consent["hashed_id_number"],
            "id_type": consent["id_type"], "hashed_id_number": consent["hashed_id_number"],
            "aud": consent["dc_id"], "iat": int(time.time()),
        })
        return {
            "access_token": tok, "refresh_token": rtok, "token_type": "Bearer",
            "expires_in": 3600, "id_token": id_token,
            "authorization_details": [public_consent(consent)],
        }

    if grant == "refresh_token":
        return err(400, "Request.InvalidParameter", "refresh without rotation not exercised in POC")
    return err(400, "Request.InvalidParameter", f"unsupported grant_type {grant!r}")


@app.post("/v1/oauth/introspect")
async def introspect(request: Request):
    form = dict(await request.form())
    doc = await db.tokens.find_one({"token": form.get("token")})
    if not doc:
        return {"active": False}
    return {"active": True, "token_type": doc["type"],
            "consent_id": doc.get("consent_id"), "sub": doc.get("hashed_id_number")}


@app.post("/v1/oauth/revoke")
async def revoke_token(request: Request):
    form = dict(await request.form())
    await db.tokens.delete_one({"token": form.get("token")})
    return JSONResponse(status_code=200, content={})


@app.get("/v1/oauth/userinfo")
async def userinfo(request: Request):
    tok = await bearer(request)
    if not tok or tok["type"] != "user":
        return err(401, "Request.InvalidParameter", "user token required")
    user = await db.users.find_one({"hashed_id_number": tok["hashed_id_number"]})
    return {"sub": tok["hashed_id_number"], "id_type": user["id_type"],
            "hashed_id_number": tok["hashed_id_number"],
            "email": user.get("email_masked")}


# ==========================================================================
# RS — Resource Server
# ==========================================================================

@app.get("/v1/providers")
async def providers(x_fapi_interaction_id: str | None = Header(default=None)):
    docs = await db.providers.find({}, {"_id": 0}).to_list(None)
    return JSONResponse(content={"data": docs, "meta": {"next_page_params": None}},
                        headers=fapi_headers(x_fapi_interaction_id))


async def consent_for_request(consent_id: str, request: Request,
                              require_client_token: bool = True):
    tok = await bearer(request)
    if require_client_token and not tok:
        return None, err(401, "Request.InvalidParameter", "bearer token required")
    doc = await db.consents.find_one({"consent_id": consent_id})
    if not doc:
        return None, err(404, "Consent.NotFound", f"{consent_id} not found")
    doc = await apply_lazy_transitions(doc)
    return doc, None


@app.get("/v1/consents/{consent_id:path}")
async def get_consent(consent_id: str, request: Request,
                      x_fapi_interaction_id: str | None = Header(default=None)):
    doc, e = await consent_for_request(consent_id, request)
    if e:
        return e
    return JSONResponse(content=public_consent(doc), headers=fapi_headers(x_fapi_interaction_id))


@app.post("/v1/consents/{consent_path:path}")
async def consent_lcm(consent_path: str, request: Request,
                      x_fapi_interaction_id: str | None = Header(default=None)):
    """DC-initiated LCM: revoke | suspend | reactivate → 201 + updated object.

    A consent_id may legitimately contain '/' (e.g. the NPF provider), so
    capture the whole tail as a path and split the trailing action — otherwise
    the embedded '/' breaks route matching and the request 404s."""
    consent_id, _, action = consent_path.rpartition("/")
    if action not in ("revoke", "suspend", "reactivate"):
        return err(404, "Request.InvalidParameter", f"unknown action {action!r}")
    doc, e = await consent_for_request(consent_id, request)
    if e:
        return e
    body = await request.json() if (await request.body()) else {}
    updated_by = body.get("updated_by", "data_consumer")
    status_reason = body.get("status_reason")

    target = {"revoke": "revoked", "suspend": "suspended", "reactivate": "authorized"}[action]
    allowed = {
        "revoked": {"authorized", "suspended"},
        "suspended": {"authorized"},
        "authorized": {"suspended"},
    }[target]
    if doc["status"] not in allowed:
        return err(403, "Consent.Invalid",
                   f"cannot {action} a consent in status {doc['status']}")
    if target == "suspended" and not status_reason:
        return err(400, "Consent.ValidationError", "status_reason mandatory for suspended")
    doc = await transition(doc, target, status_reason, updated_by, webhook=False)
    return JSONResponse(status_code=201, content=public_consent(doc),
                        headers=fapi_headers(x_fapi_interaction_id))


async def data_access_check(request: Request, account_id: str, permission: str):
    """User-token → consent → status/permission/account-scope/expiry + chaos gates."""
    tok = await bearer(request)
    if not tok or tok["type"] != "user":
        return None, None, err(401, "Request.InvalidParameter", "user access token required")
    consent = await db.consents.find_one({"consent_id": tok["consent_id"]})
    consent = await apply_lazy_transitions(consent)
    if consent["status"] != "authorized":
        return None, None, err(403, "Consent.Invalid", f"consent is {consent['status']}")
    if permission not in consent["permissions"]:
        return None, None, err(403, "Consent.Invalid", f"{permission} not granted")
    if account_id not in {a["account_id"] for a in (consent["accounts"] or [])}:
        return None, None, err(403, "Consent.Invalid", "account not in consent scope")
    if account_id in chaos["blocked_accounts"]:
        return None, None, err(403, "Consent.AccountTemporarilyBlocked",
                               "account temporarily blocked by provider")
    acc = await db.accounts.find_one({"account_id": account_id}, {"_id": 0})
    if not acc:
        return None, None, err(404, "Request.InvalidParameter", "unknown account")
    gate = await rs_gate(acc["dp_id"])
    if gate:
        return None, None, gate
    return consent, acc, None


def account_object(acc: dict) -> dict:
    """Project the mock's account doc onto the spec Account Object."""
    keys = ["account_id", "account_number", "account_name", "account_holder_name",
            "institution_name", "category", "type", "subtype", "loan_details",
            "limit", "interest_rate", "minimum_payment_amount", "payment_due_date",
            "custom_data"]
    out = {k: acc.get(k) for k in keys}
    if acc["type"] == "credit":
        out["account_number"] = mask_for_consent(acc)
    return out


@app.get("/v1/accounts/{account_id}")
async def get_account(account_id: str, request: Request,
                      x_fapi_interaction_id: str | None = Header(default=None)):
    consent, acc, e = await data_access_check(request, account_id, "read_accounts")
    if e:
        return e
    return JSONResponse(content=jsonable(account_object(acc)),
                        headers=fapi_headers(x_fapi_interaction_id))


@app.get("/v1/accounts/{account_id}/balances")
async def get_balances(account_id: str, request: Request,
                       x_fapi_interaction_id: str | None = Header(default=None)):
    consent, acc, e = await data_access_check(request, account_id, "read_balances")
    if e:
        return e
    bal = acc["balance_state"]
    return JSONResponse(content=jsonable({
        "account_id": account_id,
        "current_balance": bal["current_balance"],
        "available_balance": bal.get("available_balance"),
        "statement_balance": bal.get("statement_balance"),
        "credit_lines_included": bal.get("credit_lines_included", False),
        "statement_date": bal.get("statement_date"),
        "custom_data": bal.get("custom_data"),
    }), headers=fapi_headers(x_fapi_interaction_id))


def encode_cursor(txn_date: datetime, txn_id: str) -> str:
    raw = json.dumps([txn_date.isoformat(), txn_id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(after: str) -> tuple[datetime, str]:
    pad = after + "=" * (-len(after) % 4)
    d, i = json.loads(base64.urlsafe_b64decode(pad))
    return datetime.fromisoformat(d), i


@app.get("/v1/accounts/{account_id}/transactions")
async def get_transactions(account_id: str, request: Request,
                           from_date: str | None = None, to_date: str | None = None,
                           page_size: int = 100, next_page_params: str | None = None,
                           x_fapi_interaction_id: str | None = Header(default=None)):
    consent, acc, e = await data_access_check(request, account_id, "read_transactions")
    if e:
        return e
    if not 10 <= page_size <= 500:
        return err(400, "Request.InvalidParameter", "page_size must be 10..500")
    q: dict = {"account_id": account_id}
    try:
        if from_date:
            q.setdefault("transaction_date", {})["$gte"] = datetime.fromisoformat(from_date).replace(
                tzinfo=timezone.utc) if len(from_date) == 10 else datetime.fromisoformat(from_date)
        if to_date:
            end = datetime.fromisoformat(to_date)
            if len(to_date) == 10:
                end = end.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            q.setdefault("transaction_date", {})["$lte"] = end
    except ValueError:
        return err(400, "Request.InvalidParameter", "invalid from_date/to_date")

    if next_page_params:
        try:
            cdate, cid = decode_cursor(next_page_params)
        except Exception:
            return err(400, "Pagination.InvalidCursor", "cursor is opaque; replay it verbatim")
        # newest-first keyset: strictly after the cursor position
        q["$or"] = [
            {"transaction_date": {"$lt": cdate}},
            {"transaction_date": cdate, "transaction_id": {"$lt": cid}},
        ]

    docs = await db.transactions.find(q, {"_id": 0, "dp_id": 0}) \
        .sort([("transaction_date", -1), ("transaction_id", -1)]) \
        .limit(page_size + 1).to_list(None)

    nxt = None
    if len(docs) > page_size:
        docs = docs[:page_size]
        last = docs[-1]
        nxt = encode_cursor(last["transaction_date"], last["transaction_id"])
    return JSONResponse(content={"data": [jsonable(d) for d in docs],
                                 "meta": {"next_page_params": nxt}},
                        headers=fapi_headers(x_fapi_interaction_id))


def jsonable(doc):
    """BSON → spec JSON: dates → ISO MYT, Decimal128 → string amounts."""
    from bson import Decimal128
    from datetime import date as _date
    if isinstance(doc, dict):
        return {k: jsonable(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [jsonable(v) for v in doc]
    if isinstance(doc, Decimal128):
        return f"{doc.to_decimal():.2f}"
    if isinstance(doc, datetime):
        return dt_iso_myt(doc)
    if isinstance(doc, _date):
        return doc.isoformat()
    return doc


# ==========================================================================
# Admin — chaos toggles + DP-side actions (webhook demo paths)
# ==========================================================================

@app.post("/admin/chaos", dependencies=[Depends(require_api_key)])
async def set_chaos(request: Request):
    body = await request.json()
    for key in ("rate_storm_dps", "slow_dps", "blocked_accounts", "offline_dps"):
        if key in body:
            chaos[key] = set(body[key])
    if "rate_storm_retry_after" in body:
        chaos["rate_storm_retry_after"] = bool(body["rate_storm_retry_after"])
    return {k: sorted(v) if isinstance(v, set) else v for k, v in chaos.items()}


@app.post("/admin/dp-action/{consent_path:path}", dependencies=[Depends(require_api_key)])
async def dp_action(consent_path: str, request: Request):
    """Simulate a DP-side consent change → fires the platform→DC webhook.
    e.g. 'Bank Beta suspends consent for fraud review'. (consent_id may contain '/'.)"""
    consent_id, _, action = consent_path.rpartition("/")
    target = {"suspend": "suspended", "revoke": "revoked", "reactivate": "authorized"}.get(action)
    if not target:
        return err(400, "Request.InvalidParameter", f"unknown action {action!r}")
    doc = await db.consents.find_one({"consent_id": consent_id})
    if not doc:
        return err(404, "Consent.NotFound", consent_id)
    body = await request.json() if (await request.body()) else {}
    reason = body.get("status_reason") or {
        "reason_code": "not_allowed", "reason_description": "provider action (simulated)"}
    doc = await transition(doc, target,
                           None if target == "authorized" else reason,
                           "data_provider", webhook=True)
    return public_consent(doc)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "mock_ofp", "crypto": S.ofp_crypto}
