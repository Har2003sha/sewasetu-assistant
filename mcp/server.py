# -*- coding: utf-8 -*-
"""
Sewa Setu MCP servers (streamable HTTP, OAuth 2.1 resource servers).

    python server.py citizen   -> https://<host>/citizen/mcp   (scope: citizen)
    python server.py officer   -> https://<host>/officer/mcp   (scope: officer)

Two processes, two endpoints, two tool sets: a citizen's assistant never sees an officer tool.
Neither process touches the database. Each request is authenticated with the portal's own
access token (RS256, checked against the portal's JWKS: iss = portal, aud = THIS server's URL,
role/scope for this server). Who the caller is comes only from the token's `sub`, never from a
tool argument. Business rules live in the portal (app/api.py); this layer is the interface.
The servers are stateless (no session affinity) so they scale by adding replicas.
"""
import os, sys, asyncio
from typing import Optional
from urllib.parse import urlparse

import httpx, jwt
from jwt import PyJWKClient
from pydantic import AnyHttpUrl
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import PlainTextResponse

ROLE = sys.argv[1] if len(sys.argv) > 1 else "citizen"
assert ROLE in ("citizen", "officer")
PUBLIC = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
PORTAL = os.environ.get("PORTAL_INTERNAL_URL", "http://app:8000").rstrip("/")
KEY = os.environ["INTERNAL_API_KEY"]
PORT = int(os.environ.get("PORT", {"citizen": 8101, "officer": 8102}[ROLE]))
PATH = "/%s/mcp" % ROLE
RESOURCE = PUBLIC + PATH                      # canonical URL = the token audience
_jwks = PyJWKClient(PORTAL + "/.well-known/jwks.json", cache_keys=True, lifespan=600)


class PortalTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> Optional[AccessToken]:
        try:
            key = await asyncio.to_thread(_jwks.get_signing_key_from_jwt, token)
            c = jwt.decode(token, key.key, algorithms=["RS256"], issuer=PUBLIC,
                           audience=[RESOURCE, RESOURCE + "/"], options={"require": ["exp", "sub", "aud", "iss"]})
        except Exception:
            return None
        sub = str(c.get("sub", ""))
        if c.get("role") != ROLE or c.get("scope") != ROLE or not sub.startswith(ROLE + ":"):
            return None
        return AccessToken(token=token, client_id=str(c.get("client_id", "")), scopes=[ROLE], expires_at=int(c["exp"]),
                           resource=RESOURCE, subject=sub, claims=c)


def who() -> tuple:
    t = get_access_token()
    if not t:
        raise ToolError("Not signed in.")
    return t.subject.split(":", 1)[1], t.claims


_http = httpx.AsyncClient(base_url=PORTAL, headers={"X-Internal-Key": KEY}, timeout=20)


async def portal(path: str, body: Optional[dict] = None) -> dict:
    try:
        r = await (_http.get(path) if body is None else _http.post(path, json=body))
    except httpx.HTTPError:
        raise ToolError("The Sewa Setu portal is not reachable right now. Please try again in a few minutes.")
    if r.status_code >= 500:
        raise ToolError("The portal had a problem. Nothing was changed. Please try again shortly.")
    try:
        return r.json()
    except ValueError:
        raise ToolError("Unexpected reply from the portal.")


host = urlparse(PUBLIC).netloc
mcp = FastMCP(
    "Sewa Setu " + ("Applicant Assistant" if ROLE == "citizen" else "Officer Assistant"),
    instructions=open(os.path.join(os.path.dirname(__file__), "instructions_%s.txt" % ROLE), encoding="utf-8").read(),
    token_verifier=PortalTokenVerifier(),
    auth=AuthSettings(issuer_url=AnyHttpUrl(PUBLIC), resource_server_url=AnyHttpUrl(RESOURCE), required_scopes=[ROLE]),
    host="0.0.0.0", port=PORT, streamable_http_path=PATH, stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                 allowed_hosts=[host, "localhost:*", "127.0.0.1:*", "mcp-%s:*" % ROLE],
                                                 allowed_origins=["*"]),
)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request):
    return PlainTextResponse("ok")


if ROLE == "citizen":
    @mcp.tool()
    async def scheme_info() -> dict:
        """Current, official facts about the Old Age Pension: amount, minimum age, documents, blocks, the date the
        application window closes and hours left, and the 15-day decision rule. Use this instead of memory: the
        scheme details change."""
        return await portal("/internal/v1/scheme")

    @mcp.tool()
    async def my_applications() -> dict:
        """The signed-in citizen's own pension applications (latest first) with status, dates and, if pending, how many
        days remain before it is approved automatically; also whether a half-finished application is saved. Applications made
        on the web portal or at the counter appear here too."""
        mobile, _ = who()
        return await portal("/internal/v1/citizen/status", {"mobile": mobile})

    @mcp.tool()
    async def save_details(applicant_name: Optional[str] = None, date_of_birth: Optional[str] = None,
                           gender: Optional[str] = None, marital_status: Optional[str] = None,
                           late_husbands_name: Optional[str] = None, village: Optional[str] = None,
                           block: Optional[str] = None, bank_account_number: Optional[str] = None,
                           ifsc: Optional[str] = None) -> dict:
        """Save what the citizen has told you so far (send only the fields you learned just now; earlier ones are kept).
        Name must be as on the age-proof document. Date of birth in any common form ('12 March 1955', 12/03/1955).
        gender: Male/Female/Other. marital_status: Married/Unmarried/Widowed. block must be one of the scheme's blocks.
        Returns what is saved (bank account masked), what is still needed, and any problem the portal's rules found,
        for example 'applicant must be 60 years of age or above'. Tell the citizen about problems plainly and kindly;
        never change an answer to get past a rule."""
        mobile, _ = who()
        f = {"applicant_name": applicant_name, "dob": date_of_birth, "gender": gender, "marital_status": marital_status,
             "husband_name": late_husbands_name, "village": village, "block": block,
             "bank_account": bank_account_number, "ifsc": ifsc}
        return await portal("/internal/v1/citizen/draft", {"mobile": mobile, "fields": f})

    @mcp.tool()
    async def get_age_proof_upload_link() -> dict:
        """Get a short, single-use web link (valid 30 minutes) where the citizen, or the family member helping,
        uploads a photo or PDF of the age-proof document from their phone. You cannot see or upload the document yourself.
        Give the link to the citizen, ask them to open it, send the photo, and come back and tell you; then call
        save_details with no arguments to confirm the document is recorded."""
        mobile, _ = who()
        return await portal("/internal/v1/citizen/upload-link", {"mobile": mobile})

    @mcp.tool()
    async def submit_application(declaration_accepted: bool) -> dict:
        """Submit the saved application. Only call this after: (1) save_details says ready_to_submit, (2) you read the saved
        details back and the citizen said they are correct, (3) you read the declaration (from scheme_info) aloud and the
        citizen agreed. Pass declaration_accepted=true only if they agreed in words. The portal applies exactly the same
        checks as the web form and the counter; if it refuses, explain why in simple words. Returns the application number."""
        mobile, _ = who()
        return await portal("/internal/v1/citizen/submit", {"mobile": mobile, "declaration_accepted": declaration_accepted})

    @mcp.tool()
    async def withdraw_application(application_no: str) -> dict:
        """Withdraw the citizen's own PENDING application (for example to correct a mistake and apply again). Ask the citizen to
        confirm first: they can then apply afresh with the same mobile number. Approved, rejected or already withdrawn
        applications cannot be withdrawn."""
        mobile, _ = who()
        return await portal("/internal/v1/citizen/withdraw", {"mobile": mobile, "application_no": application_no})

    @mcp.tool()
    async def sign_out(clear_saved_draft: bool = True) -> dict:
        """End this citizen's sign-in now and, by default, delete any half-finished application saved by the assistant.
        Phones and kiosks are shared: call this when the citizen is done or hands over the phone."""
        mobile, _ = who()
        return await portal("/internal/v1/citizen/signout", {"mobile": mobile, "clear_draft": clear_saved_draft})

else:
    @mcp.tool()
    async def list_applications(status: str = "PENDING", block: Optional[str] = None, search: Optional[str] = None,
                                limit: int = 15) -> dict:
        """The officer's queue, oldest first. status: PENDING (default), APPROVED, REJECTED, DEEMED_APPROVED, WITHDRAWN or ALL.
        block: optional block name. search: an application number (SSP...), a 10-digit mobile, or the start of a name.
        Each row shows days waiting and the date it will be approved automatically if nobody decides (15-day rule); the
        summary says how many pending applications are within 3 days of that."""
        who()
        return await portal("/internal/v1/officer/list", {"status": status, "block": block, "query": search, "limit": limit})

    @mcp.tool()
    async def get_application(application_no: str) -> dict:
        """Full case file for one application: details (bank account masked), whether an age proof is on file, the audit trail
        (who did what and when), the auto-approval date, and FLAGS a careful clerk would check: under age when submitted,
        another live application on the same mobile, the same bank account on other live applications, no document on file,
        invalid IFSC, submitted after the deadline. Summarise the flags to the officer before any decision."""
        who()
        return await portal("/internal/v1/officer/application", {"application_no": application_no})

    @mcp.tool()
    async def decide_application(application_no: str, decision: str, reason: str, acknowledge_flags: bool = False) -> dict:
        """Approve or reject one PENDING application. decision: 'approve' or 'reject'. reason: the officer's own reason in a
        sentence; it is recorded in the audit trail against the officer's name. Only call after the officer has told you
        which application and which decision; never decide on your own or in bulk. Approval is blocked if the application was
        under age or duplicates another live application, unless the officer has seen that flag and you pass
        acknowledge_flags=true with their reason. Not available with unattended program tokens."""
        officer, claims = who()
        if claims.get("kind") == "program":
            raise ToolError("Decisions need an officer who is signed in personally, not an unattended program token.")
        return await portal("/internal/v1/officer/decide", {"officer": officer, "application_no": application_no,
                            "decision": decision, "reason": reason, "acknowledge_flags": acknowledge_flags})

def build_app():
    """The SDK advertises the issuer as 'https://host/' (trailing slash). The portal's issuer is exactly
    PUBLIC_BASE_URL, and strict clients compare the two strings, so serve our own metadata document."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    app = mcp.streamable_http_app()
    meta_path = "/.well-known/oauth-protected-resource" + PATH
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != meta_path]

    async def meta(_: Request):
        return JSONResponse({"resource": RESOURCE, "authorization_servers": [PUBLIC], "scopes_supported": [ROLE],
                             "bearer_methods_supported": ["header"], "resource_name": mcp.name},
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=300"})
    app.router.routes.insert(0, Route(meta_path, meta, methods=["GET", "OPTIONS"]))
    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT, log_level="info", proxy_headers=True, forwarded_allow_ips="*")
