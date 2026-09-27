"""
api.py: the FastAPI app (HTTP layer). It was backend/app/main.py before the integration.

Job in the system
-----------------
This is the front door. It defines the URLs, checks WHO is calling (auth.py),
parses request bodies, and turns results into JSON. It contains no business
rules of its own:

    HTTP request -> api.py route -> auth.py (who?) -> services.py -> intent_diff.py / db.py
                                                   <- result or ServiceError
    HTTP response (JSON)

It also defines the backend-only request/response shapes that models.py
does not have (PurchaseSubmission, PurchaseResponse, ...). models.py itself
is shared with the team and is never edited.

The app is built by create_app(), so tests can build a fresh app after
changing settings (payment mode, credential mode). `app` at the bottom is the
instance uvicorn serves: `uvicorn handshake.api:app`.

Every error response has one shape, so the frontend can handle all of them
the same way:
    {"error": "<code>", "message": "<human sentence>", "details": {...}}
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from handshake import auth, compiler, db, payments, services
from handshake.auth import AuthError, Principal, current_principal
from handshake.config import get_settings
from handshake.extractor import Extractor, get_extractor
from handshake.models import (
    Contract,
    PurchaseRequest,
    PurchaseStatusResponse,
    SignContractRequest,
)
from handshake.services import CredentialSummary, ServiceError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("handshake")


# ============================================================
# Backend-only schemas
# ============================================================


class PurchaseSubmission(PurchaseRequest):
    """
    The POST /purchases body: PurchaseRequest (from models.py) plus an idempotency key.

    Deliberately NO proposal field. The caller says WHERE the checkout is
    (checkout_url); Handshake's extractor reads WHAT is in it. Letting the
    proposing side supply its own facts would let it grade its own homework.
    Unknown keys (including an old-style "proposal") are refused.

    idempotency_key is required for the agent: retries after a timeout must
    return the same purchase, never create a second one.
    """

    model_config = {"extra": "forbid"}

    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class PurchaseResponse(PurchaseStatusResponse):
    """PurchaseStatusResponse (purchase_id, status, decision) plus the credential summary and a human sentence."""

    contract_id: str
    proposal_id: str | None = None
    credential: CredentialSummary | None = None
    summary: str
    idempotent_replay: bool = False  # true when an idempotency key returned an existing purchase


class HumanDecisionRequest(BaseModel):
    """Optional body for approve/reject: a free-text note from the user, kept in the evidence."""

    note: str | None = None


class CompleteRequest(BaseModel):
    """Body for POST /purchases/{id}/complete: what the merchant actually charged."""

    charged_amount: float = Field(ge=0)


class CompileRequest(BaseModel):
    """Body for POST /drafts/compile: the user's shopping request in their own words."""

    intent: str = Field(min_length=1, max_length=4000)


class DraftPatch(BaseModel):
    """
    Body for PATCH /drafts/{id} (Rohan's DraftPatch). Every field is optional;
    only the keys actually sent are applied (model_fields_set decides).
    Unknown keys are refused, so a typo can't silently do nothing.
    """

    model_config = {"extra": "forbid"}

    goal: str | None = None
    target: float | None = None
    hard_cap_all_in: float | None = None
    max_shipping: float | None = None
    deliver_by: datetime | None = None
    constraints: list[dict[str, Any]] | None = None


class DeviceAuthorizationRequest(BaseModel):
    """Body for POST /oauth/device_authorization: which agent wants to connect."""

    client_id: str
    client_name: str | None = None


class DeviceDecision(BaseModel):
    """Body for approving or denying a connect request."""

    user_code: str


DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DemoLoginRequest(BaseModel):
    """Body for POST /auth/demo-login. DEMO ONLY: anyone who types an email gets a token."""

    email: str


class GoogleLoginRequest(BaseModel):
    """Body for POST /auth/google: the ID token Google Identity Services gave the frontend."""

    credential: str = Field(min_length=20, max_length=8192)


class AuthorizationDecision(BaseModel):
    """Body for POST /oauth/authorize/decision (the consent page)."""

    request_id: str
    approve: bool


class AgentKeyRequest(BaseModel):
    """Body for POST /agents/keys."""

    name: str


class ContractListItem(BaseModel):
    """One row in GET /contracts (either a draft or a signed contract)."""

    id: str
    kind: str  # "draft" or "contract"
    status: str
    goal: str
    created_at: datetime
    signed_at: datetime | None = None
    signed_contract_id: str | None = None
    draft_id: str | None = None
    funding_state: str | None = None  # contracts only: not_funded / awaiting_approval / funded / released / used / ...


# ============================================================
# Errors
# ============================================================


def error_response(status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    """Build the standard error JSON."""
    content = {"error": code, "message": message, "details": details or {}}
    return JSONResponse(status_code=status_code, content=content)


def _clean_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce validation errors to {loc, msg, type} so they are JSON-safe and readable."""
    return [{"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in errors]


async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """Turn any ServiceError raised by services.py into the standard error JSON."""
    return error_response(exc.status_code, exc.code, exc.message, services._jsonable(exc.details))


async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    """Turn authentication/permission failures into the standard error JSON."""
    response = error_response(exc.status_code, exc.code, exc.message)
    # OAuth clients read `error_description` (RFC 6749); add it alongside our own `message`.
    body = json.loads(response.body)
    body["error_description"] = exc.message
    response = JSONResponse(status_code=exc.status_code, content=body)
    if exc.status_code == 401:
        # Tells HTTP clients which scheme to use; harmless for browsers.
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Give FastAPI's own body-validation failures the same error shape as everything else."""
    return error_response(422, "invalid_request", "Request body failed validation.", {"errors": _clean_errors(exc.errors())})


# How each compile failure maps to HTTP. The body always carries the code.
COMPILE_ERROR_STATUS = {
    "timeout": 504,
    "provider_error": 502,
    "missing_api_key": 503,
}


def compile_error(exc: compiler.CompileError) -> ServiceError:
    """Turn a CompileError into the standard API error (no draft was created)."""
    return ServiceError(COMPILE_ERROR_STATUS.get(exc.code, 422), exc.code, exc.message, {"draft_created": False, **exc.details})


# ============================================================
# Role helper
# ============================================================


def require_user(session: Session, principal: Principal, action: str, **target: str) -> None:
    """
    Stop the agent from performing a user-only action.

    This is THE enforcement point for "the agent can never sign its own
    contract or approve its own purchase". It runs in backend code on every
    such request; prompt wording in the MCP server is guidance on top of it,
    not a substitute. The refused attempt is written to the evidence ledger.
    """
    if principal.is_agent:
        raise services.deny_agent_action(session, principal, action, **target)


router = APIRouter()


# ============================================================
# Auth
# ============================================================


def _login_response(email: str, demo: bool) -> dict[str, Any]:
    """The user token every login method returns (same shape, so the frontend treats them alike)."""
    token, expires_at = auth.issue_user_token(email)
    return {"token": token, "token_type": "bearer", "email": auth.normalize_email(email), "role": auth.USER, "expires_at": expires_at, "demo_auth": demo}


@router.get("/auth/config")
def auth_config() -> dict[str, Any]:
    """Which sign-in methods this server offers (public: the login page needs it before anyone is logged in)."""
    settings = get_settings()
    return {"google_client_id": settings.google_client_id, "demo_login": settings.demo_login_enabled}


@router.post("/auth/google")
def google_login(body: GoogleLoginRequest) -> dict[str, Any]:
    """Sign in with Google: the verified Google email becomes the Handshake user."""
    return _login_response(auth.verify_google_credential(body.credential), demo=False)


@router.post("/auth/demo-login")
def demo_login(body: DemoLoginRequest) -> dict[str, Any]:
    """
    DEMO AUTH: exchange an email for a signed user token. No password, no email check.

    Off in prod unless HANDSHAKE_ALLOW_DEMO_LOGIN=true: on a public server it
    would let anyone act as anyone. Real users sign in with Google.
    """
    if not get_settings().demo_login_enabled:
        raise AuthError(404, "demo_login_disabled", "Demo login is turned off on this server. Sign in with Google.")
    return _login_response(body.email, demo=True)


# ------------------------------------------------------------
# Connecting an agent: OAuth 2.0 device authorization grant (RFC 8628 style)
# ------------------------------------------------------------


@router.get("/.well-known/oauth-authorization-server")
def oauth_metadata() -> dict[str, Any]:
    """
    Discovery (RFC 8414). MCP clients such as Muse find this from the MCP
    server's protected-resource metadata, then register, send the user to
    authorization_endpoint, and trade the code at token_endpoint.
    """
    base = get_settings().api_url
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "device_authorization_endpoint": f"{base}/oauth/device_authorization",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token", DEVICE_CODE_GRANT],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": [auth.AGENT_SCOPE],
        "token_endpoint_auth_methods_supported": list(auth.CLIENT_AUTH_METHODS),
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": f"{get_settings().frontend_url}/agents",
    }


@router.post("/oauth/register", status_code=201)
def oauth_register(body: dict[str, Any] = Body(...), session: Session = Depends(db.get_session)) -> dict[str, Any]:
    """Dynamic client registration (RFC 7591), which MCP clients do before their first sign-in."""
    return auth.register_client(session, body)


@router.get("/oauth/authorize")
def oauth_authorize(request: Request, session: Session = Depends(db.get_session)) -> Any:
    """
    Where an agent sends the user's browser. We validate the request and send
    the user on to the frontend's consent page, where they sign in and decide.
    """
    from fastapi.responses import RedirectResponse

    result = auth.start_authorization(session, dict(request.query_params))
    if "redirect_error" in result:
        return RedirectResponse(result["redirect_error"], status_code=302)
    return RedirectResponse(f"{get_settings().frontend_url}/authorize?request_id={result['request_id']}", status_code=302)


@router.get("/oauth/authorize/request")
def oauth_authorize_request(
    request_id: str, session: Session = Depends(db.get_session), principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """For the consent page: which agent is asking, and where the user will be sent back. Users only."""
    return auth.describe_authorization(session, principal, request_id)


@router.post("/oauth/authorize/decision")
def oauth_authorize_decision(
    body: AuthorizationDecision, session: Session = Depends(db.get_session), principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """The USER approves or denies connecting the agent. Returns where to send the browser."""
    return auth.decide_authorization(session, principal, body.request_id, body.approve)


@router.post("/oauth/device_authorization")
def device_authorization(body: DeviceAuthorizationRequest, session: Session = Depends(db.get_session)) -> dict[str, Any]:
    """Start connecting an agent. The agent shows the user `verification_uri_complete`; the user logs in and approves."""
    return auth.start_device_authorization(session, body.client_id, body.client_name)


async def _token_params(request: Request) -> dict[str, str]:
    """
    /oauth/token parameters. OAuth clients send a form (RFC 6749); our own MCP
    server sends JSON. Client credentials may also come as HTTP Basic auth.
    """
    import base64
    from urllib.parse import unquote

    if "application/json" in request.headers.get("content-type", ""):
        try:
            raw = await request.json()
        except ValueError:
            raise AuthError(400, "invalid_request", "The body isn't valid JSON.")
        params = {k: str(v) for k, v in raw.items() if v is not None} if isinstance(raw, dict) else {}
    else:
        params = {k: str(v) for k, v in (await request.form()).items()}
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            client_id, _, secret = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except ValueError:
            raise AuthError(401, "invalid_client", "Malformed Basic credentials.")
        params.setdefault("client_id", unquote(client_id))
        params["client_secret"] = unquote(secret)
    return params


@router.post("/oauth/token")
async def oauth_token(request: Request) -> Any:
    """
    Every token grant: device_code (our MCP server polling), authorization_code
    (with the PKCE verifier), and refresh_token (rotating). Responses are never cached.
    """
    params = await _token_params(request)
    grant = params.get("grant_type", "")
    session = db.SessionLocal()
    try:
        if grant == DEVICE_CODE_GRANT:
            result = auth.exchange_device_code(session, params.get("device_code", ""), params.get("client_id", ""))
        elif grant in ("authorization_code", "refresh_token"):
            auth.authenticate_client(session, params.get("client_id", ""), params.get("client_secret"))
            if grant == "authorization_code":
                result = auth.exchange_authorization_code(
                    session, params.get("code", ""), params["client_id"], params.get("redirect_uri"), params.get("code_verifier", "")
                )
            else:
                result = auth.refresh_access_token(session, params.get("refresh_token", ""), params["client_id"])
        else:
            raise AuthError(400, "unsupported_grant_type", "Use authorization_code, refresh_token, or the device_code grant.")
    finally:
        session.close()
    return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.get("/oauth/device")
def describe_device(
    user_code: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """For the approval screen: which agent is asking to connect. Users only."""
    if principal.is_agent:
        raise AuthError(403, "agent_not_permitted", "Only the user reviews connect requests.")
    return auth.describe_device_authorization(session, user_code)


@router.post("/oauth/device/approve")
def approve_device(
    body: DeviceDecision,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """The USER approves connecting the agent to their account (an agent calling this gets 403)."""
    return auth.decide_device_authorization(session, principal, body.user_code, approve=True)


@router.post("/oauth/device/deny")
def deny_device(
    body: DeviceDecision,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """The user declines connecting the agent."""
    return auth.decide_device_authorization(session, principal, body.user_code, approve=False)


@router.get("/auth/me")
def whoami(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Who the current token belongs to (the frontend validates sessions with it; the MCP server checks agent tokens with it)."""
    return {"email": principal.email, "role": principal.role, "agent_id": principal.agent_id}


# ------------------------------------------------------------
# The user's connected agents (Agents page)
# ------------------------------------------------------------


@router.get("/agents")
def list_agents(session: Session = Depends(db.get_session), principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Every agent the user connected (OAuth, device login, or key), active first. User only."""
    return auth.list_grants(session, principal)


@router.post("/agents/keys", status_code=201)
def create_agent_key(
    body: AgentKeyRequest, session: Session = Depends(db.get_session), principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """Make an agent key (shown once) for an agent that can't sign in through OAuth. User only."""
    return auth.create_agent_key(session, principal, body.name)


@router.post("/agents/{agent_id}/revoke")
def revoke_agent(
    agent_id: str, session: Session = Depends(db.get_session), principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """Disconnect an agent: its tokens stop working on the next request. User only."""
    return auth.revoke_grant(session, principal, agent_id)


# ============================================================
# Contracts
# ============================================================


@router.post("/contracts", status_code=201)
def create_contract(
    body: Any = Body(...),
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Create a draft from a ContractDraft or a CompilerOutput. User only (agents use POST /drafts/compile)."""
    if principal.is_agent:
        raise ServiceError(403, "agent_not_permitted", "The agent creates drafts through POST /drafts/compile.")
    return services.create_draft(session, body, owner=principal.email)


@router.get("/contracts", response_model=list[ContractListItem])
def list_contracts(
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> list[ContractListItem]:
    """List the caller's drafts and signed contracts, newest first."""
    items: list[ContractListItem] = []

    for row in db.list_drafts(session, owner=principal.email):
        items.append(
            ContractListItem(
                id=row.id,
                kind="draft",
                status="draft",
                goal=row.data["goal"],
                created_at=db.utc(row.created_at),
                signed_contract_id=row.signed_contract_id,
            )
        )

    for row in db.list_contracts(session, owner=principal.email):
        items.append(
            ContractListItem(
                id=row.id,
                kind="contract",
                status=row.status,
                goal=row.data.get("goal", ""),
                created_at=db.utc(row.created_at),
                signed_at=db.utc(row.signed_at),
                draft_id=row.draft_id,
                funding_state=(db.current_funding(session, row.id).state if db.current_funding(session, row.id) else "not_funded"),
            )
        )

    # Signed contracts sort by when they were signed; drafts by when created.
    def newest_first_key(item: ContractListItem) -> datetime:
        """The timestamp used for ordering."""
        return item.signed_at or item.created_at

    return sorted(items, key=newest_first_key, reverse=True)


@router.get("/contracts/{contract_id}")
def get_contract(
    contract_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Return a signed contract (with a live hash/signature check) or a draft (with compiler metadata)."""
    row = db.get_contract_row(session, contract_id)
    if row is not None and row.owner == principal.email:
        try:
            contract: Contract | dict[str, Any] = db.contract_from_row(row)
            verification = services.verify_contract(contract)
        except ValidationError as exc:
            # The stored JSON was edited into something invalid: definitely tampered.
            contract = row.data
            verification = {"valid": False, "errors": _clean_errors(exc.errors(include_url=False))}
        try:
            funding = services.refresh_funding(session, row.id, principal.email)
        except ServiceError:
            funding = services.funding_for_api(db.current_funding(session, row.id))
        return {
            "kind": "contract",
            "id": row.id,
            "status": row.status,
            "draft_id": row.draft_id,
            "contract": contract,
            "verification": verification,
            "funding": funding,
        }

    draft_row = db.get_draft_row(session, contract_id)
    if draft_row is not None and draft_row.owner == principal.email:
        return {
            "kind": "draft",
            "id": draft_row.id,
            "status": "draft",
            "signed_contract_id": draft_row.signed_contract_id,
            "draft": db.load_draft(draft_row),
            "verification": None,
            **draft_row.meta,  # assumptions, clarifications_needed, compiler_notes
        }

    # Unknown and foreign-owned ids get the same answer.
    raise ServiceError(404, "contract_not_found", f"No contract or draft with id {contract_id!r}.")


@router.post("/contracts/{draft_id}/sign", response_model=Contract)
def sign_contract(
    draft_id: str,
    body: SignContractRequest | None = None,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> Contract:
    """Sign a draft. User only. The body is optional; if it names a draft_id, that must match the path."""
    require_user(session, principal, "sign a contract", draft_id=draft_id)
    if body is not None and body.draft_id and body.draft_id != draft_id:
        raise ServiceError(400, "draft_id_mismatch", "draft_id in the body does not match the draft id in the path.")

    agent_key = body.agent_key if body else None
    client_signature = body.signature if body else None
    return services.sign_draft(
        session, draft_id, owner=principal.email, agent_key=agent_key, client_signature=client_signature
    )


@router.post("/contracts/{contract_id}/amend")
def amend_contract(
    contract_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Start a new version of a signed contract as a draft (previous_contract_id points back). User only."""
    require_user(session, principal, "amend a contract", contract_id=contract_id)
    return services.amend_contract(session, contract_id, owner=principal.email)


@router.get("/contracts/{contract_id}/purchases")
def list_contract_purchases(
    contract_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> list[dict[str, Any]]:
    """Every purchase attempt against one of the caller's contracts, newest first."""
    services._require_contract(session, contract_id, principal.email)
    return [_purchase_response(item) for item in services.list_purchase_outcomes(session, principal.email, contract_id)]


# ============================================================
# Drafts
# ============================================================


@router.post("/drafts/compile", status_code=201)
async def compile_draft(
    body: CompileRequest,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """
    Compile a shopping request into a DRAFT (user or agent). It has no authority until the user signs it.

    The compiler only sees the user's words plus trusted date context; the
    result is linted and stored, and review_url is where the user reviews it.
    """
    try:
        result = await compiler.compile_intent(body.intent, now=services.clock())
    except compiler.CompileError as exc:
        raise compile_error(exc)
    stored = services.store_compiled_draft(session, result, owner=principal.email, proposed_by=principal)
    return {**stored["record"], "compiler_source": result.source}


@router.get("/drafts")
def list_drafts(
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> list[dict[str, Any]]:
    """The caller's drafts, newest first, as DraftRecords (ContractDraft fields flattened with the compiler metadata)."""
    return services.list_draft_records(session, principal.email)


@router.get("/drafts/{draft_id}")
def get_draft(
    draft_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """One draft as a DraftRecord."""
    return services.get_draft_record(session, draft_id, principal.email)


@router.patch("/drafts/{draft_id}")
def patch_draft(
    draft_id: str,
    body: DraftPatch,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Edit an unsigned draft (user only). Edited values become source=user; lint re-runs."""
    require_user(session, principal, "edit a draft", draft_id=draft_id)
    patch = body.model_dump(mode="json", include=body.model_fields_set)
    return services.patch_draft(session, draft_id, patch, owner=principal.email)


# ============================================================
# The user's own Stripe Link account, and contract funding
# ============================================================


@router.post("/link/connect")
def link_connect(session: Session = Depends(db.get_session), principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """
    Connect the logged-in user's OWN Stripe Link account (user only).

    Returns Link's login link and phrase right away; the user approves in the
    Link app, and Handshake keeps that login in the user's private Link
    directory. Stub mode: simulated, instantly connected.
    """
    if principal.is_agent:
        raise AuthError(403, "agent_not_permitted", "Only the user can connect their Link account.")
    return payments.start_link_login(principal.email)


@router.get("/link/status")
def link_status(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Whether the logged-in user's Link account is connected (the access token itself is never returned)."""
    if principal.is_agent:
        raise AuthError(403, "agent_not_permitted", "Only the user manages their Link account.")
    return payments.link_connection(principal.email)


@router.post("/link/disconnect")
def link_disconnect(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Log the user's Link account out of Handshake."""
    if principal.is_agent:
        raise AuthError(403, "agent_not_permitted", "Only the user manages their Link account.")
    payments.disconnect_link(principal.email)
    return payments.link_connection(principal.email)


@router.get("/contracts/{contract_id}/funding")
def get_funding(contract_id: str, session: Session = Depends(db.get_session), principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """The contract's funding (refreshed): awaiting_approval, funded (card stored, locked), released, used, ... Never card data."""
    return services.refresh_funding(session, contract_id, principal.email)


@router.post("/contracts/{contract_id}/funding")
def restart_funding(contract_id: str, session: Session = Depends(db.get_session), principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Fund the contract again (after a denied, expired, or used card). User only."""
    require_user(session, principal, "fund a contract", contract_id=contract_id)
    return services.restart_funding(session, contract_id, principal.email)


@router.post("/contracts/{contract_id}/revoke", response_model=Contract)
def revoke_contract(
    contract_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> Contract:
    """Revoke an active, revocable contract. User only."""
    require_user(session, principal, "revoke a contract", contract_id=contract_id)
    return services.revoke_contract(session, contract_id, owner=principal.email)


# ============================================================
# Purchases
# ============================================================


def _purchase_response(outcome: dict[str, Any]) -> dict[str, Any]:
    """
    Every purchase response: the original PurchaseResponse keys (purchase_id,
    status, decision, contract_id, proposal_id, credential, summary) PLUS the
    section 6.4 superset (purchase, proposal, payment, payment_state,
    approval_url, resolution, next_action, review_url). Never card data.
    """
    with db.SessionLocal() as session:
        purchase = db.load_purchase(session, outcome["purchase"].id) or outcome["purchase"]
        return services.purchase_detail(session, purchase, idempotent_replay=outcome.get("idempotent_replay"))


@router.post("/purchases")
def create_purchase(
    body: Any = Body(...),
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
    extractor: Extractor = Depends(get_extractor),
) -> dict[str, Any]:
    """
    Run the full purchase flow for one proposed checkout (see services.submit_purchase). User or agent.

    We validate the body ourselves instead of letting FastAPI do it, so that
    even a malformed submission against a known contract leaves an evidence
    trail (a FAILED purchase) instead of a bare 422.
    """
    try:
        submission = PurchaseSubmission.model_validate(body)
    except ValidationError as exc:
        errors = _clean_errors(exc.errors(include_url=False))
        purchase_id = services.record_malformed_submission(session, body, errors, owner=principal.email)
        details: dict[str, Any] = {"errors": errors}
        if purchase_id is not None:
            details["purchase_id"] = purchase_id
            details["status"] = "failed"
        raise ServiceError(422, "invalid_request", "Purchase submission failed validation.", details)

    if principal.is_agent and not submission.idempotency_key:
        raise ServiceError(422, "idempotency_key_required", "The agent must send an idempotency_key (a fresh random string per purchase attempt).")

    outcome = services.submit_purchase(
        session,
        contract_id=submission.contract_id,
        checkout_url=submission.checkout_url,
        selection_report=submission.selection_report,
        principal=principal,
        extractor=extractor,
        idempotency_key=submission.idempotency_key,
        submission=submission.model_dump(mode="json"),
    )
    return _purchase_response(outcome)


@router.get("/purchases")
def list_purchases(
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> list[dict[str, Any]]:
    """The caller's purchases across all contracts, newest first."""
    return [_purchase_response(item) for item in services.list_purchase_outcomes(session, principal.email)]


@router.get("/purchases/{purchase_id}")
def get_purchase(
    purchase_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
    extractor: Extractor = Depends(get_extractor),
) -> dict[str, Any]:
    """
    Current state of one purchase. Runs the payment refresh first (idempotent),
    so polling this is enough to move a purchase along.
    """
    purchase = services.get_owned_purchase(session, purchase_id, principal.email)
    purchase = _safe_refresh(session, purchase, principal, extractor)
    return services.purchase_detail(session, purchase)


def _safe_refresh(session: Session, purchase: Any, principal: Principal, extractor: Extractor) -> Any:
    """Refresh the payment, but never let a provider hiccup break a read (the error is kept on the payment)."""
    try:
        return services.refresh_payment(session, purchase.id, principal.email, extractor)
    except ServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 - a read must still answer; the next poll retries
        session.rollback()
        log.warning("payment refresh failed for %s: %s", purchase.id, type(exc).__name__)
        return db.load_purchase(session, purchase.id)


@router.post("/purchases/{purchase_id}/payment/refresh")
def refresh_payment(
    purchase_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
    extractor: Extractor = Depends(get_extractor),
) -> dict[str, Any]:
    """Poll the payment provider and advance the payment state machine (user or agent). Idempotent."""
    purchase = services.get_owned_purchase(session, purchase_id, principal.email)
    purchase = services.refresh_payment(session, purchase.id, principal.email, extractor)
    return services.purchase_detail(session, purchase)


@router.post("/purchases/{purchase_id}/approve")
def approve_purchase(
    purchase_id: str,
    body: HumanDecisionRequest | None = None,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """
    The user approves an ESCALATED purchase: it becomes AUTHORIZED and a credential is issued. User only.

    Refused with 409 for anything else (blocked, already decided, a hard
    FAIL in the decision, a stale escalation, or a contract that is no longer
    active/valid).
    """
    require_user(session, principal, "approve an escalated purchase", purchase_id=purchase_id)
    note = body.note if body else None
    return _purchase_response(services.approve_purchase(session, purchase_id, note, owner=principal.email))


@router.post("/purchases/{purchase_id}/reject")
def reject_purchase(
    purchase_id: str,
    body: HumanDecisionRequest | None = None,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """The user rejects an ESCALATED purchase: it becomes BLOCKED. 409 if it is not escalated. User only."""
    require_user(session, principal, "reject a purchase", purchase_id=purchase_id)
    note = body.note if body else None
    return _purchase_response(services.reject_purchase(session, purchase_id, note, owner=principal.email))


@router.post("/purchases/{purchase_id}/complete")
def complete_purchase(purchase_id: str, principal: Principal = Depends(current_principal)) -> None:
    """
    Internal: reconciliation is driven by the payment flow, never by a caller.

    If the agent (or anyone) could call this, it could report "the merchant
    charged the right amount" without that being true. Reconciliation now
    happens inside the backend after it verifies the merchant's order
    independently. Tests call services.complete_purchase directly.
    """
    raise ServiceError(
        403, "internal_only",
        "Purchase completion is recorded by Handshake after it verifies the merchant order; it cannot be called directly.",
    )


# ============================================================
# Evidence and health
# ============================================================


@router.get("/evidence/{purchase_id}")
def get_evidence(
    purchase_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """The whole story of one purchase in one call: contract, proposal, decision, credential, events."""
    return services.evidence_chain(session, purchase_id, owner=principal.email)


@router.get("/health")
def health() -> dict[str, Any]:
    """Liveness check that also says which database, payment rail, and modes are in use. No auth."""
    settings = get_settings()
    return {
        "status": "ok",
        "database": db.database_type(),
        "payment_mode": settings.payment_mode,
        "payment_label": settings.payment_label,
        "credential_mode": settings.credential_mode,
        "compiler_mode": "fixture" if settings.compiler_uses_fixture else "openai",
    }


# ============================================================
# Mode-specific routes (registered only in their mode)
# ============================================================

stub_router = APIRouter()


@stub_router.post("/contracts/{contract_id}/funding/simulate-approval")
def simulate_funding_approval(
    contract_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """
    SIMULATED PROVIDER APPROVAL (stub mode only, user only). Stands in for
    approving the contract's funding card in Link; the simulated card is then
    stored, encrypted, on the contract.
    """
    require_user(session, principal, "approve a payment", contract_id=contract_id)
    return services.simulate_funding_approval(session, contract_id, principal.email)


credential_router = APIRouter()


@credential_router.post("/purchases/{purchase_id}/credential")
def release_credential(
    purchase_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
    extractor: Extractor = Depends(get_extractor),
) -> JSONResponse:
    """
    AGENT-VISIBLE MODE ONLY, agent only, ONCE: the single-use Link TEST card for this purchase.

    This is the only response in Handshake that contains card values. It is
    sent with Cache-Control: no-store and is never logged or stored.
    """
    if not principal.is_agent:
        raise ServiceError(403, "agent_only", "Only the shopping agent collects the card, and only once.")
    release = services.release_credential(session, purchase_id, principal, extractor)
    return JSONResponse(
        content=release.model_dump(mode="json"),
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


# ============================================================
# App factory
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: create tables, warn about dev secrets, and say which payment rail is active."""
    db.create_tables()
    services.warn_if_dev_secret()
    log.info("Handshake backend starting. %s. Credential mode: %s.", get_settings().payment_label, get_settings().credential_mode)
    yield


def create_app() -> FastAPI:
    """Build the FastAPI app for the CURRENT settings (tests call this after overriding settings)."""
    application = FastAPI(title="Handshake backend", version="0.2", lifespan=lifespan)

    # CORS lets a browser page on another origin (Rohan's frontend, Sri's merchant
    # page) call this API. The allowed origins come from config.py: by default the
    # frontend and merchant URLs, and never a wildcard in prod (config refuses it).
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(get_settings().effective_cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.add_exception_handler(ServiceError, service_error_handler)
    application.add_exception_handler(AuthError, auth_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)

    application.include_router(router)
    settings = get_settings()
    # Simulated approval exists wherever simulated funding can happen (it refuses Link-funded cards itself).
    if settings.payment_mode in ("stub", "link_optional"):
        application.include_router(stub_router)
    if settings.credential_mode == "agent_visible":
        application.include_router(credential_router)
    return application


# The instance uvicorn serves: `uvicorn handshake.api:app`.
app = create_app()
