"""
api.py: the FastAPI app (HTTP layer). It was backend/app/main.py before the integration.

Job in the system
-----------------
This is the front door. It defines the URLs, parses request bodies, and
turns results into JSON. It contains no business rules of its own:

    HTTP request -> api.py route -> services.py -> intent_diff.py / db.py
                                  <- result or ServiceError
    HTTP response (JSON)

It also defines the few backend-only request/response shapes that models.py
does not have (PurchaseSubmission, PurchaseResponse, ...). models.py itself
is shared with the team and is never edited.

Every error response has one shape, so the frontend can handle all of them
the same way:
    {"error": "<code>", "message": "<human sentence>", "details": {...}}
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from handshake import db, services
from handshake.config import get_settings
from handshake.models import (
    Contract,
    PurchaseRequest,
    PurchaseStatusResponse,
    SignContractRequest,
)
from handshake.services import CredentialSummary, ServiceError

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: create tables and warn if the dev signing secret is in use."""
    db.create_tables()
    services.warn_if_dev_secret()
    yield


app = FastAPI(title="Handshake backend", version="0.2", lifespan=lifespan)

# CORS lets a browser page on another origin (Rohan's frontend, Sri's merchant
# page) call this API. The allowed origins come from config.py: by default the
# frontend and merchant URLs, and never a wildcard in prod (config refuses it).
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().effective_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """Turn any ServiceError raised by services.py into the standard error JSON."""
    return error_response(exc.status_code, exc.code, exc.message, services._jsonable(exc.details))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Give FastAPI's own body-validation failures the same error shape as everything else."""
    return error_response(422, "invalid_request", "Request body failed validation.", {"errors": _clean_errors(exc.errors())})


# ============================================================
# Contracts
# ============================================================


@app.post("/contracts", status_code=201)
def create_contract(body: Any = Body(...), session: Session = Depends(db.get_session)) -> dict[str, Any]:
    """Create a draft from a ContractDraft or a CompilerOutput ({"draft": {...}, "assumptions": [...], ...})."""
    return services.create_draft(session, body)


@app.get("/contracts", response_model=list[ContractListItem])
def list_contracts(session: Session = Depends(db.get_session)) -> list[ContractListItem]:
    """List every draft and signed contract, newest first."""
    items: list[ContractListItem] = []

    for row in db.list_drafts(session):
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

    for row in db.list_contracts(session):
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


@app.get("/contracts/{contract_id}")
def get_contract(contract_id: str, session: Session = Depends(db.get_session)) -> dict[str, Any]:
    """Return a signed contract (with a live hash/signature check) or a draft (with compiler metadata)."""
    row = db.get_contract_row(session, contract_id)
    if row is not None:
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
    if draft_row is not None:
        return {
            "kind": "draft",
            "id": draft_row.id,
            "status": "draft",
            "signed_contract_id": draft_row.signed_contract_id,
            "draft": db.load_draft(draft_row),
            "verification": None,
            **draft_row.meta,  # assumptions, clarifications_needed, compiler_notes
        }

    raise ServiceError(404, "contract_not_found", f"No contract or draft with id {contract_id!r}.")


@app.post("/contracts/{draft_id}/sign", response_model=Contract)
def sign_contract(
    draft_id: str,
    body: SignContractRequest | None = None,
    session: Session = Depends(db.get_session),
) -> Contract:
    """Sign a draft. The body is optional; if it names a draft_id, that must match the path."""
    if body is not None and body.draft_id and body.draft_id != draft_id:
        raise ServiceError(400, "draft_id_mismatch", "draft_id in the body does not match the draft id in the path.")

    agent_key = body.agent_key if body else None
    client_signature = body.signature if body else None
    return services.sign_draft(session, draft_id, agent_key=agent_key, client_signature=client_signature)


@app.post("/contracts/{contract_id}/revoke", response_model=Contract)
def revoke_contract(contract_id: str, session: Session = Depends(db.get_session)) -> Contract:
    """Revoke an active, revocable contract."""
    return services.revoke_contract(session, contract_id)


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


@app.post("/purchases", response_model=PurchaseResponse)
def create_purchase(body: Any = Body(...), session: Session = Depends(db.get_session)) -> PurchaseResponse:
    """
    Run the full purchase flow for one proposed checkout (see services.submit_purchase).

    We validate the body ourselves instead of letting FastAPI do it, so that
    even a malformed submission against a known contract leaves an evidence
    trail (a FAILED purchase) instead of a bare 422.
    """
    try:
        submission = PurchaseSubmission.model_validate(body)
    except ValidationError as exc:
        errors = _clean_errors(exc.errors(include_url=False))
        purchase_id = services.record_malformed_submission(session, body, errors)
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
    )
    return _purchase_response(outcome)


@app.get("/purchases/{purchase_id}", response_model=PurchaseResponse)
def get_purchase(purchase_id: str, session: Session = Depends(db.get_session)) -> PurchaseResponse:
    """Current state of one purchase, with its decision and credential summary."""
    purchase = db.load_purchase(session, purchase_id)
    if purchase is None:
        raise ServiceError(404, "purchase_not_found", f"No purchase with id {purchase_id!r}.")

    return _purchase_response(
        {
            "purchase": purchase,
            "decision": db.load_decision(session, purchase.decision_id),
            "credential": db.load_credential(session, purchase.credential_id),
        }
    )


@app.post("/purchases/{purchase_id}/approve", response_model=PurchaseResponse)
def approve_purchase(
    purchase_id: str,
    body: HumanDecisionRequest | None = None,
    session: Session = Depends(db.get_session),
) -> PurchaseResponse:
    """
    The user approves an ESCALATED purchase: it becomes AUTHORIZED and a credential is issued.

    Refused with 409 for anything else (blocked, already decided, a hard
    FAIL in the decision, or a contract that is no longer active/valid).
    """
    note = body.note if body else None
    return _purchase_response(services.approve_purchase(session, purchase_id, note))


@app.post("/purchases/{purchase_id}/reject", response_model=PurchaseResponse)
def reject_purchase(
    purchase_id: str,
    body: HumanDecisionRequest | None = None,
    session: Session = Depends(db.get_session),
) -> PurchaseResponse:
    """The user rejects an ESCALATED purchase: it becomes BLOCKED. 409 if it is not escalated."""
    note = body.note if body else None
    return _purchase_response(services.reject_purchase(session, purchase_id, note))


@app.post("/purchases/{purchase_id}/complete", response_model=PurchaseResponse)
def complete_purchase(
    purchase_id: str, body: CompleteRequest, session: Session = Depends(db.get_session)
) -> PurchaseResponse:
    """Reconcile the actual charge against the authorized amount."""
    return _purchase_response(services.complete_purchase(session, purchase_id, body.charged_amount))


# ============================================================
# Evidence and health
# ============================================================


@app.get("/evidence/{purchase_id}")
def get_evidence(purchase_id: str, session: Session = Depends(db.get_session)) -> dict[str, Any]:
    """The whole story of one purchase in one call: contract, proposal, decision, credential, events."""
    return services.evidence_chain(session, purchase_id)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check that also says which database is in use."""
    return {"status": "ok", "database": db.database_type()}
