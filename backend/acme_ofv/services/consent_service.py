"""Consent service (brief §5) — consent centre, PAR→authorize link flow, the
the Open Finance platform webhook receiver, and customer-initiated LCM. Every state change rides
the one ordered path (publish_consent_event → consents → change stream),
never a direct write into customer_profiles.

Relocated from the former api/app.py consent handlers (behavior-preserving)."""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException

from acme_ofv.api.serialize import jsonable
from acme_ofv.config import settings
from acme_ofv.consent.producer import publish_consent_event

MYT = timezone(timedelta(hours=8))


def close_html(msg: str) -> str:
    return f"""<html><body style="font-family:sans-serif;text-align:center;padding-top:80px">
    <h3>{msg}</h3>
    <script>
      if (window.opener) window.opener.postMessage('consent-flow-done', '*');
      if (window.parent !== window) window.parent.postMessage('consent-flow-done', '*');
      setTimeout(() => window.close(), 1200);
    </script></body></html>"""


class ConsentService:
    """Constructed with the FastAPI app (needs app.state for db + the cached
    client-credentials token)."""

    def __init__(self, app):
        self.app = app
        self.db = app.state.db
        self.s = settings()

    async def _client_token(self) -> str:
        if not self.app.state.cc_token:
            async with httpx.AsyncClient(base_url=self.s.ofp_base_url, timeout=10) as hc:
                r = await hc.post("/v1/oauth/token",
                                  json={"grant_type": "client_credentials", "client_id": self.s.dc_id})
            self.app.state.cc_token = r.json()["access_token"]
        return self.app.state.cc_token

    async def consent_centre(self, customer_id: str) -> dict:
        db = self.db
        consents = await db.consents.find({"customer_id": customer_id}) \
            .sort("_rcp_version", -1).to_list(None)
        institutions = {i["_id"]: i async for i in db.institutions.find({})}
        now = datetime.now(timezone.utc)

        def _active(c) -> bool:
            exp = c.get("expiration_datetime")
            if exp is not None and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return c["status"] == "authorized" and (exp is None or exp > now)

        # One "current" card per (institution, purpose): prefer the currently-
        # AUTHORIZED (non-expired) consent over a higher-_rcp_version revoked
        # predecessor. The duplicate rule revokes the predecessor AT re-link time,
        # so it can outrank the authorized successor by version — picking purely by
        # version would show the active link as "revoked" and point the
        # Suspend/Revoke buttons at the wrong (already-revoked) consent. Everything
        # else is history. Same precedence as the gate's build_consent_boxes.
        groups: dict[tuple, list] = {}
        for c in consents:                          # sorted _rcp_version DESC
            c["institution"] = {
                "name": institutions.get(c["dp_id"], {}).get("name", c["dp_id"]),
                "display": institutions.get(c["dp_id"], {}).get("display", {}),
            }
            groups.setdefault((c["dp_id"], c["consent_purpose"]), []).append(c)
        current, history = {}, []
        for key, cs in groups.items():
            chosen = next((c for c in cs if _active(c)), cs[0])  # else highest version
            current[key] = chosen
            history.extend(c for c in cs if c is not chosen)
        history.sort(key=lambda c: c.get("_rcp_version", 0), reverse=True)
        linkable = [
            {"dp_id": i["_id"], "name": i["name"], "display": i.get("display", {}),
             "provider_type": i.get("provider_type"),
             "supported_use_cases": i.get("supported_use_cases", []),
             "status": i.get("status")}
            for i in institutions.values() if i["_id"] != "Acme-INTERNAL"
        ]
        return {"current": jsonable(list(current.values())),
                "history": jsonable(history), "providers": jsonable(linkable)}

    async def link(self, customer_id: str, body) -> dict:
        db = self.db
        profile = await db.customer_profiles.find_one({"_id": customer_id}, {"customer": 1})
        if not profile:
            raise HTTPException(404, "unknown customer")

        validity_days = int(body.validity_days)
        exp = (datetime.now(MYT) + timedelta(days=validity_days)).replace(
            hour=23, minute=59, second=59, microsecond=0)   # EOD rule
        state = uuid.uuid4().hex
        # Browser-facing bases: the iframe (OFP authorize page) and the post-auth
        # redirect (DC callback) must be reachable BY THE BROWSER. With a local UI
        # against the AWS backend, those go through the Vite dev proxy, so the
        # client passes its own bases; absent them we fall back to the internal
        # server URLs (fully-local). Server→server httpx still uses ofp_base_url.
        authorize_base = body.authorize_base if body.authorize_base is not None else self.s.ofp_base_url
        redirect_base = body.redirect_base if body.redirect_base is not None else self.s.dc_base_url
        await db.dc_link_states.insert_one({
            "_id": state, "customer_id": customer_id, "created_at": datetime.now(timezone.utc),
            "dp_id": body.dp_id, "consent_purpose": body.consent_purpose,
        })
        async with httpx.AsyncClient(base_url=self.s.ofp_base_url, timeout=10) as hc:
            r = await hc.post("/v1/oauth/par", json={
                "client_id": self.s.dc_id,
                "redirect_uri": f"{redirect_base}/consents-callback",
                "state": state,
                "login_hint": {"id_type": profile["customer"]["id_type"],
                               "hashed_id_number": profile["customer"]["hashed_id_number"]},
                "authorization_details": {
                    "dp_id": body.dp_id,
                    "consent_purpose": body.consent_purpose,
                    "permissions": body.permissions,
                    "expiration_datetime": exp.isoformat(),
                },
            })
        if r.status_code != 201:
            raise HTTPException(400, r.json())
        request_uri = r.json()["request_uri"]
        return {"authorize_url": f"{authorize_base}/v1/oauth/authorize?request_uri={request_uri}",
                "state": state}

    async def link_callback(self, state: str, code: str | None = None,
                            error: str | None = None) -> str:
        db = self.db
        link = await db.dc_link_states.find_one({"_id": state})
        if not link:
            raise HTTPException(400, "unknown state")
        if error:
            return close_html("Consent was rejected.")

        async with httpx.AsyncClient(base_url=self.s.ofp_base_url, timeout=10) as hc:
            r = await hc.post("/v1/oauth/token",
                              json={"grant_type": "authorization_code", "code": code,
                                    "client_id": self.s.dc_id})
        if r.status_code != 200:
            raise HTTPException(400, r.json())
        tok = r.json()
        consent = tok["authorization_details"][0]
        await db.dc_tokens.replace_one(
            {"consent_id": consent["consent_id"]},
            {"consent_id": consent["consent_id"], "access_token": tok["access_token"],
             "refresh_token": tok.get("refresh_token"), "obtained_at": datetime.now(timezone.utc),
             "customer_id": link["customer_id"]},
            upsert=True)
        await publish_consent_event(db, consent)
        return close_html("Institution linked. You can close this window.")

    async def action(self, consent_id: str, action: str) -> dict:
        if action not in ("revoke", "suspend", "reactivate"):
            raise HTTPException(400, "unknown action")
        db = self.db
        token = await self._client_token()
        body = {"updated_by": "data_consumer_user"}
        if action == "suspend":
            body["status_reason"] = {"reason_code": "not_allowed",
                                     "reason_description": "customer-requested pause"}
        async with httpx.AsyncClient(base_url=self.s.ofp_base_url, timeout=10) as hc:
            r = await hc.post(f"/v1/consents/{consent_id}/{action}", json=body,
                              headers={"authorization": f"Bearer {token}"})
        if r.status_code != 201:
            raise HTTPException(r.status_code, r.json())
        post_image = r.json()
        await publish_consent_event(db, post_image)
        return {"ok": True, "consent": post_image}

    async def webhook(self, event: dict) -> dict:
        db = self.db
        await db.webhook_events.insert_one({
            "received_at": datetime.now(timezone.utc), "event": event})
        if event.get("event_type") == "consent_status_updated":
            await publish_consent_event(db, event["consent"])
        return {"accepted": True}
