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

from handshake import auth, compiler, db, services
from handshake.auth import AuthError, Principal, current_principal
from handshake.config import get_settings
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
    The POST /purchases body: PurchaseRequest (from models.py) plus the extracted checkout facts.

    TODO: confirm this exact payload with Ajay (producer) and Rohan (consumer).

    `proposal` is a raw dict on purpose. If it were typed as
    TransactionProposal, FastAPI would reject a fudged total with a bare 422
    before our code ran, and nothing would be logged. As a dict, the purchase
    flow parses it itself and records a BLOCKED decision instead.
    """

    proposal: dict[str, Any]


class PurchaseResponse(PurchaseStatusResponse):
    """PurchaseStatusResponse (purchase_id, status, decision) plus the credential summary and a human sentence."""

    contract_id: str
    proposal_id: str | None = None
    credential: CredentialSummary | None = None
    summary: str


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


class DemoLoginRequest(BaseModel):
    """Body for POST /auth/demo-login. DEMO ONLY: anyone who types an email gets a token."""

    email: str


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


@router.post("/auth/demo-login")
def demo_login(body: DemoLoginRequest) -> dict[str, Any]:
    """
    DEMO AUTH: exchange an email for a signed user token. No password, no email check.

    Replace with passkeys or OAuth before any real user touches this (README, Future work).
    """
    token, expires_at = auth.issue_user_token(body.email)
    return {
        "token": token,
        "token_type": "bearer",
        "email": auth.normalize_email(body.email),
        "role": auth.USER,
        "expires_at": expires_at,
        "demo_auth": True,
    }


@router.get("/auth/me")
def whoami(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Who the current token belongs to (the frontend uses this to validate a stored session)."""
    return {"email": principal.email, "role": principal.role, "agent_id": principal.agent_id}


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
        return {
            "kind": "contract",
            "id": row.id,
            "status": row.status,
            "draft_id": row.draft_id,
            "contract": contract,
            "verification": verification,
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


@router.get("/contracts/{contract_id}/purchases", response_model=list[PurchaseResponse])
def list_contract_purchases(
    contract_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> list[PurchaseResponse]:
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
    stored = services.store_compiled_draft(session, result, owner=principal.email)
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


def _purchase_response(outcome: dict[str, Any]) -> PurchaseResponse:
    """Build the PurchaseResponse from {"purchase", "decision", "credential"}."""
    purchase = outcome["purchase"]
    decision = outcome["decision"]
    return PurchaseResponse(
        purchase_id=purchase.id,
        status=purchase.status,
        decision=decision,
        contract_id=purchase.contract_id,
        proposal_id=purchase.proposal_id,
        credential=services.credential_summary(outcome.get("credential")),
        summary=services.summarize(purchase, decision),
    )


@router.post("/purchases", response_model=PurchaseResponse)
def create_purchase(
    body: Any = Body(...),
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> PurchaseResponse:
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

    outcome = services.submit_purchase(
        session,
        contract_id=submission.contract_id,
        checkout_url=submission.checkout_url,
        selection_report=submission.selection_report,
        raw_proposal=submission.proposal,
        principal=principal,
    )
    return _purchase_response(outcome)


@router.get("/purchases", response_model=list[PurchaseResponse])
def list_purchases(
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> list[PurchaseResponse]:
    """The caller's purchases across all contracts, newest first."""
    return [_purchase_response(item) for item in services.list_purchase_outcomes(session, principal.email)]


@router.get("/purchases/{purchase_id}", response_model=PurchaseResponse)
def get_purchase(
    purchase_id: str,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> PurchaseResponse:
    """Current state of one purchase, with its decision and credential summary."""
    purchase = services.get_owned_purchase(session, purchase_id, principal.email)
    return _purchase_response(
        {
            "purchase": purchase,
            "decision": db.load_decision(session, purchase.decision_id),
            "credential": db.load_credential(session, purchase.credential_id),
        }
    )


@router.post("/purchases/{purchase_id}/approve", response_model=PurchaseResponse)
def approve_purchase(
    purchase_id: str,
    body: HumanDecisionRequest | None = None,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> PurchaseResponse:
    """
    The user approves an ESCALATED purchase: it becomes AUTHORIZED and a credential is issued. User only.

    Refused with 409 for anything else (blocked, already decided, a hard
    FAIL in the decision, a stale escalation, or a contract that is no longer
    active/valid).
    """
    require_user(session, principal, "approve an escalated purchase", purchase_id=purchase_id)
    note = body.note if body else None
    return _purchase_response(services.approve_purchase(session, purchase_id, note, owner=principal.email))


@router.post("/purchases/{purchase_id}/reject", response_model=PurchaseResponse)
def reject_purchase(
    purchase_id: str,
    body: HumanDecisionRequest | None = None,
    session: Session = Depends(db.get_session),
    principal: Principal = Depends(current_principal),
) -> PurchaseResponse:
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
    return application


# The instance uvicorn serves: `uvicorn handshake.api:app`.
app = create_app()
