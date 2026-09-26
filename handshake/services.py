"""
services.py: the business logic that ties the system together.

Job in the system
-----------------
    api.py (HTTP routes)
      -> services.py (this file)
           -> intent_diff.py  : decides PASS/FAIL/UNVERIFIABLE (pure rules)
           -> db.py           : stores everything
           -> models.py       : the shared Pydantic shapes

This file contains:
  1. Contract canonicalization, hashing, and the (demo) HMAC signature.
  2. The contract lifecycle: create draft, sign, revoke, expire.
  3. The evidence ledger writer (log_event).
  4. The stub credential broker (issue_credential).
  5. The purchase flow (submit_purchase), human approval/rejection of
     escalated purchases (approve_purchase / reject_purchase), and
     reconciliation (complete_purchase).
  6. Read models for the API (summaries and the evidence chain).

Nothing here calls an LLM. The authorization decision comes only from
intent_diff.evaluate(); this file just acts on its answer.

Errors are raised as ServiceError(status_code, code, message, details), and
api.py turns them into the standard JSON error shape.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError, model_validator
from sqlalchemy.orm import Session

from handshake import db
from handshake.config import DEV_SIGNING_SECRET, get_settings
from handshake.intent_diff import (
    as_utc,
    cents_to_amount,
    compute_outcome,
    computed_total_cents,
    decision_verdict,
    evaluate,
    explicit_fields,
    fmt_money,
    payable_total_cents,
    result,
    to_cents,
)
from handshake.models import (
    CompilerOutput,
    ConstraintResult,
    ConstraintSeverity,
    ConstraintVerdict,
    Contract,
    ContractDraft,
    ContractStatus,
    Credential,
    CredentialStatus,
    EvidenceEvent,
    EvidenceEventType,
    Purchase,
    PurchaseStatus,
    SelectionReport,
    TransactionProposal,
    ValidationDecision,
    utc_now,
)

log = logging.getLogger("handshake")

# ------------------------------------------------------------
# Signing secret
# ------------------------------------------------------------
# DEMO SIGNATURE ONLY. The "signature" is an HMAC computed with a secret that
# lives on this server. It proves the stored contract has not changed since
# the server signed it. It does NOT prove the user signed it (that would need
# a key the user holds, e.g. a passkey). Real user-held signatures are P2.
# The secret itself comes from config.py (HANDSHAKE_SIGNING_SECRET); the
# credential lifetime (HANDSHAKE_CREDENTIAL_TTL_MINUTES) does too.

# Fields left out of the contract hash:
#   status        changes over the lifecycle (active -> used/revoked/expired);
#                 revoking a contract must not "break" its hash.
#   contract_hash is the output of hashing, so it cannot be an input.
#   signature     is computed from the hash, so it cannot be an input either.
HASH_EXCLUDED_FIELDS = {"status", "contract_hash", "signature"}


def clock() -> datetime:
    """The single source of "now" for the service layer. Tests replace this to control time."""
    return utc_now()


def warn_if_dev_secret() -> None:
    """Log a loud warning at startup if the hardcoded dev signing secret is in use."""
    if get_settings().signing_secret == DEV_SIGNING_SECRET:
        banner = "!" * 72
        log.warning(
            "\n%s\n  HANDSHAKE_SIGNING_SECRET is not set. Using the hardcoded DEV secret.\n"
            "  Contract signatures are NOT secure. Set the env var before any real use.\n%s",
            banner,
            banner,
        )


class ServiceError(Exception):
    """An expected rejection (404, 409, 422, ...). api.py converts it to the standard error JSON."""

    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        """Store the HTTP status, a machine-readable code, a human message, and optional details."""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


# ============================================================
# Canonical JSON, hashing, signing
# ============================================================
#
# To detect tampering we need a fingerprint (hash) of the contract. A hash is
# only useful if the SAME contract always produces the SAME bytes. Plain
# json.dumps would not guarantee that: key order could differ, spacing could
# differ, and so on. "Canonical JSON" fixes every one of those choices:
#   - model_dump(mode="json"): datetimes become ISO 8601 strings
#   - drop status, contract_hash, signature (see HASH_EXCLUDED_FIELDS)
#   - sort_keys=True: keys always in alphabetical order
#   - separators=(",", ":"): no spaces at all
#   - ensure_ascii=False: non-English characters stay as themselves
# All datetimes are normalized to UTC at signing time (see _utc_datetimes),
# so "2026-10-10T19:59:59-04:00" and "2026-10-10T23:59:59Z" cannot produce
# two different hashes for the same instant.


def canonical_json(contract: Contract) -> str:
    """The one exact string representation of a contract that we hash."""
    data = contract.model_dump(mode="json", exclude=HASH_EXCLUDED_FIELDS)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def contract_hash(contract: Contract) -> str:
    """SHA-256 (hex) of the canonical JSON. Any change to any hashed field changes this completely."""
    return hashlib.sha256(canonical_json(contract).encode("utf-8")).hexdigest()


def sign_hash(hash_hex: str) -> str:
    """
    Demo signature: HMAC-SHA256 of the hash, keyed with HANDSHAKE_SIGNING_SECRET.

    Why HMAC and not just the hash? Anyone can recompute a SHA-256, so an
    attacker who edits the contract could also update its hash. An HMAC needs
    the secret key to compute, so without the key a matching signature
    cannot be forged.
    """
    key = get_settings().signing_secret.encode("utf-8")
    return hmac.new(key, hash_hex.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_contract(contract: Contract) -> dict[str, Any]:
    """
    Recompute the hash and signature and compare them with the stored values.

    We use hmac.compare_digest instead of ==. A normal == stops at the first
    character that differs, so it returns a tiny bit faster the earlier the
    mismatch is. By timing many guesses, an attacker could learn a valid
    signature one character at a time (a "timing attack"). compare_digest
    always takes the same time regardless of where the strings differ.
    """
    recomputed = contract_hash(contract)
    hash_ok = hmac.compare_digest(recomputed, contract.contract_hash)
    signature_ok = hmac.compare_digest(sign_hash(contract.contract_hash), contract.signature)
    return {
        "valid": hash_ok and signature_ok,
        "hash_matches": hash_ok,
        "signature_matches": signature_ok,
        "stored_hash": contract.contract_hash,
        "recomputed_hash": recomputed,
    }


def _utc_datetimes(value: Any) -> Any:
    """Walk a dict/list structure and convert every datetime to aware UTC (for a stable canonical form)."""
    if isinstance(value, datetime):
        return as_utc(value)[0]
    if isinstance(value, dict):
        return {key: _utc_datetimes(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_utc_datetimes(inner) for inner in value]
    return value


# ============================================================
# Evidence ledger writer
# ============================================================


def _jsonable(value: Any) -> Any:
    """Convert models, datetimes, sets, etc. into plain JSON-safe values for storage."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(inner) for inner in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def log_event(
    session: Session,
    contract_id: str,
    event_type: EvidenceEventType,
    message: str,
    data: dict[str, Any] | None = None,
    purchase_id: str | None = None,
) -> EvidenceEvent:
    """Append one event to the evidence ledger (db.append_evidence handles sequence and hash chain)."""
    event = EvidenceEvent(
        contract_id=contract_id,
        purchase_id=purchase_id,
        event_type=event_type,
        timestamp=clock(),
        message=message,
        data=_jsonable(data or {}),
    )
    return db.append_evidence(session, event)


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic validation errors reduced to {loc, msg, type} (no docs URLs, JSON-safe)."""
    cleaned: list[dict[str, Any]] = []
    for error in exc.errors(include_url=False):
        cleaned.append({"loc": list(error.get("loc", ())), "msg": error.get("msg"), "type": error.get("type")})
    return cleaned


# ============================================================
# Contract lifecycle
# ============================================================


def create_draft(session: Session, body: dict[str, Any]) -> dict[str, Any]:
    """Store a new draft. Accepts a bare ContractDraft or a CompilerOutput ({"draft": {...}, ...})."""
    if not isinstance(body, dict):
        raise ServiceError(422, "invalid_draft", "Request body must be a JSON object.")

    # A body with a "draft" key is Ajay's CompilerOutput wrapper; otherwise
    # it is a bare draft, which we wrap ourselves so the rest is identical.
    try:
        if "draft" in body:
            compiler = CompilerOutput.model_validate(body)
        else:
            compiler = CompilerOutput(draft=ContractDraft.model_validate(body))
    except ValidationError as exc:
        raise ServiceError(422, "invalid_draft", "Contract draft failed validation.", {"errors": _errors(exc)})
    except TypeError as exc:
        # models.py compares datetimes in its validators. A naive and an aware
        # datetime raise TypeError (not ValidationError), so catch it here.
        raise ServiceError(422, "invalid_draft", f"Contract draft could not be validated: {exc}")

    draft = compiler.draft
    if db.get_draft_row(session, draft.id) is not None:
        raise ServiceError(409, "draft_exists", f"A draft with id {draft.id!r} already exists.")

    meta = {
        "assumptions": compiler.assumptions,
        "clarifications_needed": compiler.clarifications_needed,
        "compiler_notes": compiler.compiler_notes,
    }
    db.save_draft(session, draft, meta)

    # Draft events are keyed by the draft id (there is no contract id yet).
    log_event(
        session,
        draft.id,
        EvidenceEventType.CONTRACT_CREATED,
        f"Contract draft created for goal: {draft.goal!r}.",
        {"draft": draft, **meta},
    )
    session.commit()
    return {"draft_id": draft.id, "draft": draft, **meta}


def sign_draft(
    session: Session,
    draft_id: str,
    agent_key: str | None = None,
    client_signature: str | None = None,
) -> Contract:
    """Turn a draft into an ACTIVE signed Contract with a new id, hash, and server signature."""
    row = db.get_draft_row(session, draft_id)
    if row is None:
        raise ServiceError(404, "draft_not_found", f"No contract draft with id {draft_id!r}.")
    if row.signed_contract_id is not None:
        raise ServiceError(
            409, "already_signed", "This draft has already been signed.", {"contract_id": row.signed_contract_id}
        )

    draft = db.load_draft(row)

    # Copy every draft field except its id (a signed contract gets a new
    # contract_... id), and normalize all datetimes to UTC so the canonical
    # JSON, and therefore the hash, is stable.
    fields = _utc_datetimes(draft.model_dump(exclude={"id"}))

    try:
        # Build it with placeholder hash/signature first, because the hash is
        # computed FROM the contract (minus those two fields).
        unsigned = Contract(
            **fields,
            status=ContractStatus.ACTIVE,
            agent_key=agent_key,
            signed_at=clock(),
            contract_hash="pending",
            signature="pending",
        )
    except ValidationError as exc:
        # e.g. the draft's expires_at is already in the past
        raise ServiceError(409, "draft_not_signable", "This draft cannot be signed (it may have expired).", {"errors": _errors(exc)})
    except TypeError as exc:
        raise ServiceError(409, "draft_not_signable", "This draft cannot be signed.", {"error": str(exc)})

    hash_hex = contract_hash(unsigned)
    contract = unsigned.model_copy(update={"contract_hash": hash_hex, "signature": sign_hash(hash_hex)})

    db.save_contract(session, contract, draft_id=draft_id)
    db.mark_draft_signed(session, draft_id, contract.id)

    data: dict[str, Any] = {
        "draft_id": draft_id,
        "contract_hash": contract.contract_hash,
        "signature": contract.signature,
        "signature_type": "demo_hmac_sha256",
        "agent_key": agent_key,
    }
    # If the caller sent its own signature we keep it as evidence, but OUR
    # server signature is the authoritative one used for verification.
    if client_signature is not None:
        data["client_signature"] = client_signature

    log_event(session, contract.id, EvidenceEventType.CONTRACT_SIGNED, "User signed the contract.", data)
    session.commit()
    return contract


def _require_contract(session: Session, contract_id: str) -> Contract:
    """Load a contract or raise 404; raise 409 if its stored JSON no longer parses (tampered)."""
    row = db.get_contract_row(session, contract_id)
    if row is None:
        raise ServiceError(404, "contract_not_found", f"No signed contract with id {contract_id!r}.")
    try:
        return db.contract_from_row(row)
    except ValidationError as exc:
        raise ServiceError(409, "contract_tampered", "Stored contract is no longer valid.", {"errors": _errors(exc)})


def revoke_contract(session: Session, contract_id: str) -> Contract:
    """Revoke an ACTIVE, revocable contract and revoke any of its credentials that are still usable."""
    contract = _require_contract(session, contract_id)
    if not contract.revocable:
        raise ServiceError(409, "not_revocable", "This contract was signed as non-revocable.")
    if contract.status != ContractStatus.ACTIVE:
        raise ServiceError(
            409, "contract_not_active", f"Only an active contract can be revoked; this one is {contract.status.value}."
        )

    db.update_contract_status(session, contract_id, ContractStatus.REVOKED)

    # A revoked contract must not leave a live credential behind.
    revoked_ids: list[str] = []
    for credential in db.list_credentials_for_contract(session, contract_id):
        if credential.status in (CredentialStatus.CREATED, CredentialStatus.ACTIVE):
            db.update_credential(session, credential.model_copy(update={"status": CredentialStatus.REVOKED}))
            revoked_ids.append(credential.id)

    log_event(
        session, contract_id, EvidenceEventType.CONTRACT_REVOKED,
        "User revoked the contract.", {"revoked_credentials": revoked_ids},
    )
    session.commit()
    return db.load_contract(session, contract_id)


def expire_if_needed(session: Session, contract: Contract) -> Contract:
    """If an ACTIVE contract's expires_at has passed, persist status EXPIRED and return the updated copy."""
    if contract.expires_at is None or contract.status != ContractStatus.ACTIVE:
        return contract

    expires_at = as_utc(contract.expires_at)[0]
    if expires_at > clock():
        return contract  # still valid

    db.update_contract_status(session, contract.id, ContractStatus.EXPIRED)
    session.commit()
    return contract.model_copy(update={"status": ContractStatus.EXPIRED})


# ============================================================
# Stub credential broker
# ============================================================


class CredentialSummary(BaseModel):
    """
    The ONLY credential information that leaves the backend.

    No card number, CVV, PAN, or reusable token exists in this stub, and
    even the internal provider_reference is not included here.
    """

    credential_id: str
    merchant_name: str
    merchant_id: str | None = None
    max_amount: float
    currency: str
    single_use: bool
    status: CredentialStatus
    expires_at: datetime


def credential_summary(credential: Credential | None) -> CredentialSummary | None:
    """Reduce a stored Credential to the safe summary shown to the agent and the frontend."""
    if credential is None:
        return None
    return CredentialSummary(
        credential_id=credential.id,
        merchant_name=credential.merchant_name,
        merchant_id=credential.merchant_id,
        max_amount=credential.max_amount,
        currency=credential.currency,
        single_use=credential.single_use,
        status=credential.status,
        expires_at=credential.expires_at,
    )


def issue_credential(session: Session, proposal: TransactionProposal, purchase_id: str) -> Credential:
    """
    Create a single-use stub credential for exactly the authorized amount. Only called after AUTHORIZED.

    max_amount is the checkout's payable total (e.g. 128.39), NOT the
    contract cap (135.00). If we used the cap, a merchant could later charge
    up to 135 for a 128.39 cart. Locking it to the exact authorized amount
    means the credential cannot be stretched.

    "Payable total" is the larger of the claimed total and the total computed
    from its parts (see intent_diff.payable_total_cents). It is the same
    number the cap check used, so the credential covers exactly what was
    approved: no more, and not a cent less than the real charge.

    P2: this is where a real payment / network-token sandbox would mint a
    single-use virtual card. The stub only stores a random opaque reference.
    """
    now = clock()
    credential = Credential(
        contract_id=proposal.contract_id,
        proposal_id=proposal.id,
        merchant_name=proposal.merchant.name,
        merchant_id=proposal.merchant.merchant_id,
        max_amount=cents_to_amount(payable_total_cents(proposal)),
        currency=proposal.currency.upper(),
        single_use=True,
        status=CredentialStatus.ACTIVE,
        created_at=now,
        expires_at=now + timedelta(minutes=get_settings().credential_ttl_minutes),
        provider_reference=f"stub_{secrets.token_hex(12)}",  # internal only, never returned
    )
    db.save_credential(session, credential)

    amount = fmt_money(to_cents(credential.max_amount), credential.currency)
    log_event(
        session, credential.contract_id, EvidenceEventType.CREDENTIAL_CREATED,
        f"Single-use stub credential issued for {amount} at {credential.merchant_name}.",
        {"credential": credential_summary(credential)},  # the summary, never the raw record
        purchase_id=purchase_id,
    )
    return credential


# ============================================================
# Purchase flow helpers
# ============================================================


class _UncheckedProposal(TransactionProposal):
    """
    TransactionProposal with its total-mismatch validator switched off.

    models.py's validate_total RAISES when the numbers do not add up. If we
    let that happen, the request would die as a generic 422 and we would lose
    the most interesting evidence of all: a checkout whose total was fudged.
    Pydantic replaces a parent validator when a subclass defines one with
    the same name, so this no-op lets us parse the facts anyway and then
    record a proper BLOCKED decision with a total_integrity FAIL.
    """

    @model_validator(mode="after")
    def validate_total(self) -> "_UncheckedProposal":
        """Deliberately does nothing (see the class docstring)."""
        return self


def _is_total_mismatch(exc: ValidationError) -> bool:
    """True if the ONLY problem with the payload is models.py's total-mismatch check."""
    errors = exc.errors(include_url=False)
    if not errors:
        return False
    for error in errors:
        if "Transaction total mismatch" not in str(error.get("msg", "")):
            return False
    return True


def _make_decision(contract_id: str, proposal_id: str, results: list[ConstraintResult]) -> ValidationDecision:
    """Build a ValidationDecision whose verdict always satisfies the model's own validator."""
    return ValidationDecision(
        contract_id=contract_id,
        proposal_id=proposal_id,
        verdict=decision_verdict(results),
        results=results,
        evaluated_at=clock(),
    )


def _set_purchase(session: Session, purchase: Purchase, **changes: Any) -> Purchase:
    """Return a copy of the purchase with `changes` applied, and write it to the database."""
    updated = purchase.model_copy(update=changes)
    db.update_purchase(session, updated)
    return updated


def _brief(r: ConstraintResult) -> dict[str, Any]:
    """A compact view of one result, for listing failing/unverifiable rules in events."""
    return {"constraint": r.constraint, "verdict": r.verdict.value, "severity": r.severity.value, "reason": r.reason}


def _merchant_name_from_raw(raw_proposal: Any) -> str | None:
    """Best-effort merchant name from an unparsed payload, so a rejected attempt still shows who it was for."""
    if not isinstance(raw_proposal, dict):
        return None
    merchant = raw_proposal.get("merchant")
    if isinstance(merchant, dict) and isinstance(merchant.get("name"), str):
        return merchant["name"]
    return None


def _currency_from_row(row: db.ContractRow) -> str:
    """The contract's currency from its stored JSON, falling back to USD if the JSON is damaged."""
    spend = row.data.get("spend") if isinstance(row.data, dict) else None
    if isinstance(spend, dict) and isinstance(spend.get("currency"), str):
        return spend["currency"]
    return "USD"


def _record_rejected_attempt(
    session: Session,
    row: db.ContractRow,
    status: PurchaseStatus,
    status_code: int,
    code: str,
    message: str,
    checkout_url: str | None,
    raw_payload: Any,
    extra: dict[str, Any] | None = None,
) -> ServiceError:
    """
    Record a purchase attempt that was rejected before evaluation, and return the error to raise.

    Why create a Purchase for a rejection? So that GET /evidence/{purchase_id}
    can show it like any other purchase. Without a purchase id, a
    "tampered contract" or "second purchase on a used contract" rejection
    would be invisible to the frontend, and those are some of the best
    things to demo.
    """
    now = clock()
    purchase = Purchase(
        contract_id=row.id,
        status=status,
        merchant_name=_merchant_name_from_raw(raw_payload),
        currency=_currency_from_row(row),
        created_at=now,
        completed_at=now,
        error=message,
    )
    db.save_purchase(session, purchase)

    log_event(
        session, row.id, EvidenceEventType.SHOPPING_STARTED,
        "Shopping agent submitted a checkout for validation.",
        {"checkout_url": checkout_url},
        purchase.id,
    )
    log_event(
        session, row.id, EvidenceEventType.PURCHASE_BLOCKED,
        f"Purchase attempt rejected: {message}",
        {"reason": code, "raw_payload": raw_payload, **(extra or {})},
        purchase.id,
    )
    session.commit()

    details = {"purchase_id": purchase.id, "status": status.value, **(extra or {})}
    return ServiceError(status_code, code, message, details)


# ============================================================
# Purchase flow
# ============================================================


def _load_contract_for_purchase(
    session: Session, contract_id: str, checkout_url: str, raw_proposal: Any
) -> Contract:
    """
    Step 1: load the contract and make sure it can be spent against.

    Unknown id -> plain 404 with no records (there is nothing to attach them to).
    Known contract but unusable (tampered / expired / used / revoked) ->
    a BLOCKED purchase with evidence, then 409 with its purchase_id.
    """
    row = db.get_contract_row(session, contract_id)
    if row is None:
        raise ServiceError(404, "contract_not_found", f"No signed contract with id {contract_id!r}.")

    def reject(code: str, message: str, extra: dict[str, Any]) -> ServiceError:
        """Shortcut for recording a BLOCKED attempt against this contract."""
        return _record_rejected_attempt(
            session, row, PurchaseStatus.BLOCKED, 409, code, message, checkout_url, raw_proposal, extra
        )

    # If someone edited the stored JSON into something that is not even a
    # valid Contract, that is tampering too.
    try:
        contract = db.contract_from_row(row)
    except ValidationError as exc:
        raise reject(
            "contract_tampered",
            "Stored contract is no longer a valid contract; it may have been tampered with.",
            {"errors": _errors(exc)},
        )

    contract = expire_if_needed(session, contract)

    # Re-verify the hash and signature on EVERY purchase, not just at signing.
    verification = verify_contract(contract)
    if not verification["valid"]:
        raise reject(
            "contract_tampered",
            "Stored contract no longer matches its hash/signature; it may have been tampered with.",
            {"verification": verification},
        )

    if contract.status != ContractStatus.ACTIVE:
        status = contract.status.value
        raise reject(
            f"contract_{status}",  # contract_used / contract_revoked / contract_expired
            f"Contract is {status}, not active; no purchase can be made against it.",
            {"contract_status": status},
        )

    return contract


def _parse_proposal(
    session: Session, contract: Contract, purchase: Purchase, raw: dict[str, Any]
) -> tuple[TransactionProposal, list[ConstraintResult] | None]:
    """
    Step 3: parse the raw proposal dict.

    Returns (proposal, None) normally, or (proposal, [total_integrity FAIL])
    when the only problem is that the total does not add up. Any other
    validation problem records a FAILED purchase and raises 422.
    """
    try:
        return TransactionProposal.model_validate(raw), None
    except ValidationError as exc:
        if not _is_total_mismatch(exc):
            errors = _errors(exc)
            _set_purchase(
                session, purchase, status=PurchaseStatus.FAILED, error="Proposal failed validation.", completed_at=clock()
            )
            log_event(
                session, contract.id, EvidenceEventType.PURCHASE_BLOCKED,
                "Checkout proposal was rejected: it is missing required fields or has invalid values.",
                {"reason": "proposal_invalid", "errors": errors, "raw_payload": raw},
                purchase.id,
            )
            session.commit()
            raise ServiceError(
                422, "invalid_proposal", "Checkout proposal failed validation.",
                {"errors": errors, "purchase_id": purchase.id, "status": PurchaseStatus.FAILED.value},
            )

    # The total does not add up. Parse without that check so we still have
    # the facts, then build the total_integrity result ourselves, in cents.
    proposal = _UncheckedProposal.model_validate(raw)
    components = computed_total_cents(proposal)  # subtotal + tax + shipping + fees - discounts
    claimed = to_cents(proposal.total)
    reason = (
        f"Checkout claims a total of {fmt_money(claimed, proposal.currency)}, but subtotal + tax "
        f"+ shipping + fees - discounts = {fmt_money(components, proposal.currency)}."
    )
    total_integrity = result(
        "total_integrity",
        ConstraintVerdict.FAIL,
        fmt_money(components, proposal.currency),  # expected: what it should add up to
        fmt_money(claimed, proposal.currency),  # actual: what the checkout claimed
        reason,
        {"source": "proposal.total", "difference_cents": claimed - components},
    )
    return proposal, [total_integrity]


def submit_purchase(
    session: Session,
    contract_id: str,
    checkout_url: str,
    selection_report: SelectionReport | None,
    raw_proposal: dict[str, Any],
) -> dict[str, Any]:
    """
    The full purchase flow: check contract -> record -> parse -> evaluate -> block / escalate / authorize.

    Every step writes an evidence event. Returns {"purchase", "decision",
    "credential"}; purchase.status is the effective outcome.
    """
    # ---- Step 1: is the contract usable at all? ------------------------
    contract = _load_contract_for_purchase(session, contract_id, checkout_url, raw_proposal)

    # ---- Step 2: create the purchase record ----------------------------
    purchase = Purchase(contract_id=contract.id, currency=contract.spend.currency, created_at=clock())
    db.save_purchase(session, purchase)

    shopping_data: dict[str, Any] = {"checkout_url": checkout_url, "selection_report": selection_report}
    # Informational only: the contract asked for a report on how the agent
    # chose this product, and none was supplied. We note it; we do not block.
    if contract.selection_disclosure_required and selection_report is None:
        shopping_data["selection_disclosure_missing"] = True
    log_event(
        session, contract.id, EvidenceEventType.SHOPPING_STARTED,
        "Shopping agent submitted a checkout for validation.", shopping_data, purchase.id,
    )
    session.commit()

    # ---- Step 3: parse and store the proposal --------------------------
    proposal, total_mismatch_results = _parse_proposal(session, contract, purchase, raw_proposal)

    # Record which fields were explicitly sent BEFORE anything is re-loaded
    # from the database (see intent_diff.explicit_fields for why).
    fields_set = explicit_fields(proposal)
    db.save_proposal(session, proposal, raw_proposal, fields_set, purchase.id)
    purchase = _set_purchase(session, purchase, proposal_id=proposal.id, merchant_name=proposal.merchant.name)
    log_event(
        session, contract.id, EvidenceEventType.PROPOSAL_CREATED,
        f"Checkout proposal recorded: {fmt_money(to_cents(proposal.total), proposal.currency)} at {proposal.merchant.name}.",
        {"proposal": proposal, "fields_set": sorted(fields_set)},
        purchase.id,
    )

    # ---- Step 4: run the Intent Diff ------------------------------------
    purchase = _set_purchase(session, purchase, status=PurchaseStatus.VALIDATING)
    log_event(session, contract.id, EvidenceEventType.VALIDATION_STARTED, "Intent diff started.", {}, purchase.id)

    if total_mismatch_results is not None:
        # The numbers do not add up, so nothing else about this checkout can
        # be trusted. The decision is that single hard FAIL.
        decision = _make_decision(contract.id, proposal.id, total_mismatch_results)
    elif proposal.contract_id != contract.id:
        # The facts were extracted for a DIFFERENT contract. Run the normal
        # rules for the record, but put a hard FAIL first.
        binding = result(
            "contract_binding", ConstraintVerdict.FAIL, contract.id, proposal.contract_id,
            "Checkout proposal was extracted for a different contract than the one submitted.",
            {"source": "proposal.contract_id"},
        )
        engine_decision, _ = evaluate(contract, proposal, now=clock(), raw_fields_set=fields_set)
        decision = _make_decision(contract.id, proposal.id, [binding, *engine_decision.results])
    else:
        decision, _ = evaluate(contract, proposal, now=clock(), raw_fields_set=fields_set)

    # compute_outcome is the only source of BLOCKED / ESCALATED / AUTHORIZED.
    outcome = compute_outcome(decision.results, contract.escalate_if)

    db.save_decision(session, decision, outcome.value)
    purchase = _set_purchase(session, purchase, decision_id=decision.id)

    pass_count = sum(1 for r in decision.results if r.verdict == ConstraintVerdict.PASS)
    fail_count = sum(1 for r in decision.results if r.verdict == ConstraintVerdict.FAIL)
    unverifiable_count = sum(1 for r in decision.results if r.verdict == ConstraintVerdict.UNVERIFIABLE)
    log_event(
        session, contract.id, EvidenceEventType.VALIDATION_COMPLETED,
        f"Intent diff completed: {pass_count} pass, {fail_count} fail, {unverifiable_count} unverifiable.",
        {
            "decision_id": decision.id,
            "verdict": decision.verdict.value,
            "outcome": outcome.value,
            "results": decision.results,  # every individual result, for the frontend
        },
        purchase.id,
    )
    session.commit()

    # ---- Step 5: act on the outcome -------------------------------------
    credential: Credential | None = None
    if outcome == PurchaseStatus.BLOCKED:
        purchase = _block(session, purchase, decision)
    elif outcome == PurchaseStatus.ESCALATED:
        purchase = _escalate(session, purchase, decision)
    else:
        purchase, credential = _authorize(session, contract, proposal, purchase, decision)

    # purchase.status is the effective outcome. It can differ from `outcome`
    # in one case: AUTHORIZED by the engine, but the contract was consumed by
    # a concurrent purchase a moment earlier, so we end up BLOCKED.
    return {"purchase": purchase, "decision": decision, "credential": credential}


def _block(session: Session, purchase: Purchase, decision: ValidationDecision, error: str | None = None) -> Purchase:
    """Mark the purchase BLOCKED and log which hard rules failed (or the given error)."""
    failing: list[ConstraintResult] = []
    for r in decision.results:
        if r.verdict == ConstraintVerdict.FAIL and r.severity == ConstraintSeverity.HARD:
            failing.append(r)

    purchase = _set_purchase(session, purchase, status=PurchaseStatus.BLOCKED, error=error)

    if error:
        message = error
    else:
        message = "Purchase blocked: " + " ".join(r.reason for r in failing)

    data: dict[str, Any] = {"constraints": [_brief(r) for r in failing]}
    if error:
        data["error"] = error

    log_event(session, purchase.contract_id, EvidenceEventType.PURCHASE_BLOCKED, message, data, purchase.id)
    session.commit()
    return purchase


def _escalate(session: Session, purchase: Purchase, decision: ValidationDecision) -> Purchase:
    """Mark the purchase ESCALATED and log which rules need the user's attention."""
    # Everything that did not pass and is not merely SOFT (display-only).
    flagged: list[ConstraintResult] = []
    for r in decision.results:
        if r.verdict != ConstraintVerdict.PASS and r.severity != ConstraintSeverity.SOFT:
            flagged.append(r)

    purchase = _set_purchase(session, purchase, status=PurchaseStatus.ESCALATED)

    if flagged:
        message = "Purchase escalated to the user: " + " ".join(r.reason for r in flagged)
    else:
        message = "Purchase escalated to the user: the engine evaluated no hard rules."

    log_event(
        session, purchase.contract_id, EvidenceEventType.PURCHASE_ESCALATED,
        message, {"constraints": [_brief(r) for r in flagged]}, purchase.id,
    )
    session.commit()
    return purchase


def _claim_contract_for_authorization(session: Session, contract: Contract) -> bool:
    """
    Inside the authorizing transaction: re-check the contract is ACTIVE and, if single-use, consume it.

    For single-use contracts the check and the ACTIVE -> USED flip are one
    conditional UPDATE (db.claim_single_use_contract), so two concurrent
    authorizations cannot both win. Multi-use contracts have nothing to flip,
    but we still re-read the row so a revoke that happened a moment ago counts.
    Returns False if the contract can no longer be spent against.
    """
    if contract.single_use:
        return db.claim_single_use_contract(session, contract.id)

    row = db.get_contract_row(session, contract.id)
    session.refresh(row)  # read the CURRENT status, not a cached one
    return row.status == ContractStatus.ACTIVE.value


def _grant_authorization(
    session: Session,
    contract: Contract,
    proposal: TransactionProposal,
    purchase: Purchase,
    message: str,
    extra_data: dict[str, Any] | None = None,
) -> tuple[Purchase, Credential]:
    """
    Mark the purchase AUTHORIZED, log it, and issue the credential. Does NOT commit.

    Shared by the automatic path (_authorize) and the human-approval path
    (approve_purchase) so both issue credentials in exactly the same way.
    The authorized amount is the payable total (the larger of the claimed
    and computed totals), matching what the cap check used.
    """
    payable_cents = payable_total_cents(proposal)
    authorized_amount = cents_to_amount(payable_cents)

    purchase = _set_purchase(session, purchase, status=PurchaseStatus.AUTHORIZED, authorized_amount=authorized_amount)

    data: dict[str, Any] = {
        "authorized_amount": authorized_amount,
        "claimed_total": proposal.total,
        "currency": proposal.currency,
        "contract_marked_used": contract.single_use,
    }
    if extra_data:
        data.update(extra_data)
    log_event(session, contract.id, EvidenceEventType.PURCHASE_AUTHORIZED, message, data, purchase.id)

    credential = issue_credential(session, proposal, purchase.id)
    purchase = _set_purchase(session, purchase, credential_id=credential.id)
    return purchase, credential


def _authorize(
    session: Session,
    contract: Contract,
    proposal: TransactionProposal,
    purchase: Purchase,
    decision: ValidationDecision,
) -> tuple[Purchase, Credential | None]:
    """
    Automatic authorization after an AUTHORIZED outcome, atomically with consuming a single-use contract.

    Double-spend protection: "contract is still ACTIVE -> authorize -> mark
    USED" all happens inside ONE database transaction, and the ACTIVE check
    is re-done at this moment (not trusted from step 1). If anything fails,
    the whole transaction rolls back and no credential exists.
    """
    try:
        if not _claim_contract_for_authorization(session, contract):
            session.rollback()
            error = "Contract was no longer active at authorization time (already used or revoked)."
            return _block(session, purchase, decision, error), None

        amount = fmt_money(payable_total_cents(proposal), proposal.currency)
        purchase, credential = _grant_authorization(
            session, contract, proposal, purchase, f"Purchase authorized for {amount} at {proposal.merchant.name}."
        )

        # One commit for everything above: USED flip, purchase, events, credential.
        session.commit()
        return purchase, credential
    except Exception:
        session.rollback()
        raise


# ============================================================
# Human decisions on escalated purchases
# ============================================================
#
# ESCALATED means "the engine could not prove this checkout is OK, so ask
# the user". These two functions are the user's answer.
#
# The hard line: approval can only accept UNCERTAINTY (unverifiable or
# escalating results). It can NEVER override a hard FAIL. A FAIL means the
# checkout definitely breaks the signed contract (over the cap, wrong size,
# hidden subscription, ...). If the user wants that, they must sign a new
# contract, which leaves a new signed record behind, not a one-click override.


def _load_escalated(session: Session, purchase_id: str, action: str) -> tuple[Purchase, ValidationDecision]:
    """Load a purchase and its decision, or raise 404/409 if it is not an escalated purchase."""
    purchase = db.load_purchase(session, purchase_id)
    if purchase is None:
        raise ServiceError(404, "purchase_not_found", f"No purchase with id {purchase_id!r}.")
    if purchase.status != PurchaseStatus.ESCALATED:
        raise ServiceError(
            409, "purchase_not_escalated",
            f"Purchase is {purchase.status.value}; only an escalated purchase can be {action}.",
            {"status": purchase.status.value},
        )
    decision = db.load_decision(session, purchase.decision_id)
    if decision is None:
        raise ServiceError(409, "decision_missing", "This purchase has no stored decision to review.")
    return purchase, decision


def approve_purchase(session: Session, purchase_id: str, note: str | None = None) -> dict[str, Any]:
    """
    A human approves an ESCALATED purchase: issue the credential exactly like a normal authorization.

    Refused (409) unless ALL of these hold:
      - the purchase is ESCALATED (not blocked, not already approved/rejected)
      - its decision contains zero hard FAIL results
      - the contract is still ACTIVE, not expired, and its hash/signature verify
    """
    purchase, decision = _load_escalated(session, purchase_id, "approved")

    # --- Never override a FAIL -------------------------------------------
    # compute_outcome would already have BLOCKED any hard FAIL, so this should
    # never trigger. It is here on purpose: defense in depth, in case a
    # stored decision is ever edited or the outcome logic changes.
    hard_fails = [
        r for r in decision.results
        if r.verdict == ConstraintVerdict.FAIL and r.severity == ConstraintSeverity.HARD
    ]
    if hard_fails:
        raise ServiceError(
            409, "cannot_override_fail",
            "This purchase has hard rule failures; a human approval can only accept unverifiable results, never a failure.",
            {"constraints": [_brief(r) for r in hard_fails]},
        )

    # --- The contract must still be spendable -----------------------------
    contract = _require_contract(session, purchase.contract_id)  # 404, or 409 if its JSON no longer parses
    contract = expire_if_needed(session, contract)
    verification = verify_contract(contract)
    if not verification["valid"]:
        raise ServiceError(
            409, "contract_tampered",
            "Stored contract no longer matches its hash/signature; it may have been tampered with.",
            {"verification": verification},
        )
    if contract.status != ContractStatus.ACTIVE:
        status = contract.status.value
        raise ServiceError(
            409, f"contract_{status}",
            f"Contract is {status}, not active; the purchase can no longer be approved.",
            {"contract_status": status},
        )

    # --- The facts the user is approving ---------------------------------
    proposal_row = db.get_proposal_row(session, purchase.proposal_id) if purchase.proposal_id else None
    if proposal_row is None:
        raise ServiceError(409, "proposal_missing", "This purchase has no stored checkout to approve.")
    proposal = TransactionProposal.model_validate(proposal_row.data)

    # Exactly which uncertain results the human is choosing to accept. SOFT
    # results never blocked anything, so they are not part of the approval.
    accepted = [
        _brief(r) for r in decision.results
        if r.verdict != ConstraintVerdict.PASS and r.severity != ConstraintSeverity.SOFT
    ]

    # --- One atomic transaction -------------------------------------------
    # 1. ESCALATED -> AUTHORIZED on the purchase (conditional UPDATE), so a
    #    double click or two browser tabs cannot approve twice.
    # 2. Consume the single-use contract (the same conditional UPDATE used by
    #    automatic authorization), so an approval cannot double-spend either.
    # 3. Log the human approval and issue the credential.
    # If step 1 or 2 loses a race, everything rolls back and nothing is issued.
    try:
        won_purchase = db.claim_purchase_status(
            session, purchase.id, PurchaseStatus.ESCALATED.value, PurchaseStatus.AUTHORIZED.value
        )
        if not won_purchase:
            session.rollback()
            raise ServiceError(409, "purchase_not_escalated", "This purchase was already approved or rejected.")

        if not _claim_contract_for_authorization(session, contract):
            session.rollback()
            raise ServiceError(
                409, "contract_not_active",
                "Contract was no longer active at approval time (already used or revoked).",
            )

        amount = fmt_money(payable_total_cents(proposal), proposal.currency)
        accepted_names = ", ".join(item["constraint"] for item in accepted) or "none"
        purchase, credential = _grant_authorization(
            session, contract, proposal, purchase,
            f"User approved the escalated purchase for {amount} at {proposal.merchant.name}, "
            f"accepting: {accepted_names}.",
            {"human_approval": True, "accepted_constraints": accepted, "note": note},
        )
        session.commit()
    except ServiceError:
        raise
    except Exception:
        session.rollback()
        raise

    return {"purchase": purchase, "decision": decision, "credential": credential}


def reject_purchase(session: Session, purchase_id: str, note: str | None = None) -> dict[str, Any]:
    """A human rejects an ESCALATED purchase: it becomes BLOCKED and no credential is ever issued."""
    purchase, decision = _load_escalated(session, purchase_id, "rejected")

    # Same conditional UPDATE as approval, so approve and reject racing each
    # other cannot both succeed: whichever runs first wins, the other gets 409.
    won = db.claim_purchase_status(session, purchase.id, PurchaseStatus.ESCALATED.value, PurchaseStatus.BLOCKED.value)
    if not won:
        session.rollback()
        raise ServiceError(409, "purchase_not_escalated", "This purchase was already approved or rejected.")

    error = "Rejected by the user after escalation."
    purchase = _set_purchase(session, purchase, status=PurchaseStatus.BLOCKED, error=error, completed_at=clock())
    log_event(
        session, purchase.contract_id, EvidenceEventType.PURCHASE_BLOCKED,
        "User rejected the escalated purchase.",
        {"human_rejection": True, "note": note},
        purchase.id,
    )
    session.commit()
    return {"purchase": purchase, "decision": decision, "credential": None}


def record_malformed_submission(session: Session, body: Any, errors: list[dict[str, Any]]) -> str | None:
    """
    Record a POST /purchases body that did not even match PurchaseSubmission.

    If the body names a known contract, create a FAILED purchase with evidence
    and return its id; otherwise return None (nothing to attach records to).
    """
    if not isinstance(body, dict) or not isinstance(body.get("contract_id"), str):
        return None
    row = db.get_contract_row(session, body["contract_id"])
    if row is None:
        return None

    checkout_url = body.get("checkout_url") if isinstance(body.get("checkout_url"), str) else None
    error = _record_rejected_attempt(
        session, row, PurchaseStatus.FAILED, 422, "invalid_request",
        "Purchase submission failed validation.", checkout_url, body, {"errors": errors},
    )
    return error.details["purchase_id"]


# ============================================================
# Reconciliation
# ============================================================


def complete_purchase(session: Session, purchase_id: str, charged_amount: float) -> dict[str, Any]:
    """Compare what the merchant actually charged with what was authorized, and close the purchase."""
    purchase = db.load_purchase(session, purchase_id)
    if purchase is None:
        raise ServiceError(404, "purchase_not_found", f"No purchase with id {purchase_id!r}.")
    if purchase.status != PurchaseStatus.AUTHORIZED:
        raise ServiceError(
            409, "purchase_not_authorized", f"Purchase is {purchase.status.value}; only an authorized purchase can be completed."
        )

    credential = db.load_credential(session, purchase.credential_id)
    if credential is None or credential.status != CredentialStatus.ACTIVE:
        raise ServiceError(409, "credential_not_active", "The credential for this purchase is not active.")

    try:
        charged_cents = to_cents(charged_amount)
    except ValueError:
        raise ServiceError(422, "invalid_amount", f"charged_amount {charged_amount!r} is not a valid amount.")
    if charged_cents < 0:
        raise ServiceError(422, "invalid_amount", "charged_amount cannot be negative.")

    authorized_cents = to_cents(purchase.authorized_amount)
    now = clock()
    amounts = {
        "charged": fmt_money(charged_cents, purchase.currency),
        "authorized": fmt_money(authorized_cents, purchase.currency),
    }

    # An expired credential cannot be used, even for the right amount.
    if as_utc(credential.expires_at)[0] <= now:
        db.update_credential(session, credential.model_copy(update={"status": CredentialStatus.EXPIRED}))
        _set_purchase(
            session, purchase, status=PurchaseStatus.FAILED, charged_amount=charged_amount,
            error="Credential expired before the charge.", completed_at=now,
        )
        log_event(
            session, purchase.contract_id, EvidenceEventType.PAYMENT_MISMATCH,
            "Charge attempted after the credential expired.", amounts, purchase.id,
        )
        session.commit()
        raise ServiceError(409, "credential_expired", "The single-use credential expired before the charge was made.")

    # Integer cents again: 12840 > 12839 is exact, no float fuzz.
    if charged_cents > authorized_cents:
        credential = credential.model_copy(update={"status": CredentialStatus.REVOKED})
        db.update_credential(session, credential)
        purchase = _set_purchase(
            session, purchase, status=PurchaseStatus.FAILED, charged_amount=charged_amount, completed_at=now,
            error=f"Charged {amounts['charged']} exceeds authorized {amounts['authorized']}.",
        )
        log_event(
            session, purchase.contract_id, EvidenceEventType.PAYMENT_MISMATCH,
            f"Merchant tried to charge {amounts['charged']}, more than the authorized {amounts['authorized']}. Credential revoked.",
            amounts, purchase.id,
        )
    else:
        credential = credential.model_copy(update={"status": CredentialStatus.USED})
        db.update_credential(session, credential)
        purchase = _set_purchase(
            session, purchase, status=PurchaseStatus.COMPLETED, charged_amount=charged_amount, completed_at=now
        )
        log_event(session, purchase.contract_id, EvidenceEventType.CREDENTIAL_USED, f"Credential {credential.id} used once.", amounts, purchase.id)
        log_event(session, purchase.contract_id, EvidenceEventType.PAYMENT_COMPLETED, f"Payment of {amounts['charged']} completed.", amounts, purchase.id)

    session.commit()
    return {"purchase": purchase, "decision": db.load_decision(session, purchase.decision_id), "credential": credential}


# ============================================================
# Read models for the API
# ============================================================


def summarize(purchase: Purchase, decision: ValidationDecision | None) -> str:
    """One human sentence describing where a purchase stands, for the frontend."""
    status = purchase.status
    results = decision.results if decision else []

    if status == PurchaseStatus.AUTHORIZED:
        amount = fmt_money(to_cents(purchase.authorized_amount), purchase.currency)
        return f"Authorized {amount} at {purchase.merchant_name}. A single-use credential was issued."

    if status == PurchaseStatus.COMPLETED:
        charged = fmt_money(to_cents(purchase.charged_amount), purchase.currency)
        return f"Completed: charged {charged} at {purchase.merchant_name}."

    if status == PurchaseStatus.FAILED:
        return f"Failed: {purchase.error}"

    if status == PurchaseStatus.BLOCKED:
        failing = [r.reason for r in results if r.verdict == ConstraintVerdict.FAIL and r.severity == ConstraintSeverity.HARD]
        if failing:
            return "Blocked: " + " ".join(failing)
        return "Blocked: " + (purchase.error or "a hard rule failed.")

    if status == PurchaseStatus.ESCALATED:
        flagged = [r.reason for r in results if r.verdict != ConstraintVerdict.PASS and r.severity != ConstraintSeverity.SOFT]
        if flagged:
            return "Needs your approval: " + " ".join(flagged)
        return "Needs your approval: no hard rules were evaluated."

    return f"Purchase is {status.value}."


CONTRACT_LEVEL_EVENTS = {
    EvidenceEventType.CONTRACT_CREATED.value,
    EvidenceEventType.CONTRACT_SIGNED.value,
    EvidenceEventType.CONTRACT_REVOKED.value,
}


def evidence_chain(session: Session, purchase_id: str) -> dict[str, Any]:
    """Everything the frontend needs to tell one purchase's story, in a single response."""
    purchase = db.load_purchase(session, purchase_id)
    if purchase is None:
        raise ServiceError(404, "purchase_not_found", f"No purchase with id {purchase_id!r}.")

    contract_row = db.get_contract_row(session, purchase.contract_id)

    # A tampered contract might not even parse; show its raw JSON instead of crashing.
    try:
        contract: Contract | dict[str, Any] = db.contract_from_row(contract_row)
        verification = verify_contract(contract)
    except ValidationError as exc:
        contract = contract_row.data
        verification = {"valid": False, "errors": _errors(exc)}

    # Collect the events: contract-level ones (draft created, signed,
    # revoked), then everything attached to this purchase. Contract events
    # for the draft are stored under the draft id, so we look there too.
    rows_by_id: dict[str, db.EvidenceRow] = {}
    for row in db.list_evidence_for_contract(session, contract_row.id):
        if row.event_type in CONTRACT_LEVEL_EVENTS and row.purchase_id is None:
            rows_by_id[row.id] = row
    if contract_row.draft_id:
        for row in db.list_evidence_for_contract(session, contract_row.draft_id):
            if row.event_type in CONTRACT_LEVEL_EVENTS:
                rows_by_id[row.id] = row
    for row in db.list_evidence_for_purchase(session, purchase_id):
        rows_by_id[row.id] = row

    # `sequence` is strictly increasing, so sorting by it gives the true order.
    ordered_rows = sorted(rows_by_id.values(), key=lambda row: row.sequence)
    events = [db.event_from_row(row) for row in ordered_rows]

    proposal_row = db.get_proposal_row(session, purchase.proposal_id) if purchase.proposal_id else None
    decision = db.load_decision(session, purchase.decision_id)
    credential = db.load_credential(session, purchase.credential_id)

    return {
        "purchase": purchase,
        "status": purchase.status,
        "summary": summarize(purchase, decision),
        "contract": contract,
        "contract_verification": verification,
        "proposal": proposal_row.data if proposal_row else None,
        "proposal_raw_payload": proposal_row.raw_payload if proposal_row else None,
        "decision": decision,
        "credential": credential_summary(credential),  # never the raw credential
        "ledger_intact": db.verify_evidence_chain(session, contract_row.id),
        "events": events,
    }
