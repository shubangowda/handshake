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
import asyncio
from decimal import Decimal
import concurrent.futures
import secrets
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from handshake import compiler, db, payments
from handshake.extractor import Extractor, ExtractionError
from handshake.auth import Principal
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
    generate_id,
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
# Ownership and role checks
# ============================================================
#
# Every draft, contract, and purchase row has an `owner` (a user email).
# Public service functions take the caller's owner email and refuse to touch
# a record owned by someone else. A foreign record gets the SAME 404 as a
# missing one, so a caller can't learn which ids exist by probing.
# Passing owner=None means "internal call" (tests and the payment executor)
# and skips the check.


def _check_owner(row_owner: str | None, owner: str | None, code: str, message: str) -> None:
    """Raise a 404 ServiceError unless `owner` is None (internal) or matches the row's owner."""
    if owner is not None and row_owner != owner:
        raise ServiceError(404, code, message)


def get_owned_purchase(session: Session, purchase_id: str, owner: str | None) -> Purchase:
    """Load a purchase the caller owns, or raise 404."""
    row = session.get(db.PurchaseRow, purchase_id)
    message = f"No purchase with id {purchase_id!r}."
    if row is None:
        raise ServiceError(404, "purchase_not_found", message)
    _check_owner(row.owner, owner, "purchase_not_found", message)
    return Purchase.model_validate(row.data)


def deny_agent_action(
    session: Session,
    principal: Principal,
    action: str,
    *,
    draft_id: str | None = None,
    contract_id: str | None = None,
    purchase_id: str | None = None,
) -> ServiceError:
    """
    Record that the AGENT tried a user-only action, and return the 403 to raise.

    Signing, editing drafts, amending, revoking, approving, rejecting, and
    simulating payment approval are the user's decisions. The agent proposing
    a purchase must never be able to approve it, and that is enforced HERE, in
    backend code, not by prompt wording. The attempt is written to the evidence
    ledger of the record it targeted, so the user can see that it happened.

    The record must exist and belong to the agent's owner; otherwise the
    caller gets the ordinary 404 (no evidence is written for foreign ids).
    """
    ledger_id: str | None = None
    if purchase_id is not None:
        purchase = get_owned_purchase(session, purchase_id, principal.email)
        ledger_id = purchase.contract_id
    elif contract_id is not None:
        row = db.get_contract_row(session, contract_id)
        message = f"No signed contract with id {contract_id!r}."
        if row is None:
            raise ServiceError(404, "contract_not_found", message)
        _check_owner(row.owner, principal.email, "contract_not_found", message)
        ledger_id = contract_id
    elif draft_id is not None:
        row = db.get_draft_row(session, draft_id)
        message = f"No contract draft with id {draft_id!r}."
        if row is None:
            raise ServiceError(404, "draft_not_found", message)
        _check_owner(row.owner, principal.email, "draft_not_found", message)
        ledger_id = draft_id

    message = f"The shopping agent is not permitted to {action}; only the user can."
    if ledger_id is not None:
        # models.py has no event type for "denied action", so we use the
        # closest one and put the precise meaning in data.kind.
        log_event(
            session, ledger_id, EvidenceEventType.PURCHASE_BLOCKED,
            f"Refused: the agent tried to {action}.",
            {"kind": "agent_action_denied", "action": action, "actor": principal.label},
            purchase_id,
        )
        session.commit()
    return ServiceError(403, "agent_not_permitted", message, {"action": action})


# ============================================================
# Contract lifecycle
# ============================================================


def create_draft(session: Session, body: dict[str, Any], owner: str) -> dict[str, Any]:
    """Store a new draft owned by `owner`. Accepts a bare ContractDraft or a CompilerOutput ({"draft": {...}, ...})."""
    if not isinstance(body, dict):
        raise ServiceError(422, "invalid_draft", "Request body must be a JSON object.")

    # A body with a "draft" key is Ajay's CompilerOutput wrapper; otherwise
    # it is a bare draft, which we wrap ourselves so the rest is identical.
    try:
        if "draft" in body:
            output = CompilerOutput.model_validate(body)
        else:
            output = CompilerOutput(draft=ContractDraft.model_validate(body))
    except ValidationError as exc:
        raise ServiceError(422, "invalid_draft", "Contract draft failed validation.", {"errors": _errors(exc)})
    except TypeError as exc:
        # models.py compares datetimes in its validators. A naive and an aware
        # datetime raise TypeError (not ValidationError), so catch it here.
        raise ServiceError(422, "invalid_draft", f"Contract draft could not be validated: {exc}")

    if db.get_draft_row(session, output.draft.id) is not None:
        raise ServiceError(409, "draft_exists", f"A draft with id {output.draft.id!r} already exists.")

    return _store_draft(session, output, owner, source="posted")


def store_compiled_draft(session: Session, result: "compiler.CompileResult", owner: str) -> dict[str, Any]:
    """Persist a successful compile (the compiler already set the id and created_at server-side)."""
    return _store_draft(session, result.output, owner, source=result.source)


def _store_draft(
    session: Session,
    output: CompilerOutput,
    owner: str,
    source: str,
    previous_contract_id: str | None = None,
) -> dict[str, Any]:
    """
    Lint, persist, and log a draft. Every path that creates a draft goes through here.

    Lint runs BEFORE storing, so a draft is never saved without its blocking
    issues attached (they show up in clarifications_needed with a 'lint: '
    prefix, and sign_draft refuses the draft until they are resolved).
    """
    report = compiler.lint_draft(output.draft, clock())
    draft = report.draft  # naive datetimes were given the user's timezone
    meta = compiler.merge_lint(
        {
            "assumptions": list(output.assumptions),
            "clarifications_needed": list(output.clarifications_needed),
            "compiler_notes": list(output.compiler_notes),
        },
        report,
    )
    db.save_draft(session, draft, meta, owner=owner, previous_contract_id=previous_contract_id)

    # Draft events are keyed by the draft id (there is no contract id yet).
    log_event(
        session,
        draft.id,
        EvidenceEventType.CONTRACT_CREATED,
        f"Contract draft created for goal: {draft.goal!r}.",
        {"draft": draft, "source": source, "previous_contract_id": previous_contract_id, **meta},
    )
    session.commit()
    record = draft_record(db.get_draft_row(session, draft.id))
    # The original POST /contracts response keys, kept for compatibility...
    return {"draft_id": draft.id, "draft": draft, **meta, **{k: record[k] for k in ("review_url", "blocking_issues", "previous_contract_id")}, "record": record}


# ============================================================
# Draft records (Rohan's DraftRecord shape)
# ============================================================


def review_url_for(record_id: str) -> str:
    """Where the user reviews a draft or contract in the frontend (from config, never hardcoded)."""
    return f"{get_settings().frontend_url}/contracts/{record_id}"


def purchase_url_for(purchase_id: str) -> str:
    """Where the user sees a purchase in the frontend."""
    return f"{get_settings().frontend_url}/purchases/{purchase_id}"


def blocking_issues(meta: dict[str, Any]) -> list[str]:
    """The lint errors that currently prevent signing (without their 'lint: ' prefix)."""
    return [c[len(compiler.LINT_PREFIX):] for c in meta.get("clarifications_needed", []) if c.startswith(compiler.LINT_PREFIX)]


def draft_record(row: db.DraftRow) -> dict[str, Any]:
    """
    A draft as the frontend's DraftRecord: every ContractDraft field, flattened,
    plus status "draft", the compiler metadata, and previous_contract_id.

    API-only keys (not in models.py): status, assumptions, clarifications_needed,
    compiler_notes, previous_contract_id, signed_contract_id, review_url,
    blocking_issues.
    """
    draft = db.load_draft(row)
    return {
        **draft.model_dump(mode="json"),
        "status": "draft",
        "assumptions": row.meta.get("assumptions", []),
        "clarifications_needed": row.meta.get("clarifications_needed", []),
        "compiler_notes": row.meta.get("compiler_notes", []),
        "previous_contract_id": row.previous_contract_id,
        "signed_contract_id": row.signed_contract_id,
        "review_url": review_url_for(row.id),
        "blocking_issues": blocking_issues(row.meta),
    }


def _owned_draft_row(session: Session, draft_id: str, owner: str | None) -> db.DraftRow:
    """Load a draft row the caller owns, or raise 404."""
    row = db.get_draft_row(session, draft_id)
    missing = f"No contract draft with id {draft_id!r}."
    if row is None:
        raise ServiceError(404, "draft_not_found", missing)
    _check_owner(row.owner, owner, "draft_not_found", missing)
    return row


def list_draft_records(session: Session, owner: str) -> list[dict[str, Any]]:
    """The caller's drafts, newest first, as DraftRecords."""
    return [draft_record(row) for row in db.list_drafts(session, owner=owner)]


def get_draft_record(session: Session, draft_id: str, owner: str | None) -> dict[str, Any]:
    """One draft as a DraftRecord (404 if missing or foreign)."""
    return draft_record(_owned_draft_row(session, draft_id, owner))


# The editable subset of a draft (Rohan's DraftPatch).
PATCHABLE_KEYS = {"goal", "target", "hard_cap_all_in", "max_shipping", "deliver_by", "constraints"}


def patch_draft(session: Session, draft_id: str, patch: dict[str, Any], owner: str | None) -> dict[str, Any]:
    """
    Apply the user's edits to an unsigned draft, re-validate, and re-lint. User only.

    Any value the user edits becomes source=USER: the user has now stated it
    themselves, so the review screen stops showing it as "inferred". A signed
    draft can't be edited (that would change what was signed); amend the
    contract instead, which creates a new draft.
    """
    row = _owned_draft_row(session, draft_id, owner)
    if row.signed_contract_id is not None:
        raise ServiceError(409, "already_signed", "This draft was already signed. Amend the contract to change it.", {"contract_id": row.signed_contract_id})

    unknown = set(patch) - PATCHABLE_KEYS
    if unknown:
        raise ServiceError(422, "invalid_patch", f"These fields can't be edited here: {', '.join(sorted(unknown))}.")

    data = db.load_draft(row).model_dump(mode="json")
    changed: list[str] = []

    if "goal" in patch and patch["goal"] != data["goal"]:
        data["goal"] = patch["goal"]
        changed.append("goal")
    if "target" in patch and patch["target"] != data["spend"]["target"]:
        data["spend"]["target"] = patch["target"]
        data["spend"]["target_source"] = "user"
        changed.append("target")
    if "hard_cap_all_in" in patch and patch["hard_cap_all_in"] != data["spend"]["hard_cap_all_in"]:
        data["spend"]["hard_cap_all_in"] = patch["hard_cap_all_in"]
        data["spend"]["hard_cap_source"] = "user"
        changed.append("hard_cap_all_in")

    # Delivery edits create the delivery policy if the draft had none.
    if "max_shipping" in patch or "deliver_by" in patch:
        delivery = data.get("delivery") or {}
        if "max_shipping" in patch and patch["max_shipping"] != delivery.get("max_shipping"):
            delivery["max_shipping"] = patch["max_shipping"]
            delivery["max_shipping_source"] = "user"
            changed.append("max_shipping")
        if "deliver_by" in patch and patch["deliver_by"] != delivery.get("deliver_by"):
            delivery["deliver_by"] = patch["deliver_by"]
            delivery["deliver_by_source"] = "user"
            changed.append("deliver_by")
        data["delivery"] = delivery

    if "constraints" in patch:
        old = {(c["field"], c["operator"], repr(c["value"]), c["severity"]) for c in data["constraints"]}
        new_constraints = []
        for item in patch["constraints"] or []:
            if not isinstance(item, dict):
                raise ServiceError(422, "invalid_patch", "Each constraint must be an object.")
            constraint = dict(item)
            key = (constraint.get("field"), constraint.get("operator"), repr(constraint.get("value")), constraint.get("severity", "hard"))
            if key not in old:
                constraint["source"] = "user"  # new or changed by the user
            new_constraints.append(constraint)
        data["constraints"] = new_constraints
        changed.append("constraints")

    try:
        draft = ContractDraft.model_validate(data)
    except ValidationError as exc:
        raise ServiceError(422, "invalid_patch", "The edited draft is not valid.", {"errors": _errors(exc)})
    except TypeError as exc:
        raise ServiceError(422, "invalid_patch", f"The edited draft is not valid: {exc}")

    report = compiler.lint_draft(draft, clock())
    meta = compiler.merge_lint(dict(row.meta), report)
    db.update_draft(session, report.draft, meta)
    log_event(
        session, draft_id, EvidenceEventType.CONTRACT_CREATED,
        f"Draft edited by the user: {', '.join(changed) or 'no changes'}.",
        {"kind": "draft_edited", "changed": changed, "blocking_issues": report.errors},
    )
    session.commit()
    return draft_record(db.get_draft_row(session, draft_id))


def amend_contract(session: Session, contract_id: str, owner: str | None) -> dict[str, Any]:
    """
    Start a new version of a signed contract: a new DRAFT copied from it, pointing back at it. User only.

    Nothing changes for the old contract yet. Signing the new draft revokes
    the old version atomically (sign_draft). Until then, the old one stays in force.
    """
    contract = _require_contract(session, contract_id, owner)
    row = db.get_contract_row(session, contract_id)
    if contract.status == ContractStatus.ACTIVE and not contract.revocable:
        raise ServiceError(409, "not_revocable", "This contract was signed as non-revocable, so it can't be replaced.")

    # Copy every field ContractDraft has; drop the signed-only ones.
    signed_only = {"id", "status", "agent_key", "signed_at", "contract_hash", "signature", "previous_contract_id", "created_at"}
    data = contract.model_dump(mode="json", exclude=signed_only)
    data["id"] = generate_id("draft")
    data["created_at"] = clock().isoformat()
    draft = ContractDraft.model_validate(data)

    result = _store_draft(
        session,
        CompilerOutput(draft=draft, compiler_notes=[f"Amendment of contract {contract_id}."]),
        row.owner,
        source="amend",
        previous_contract_id=contract_id,
    )
    log_event(
        session, contract_id, EvidenceEventType.CONTRACT_CREATED,
        f"A new version was started as draft {draft.id}. This contract stays in force until that draft is signed.",
        {"kind": "contract_amended", "new_draft_id": draft.id},
    )
    session.commit()
    return result["record"]


def sign_draft(
    session: Session,
    draft_id: str,
    owner: str | None,
    agent_key: str | None = None,
    client_signature: str | None = None,
) -> Contract:
    """
    Turn a draft into an ACTIVE signed Contract with a new id, hash, and server signature.

    Only the user who owns the draft may sign it (api.py refuses agents before
    this is called). The contract is bound to one agent via agent_key: if the
    caller names none, it binds to the configured HANDSHAKE_AGENT_ID, so a
    contract is never usable by "any agent".
    """
    row = db.get_draft_row(session, draft_id)
    missing = f"No contract draft with id {draft_id!r}."
    if row is None:
        raise ServiceError(404, "draft_not_found", missing)
    _check_owner(row.owner, owner, "draft_not_found", missing)
    if not agent_key:
        agent_key = get_settings().agent_id
    if row.signed_contract_id is not None:
        raise ServiceError(
            409, "already_signed", "This draft has already been signed.", {"contract_id": row.signed_contract_id}
        )

    draft = db.load_draft(row)

    # --- Lint again, NOW -----------------------------------------------------
    # The draft may have been fine when compiled but not anymore (its
    # delivery deadline passed overnight), or the user may not have resolved
    # an earlier problem. A draft with blocking lint errors is never signed.
    report = compiler.lint_draft(draft, clock())
    if report.blocking:
        db.update_draft(session, report.draft, compiler.merge_lint(dict(row.meta), report))
        session.commit()
        raise ServiceError(
            409, "draft_has_blocking_issues",
            "This draft has problems that must be fixed before it can be signed.",
            {"blocking_issues": report.errors},
        )
    draft = report.draft

    # Copy every draft field except its id (a signed contract gets a new
    # contract_... id), and normalize all datetimes to UTC so the canonical
    # JSON, and therefore the hash, is stable.
    fields = _utc_datetimes(draft.model_dump(exclude={"id"}))
    # An amendment draft points at the contract it replaces.
    fields["previous_contract_id"] = row.previous_contract_id

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

    # --- Amendment: replace the old version atomically -----------------------
    # Signing a new version and revoking the old one happen in ONE transaction,
    # so there is never a moment with two active versions (or with none). The
    # conditional UPDATE only revokes the old contract if it is still ACTIVE.
    replaced: str | None = None
    if row.previous_contract_id:
        old_row = db.get_contract_row(session, row.previous_contract_id)
        if old_row is not None and old_row.owner == row.owner and old_row.status == ContractStatus.ACTIVE.value:
            if db.claim_contract_status(session, old_row.id, ContractStatus.ACTIVE, ContractStatus.REVOKED):
                replaced = old_row.id
                for credential in db.list_credentials_for_contract(session, old_row.id):
                    if credential.status in (CredentialStatus.CREATED, CredentialStatus.ACTIVE):
                        db.update_credential(session, credential.model_copy(update={"status": CredentialStatus.REVOKED}))

    db.save_contract(session, contract, draft_id=draft_id, owner=row.owner)
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

    if replaced:
        data["replaces_contract_id"] = replaced
    log_event(session, contract.id, EvidenceEventType.CONTRACT_SIGNED, "User signed the contract.", data)
    if replaced:
        log_event(
            session, replaced, EvidenceEventType.CONTRACT_REVOKED,
            f"Replaced by the new version {contract.id}, which the user signed.",
            {"kind": "contract_amended", "replaced_by": contract.id},
        )
    session.commit()
    return contract


def _require_contract(session: Session, contract_id: str, owner: str | None = None) -> Contract:
    """Load a contract the caller owns or raise 404; raise 409 if its stored JSON no longer parses (tampered)."""
    row = db.get_contract_row(session, contract_id)
    missing = f"No signed contract with id {contract_id!r}."
    if row is None:
        raise ServiceError(404, "contract_not_found", missing)
    _check_owner(row.owner, owner, "contract_not_found", missing)
    try:
        return db.contract_from_row(row)
    except ValidationError as exc:
        raise ServiceError(409, "contract_tampered", "Stored contract is no longer valid.", {"errors": _errors(exc)})


def revoke_contract(session: Session, contract_id: str, owner: str | None) -> Contract:
    """Revoke an ACTIVE, revocable contract and revoke any of its credentials that are still usable."""
    contract = _require_contract(session, contract_id, owner)
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

    # ...and no pending payment request either: cancel any that haven't
    # released a card yet. (Ones already paying are left to reconcile.)
    canceled_payments: list[str] = []
    for payment in db.list_open_payments_for_contract(session, contract_id):
        if payment.state in (payments.AWAITING_APPROVAL, payments.APPROVED, payments.CREDENTIAL_READY):
            _cancel_provider_request(payment)
            if db.claim_payment_state(session, payment.id, (payment.state,), payments.DENIED):
                canceled_payments.append(payment.purchase_id)
                purchase = db.load_purchase(session, payment.purchase_id)
                if purchase is not None and purchase.status == PurchaseStatus.AUTHORIZED:
                    _set_purchase(session, purchase, status=PurchaseStatus.BLOCKED, error="Contract revoked before payment.", completed_at=clock())

    log_event(
        session, contract_id, EvidenceEventType.CONTRACT_REVOKED,
        "User revoked the contract.", {"revoked_credentials": revoked_ids, "canceled_payments": canceled_payments},
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
    idempotency_key: str | None = None,
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
    db.save_purchase(session, purchase, owner=row.owner, idempotency_key=idempotency_key)

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
    session: Session,
    contract_id: str,
    checkout_url: str,
    raw_proposal: Any,
    principal: Principal | None = None,
    idempotency_key: str | None = None,
) -> Contract:
    """
    Step 1: load the contract and make sure it can be spent against.

    Unknown or foreign id -> plain 404 with no records (nothing to attach them to).
    Known contract but unusable (tampered / expired / used / revoked, or bound
    to a different agent) -> a BLOCKED purchase with evidence, then an error
    that carries its purchase_id.
    """
    row = db.get_contract_row(session, contract_id)
    missing = f"No signed contract with id {contract_id!r}."
    if row is None:
        raise ServiceError(404, "contract_not_found", missing)
    _check_owner(row.owner, principal.email if principal else None, "contract_not_found", missing)

    def reject(code: str, message: str, extra: dict[str, Any]) -> ServiceError:
        """Shortcut for recording a BLOCKED attempt against this contract."""
        return _record_rejected_attempt(
            session, row, PurchaseStatus.BLOCKED, 409, code, message, checkout_url, raw_proposal, extra,
            idempotency_key=idempotency_key,
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

    # A signed contract names the ONE agent allowed to spend against it
    # (agent_key). Any other agent is refused, even if it has the same owner.
    if principal is not None and principal.is_agent and contract.agent_key != principal.agent_id:
        raise _record_rejected_attempt(
            session, row, PurchaseStatus.BLOCKED, 403, "agent_not_authorized",
            "This contract was signed for a different agent; this agent may not use it.",
            checkout_url, raw_proposal, {"contract_agent_key": contract.agent_key, "agent_id": principal.agent_id},
            idempotency_key=idempotency_key,
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


def _outcome_for(session: Session, purchase: Purchase, **extra: Any) -> dict[str, Any]:
    """The {purchase, decision, credential} bundle for an existing purchase."""
    return {
        "purchase": purchase,
        "decision": db.load_decision(session, purchase.decision_id),
        "credential": db.load_credential(session, purchase.credential_id),
        **extra,
    }


def submit_purchase(
    session: Session,
    contract_id: str,
    checkout_url: str,
    selection_report: SelectionReport | None,
    principal: Principal | None = None,
    extractor: Extractor | None = None,
    idempotency_key: str | None = None,
    submission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    The full purchase flow:
        ownership -> idempotency -> contract checks -> record -> EXTRACT -> parse -> evaluate
        -> block / escalate / authorize

    The caller supplies only WHERE the checkout is. Handshake's extractor
    reads the facts itself; the agent's claims about price, items, or
    approval are never an input to the decision.

    Every step writes an evidence event. Returns {"purchase", "decision",
    "credential"}; purchase.status is the effective outcome.
    """
    owner = principal.email if principal else None

    # ---- Step 0a: ownership (unknown and foreign both look missing) --------
    row = db.get_contract_row(session, contract_id)
    missing = f"No signed contract with id {contract_id!r}."
    if row is None:
        raise ServiceError(404, "contract_not_found", missing)
    _check_owner(row.owner, owner, "contract_not_found", missing)

    # ---- Step 0b: idempotency ---------------------------------------------
    # The agent retries after timeouts. The same (contract, idempotency_key)
    # must return the SAME purchase, never create a second one. This runs
    # before the contract checks on purpose: after a successful purchase the
    # contract is "used", and a retry must get its purchase back, not a
    # "contract used" rejection.
    if idempotency_key:
        existing = db.find_purchase_by_idempotency_key(session, contract_id, idempotency_key)
        if existing is not None:
            return _outcome_for(session, existing, idempotent_replay=True)

    # ---- Step 1: is the contract usable at all? ------------------------
    contract = _load_contract_for_purchase(session, contract_id, checkout_url, submission, principal, idempotency_key)
    contract_owner = row.owner

    # ---- Step 2: create the purchase record ----------------------------
    purchase = Purchase(contract_id=contract.id, currency=contract.spend.currency, created_at=clock())
    try:
        db.save_purchase(session, purchase, owner=contract_owner, idempotency_key=idempotency_key)
        session.flush()
    except IntegrityError:
        # Two requests with the same key raced; the other one won. Return its purchase.
        session.rollback()
        existing = db.find_purchase_by_idempotency_key(session, contract_id, idempotency_key or "")
        if existing is None:
            raise
        return _outcome_for(session, existing, idempotent_replay=True)

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

    # ---- Step 3a: Handshake reads the checkout itself ----------------------
    if extractor is None:
        from handshake.extractor import get_extractor

        extractor = get_extractor()
    try:
        extraction = extractor.extract(checkout_url, contract.id)
    except ExtractionError as exc:
        # A URL Handshake refuses to fetch (SSRF rules) is BLOCKED; a merchant
        # that can't be read is FAILED. Either way: evidence, then an error
        # that carries the purchase_id so the frontend can show the attempt.
        status = PurchaseStatus.BLOCKED if exc.code == "unsupported_checkout_url" else PurchaseStatus.FAILED
        purchase = _set_purchase(session, purchase, status=status, error=exc.message, completed_at=clock())
        log_event(
            session, contract.id, EvidenceEventType.PURCHASE_BLOCKED,
            f"Handshake could not read the checkout: {exc.message}",
            {"reason": exc.code, "kind": "extraction_failed", "checkout_url": checkout_url, **exc.details},
            purchase.id,
        )
        session.commit()
        raise ServiceError(exc.status_code, exc.code, exc.message, {"purchase_id": purchase.id, "status": status.value, **exc.details})
    raw_proposal = extraction.raw_proposal

    # ---- Step 3b: parse and store the proposal --------------------------
    proposal, total_mismatch_results = _parse_proposal(session, contract, purchase, raw_proposal)

    # Record which fields were explicitly sent BEFORE anything is re-loaded
    # from the database (see intent_diff.explicit_fields for why).
    fields_set = explicit_fields(proposal)
    db.save_proposal(session, proposal, raw_proposal, fields_set, purchase.id, snapshot_hash=extraction.snapshot_hash)
    purchase = _set_purchase(session, purchase, proposal_id=proposal.id, merchant_name=proposal.merchant.name)
    log_event(
        session, contract.id, EvidenceEventType.PROPOSAL_CREATED,
        f"Checkout extracted by Handshake: {fmt_money(to_cents(proposal.total), proposal.currency)} at {proposal.merchant.name}.",
        {
            "proposal": proposal,
            "fields_set": sorted(fields_set),
            "checkout_snapshot_hash": extraction.snapshot_hash,
            "extractor": extraction.evidence.get("extractor"),
            "extractor_disagreements": extraction.evidence.get("disagreements", []),
        },
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

    # The checkout-link parser (extractor.checkout_link_results): does the
    # LINK the agent found match the contract (right merchant, the page is who
    # the link says, and it's what the agent says it picked)? Those hard
    # results go FIRST. Test extractors that supply no link facts skip this.
    if extraction.link is not None:
        from handshake.extractor import checkout_link_results

        link_results = checkout_link_results(contract, extraction.link, raw_proposal, selection_report)
        decision = _make_decision(contract.id, proposal.id, [*link_results, *decision.results])

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
        if purchase.status == PurchaseStatus.AUTHORIZED:
            # Handshake's decision is made; now the PAYMENT can begin (section 9):
            # a Link TEST spend request the user approves in Link (or the simulated one).
            purchase = start_payment(session, purchase, contract, proposal)

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


def _load_escalated(
    session: Session, purchase_id: str, action: str, owner: str | None = None
) -> tuple[Purchase, ValidationDecision]:
    """Load a purchase the caller owns and its decision, or raise 404/409 if it is not an escalated purchase."""
    purchase = get_owned_purchase(session, purchase_id, owner)
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


def approve_purchase(session: Session, purchase_id: str, note: str | None = None, owner: str | None = None) -> dict[str, Any]:
    """
    A human approves an ESCALATED purchase: issue the credential exactly like a normal authorization.

    Refused (409) unless ALL of these hold:
      - the purchase is ESCALATED (not blocked, not already approved/rejected)
      - its decision contains zero hard FAIL results
      - the contract is still ACTIVE, not expired, and its hash/signature verify
      - the escalation is fresh (younger than HANDSHAKE_ESCALATION_TTL_MINUTES)
    """
    purchase, decision = _load_escalated(session, purchase_id, "approved", owner)

    # --- Stale escalations cannot be approved ----------------------------
    # The checkout was evaluated at decision.evaluated_at. Prices, stock, and
    # delivery promises change, so approving facts that are an hour old could
    # approve a checkout that no longer exists in that form. Past the TTL the
    # agent must request the purchase again, which re-extracts the checkout.
    ttl = timedelta(minutes=get_settings().escalation_ttl_minutes)
    age = clock() - as_utc(decision.evaluated_at)[0]
    if age > ttl:
        raise ServiceError(
            409, "escalation_stale",
            f"This escalation is {int(age.total_seconds() // 60)} minutes old (limit {int(ttl.total_seconds() // 60)}). "
            "Prices may have changed; ask the agent to request the purchase again.",
            {"evaluated_at": decision.evaluated_at.isoformat()},
        )

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
    contract = _require_contract(session, purchase.contract_id, owner)  # 404, or 409 if its JSON no longer parses
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

    # Consent #1 (accepting the exception, here in Handshake) is done.
    # Consent #2 is separate: the user still approves the payment in Link.
    purchase = start_payment(session, purchase, contract, proposal)
    return {"purchase": purchase, "decision": decision, "credential": credential}


def reject_purchase(session: Session, purchase_id: str, note: str | None = None, owner: str | None = None) -> dict[str, Any]:
    """
    A human says no. Two cases:
      - an ESCALATED purchase: it becomes BLOCKED and no credential is ever issued
      - an AUTHORIZED purchase whose payment has not started (no card released,
        nothing submitted): the pending provider request is cancelled, the
        purchase becomes BLOCKED ("declined"), and the contract is freed
    """
    current = get_owned_purchase(session, purchase_id, owner)
    if current.status == PurchaseStatus.AUTHORIZED:
        return decline_authorized_purchase(session, current, note)
    purchase, decision = _load_escalated(session, purchase_id, "rejected", owner)

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


def record_malformed_submission(
    session: Session, body: Any, errors: list[dict[str, Any]], owner: str | None = None
) -> str | None:
    """
    Record a POST /purchases body that did not even match PurchaseSubmission.

    If the body names a known contract, create a FAILED purchase with evidence
    and return its id; otherwise return None (nothing to attach records to).
    """
    if not isinstance(body, dict) or not isinstance(body.get("contract_id"), str):
        return None
    row = db.get_contract_row(session, body["contract_id"])
    if row is None or (owner is not None and row.owner != owner):
        return None  # unknown or foreign contract: nothing to attach records to

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
# Payments: the state machine after AUTHORIZED (section 9)
# ============================================================
#
#   awaiting_approval -> approved -> revalidating -> credential_ready -> paying -> paid -> completed
#                                                   (agent_visible only)
#   side exits: denied, expired, checkout_changed, failed, unknown
#
# PurchaseStatus (models.py) stays AUTHORIZED through all of this, and only
# becomes COMPLETED after Handshake verifies the merchant's order itself.
# refresh_payment() is the one function that advances the machine; the
# frontend and the MCP server poll it, and GET /purchases/{id} runs it too.
# It is idempotent: calling it again in a stable state changes nothing.


def _run_async(coro: Any) -> Any:
    """
    Run a provider coroutine from synchronous service code.

    The Link adapter is async (asyncio subprocesses, so the CLI never blocks
    the server's event loop). FastAPI runs our sync routes in worker threads
    with no event loop, where asyncio.run works. If there IS a running loop
    in this thread, run the coroutine on a helper thread instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _provider_for(row: db.PaymentRow) -> payments.Provider:
    """The provider that owns this payment row (a row never switches providers)."""
    if row.provider == "link_test":
        return payments.LinkTestProvider()
    return payments.StubProvider()


def _cancel_provider_request(row: db.PaymentRow) -> None:
    """Best effort: cancel the provider request so the user isn't asked to approve something dead."""
    if not row.provider_request_id:
        return
    try:
        _run_async(_provider_for(row).cancel(row.provider_request_id))
    except payments.PaymentError:
        pass


def _payment_event(session: Session, row: db.PaymentRow, contract_id: str, event_type: EvidenceEventType, kind: str, message: str, **data: Any) -> None:
    """Log a payment step. data.kind carries the precise subtype (models.py has no payment event types)."""
    log_event(
        session, contract_id, event_type, message,
        {"kind": kind, "payment_state": row.state, "provider": row.provider, "provider_request_id": row.provider_request_id, **data},
        row.purchase_id,
    )


def _set_payment(session: Session, row: db.PaymentRow, **changes: Any) -> None:
    """Update a payment row and stamp updated_at."""
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_at = clock()


def _release_reservation(session: Session, contract_id: str) -> bool:
    """
    Give a single-use contract back (USED -> ACTIVE) after a payment that
    guaranteed no money moved (denied, expired, checkout changed, failed
    before any order). The purchase holding the reservation is terminal, so
    the user's authorization is usable again. Conditional UPDATE, as always.
    """
    return db.claim_contract_status(session, contract_id, ContractStatus.USED, ContractStatus.ACTIVE)


def _end_payment(
    session: Session,
    row: db.PaymentRow,
    purchase: Purchase,
    state: str,
    kind: str,
    message: str,
    purchase_status: PurchaseStatus = PurchaseStatus.FAILED,
    release: bool = True,
    **data: Any,
) -> Purchase:
    """
    Finish a payment WITHOUT a completed order: record why, close the
    purchase, revoke Handshake's credential, and (if no money moved) release
    the single-use contract.
    """
    contract_id = purchase.contract_id
    _set_payment(session, row, state=state, last_error=message[:300])
    if purchase.status == PurchaseStatus.AUTHORIZED:
        purchase = _set_purchase(session, purchase, status=purchase_status, error=message, completed_at=clock())
    credential = db.load_credential(session, purchase.credential_id)
    if credential is not None and credential.status == CredentialStatus.ACTIVE:
        db.update_credential(session, credential.model_copy(update={"status": CredentialStatus.REVOKED}))
    released = False
    if release:
        contract = db.load_contract(session, contract_id)
        if contract is not None and contract.single_use:
            released = _release_reservation(session, contract_id)
    event_type = EvidenceEventType.PURCHASE_BLOCKED
    _payment_event(session, row, contract_id, event_type, kind, message, contract_released=released, **data)
    session.commit()
    return purchase


def _spend_spec(purchase: Purchase, contract: Contract, proposal: TransactionProposal, amount_minor: int) -> payments.SpendSpec:
    """What we ask the provider for: the exact authorized amount and a truthful description."""
    checkout_url = proposal.source_url or ""
    parts = checkout_url.split("/")
    merchant_url = "/".join(parts[:3]) if len(parts) >= 3 else checkout_url
    items = tuple((item.name, to_cents(item.unit_price), item.quantity) for item in proposal.line_items)
    context = payments.build_context(
        contract.goal, proposal.merchant.name, [item.name for item in proposal.line_items], fmt_money(amount_minor, proposal.currency)
    )
    return payments.SpendSpec(
        purchase_id=purchase.id, amount_minor=amount_minor, currency=proposal.currency.upper(),
        merchant_name=proposal.merchant.name, merchant_url=merchant_url, context=context, line_items=items,
    )


def start_payment(session: Session, purchase: Purchase, contract: Contract, proposal: TransactionProposal) -> Purchase:
    """
    AUTHORIZED -> ask the provider for a single-use TEST card that the USER must approve.

    The payment row is committed BEFORE the provider is called, and the
    request id is committed the moment it comes back, so a crash can never
    lose a request or cause a second one. Only one payment per purchase, ever.
    """
    if db.get_payment(session, purchase.id) is not None:
        return purchase  # never a second spend request for the same purchase

    settings = get_settings()
    provider = payments.get_provider(settings)
    amount_minor = to_cents(purchase.authorized_amount)
    now = clock()
    row = db.PaymentRow(
        id=generate_id("payment"), purchase_id=purchase.id, provider=provider.name, state=payments.AWAITING_APPROVAL,
        amount_minor=amount_minor, pay_amount_minor=amount_minor, currency=proposal.currency.upper(),
        checkout_url=proposal.source_url, created_at=now, updated_at=now,
    )
    session.add(row)
    session.commit()

    spec = _spend_spec(purchase, contract, proposal, amount_minor)
    try:
        request = _run_async(provider.create_request(spec))
    except payments.PaymentUncertain as exc:
        # We don't know if Link created it. Look before ever trying again.
        try:
            request = _run_async(provider.reconcile(spec))
        except payments.PaymentError:
            request = None
        if request is None:
            _set_payment(session, row, state=payments.UNKNOWN, last_error=exc.message)
            _payment_event(session, row, purchase.contract_id, EvidenceEventType.PURCHASE_AUTHORIZED, "payment_request_uncertain",
                           "The payment request's creation could not be confirmed; Handshake will reconcile before anything else.")
            session.commit()
            return purchase
    except payments.PaymentError as exc:
        return _end_payment(session, row, purchase, payments.FAILED, "payment_request_failed",
                            f"The payment provider refused the request: {exc.message}", error_code=exc.code)

    # Persist the provider's id immediately, before any polling.
    approval_url = request.approval_url
    if provider.name == "stub":
        approval_url = purchase_url_for(purchase.id)  # the "Simulated provider approval" button lives there
    _set_payment(session, row, provider_request_id=request.request_id, approval_url=approval_url)
    session.commit()

    _payment_event(
        session, row, purchase.contract_id, EvidenceEventType.PURCHASE_AUTHORIZED, "payment_requested",
        f"{provider.label}: a single-use card for {fmt_money(amount_minor, spec.currency)} was requested. "
        "It is waiting for your approval.",
        approval_url=approval_url, amount=fmt_money(amount_minor, spec.currency),
    )
    session.commit()
    return purchase


def refresh_payment(session: Session, purchase_id: str, owner: str | None = None, extractor: Extractor | None = None) -> Purchase:
    """
    Advance the payment state machine as far as it can go right now. Idempotent.

    Polled by the frontend and the MCP server, and run on GET /purchases/{id}.
    Each step re-reads the row, so two refreshes racing are safe: the atomic
    state claims decide who does a step.
    """
    purchase = get_owned_purchase(session, purchase_id, owner)
    for _ in range(8):  # a few steps per call is plenty; each loop makes progress or stops
        row = db.get_payment(session, purchase.id)
        if row is None or row.state in payments.TERMINAL_STATES:
            break
        before = row.state
        purchase = _payment_step(session, row, purchase, extractor)
        session.refresh(row)
        if row.state == before:
            break
    return purchase


def _payment_step(session: Session, row: db.PaymentRow, purchase: Purchase, extractor: Extractor | None) -> Purchase:
    """Do ONE transition from the payment's current state (see the diagram above)."""
    provider = _provider_for(row)

    if row.state == payments.AWAITING_APPROVAL:
        if not row.provider_request_id:
            return purchase
        try:
            status = _run_async(provider.get_status(row.provider_request_id))
        except payments.PaymentError as exc:
            _set_payment(session, row, last_error=payments.sanitize(exc.message))
            session.commit()
            return purchase
        if status.state == payments.P_APPROVED:
            if db.claim_payment_state(session, row.id, (payments.AWAITING_APPROVAL,), payments.APPROVED):
                _payment_event(session, row, purchase.contract_id, EvidenceEventType.PURCHASE_AUTHORIZED, "payment_approved",
                               f"The payment was approved in {provider.label}.")
                session.commit()
        elif status.state == payments.P_DENIED:
            purchase = _end_payment(session, row, purchase, payments.DENIED, "payment_denied", "The payment was declined in Link.")
        elif status.state == payments.P_EXPIRED:
            purchase = _end_payment(session, row, purchase, payments.EXPIRED, "payment_expired", "The payment approval expired before you approved it.")
        elif status.state == payments.P_FAILED:
            purchase = _end_payment(session, row, purchase, payments.FAILED, "payment_request_failed", "The payment provider reported a failure.")
        elif status.state == payments.P_UNKNOWN:
            _set_payment(session, row, last_error=f"Unrecognized provider status {status.raw_status!r}; waiting.")
            session.commit()
        return purchase

    if row.state == payments.APPROVED:
        if not db.claim_payment_state(session, row.id, (payments.APPROVED,), payments.REVALIDATING):
            return purchase
        session.commit()
        return _revalidate_and_continue(session, row, purchase, extractor)

    if row.state == payments.REVALIDATING:
        # A previous refresh died mid-revalidation; start it over.
        return _revalidate_and_continue(session, row, purchase, extractor)

    if row.state == payments.CREDENTIAL_READY:
        credential = db.load_credential(session, purchase.credential_id)
        if credential is not None and as_utc(credential.expires_at)[0] <= clock():
            _cancel_provider_request(row)
            return _end_payment(session, row, purchase, payments.EXPIRED, "payment_expired",
                                "The authorization expired before the agent collected the card.")
        return purchase  # waiting for the agent to collect the card

    if row.state in (payments.PAYING, payments.UNKNOWN):
        return _look_for_order(session, row, purchase)

    if row.state == payments.PAID:
        return _verify_and_complete(session, row, purchase)

    return purchase


def _revalidate_and_continue(session: Session, row: db.PaymentRow, purchase: Purchase, extractor: Extractor | None) -> Purchase:
    """
    After the user approves in Link and BEFORE any card moves: is everything still true?

    1. The contract still verifies and is not revoked or expired.
    2. The checkout is re-extracted. If its snapshot hash matches, nothing
       changed. If it changed, the engine runs again on the new facts; if the
       new outcome isn't AUTHORIZED or the total rose at all, stop at
       checkout_changed and pay nothing.
    """
    contract_row = db.get_contract_row(session, purchase.contract_id)
    try:
        contract = db.contract_from_row(contract_row)
    except ValidationError:
        return _end_payment(session, row, purchase, payments.FAILED, "contract_tampered", "The contract no longer verifies; nothing was paid.", release=False)
    if not verify_contract(contract)["valid"]:
        return _end_payment(session, row, purchase, payments.FAILED, "contract_tampered", "The contract no longer verifies; nothing was paid.", release=False)
    if contract.status in (ContractStatus.REVOKED, ContractStatus.EXPIRED) or (
        contract.expires_at is not None and as_utc(contract.expires_at)[0] <= clock()
    ):
        _cancel_provider_request(row)
        return _end_payment(session, row, purchase, payments.FAILED, "contract_no_longer_valid",
                            "The contract was revoked or expired before payment; nothing was paid.", release=False)

    proposal_row = db.get_proposal_row(session, purchase.proposal_id)
    if extractor is None:
        from handshake.extractor import get_extractor

        extractor = get_extractor()
    try:
        extraction = extractor.extract(row.checkout_url or "", contract.id)
    except ExtractionError as exc:
        # Can't look at the checkout, so can't pay for it. Try again next refresh.
        db.claim_payment_state(session, row.id, (payments.REVALIDATING,), payments.APPROVED)
        _set_payment(session, row, last_error=f"Could not re-read the checkout: {exc.message}")
        session.commit()
        return purchase

    authorized_minor = row.amount_minor
    changed = extraction.snapshot_hash != proposal_row.checkout_snapshot_hash
    if changed:
        new_outcome, new_total, reasons = _re_evaluate(contract, extraction.raw_proposal)
        if new_outcome != PurchaseStatus.AUTHORIZED or new_total is None or new_total > authorized_minor:
            _cancel_provider_request(row)
            return _end_payment(
                session, row, purchase, payments.CHECKOUT_CHANGED, "checkout_changed",
                "The checkout changed after authorization and no longer matches the contract; nothing was paid.",
                purchase_status=PurchaseStatus.BLOCKED,
                new_outcome=new_outcome.value if new_outcome else None,
                new_total=fmt_money(new_total, row.currency) if new_total is not None else None,
                authorized=fmt_money(authorized_minor, row.currency), reasons=reasons,
                new_snapshot_hash=extraction.snapshot_hash,
            )
        _set_payment(session, row, pay_amount_minor=new_total)

    _payment_event(
        session, row, purchase.contract_id, EvidenceEventType.VALIDATION_COMPLETED, "checkout_revalidated",
        "The checkout was read again before payment and still matches the contract." if not changed
        else "The checkout changed but still satisfies the contract at no higher cost.",
        unchanged=not changed, snapshot_hash=extraction.snapshot_hash,
    )

    if get_settings().credential_mode == "agent_visible":
        db.claim_payment_state(session, row.id, (payments.REVALIDATING,), payments.CREDENTIAL_READY)
        _payment_event(session, row, purchase.contract_id, EvidenceEventType.PURCHASE_AUTHORIZED, "credential_ready",
                       "The single-use card is ready for the agent to collect once and pay this exact checkout.")
        session.commit()
        return purchase

    session.commit()
    return _execute_payment(session, row, purchase)


def _re_evaluate(contract: Contract, raw_proposal: dict[str, Any]) -> tuple[PurchaseStatus | None, int | None, list[str]]:
    """Run the engine on a re-extracted checkout. Returns (outcome, payable cents, failing reasons)."""
    try:
        proposal = TransactionProposal.model_validate(raw_proposal)
    except ValidationError:
        return None, None, ["The new checkout's numbers don't add up."]
    decision, outcome = evaluate(contract, proposal, now=clock())
    reasons = [r.reason for r in decision.results if r.verdict != ConstraintVerdict.PASS and r.severity != ConstraintSeverity.SOFT]
    return outcome, payable_total_cents(proposal), reasons


def _execute_payment(session: Session, row: db.PaymentRow, purchase: Purchase) -> Purchase:
    """
    EXECUTOR mode: the backend retrieves the card and pays the merchant itself.
    The card never enters any agent's context. It lives only inside this function.
    """
    if not db.claim_payment_state(session, row.id, (payments.REVALIDATING,), payments.PAYING):
        return purchase
    session.commit()  # "paying" is on disk BEFORE the pay call, so a crash means "check for an order"

    try:
        card = _run_async(_provider_for(row).retrieve_card(row.provider_request_id or ""))
    except payments.PaymentError as exc:
        return _end_payment(session, row, purchase, payments.FAILED, "credential_retrieval_failed",
                            f"The card could not be retrieved ({exc.code}); nothing was paid.")
    try:
        order = payments.pay_merchant(row.checkout_url or "", card, row.pay_amount_minor, row.currency)
    except payments.PaymentUncertain as exc:
        # Maybe charged, maybe not. NEVER retry blindly: the next refresh asks the merchant.
        _set_payment(session, row, state=payments.UNKNOWN, last_error=exc.message, card_last4=card.last4)
        _payment_event(session, row, purchase.contract_id, EvidenceEventType.CREDENTIAL_USED, "payment_outcome_unknown",
                       "The merchant did not confirm the payment. Handshake will check for an order; it will not pay again.", last4=card.last4)
        session.commit()
        return purchase
    except payments.PaymentError as exc:
        return _end_payment(session, row, purchase, payments.FAILED, "payment_refused", f"The merchant refused the payment ({exc.code}).")
    finally:
        del card

    _set_payment(session, row, state=payments.PAID, order_id=str(order.get("order_id")), card_last4=str(order.get("last4") or "")[-4:] or None)
    _payment_event(session, row, purchase.contract_id, EvidenceEventType.CREDENTIAL_USED, "payment_submitted",
                   "Handshake's executor paid the merchant with the single-use card.", order_id=row.order_id, last4=row.card_last4)
    session.commit()
    return _verify_and_complete(session, row, purchase)


def _look_for_order(session: Session, row: db.PaymentRow, purchase: Purchase) -> Purchase:
    """
    PAYING or UNKNOWN: ask the MERCHANT whether this checkout has an order.

    This resolves an uncertain outcome before anything else happens: an order
    means it was paid; no order after an uncertain call means it wasn't (and
    we still never pay again on our own). For a request whose creation was
    uncertain, reconcile with the provider instead.
    """
    if row.state == payments.UNKNOWN and not row.provider_request_id:
        contract = db.load_contract(session, purchase.contract_id)
        proposal = TransactionProposal.model_validate(db.get_proposal_row(session, purchase.proposal_id).data)
        try:
            found = _run_async(_provider_for(row).reconcile(_spend_spec(purchase, contract, proposal, row.amount_minor)))
        except payments.PaymentError:
            return purchase
        if found is None:
            return _end_payment(session, row, purchase, payments.FAILED, "payment_request_failed",
                                "The payment request was never created; nothing was paid.")
        _set_payment(session, row, provider_request_id=found.request_id, approval_url=found.approval_url, state=payments.AWAITING_APPROVAL)
        session.commit()
        return purchase

    try:
        order = payments.find_session_order(row.checkout_url or "")
    except payments.PaymentError as exc:
        _set_payment(session, row, last_error=exc.message)
        session.commit()
        return purchase
    if order is None:
        if row.state == payments.UNKNOWN:
            return _end_payment(session, row, purchase, payments.FAILED, "payment_not_made",
                                "The merchant has no order for this checkout, so the uncertain payment did not happen. Nothing will be retried automatically.")
        return purchase  # agent hasn't paid yet
    _set_payment(session, row, state=payments.PAID, order_id=str(order.get("order_id")), card_last4=(str(order.get("last4") or "")[-4:] or row.card_last4))
    _payment_event(session, row, purchase.contract_id, EvidenceEventType.CREDENTIAL_USED, "payment_submitted",
                   "The merchant reports an order for this checkout.", order_id=row.order_id, last4=row.card_last4)
    session.commit()
    return _verify_and_complete(session, row, purchase)


def _verify_and_complete(session: Session, row: db.PaymentRow, purchase: Purchase) -> Purchase:
    """
    PAID: verify the order with the merchant INDEPENDENTLY, then reconcile.

    Reconciliation reuses complete_purchase(): an overcharge of even one cent
    fails the purchase and revokes the credential. Money did move then, so
    the contract is NOT released.
    """
    try:
        order = payments.get_order(row.checkout_url or "", row.order_id or "")
    except payments.PaymentUncertain as exc:
        _set_payment(session, row, last_error=exc.message)
        session.commit()
        return purchase
    except payments.PaymentError as exc:
        return _end_payment(session, row, purchase, payments.FAILED, "order_not_verified",
                            f"The merchant could not confirm order {row.order_id}: {exc.message}", release=False)

    if order.get("session_id") != (row.checkout_url or "").rstrip("/").rsplit("/", 1)[-1] or str(order.get("currency", "")).upper() != row.currency:
        return _end_payment(session, row, purchase, payments.FAILED, "order_mismatch",
                            "The merchant's order does not belong to this checkout.", release=False)

    outcome = complete_purchase(session, purchase.id, float(order.get("amount_charged") or 0))
    purchase = outcome["purchase"]
    receipt = {
        "order_id": order.get("order_id"),
        "amount_charged": order.get("amount_charged"),
        "currency": order.get("currency"),
        "last4": order.get("last4"),
        "status": order.get("status"),
        "verified_at": clock().isoformat(),
        "test_mode": bool(order.get("test_mode", True)),
    }
    if purchase.status == PurchaseStatus.COMPLETED:
        _set_payment(session, row, state=payments.COMPLETED, receipt=receipt, card_last4=str(order.get("last4") or row.card_last4 or "") or None)
        contract = db.load_contract(session, purchase.contract_id)
        if contract is not None and contract.single_use and contract.status == ContractStatus.ACTIVE:
            db.claim_single_use_contract(session, contract.id)  # already USED via the reservation; this is a no-op then
        _payment_event(session, row, purchase.contract_id, EvidenceEventType.PAYMENT_COMPLETED, "receipt_verified",
                       f"Order {order.get('order_id')} verified with the merchant: {fmt_money(to_cents(order.get('amount_charged') or 0), row.currency)} charged.",
                       receipt=receipt)
    else:
        _set_payment(session, row, state=payments.FAILED, receipt=receipt, last_error=purchase.error)
        _payment_event(session, row, purchase.contract_id, EvidenceEventType.PAYMENT_MISMATCH, "receipt_mismatch",
                       f"The verified charge did not match the authorization: {purchase.error}", receipt=receipt)
    session.commit()
    return purchase


# ============================================================
# Agent-visible mode: the one-time card release (section 9.6)
# ============================================================


class CredentialRelease(BaseModel):
    """
    The ONLY response in Handshake that contains card values. Sensitive.

    It is returned once, to the owning agent, for one purchase, and is never
    cached, logged, stored, or put into evidence. Every other response
    carries at most last4.
    """

    purchase_id: str
    contract_id: str
    merchant_name: str
    checkout_url: str
    pay_url: str
    pay_method: str = "POST"
    amount: float
    currency: str
    card_number: str
    exp_month: int
    exp_year: int
    cvc: str
    card_valid_until: str | None = None
    authorization_expires_at: datetime
    simulated: bool
    mode_label: str
    instructions: str


def release_credential(session: Session, purchase_id: str, principal: Principal) -> CredentialRelease:
    """
    Release the single-use TEST card to the owning agent, exactly once, for exactly this purchase.

    Requires: the caller is the agent the contract is bound to; the payment
    is credential_ready (approved in Link AND revalidated); the contract still
    verifies. The credential_ready -> paying claim is atomic, so a second call
    (or two at once) gets 409 credential_already_released and no card values.
    """
    purchase = get_owned_purchase(session, purchase_id, principal.email)
    contract = _require_contract(session, purchase.contract_id, principal.email)
    if not principal.is_agent or contract.agent_key != principal.agent_id:
        raise ServiceError(403, "agent_not_authorized", "Only the agent this contract is bound to can collect its card.")

    row = db.get_payment(session, purchase.id)
    if row is None or row.state in (payments.AWAITING_APPROVAL, payments.APPROVED, payments.REVALIDATING):
        raise ServiceError(409, "payment_not_ready", "The card isn't available yet: the user must approve the payment and Handshake must recheck the checkout.")
    if row.credential_released_at is not None or row.state in (payments.PAYING, payments.PAID, payments.COMPLETED, payments.UNKNOWN):
        raise ServiceError(409, "credential_already_released", "The card for this purchase was already released. It is never sent twice.")
    if row.state != payments.CREDENTIAL_READY:
        raise ServiceError(409, "payment_not_ready", f"The payment is {row.state}; there is no card to release.")
    if not verify_contract(contract)["valid"] or contract.status in (ContractStatus.REVOKED, ContractStatus.EXPIRED):
        raise ServiceError(409, "contract_not_valid", "The contract no longer verifies or is no longer in force.")
    credential = db.load_credential(session, purchase.credential_id)
    if credential is None or credential.status != CredentialStatus.ACTIVE or as_utc(credential.expires_at)[0] <= clock():
        raise ServiceError(409, "authorization_expired", "The authorization for this purchase is no longer active.")

    # The one-time claim. Whoever flips credential_ready -> paying first gets the card.
    if not db.claim_payment_state(session, row.id, (payments.CREDENTIAL_READY,), payments.PAYING):
        raise ServiceError(409, "credential_already_released", "The card for this purchase was already released. It is never sent twice.")
    session.commit()

    try:
        card = _run_async(_provider_for(row).retrieve_card(row.provider_request_id or ""))
    except payments.PaymentError as exc:
        # Nothing was delivered, so put the payment back and let the agent try again.
        db.claim_payment_state(session, row.id, (payments.PAYING,), payments.CREDENTIAL_READY)
        _set_payment(session, row, last_error=f"Card retrieval failed ({exc.code}).")
        session.commit()
        raise ServiceError(502, "credential_retrieval_failed", "The card could not be retrieved from the provider. Try again shortly.")

    _set_payment(session, row, credential_released_at=clock(), card_last4=card.last4)
    _payment_event(session, row, purchase.contract_id, EvidenceEventType.CREDENTIAL_CREATED, "credential_released",
                   f"The single-use card (ending {card.last4}) was released once to {principal.label} for this checkout only.",
                   last4=card.last4, simulated=card.simulated)
    session.commit()

    endpoints = payments.merchant_endpoints(row.checkout_url or "")
    settings = get_settings()
    return CredentialRelease(
        purchase_id=purchase.id,
        contract_id=purchase.contract_id,
        merchant_name=purchase.merchant_name or "",
        checkout_url=row.checkout_url or "",
        pay_url=endpoints["pay_url"],
        amount=float(Decimal(row.pay_amount_minor) / 100),
        currency=row.currency,
        card_number=card.number,
        exp_month=card.exp_month,
        exp_year=card.exp_year,
        cvc=card.cvc,
        card_valid_until=card.valid_until,
        authorization_expires_at=credential.expires_at,
        simulated=card.simulated,
        mode_label=settings.payment_label,
        instructions=(
            "Use these card values ONCE, only for this checkout: POST them to pay_url with exactly this amount and currency. "
            "Do not repeat them in chat, logs, or any other tool. Then poll get_purchase_status until it is completed. "
            "If the payment outcome is uncertain, do NOT pay again; poll status instead."
        ),
    )


def simulate_approval(session: Session, purchase_id: str, owner: str | None) -> Purchase:
    """
    STUB MODE ONLY: stand in for the user's approval tap in Link. Labeled "Simulated provider approval" everywhere.
    """
    purchase = get_owned_purchase(session, purchase_id, owner)
    row = db.get_payment(session, purchase.id)
    if row is None or row.provider != "stub":
        raise ServiceError(409, "not_simulated", "Only a simulated payment can be approved this way.")
    if row.state != payments.AWAITING_APPROVAL or not row.provider_request_id:
        raise ServiceError(409, "payment_not_awaiting_approval", f"The payment is {row.state}, not waiting for approval.")
    payments.StubProvider.simulate(row.provider_request_id, "approved")
    _payment_event(session, row, purchase.contract_id, EvidenceEventType.PURCHASE_AUTHORIZED, "simulated_provider_approval",
                   "Simulated provider approval: the user approved the (simulated) payment.")
    session.commit()
    return purchase


def decline_authorized_purchase(session: Session, purchase: Purchase, note: str | None) -> dict[str, Any]:
    """
    The user declines an AUTHORIZED purchase before any card has moved ("Not this one").

    Only while the payment hasn't started (awaiting approval, approved, or
    card ready but not collected). The claim to DENIED is atomic, so it can't
    race a card release: one of them wins.
    """
    row = db.get_payment(session, purchase.id)
    not_started = (payments.AWAITING_APPROVAL, payments.APPROVED, payments.CREDENTIAL_READY)
    if row is not None and not db.claim_payment_state(session, row.id, not_started, payments.DENIED):
        raise ServiceError(409, "payment_already_started", "The payment is already under way and can no longer be declined.")
    if row is not None:
        _cancel_provider_request(row)
    decision = db.load_decision(session, purchase.decision_id)
    purchase = _set_purchase(session, purchase, status=PurchaseStatus.BLOCKED, error="Declined by the user before payment.", completed_at=clock())
    credential = db.load_credential(session, purchase.credential_id)
    if credential is not None and credential.status == CredentialStatus.ACTIVE:
        db.update_credential(session, credential.model_copy(update={"status": CredentialStatus.REVOKED}))
    contract = db.load_contract(session, purchase.contract_id)
    released = bool(contract and contract.single_use and _release_reservation(session, purchase.contract_id))
    log_event(
        session, purchase.contract_id, EvidenceEventType.PURCHASE_BLOCKED,
        "You declined this purchase before payment. The agent can keep looking under the same contract.",
        {"kind": "purchase_declined", "human_rejection": True, "note": note, "contract_released": released},
        purchase.id,
    )
    session.commit()
    return {"purchase": purchase, "decision": decision, "credential": db.load_credential(session, purchase.credential_id)}


# ============================================================
# Read models for the API
# ============================================================


# Human names for results, so the UI never has to parse "constraint[0]:size:eq".
# (field, label) per built-in rule; generic constraints use FIELD_LABELS.
RULE_FIELDS: dict[str, tuple[str, str]] = {
    "currency_match": ("currency", "Currency"),
    "subtotal_integrity": ("item_subtotal", "Item subtotal adds up"),
    "total_integrity": ("total_price", "Total adds up"),
    "hard_cap_all_in": ("total_price", "Total price"),
    "max_shipping": ("shipping", "Shipping cost"),
    "contract_category": ("category", "Category"),
    "no_subscription": ("subscription", "No subscription"),
    "no_membership": ("membership", "No membership"),
    "no_addons": ("addons", "No add-ons"),
    "min_return_days": ("return_days", "Return window"),
    "delivery_deadline": ("delivery_date", "Delivery date"),
    "merchant_policy": ("merchant_name", "Merchant"),
    "seller_requirement": ("seller_of_record", "Seller"),
    "extractor_agreement": ("extractors_agreed", "Checkout readings agree"),
    "contract_binding": ("contract_id", "Right contract"),
    "checkout_link_format": ("checkout_url", "Checkout link"),
    "checkout_link_merchant": ("merchant_name", "Link belongs to merchant"),
    "checkout_link_selection": ("checkout_url", "Link matches agent's pick"),
}

FIELD_LABELS: dict[str, str] = {
    "category": "Category", "brand": "Brand", "model": "Model", "product_name": "Product",
    "condition": "Condition", "color": "Color", "size": "Size", "quantity": "Quantity",
    "screen_size_inches": "Screen size", "display_type": "Display type", "refresh_rate_hz": "Refresh rate",
    "storage_gb": "Storage", "memory_gb": "Memory", "material": "Material", "gender": "Gender", "fit": "Fit",
    "food_size": "Size", "toppings": "Toppings", "dietary": "Dietary needs", "event_name": "Event",
    "ticket_quantity": "Tickets", "seats_together": "Seats together", "section": "Section",
    "merchant_name": "Merchant", "seller_of_record": "Seller", "return_days": "Return window",
    "subscription": "Subscription", "membership": "Membership", "addons": "Add-ons",
    "delivery_date": "Delivery date", "item_price": "Item price", "shipping": "Shipping",
    "fees": "Fees", "tax": "Tax", "total_price": "Total price",
}


def result_field_and_label(constraint_name: str, category: str | None = None) -> tuple[str, str]:
    """The (field, label) the UI shows for one ConstraintResult name."""
    if constraint_name in RULE_FIELDS:
        return RULE_FIELDS[constraint_name]
    if constraint_name.startswith("constraint["):
        parts = constraint_name.split(":")
        field = parts[1] if len(parts) >= 2 else constraint_name
        label = FIELD_LABELS.get(field, field.replace("_", " ").capitalize())
        if field == "size" and category and "shoe" in category.lower():
            label = "Shoe size"
        return field, label
    return constraint_name, constraint_name.replace("_", " ").capitalize()


def decision_for_api(decision: ValidationDecision | None, category: str | None = None) -> dict[str, Any] | None:
    """
    A ValidationDecision as JSON, with two extra keys on every result: field and label.
    The ConstraintResult model itself is unchanged; the keys are added only here, when serializing.
    """
    if decision is None:
        return None
    data = decision.model_dump(mode="json")
    for item in data["results"]:
        item["field"], item["label"] = result_field_and_label(item["constraint"], category)
    return data


def payment_for_api(row: db.PaymentRow | None) -> dict[str, Any] | None:
    """The payment as the frontend sees it. No secrets: at most last4, never a card, token, or file path."""
    if row is None:
        return None
    return {
        "state": row.state,
        "provider": row.provider,
        "provider_label": "Stripe Link: TEST MODE" if row.provider == "link_test" else "Simulated provider",
        "approval_url": row.approval_url,
        "provider_reference": row.provider_request_id,
        "amount": float(Decimal(row.amount_minor) / 100),
        "pay_amount": float(Decimal(row.pay_amount_minor) / 100),
        "currency": row.currency,
        "last4": row.card_last4,
        "credential_released": row.credential_released_at is not None,
        "order_id": row.order_id,
        "receipt": row.receipt,
        "last_error": row.last_error,
        "updated_at": db.utc(row.updated_at).isoformat() if row.updated_at else None,
    }


def resolution_from_evidence(session: Session, purchase_id: str) -> dict[str, Any] | None:
    """Whether (and how) a human decided an escalation or declined, read from the evidence we already record."""
    for row in reversed(db.list_evidence_for_purchase(session, purchase_id)):
        data = row.data.get("data", {})
        if data.get("human_approval"):
            return {
                "action": "approve",
                "resolved_at": row.data.get("timestamp"),
                "accepted_constraints": [c.get("constraint") for c in data.get("accepted_constraints", [])],
                "note": data.get("note"),
            }
        if data.get("human_rejection"):
            return {
                "action": "reject" if data.get("kind") != "purchase_declined" else "decline",
                "resolved_at": row.data.get("timestamp"),
                "accepted_constraints": [],
                "note": data.get("note"),
            }
    return None


def next_action_for(purchase: Purchase, payment: db.PaymentRow | None) -> str:
    """A short machine-readable hint for the agent and the UI about what happens next."""
    status = purchase.status
    if status == PurchaseStatus.COMPLETED:
        return "completed"
    if status == PurchaseStatus.ESCALATED:
        return "wait_for_user_decision"
    if status in (PurchaseStatus.BLOCKED, PurchaseStatus.FAILED):
        if payment is not None and payment.state in (payments.EXPIRED, payments.CHECKOUT_CHANGED):
            return "request_purchase_again"
        return "blocked_no_action"
    if status != PurchaseStatus.AUTHORIZED:
        return "wait"
    if payment is None:
        return "wait"
    return {
        payments.AWAITING_APPROVAL: "wait_for_user_link_approval",
        payments.APPROVED: "wait_for_revalidation",
        payments.REVALIDATING: "wait_for_revalidation",
        payments.CREDENTIAL_READY: "get_payment_credential_and_pay" if get_settings().credential_mode == "agent_visible" else "wait_for_payment",
        payments.PAYING: "wait_for_merchant_order",
        payments.PAID: "wait_for_order_verification",
        payments.UNKNOWN: "wait_for_reconciliation",
    }.get(payment.state, "wait")


def purchase_detail(session: Session, purchase: Purchase, **extra: Any) -> dict[str, Any]:
    """
    GET /purchases/{id} (and every purchase response): a SUPERSET of the original
    PurchaseResponse, plus the full purchase, proposal, payment, resolution,
    next_action, and links. Nothing here contains card data.
    """
    decision = db.load_decision(session, purchase.decision_id)
    credential = db.load_credential(session, purchase.credential_id)
    proposal_row = db.get_proposal_row(session, purchase.proposal_id) if purchase.proposal_id else None
    contract_row = db.get_contract_row(session, purchase.contract_id)
    category = contract_row.data.get("category") if contract_row is not None else None
    payment = db.get_payment(session, purchase.id)
    summary_card = credential_summary(credential)
    return {
        # --- the original PurchaseResponse keys (unchanged) ---
        "purchase_id": purchase.id,
        "status": purchase.status.value,
        "decision": decision_for_api(decision, category),
        "contract_id": purchase.contract_id,
        "proposal_id": purchase.proposal_id,
        "credential": summary_card.model_dump(mode="json") if summary_card else None,
        "summary": summarize(purchase, decision),
        "idempotent_replay": bool(extra.get("idempotent_replay")),
        # --- the superset for Rohan's PurchaseDetail ---
        "purchase": purchase.model_dump(mode="json"),
        "proposal": proposal_row.data if proposal_row else None,
        "payment": payment_for_api(payment),
        "payment_state": payment.state if payment else None,
        "approval_url": payment.approval_url if payment else None,
        "resolution": resolution_from_evidence(session, purchase.id),
        "next_action": next_action_for(purchase, payment),
        "review_url": purchase_url_for(purchase.id),
    }


def list_purchase_outcomes(session: Session, owner: str, contract_id: str | None = None) -> list[dict[str, Any]]:
    """The owner's purchases (optionally for one contract), newest first, as {purchase, decision, credential}."""
    outcomes: list[dict[str, Any]] = []
    for purchase in db.list_purchases(session, owner, contract_id):
        outcomes.append(
            {
                "purchase": purchase,
                "decision": db.load_decision(session, purchase.decision_id),
                "credential": db.load_credential(session, purchase.credential_id),
            }
        )
    return outcomes


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


def evidence_chain(session: Session, purchase_id: str, owner: str | None = None) -> dict[str, Any]:
    """Everything the frontend needs to tell one purchase's story, in a single response."""
    purchase = get_owned_purchase(session, purchase_id, owner)

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
