"""
mcp_server.py: the Handshake MCP server for shopping agents (Ajay's tools.py, finished).

Job in the system
-----------------
This is how an AI shopping agent (Claude Code, Claude Desktop, any MCP client)
talks to Handshake. It is a THIN, authenticated HTTP client of the backend:

    agent --MCP--> mcp_server.py --HTTPS + agent token--> api.py (backend)

It needs only HANDSHAKE_API_URL, plus either HANDSHAKE_AGENT_TOKEN or a
user who approves the connection through the login link (below). It never
needs an OpenAI key, a Link session, or database access, and it never decides
anything: every rule is enforced by the backend.

Connecting (OAuth 2.0 device authorization, RFC 8628 style)
----------------------------------------------------------
With no token (or an expired one), the first tool call starts a "connect"
request and returns error "authorization_required" with a login_url and a
short user_code. The agent gives the link to the user, who logs in to
Handshake and approves this agent. The next tool call picks up the agent
token, caches it in a private file (HANDSHAKE_MCP_TOKEN_FILE, mode 0600), and
carries on. The agent never sees the user's credentials, and only the user
can approve the connection.

Tools
-----
    connect_handshake       check the connection (or get the login link)
    create_contract_draft   user's words -> a DRAFT (no authority until the user signs)
    get_contract            a draft or signed contract, with its verification
    list_contracts          paginated; list_active_contracts is Ajay's alias
    request_purchase        contract + checkout_url + idempotency key -> Handshake reads and decides
    get_purchase_status     refreshes, then status / payment_state / next_action / checks
    get_payment_credential  agent_visible mode only: the single-use TEST card, once

Only get_payment_credential ever returns card values. Logs go to stderr;
stdout carries nothing but MCP protocol traffic.

Hosted for many users (HTTP + OAuth)
------------------------------------
On a server (HANDSHAKE_MCP_PUBLIC_URL set), this is an OAuth 2.1 *resource
server* for many users at once (Muse, Claude, any MCP client):

    client -> POST /mcp (no token) -> 401 + WWW-Authenticate: resource_metadata=...
    client -> /.well-known/oauth-protected-resource/mcp -> "authorization server: the Handshake API"
    client registers, sends the user to sign in and approve, gets an access token (auth.py)
    client -> POST /mcp  Authorization: Bearer <that user's agent token>

Every request carries its own user's token; we check it with the backend
(/auth/me, cached briefly) and pass THAT token on for the tool's backend
calls. Nothing is cached to disk and no user can act as another. The
single-user device flow and token file are for local (stdio) use only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from handshake.config import LOOPBACK_HOST_PATTERNS, Settings, get_settings
from handshake.prompts import CREDENTIAL_TOOL_RULES, agent_instructions

# stdout is the MCP channel in stdio mode, so every log line goes to stderr.
log = logging.getLogger("handshake.mcp")

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
READ_RETRIES = 2  # only idempotent reads are retried


class BackendUnreachable(Exception):
    """The backend didn't answer at all."""


class NeedsAuthorization(Exception):
    """The user must approve this agent; carries the login link to show them."""

    def __init__(self, login_url: str, user_code: str, expires_in: int) -> None:
        """Keep what the agent should tell the user."""
        super().__init__("authorization_required")
        self.login_url = login_url
        self.user_code = user_code
        self.expires_in = expires_in


class BackendError(Exception):
    """The backend answered with an error (standard {error, message, details} shape)."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        """Keep the status and the parsed body."""
        super().__init__(body.get("message", "error"))
        self.status_code = status_code
        self.body = body


# ============================================================
# The backend client (with the device-flow login)
# ============================================================


class HandshakeBackend:
    """Async HTTP client for the Handshake backend, holding the agent token."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        token_file: str | None = None,
        per_request: bool = False,
    ) -> None:
        """
        transport lets tests talk to an in-process backend; token_file overrides the cache location.
        per_request (hosted mode): use the calling user's OAuth token for each call, never a stored one.
        """
        self.per_request = per_request
        self.settings = settings or get_settings()
        self.base_url = self.settings.api_url
        self.transport = transport
        self.token_file = Path(token_file or self.settings.mcp_token_file).expanduser()
        self.client_id = self.settings.agent_id
        self._token: str | None = None if per_request else (self.settings.agent_token or self._load_cached_token())
        self._pending: dict[str, Any] | None = None  # the device authorization we're waiting on

    # ---------------- token cache ----------------

    def _load_cached_token(self) -> str | None:
        """A previously issued agent token for this backend, if it hasn't expired."""
        try:
            data = json.loads(self.token_file.read_text())
        except (OSError, ValueError):
            return None
        if data.get("api_url") != self.base_url or data.get("expires_at", 0) <= time.time() + 60:
            return None
        return data.get("access_token")

    def _save_token(self, token: str, expires_in: int) -> None:
        """Cache the agent token in a private file (dir 0700, file 0600), so restarts don't need a new login."""
        try:
            self.token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"api_url": self.base_url, "access_token": token, "expires_at": time.time() + expires_in, "agent_id": self.client_id}, handle)
            os.chmod(self.token_file, 0o600)
        except OSError as exc:
            log.warning("could not cache the agent token: %s", type(exc).__name__)

    def _forget_token(self) -> None:
        """Drop a token the backend rejected (and its cache)."""
        self._token = None
        try:
            self.token_file.unlink(missing_ok=True)
        except OSError:
            pass

    # ---------------- HTTP ----------------

    def _http(self) -> httpx.AsyncClient:
        """A client with bounded timeouts (and the test transport, if any)."""
        return httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(30.0, connect=5.0), transport=self.transport)

    async def _raw(self, method: str, path: str, json_body: Any = None, token: str | None = None) -> httpx.Response:
        """One HTTP call with a correlation id (so backend logs can be matched to this call)."""
        headers = {"X-Correlation-ID": str(uuid.uuid4()), "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self._http() as client:
                return await client.request(method, path, json=json_body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            raise BackendUnreachable(type(exc).__name__)

    async def _start_device_flow(self) -> NeedsAuthorization:
        """Ask the backend for a connect request; return what to show the user."""
        response = await self._raw("POST", "/oauth/device_authorization", {"client_id": self.client_id, "client_name": "Handshake MCP agent"})
        if response.status_code != 200:
            raise BackendError(response.status_code, _json(response))
        data = response.json()
        self._pending = {**data, "started_at": time.time()}
        return NeedsAuthorization(data["verification_uri_complete"], data["user_code"], data["expires_in"])

    async def _ensure_token(self) -> str:
        """The agent token, or raise NeedsAuthorization with the login link."""
        if self.per_request:
            # Hosted: the bearer middleware already verified this request's token.
            access = get_access_token()
            if access is None:
                raise BackendError(401, {"error": "not_authenticated", "message": "Sign in to Handshake through your MCP client first."})
            return access.token
        if self._token:
            return self._token
        if self._pending is not None:
            response = await self._raw("POST", "/oauth/token", {"grant_type": DEVICE_CODE_GRANT, "device_code": self._pending["device_code"], "client_id": self.client_id})
            data = _json(response)
            if response.status_code == 200 and data.get("access_token"):
                self._token = data["access_token"]
                self._save_token(self._token, int(data.get("expires_in", 3600)))
                self._pending = None
                return self._token
            if data.get("error") in ("authorization_pending", "slow_down"):
                raise NeedsAuthorization(self._pending["verification_uri_complete"], self._pending["user_code"], self._pending["expires_in"])
            self._pending = None  # denied, expired, or consumed: start over
        raise await self._start_device_flow()

    async def call(self, method: str, path: str, json_body: Any = None) -> Any:
        """
        An authenticated backend call. Reads (GET) are retried on connection
        errors; writes are not (the backend's idempotency keys handle retries).
        """
        token = await self._ensure_token()
        attempts = READ_RETRIES + 1 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self._raw(method, path, json_body, token)
                break
            except BackendUnreachable:
                if attempt == attempts - 1:
                    raise
        if response.status_code == 401:
            if self.per_request:
                # The user's token was revoked mid-session: the client must sign in again.
                raise BackendError(401, _json(response))
            self._forget_token()
            raise await self._start_device_flow()
        if response.status_code >= 400:
            raise BackendError(response.status_code, _json(response))
        return response.json()


def _json(response: httpx.Response) -> dict[str, Any]:
    """A response body as a dict, whatever the backend sent."""
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"error": "bad_response", "message": f"HTTP {response.status_code}"}


# ============================================================
# Shaping backend answers for the agent
# ============================================================


def _checks(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """The failing or unverifiable checks (the reasons the agent should relay)."""
    decision = detail.get("decision") or {}
    checks = []
    for item in decision.get("results", []):
        if item.get("verdict") != "pass" and item.get("severity") != "soft":
            checks.append({"check": item.get("label") or item.get("constraint"), "verdict": item.get("verdict"), "reason": item.get("reason")})
    return checks


def _purchase_view(detail: dict[str, Any]) -> dict[str, Any]:
    """A purchase as the agent sees it. Never card data (the backend never puts any here either)."""
    payment = detail.get("payment") or {}
    return {
        "purchase_id": detail.get("purchase_id"),
        "contract_id": detail.get("contract_id"),
        "status": detail.get("status"),
        "payment_state": detail.get("payment_state"),
        "next_action": detail.get("next_action"),
        "summary": detail.get("summary"),
        "checks": _checks(detail),
        "review_url": detail.get("review_url"),
        "approval_url": detail.get("approval_url"),
        "payment_rail": payment.get("provider_label"),
        "order_id": payment.get("order_id") if detail.get("status") == "completed" else None,
        "last4": payment.get("last4") if detail.get("status") == "completed" else None,
    }


def _money(value: Any, currency: str = "USD") -> str:
    """12.5 -> '$12.50' for USD, else '12.50 EUR'."""
    try:
        amount = f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "?"
    return f"${amount}" if currency.upper() == "USD" else f"{amount} {currency}"


def contract_summary(record: dict[str, Any]) -> str:
    """A plain-English one-paragraph summary of a draft or contract (deterministic, no LLM)."""
    spend = record.get("spend") or {}
    currency = spend.get("currency", "USD")
    parts = [record.get("goal", "Purchase")]
    parts.append(f"total at most {_money(spend.get('hard_cap_all_in'), currency)} including tax, shipping, and fees")
    if spend.get("target") is not None:
        parts.append(f"aiming for about {_money(spend.get('target'), currency)}")
    delivery = record.get("delivery") or {}
    if delivery.get("deliver_by"):
        parts.append(f"delivered by {str(delivery['deliver_by'])[:10]}")
    merchants = (record.get("merchants") or {}).get("allow") or []
    if merchants:
        parts.append("from " + ", ".join(merchants))
    for constraint in record.get("constraints") or []:
        if constraint.get("severity") == "hard":
            parts.append(f"{constraint['field'].replace('_', ' ')} {constraint['operator']} {constraint['value']}")
    terms = record.get("terms") or {}
    banned = [name for key, name in (("no_subscription", "subscriptions"), ("no_membership", "memberships"), ("no_addons", "add-ons")) if terms.get(key)]
    if banned:
        parts.append("no " + ", ".join(banned))
    return "; ".join(parts) + "."


# ============================================================
# The server
# ============================================================


class BackendTokenVerifier:
    """
    Checks a bearer token by asking the backend who it belongs to (GET /auth/me).

    Only AGENT tokens pass: a user's own login token must never be usable by an
    agent. Answers are cached for a few seconds so a burst of tool calls costs
    one check, while a revoked agent is still cut off almost immediately.
    """

    CACHE_SECONDS = 20.0

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """transport lets tests talk to an in-process backend."""
        self.settings = settings
        self.transport = transport
        self._cache: dict[str, tuple[float, AccessToken | None]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        """An AccessToken for a valid agent token, else None (the SDK answers 401)."""
        import hashlib

        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            async with httpx.AsyncClient(base_url=self.settings.api_url, timeout=10.0, transport=self.transport) as client:
                response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError:
            return None  # fail closed, and don't cache an outage
        me = _json(response)
        result = None
        if response.status_code == 200 and me.get("role") == "agent" and me.get("agent_id"):
            result = AccessToken(token=token, client_id=me["agent_id"], scopes=["handshake.agent"], subject=me.get("email"))
        if len(self._cache) > 5000:
            self._cache.clear()
        self._cache[key] = (time.monotonic() + self.CACHE_SECONDS, result)
        return result


def build_server(
    settings: Settings | None = None,
    backend: HandshakeBackend | None = None,
    hosted: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    """
    Create the FastMCP server with every tool; get_payment_credential only in agent_visible mode.

    hosted=True: the multi-user HTTP server (OAuth resource server; see the module docstring).
    """
    settings = settings or get_settings()
    instructions = agent_instructions(settings.credential_mode)
    if not hosted:
        backend = backend or HandshakeBackend(settings)
        server = FastMCP("Handshake", instructions=instructions)
    else:
        if not settings.mcp_public_url:
            raise ValueError("HANDSHAKE_MCP_PUBLIC_URL must be set for the hosted MCP server.")
        from urllib.parse import urlsplit

        backend = backend or HandshakeBackend(settings, transport=transport, per_request=True)
        resource = f"{settings.mcp_public_url}/mcp"
        public_host = urlsplit(settings.mcp_public_url).netloc
        server = FastMCP(
            "Handshake", instructions=instructions, stateless_http=True,
            host=settings.mcp_http_host, port=settings.mcp_http_port,
            auth=AuthSettings(issuer_url=settings.api_url, resource_server_url=resource, required_scopes=["handshake.agent"],
                              validate_token_resource=False),
            token_verifier=BackendTokenVerifier(settings, transport),
            # Only answer to our own public name (DNS-rebinding protection), plus loopback for health checks.
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[public_host, *LOOPBACK_HOST_PATTERNS],
                allowed_origins=[settings.mcp_public_url, settings.frontend_url],
            ),
        )
        _add_hosted_routes(server, settings, resource, transport)

    async def guarded(action: Any) -> dict[str, Any]:
        """Run a backend action, turning every failure into a structured answer the agent can act on."""
        try:
            return await action()
        except NeedsAuthorization as need:
            return {
                "error": "authorization_required",
                "login_url": need.login_url,
                "user_code": need.user_code,
                "expires_in_seconds": need.expires_in,
                "message": (
                    f"Handshake needs the user's permission first. Ask the user to open {need.login_url}, log in, "
                    f"and approve this agent (code {need.user_code}). Then call this tool again."
                ),
            }
        except BackendUnreachable:
            return {"error": "backend_unreachable", "message": f"The Handshake backend at {backend.base_url} is not reachable."}
        except BackendError as exc:
            body = exc.body
            return {"error": body.get("error", "error"), "message": body.get("message", "The backend refused the request."), "status_code": exc.status_code, "details": body.get("details", {})}

    @server.tool()
    async def connect_handshake() -> dict[str, Any]:
        """
        Check that this agent is connected to the user's Handshake account.

        If it isn't, this returns error "authorization_required" with a login_url: give it to the user,
        who logs in to Handshake and approves this agent. Every other tool does the same when needed.
        """
        async def action() -> dict[str, Any]:
            """Ask the backend who we are."""
            me = await backend.call("GET", "/auth/me")
            health = await backend.call("GET", "/health")
            return {"connected": True, "user": me.get("email"), "agent_id": me.get("agent_id"), "payment_rail": health.get("payment_label")}

        return await guarded(action)

    @server.tool()
    async def create_contract_draft(intent: str) -> dict[str, Any]:
        """
        Turn the user's shopping request (in their own words) into a Handshake contract DRAFT.

        The draft has NO authority until the user reviews and signs it at review_url. You cannot sign it.
        Returns draft_id, a plain-English summary, assumptions, clarifications_needed, and review_url.
        """
        async def action() -> dict[str, Any]:
            """Compile and store the draft."""
            record = await backend.call("POST", "/drafts/compile", {"intent": intent})
            return {
                "draft_id": record["id"],
                "summary": contract_summary(record),
                "assumptions": record.get("assumptions", []),
                "clarifications_needed": record.get("clarifications_needed", []),
                "review_url": record.get("review_url"),
                "status": "draft",
                "note": "Send the user the review_url. The draft does nothing until the user signs it.",
            }

        return await guarded(action)

    @server.tool()
    async def get_contract(contract_id: str) -> dict[str, Any]:
        """Read a draft or a signed contract: its status, rules, and (for signed ones) whether its signature still verifies."""
        async def action() -> dict[str, Any]:
            """Fetch the record."""
            data = await backend.call("GET", f"/contracts/{contract_id}")
            body = data.get("contract") or data.get("draft") or {}
            return {
                "id": data.get("id"),
                "kind": data.get("kind"),
                "status": data.get("status"),
                "summary": contract_summary(body) if isinstance(body, dict) else None,
                "verification": data.get("verification"),
                "funding": data.get("funding"),  # state + last4 only; the card itself stays locked
                "signed_contract_id": data.get("signed_contract_id"),
                "contract": body,
            }

        return await guarded(action)

    @server.tool()
    async def list_contracts(status: str | None = None, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        """
        List the user's drafts and contracts, newest first. Filter by status ("draft", "active", "used",
        "revoked", "expired"). Pass the returned next_cursor to get the next page.
        """
        async def action() -> dict[str, Any]:
            """Fetch and paginate."""
            items = await backend.call("GET", "/contracts")
            if status:
                items = [item for item in items if item.get("status") == status]
            start = int(cursor) if cursor and cursor.isdigit() else 0
            size = max(1, min(int(limit), 100))
            page = items[start:start + size]
            next_cursor = str(start + size) if start + size < len(items) else None
            return {"contracts": page, "next_cursor": next_cursor, "total": len(items)}

        return await guarded(action)

    @server.tool()
    async def list_active_contracts() -> dict[str, Any]:
        """Ajay's original name: the user's ACTIVE (signed, usable) contracts."""
        return await list_contracts(status="active", limit=100)

    @server.tool()
    async def request_purchase(
        contract_id: str,
        checkout_url: str,
        idempotency_key: str,
        selection_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Ask Handshake to authorize buying the checkout at checkout_url under a signed contract.

        Handshake reads the checkout ITSELF and checks the link matches the contract and your
        selection_report; it ignores any claims you make about price or approval. Use a fresh random
        idempotency_key per attempt (reuse it only to retry the same attempt after a timeout).
        Then follow next_action (poll get_purchase_status).
        """
        async def action() -> dict[str, Any]:
            """Submit the purchase request."""
            body: dict[str, Any] = {"contract_id": contract_id, "checkout_url": checkout_url, "idempotency_key": idempotency_key}
            if selection_report is not None:
                body["selection_report"] = selection_report
            return _purchase_view(await backend.call("POST", "/purchases", body))

        return await guarded(action)

    @server.tool()
    async def get_purchase_status(purchase_id: str) -> dict[str, Any]:
        """
        Refresh and read a purchase: status, payment_state, next_action, the checks that failed or
        couldn't be verified (with reasons), and the order id and last4 once completed. Never card data.
        """
        async def action() -> dict[str, Any]:
            """Refresh the payment and read the purchase."""
            return _purchase_view(await backend.call("POST", f"/purchases/{purchase_id}/payment/refresh"))

        return await guarded(action)

    if settings.credential_mode == "agent_visible":

        @server.tool(description=CREDENTIAL_TOOL_RULES)
        async def get_payment_credential(purchase_id: str) -> dict[str, Any]:
            """(Description comes from prompts.CREDENTIAL_TOOL_RULES.)"""
            async def action() -> dict[str, Any]:
                """Collect the card once."""
                return await backend.call("POST", f"/purchases/{purchase_id}/credential")

            return await guarded(action)

    @server.prompt(name="handshake_purchase_workflow", description="How to buy something through Handshake, step by step.")
    def handshake_purchase_workflow() -> str:
        """The agent workflow (same text as the server instructions)."""
        return instructions

    return server


# ============================================================
# Entry point: `handshake-mcp`
# ============================================================


def _add_hosted_routes(server: FastMCP, settings: Settings, resource: str, transport: httpx.AsyncBaseTransport | None) -> None:
    """/health, plus discovery documents at the places different MCP clients look for them."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    metadata = {
        # Same normalized form ("…/") as the SDK's metadata route and the backend's issuer.
        "resource": resource, "authorization_servers": [settings.api_url.rstrip("/") + "/"], "scopes_supported": ["handshake.agent"],
        "bearer_methods_supported": ["header"], "resource_name": "Handshake",
    }

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Liveness for the platform's health check. No auth, no backend call."""
        return JSONResponse({"status": "ok", "service": "handshake-mcp", "resource": resource})

    # The SDK serves /.well-known/oauth-protected-resource/mcp (RFC 9728); some clients try the bare path.
    @server.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def protected_resource(request: Request) -> JSONResponse:
        """Protected-resource metadata at the host root."""
        return JSONResponse(metadata)

    # Older MCP clients look for the authorization server's metadata on the MCP host itself.
    # Serve the backend's document (its endpoints are absolute URLs on the API host).
    @server.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def authorization_server(request: Request) -> JSONResponse:
        """The backend's OAuth metadata, mirrored."""
        try:
            async with httpx.AsyncClient(base_url=settings.api_url, timeout=10.0, transport=transport) as client:
                response = await client.get("/.well-known/oauth-authorization-server")
            return JSONResponse(response.json(), status_code=response.status_code)
        except (httpx.HTTPError, ValueError):
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)


def _http_app(server: FastMCP, token: str) -> Any:
    """The streamable-HTTP MCP app, behind a bearer-token check (put TLS in front of it on the real domain)."""
    import hmac

    from starlette.responses import JSONResponse

    inner = server.streamable_http_app()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Refuse any HTTP request without the MCP bearer token."""
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"").decode("latin-1")
            if not (supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:].encode(), token.encode())):
                await JSONResponse({"error": "unauthorized", "message": "Send Authorization: Bearer <HANDSHAKE_MCP_HTTP_TOKEN>."}, status_code=401)(scope, receive, send)
                return
        await inner(scope, receive, send)

    return app


def main(argv: list[str] | None = None) -> int:
    """Run the MCP server: stdio by default, or streamable HTTP with --transport http."""
    parser = argparse.ArgumentParser(prog="handshake-mcp", description="Handshake MCP server (agent proposes; Handshake decides).")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default=None, help="HTTP host (default: HANDSHAKE_MCP_HTTP_HOST)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default: HANDSHAKE_MCP_HTTP_PORT)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="handshake-mcp %(levelname)s %(message)s")
    settings = get_settings()
    server = build_server(settings)
    log.info("backend %s; %s; credential mode %s", settings.api_url, settings.payment_label, settings.credential_mode)

    if args.transport == "stdio":
        server.run("stdio")
        return 0

    import uvicorn

    host = args.host or settings.mcp_http_host
    port = args.port or settings.mcp_http_port
    if settings.mcp_public_url:
        # Hosted, many users: each request brings its own user's OAuth token.
        hosted = build_server(settings, hosted=True)
        log.info("hosted MCP at %s/mcp (OAuth via %s)", settings.mcp_public_url, settings.api_url)
        uvicorn.run(hosted.streamable_http_app(), host=host, port=port, log_level="info", proxy_headers=True, forwarded_allow_ips="*")
        return 0
    if not settings.mcp_http_token:
        print("Set HANDSHAKE_MCP_PUBLIC_URL (hosted, per-user OAuth) or HANDSHAKE_MCP_HTTP_TOKEN (single user) to serve MCP over HTTP.", file=sys.stderr)
        return 2
    uvicorn.run(_http_app(server, settings.mcp_http_token), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
