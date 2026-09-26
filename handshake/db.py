"""
db.py: the database layer (SQLAlchemy 2.x ORM).

Job in the system
-----------------
This file owns everything about storage: which database to connect to, what
the tables look like, and small save/load helpers. It knows nothing about
rules or HTTP.

    api.py       -> gets a Session per request from get_session()
    services.py  -> calls the save/load/update helpers below
    intent_diff  -> never touches the database (it stays pure)

Design choices
--------------
- Every table has a few indexed columns for querying (id, status, contract_id,
  ...) PLUS a `data` JSON column holding the full Pydantic model as
  model_dump(mode="json"). The columns make lookups fast; the JSON makes sure
  nothing is ever lost. When a value exists in both places (like status), the
  helpers here always update both together.
- DATABASE_URL picks the database. The default is a local SQLite file. Tests
  call configure() with a temporary file so they never touch the dev data.
- There are no migrations (no Alembic); tables are created on startup.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from handshake.config import get_settings
from handshake.models import (
    Contract,
    ContractDraft,
    ContractStatus,
    Credential,
    EvidenceEvent,
    Purchase,
    TransactionProposal,
    ValidationDecision,
)



# ============================================================
# Engine and sessions
# ============================================================
#
# The "engine" is the connection pool to the database. A "session" is one
# unit of work: you add/change objects, then commit() to save them all at
# once, or rollback() to throw them all away. Each HTTP request gets its own
# session.

engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def configure(url: str | None = None) -> None:
    """(Re)point this module at a database URL. With no argument, use DATABASE_URL from config.py."""
    global engine, SessionLocal
    if url is None:
        url = get_settings().database_url

    # SQLite normally refuses to share a connection across threads. FastAPI's
    # TestClient and uvicorn's thread pool do exactly that, so we turn the
    # check off. This is safe here because each request uses its own session.
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(url, connect_args=connect_args)
    # expire_on_commit=False keeps loaded objects readable after commit().
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def create_tables() -> None:
    """Create any tables that do not exist yet (called on app startup and in tests)."""
    Base.metadata.create_all(engine)


def database_type() -> str:
    """Name of the database dialect in use, e.g. 'sqlite' or 'postgresql' (shown by /health)."""
    if engine is None:
        return "unconfigured"
    return engine.dialect.name


def get_session() -> Iterator[Session]:
    """FastAPI dependency: open one session for the request and always close it afterwards."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _dump(model: Any) -> dict[str, Any]:
    """Serialize a Pydantic model to plain JSON-compatible data (datetimes become ISO strings)."""
    return model.model_dump(mode="json")


def utc(dt: datetime | None) -> datetime | None:
    """
    Re-attach UTC to a datetime read from a column.

    SQLite has no real timezone type, so it hands back naive datetimes even
    though we stored aware UTC ones. The JSON copies keep their timezone; this
    helper is only for the few places we read the plain columns.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ============================================================
# Tables
# ============================================================


class Base(DeclarativeBase):
    """Parent class for all ORM tables below."""


# Why a separate drafts table instead of storing drafts in `contracts` with
# status=draft? A draft and a signed contract are different Pydantic models
# with different ids (signing creates a NEW contract_... id), and a draft has
# no hash or signature yet. A separate table keeps `contracts` strictly
# "things a user signed" with no half-empty rows. The draft row remembers
# which contract it produced (signed_contract_id), which is also how we
# refuse to sign the same draft twice.
class DraftRow(Base):
    """An unsigned contract draft produced by Ajay's compiler."""

    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # The user (email) who owns this record. Every read and write checks it.
    owner: Mapped[str] = mapped_column(String, index=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    signed_contract_id: Mapped[str | None] = mapped_column(String, nullable=True)
    data: Mapped[dict] = mapped_column(JSON)
    # Compiler metadata: assumptions, clarifications_needed, compiler_notes.
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class ContractRow(Base):
    """A signed contract. `status` column is the authoritative lifecycle state."""

    __tablename__ = "contracts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # The user (email) who owns this record. Every read and write checks it.
    owner: Mapped[str] = mapped_column(String, index=True, default="")
    draft_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)
    contract_hash: Mapped[str] = mapped_column(String)
    signature: Mapped[str] = mapped_column(String)
    agent_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data: Mapped[dict] = mapped_column(JSON)


class ProposalRow(Base):
    """An extracted checkout (TransactionProposal), plus the raw payload exactly as received."""

    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    contract_id: Mapped[str] = mapped_column(String, index=True)
    purchase_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    merchant_name: Mapped[str] = mapped_column(String)
    total: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    # The dotted paths that were explicitly present in the raw payload (see
    # intent_diff.explicit_fields). We must store this: once the proposal is
    # re-loaded from `data`, every field is present in the JSON and would
    # look "explicitly sent", which would defeat the fail-closed check.
    fields_set: Mapped[list] = mapped_column(JSON)


class DecisionRow(Base):
    """The engine's decision, including every individual constraint result (inside `data`)."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    contract_id: Mapped[str] = mapped_column(String, index=True)
    proposal_id: Mapped[str] = mapped_column(String, index=True)
    verdict: Mapped[str] = mapped_column(String)  # ValidationDecision.verdict (hard results only)
    outcome: Mapped[str] = mapped_column(String, index=True)  # BLOCKED / ESCALATED / AUTHORIZED
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSON)


class PurchaseRow(Base):
    """One purchase attempt, from submission to authorization/block/completion."""

    __tablename__ = "purchases"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # The user (email) who owns this record. Every read and write checks it.
    owner: Mapped[str] = mapped_column(String, index=True, default="")
    contract_id: Mapped[str] = mapped_column(String, index=True)
    proposal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    credential_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)
    merchant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    authorized_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    charged_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict] = mapped_column(JSON)


class CredentialRow(Base):
    """A single-use stub credential. Holds no card number, CVV, or reusable secret."""

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    contract_id: Mapped[str] = mapped_column(String, index=True)
    proposal_id: Mapped[str] = mapped_column(String)
    merchant_name: Mapped[str] = mapped_column(String)
    max_amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    data: Mapped[dict] = mapped_column(JSON)


# The evidence ledger is APPEND-ONLY BY DESIGN. This module has an insert
# helper and read helpers for it, and deliberately no update or delete
# helper. Nothing in the app can rewrite history.
#
# It is also a simple hash chain. Each event stores:
#   prev_hash  = the event_hash of the previous event for the same contract
#   event_hash = SHA-256 of (this event's contents + prev_hash)
# Because each hash includes the one before it, editing ANY old event changes
# its hash, which no longer matches the next event's prev_hash, and so on
# down the line. verify_evidence_chain() walks the chain and spots that.
# (Someone with database access could recompute the whole chain, so the
# production version would anchor the latest hash somewhere external. That
# is P2.)
#
# `sequence` is a strictly increasing integer so ordering is unambiguous even
# when two events have the same timestamp.
class EvidenceRow(Base):
    """One entry in the append-only, hash-chained evidence ledger."""

    __tablename__ = "evidence_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    contract_id: Mapped[str] = mapped_column(String, index=True)
    purchase_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON)
    prev_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    event_hash: Mapped[str] = mapped_column(String)


# ============================================================
# Drafts
# ============================================================


def save_draft(session: Session, draft: ContractDraft, meta: dict[str, Any] | None = None, owner: str = "") -> None:
    """Insert a new unsigned draft (plus compiler metadata) owned by `owner`."""
    session.add(
        DraftRow(
            id=draft.id,
            owner=owner,
            created_at=draft.created_at,
            data=_dump(draft),
            meta=meta or {},
        )
    )


def get_draft_row(session: Session, draft_id: str) -> DraftRow | None:
    """Fetch the raw draft row, or None."""
    return session.get(DraftRow, draft_id)


def load_draft(row: DraftRow) -> ContractDraft:
    """Rebuild the ContractDraft model from a row."""
    return ContractDraft.model_validate(row.data)


def mark_draft_signed(session: Session, draft_id: str, contract_id: str) -> None:
    """Record which contract a draft became, so it cannot be signed again."""
    session.get(DraftRow, draft_id).signed_contract_id = contract_id


def list_drafts(session: Session, owner: str | None = None) -> list[DraftRow]:
    """All drafts (of `owner`, if given), newest first."""
    query = select(DraftRow).order_by(DraftRow.created_at.desc())
    if owner is not None:
        query = query.where(DraftRow.owner == owner)
    return list(session.scalars(query))


# ============================================================
# Contracts
# ============================================================


def save_contract(session: Session, contract: Contract, draft_id: str | None = None, owner: str = "") -> None:
    """Insert a newly signed contract owned by `owner`."""
    session.add(
        ContractRow(
            id=contract.id,
            owner=owner,
            draft_id=draft_id,
            status=contract.status.value,
            contract_hash=contract.contract_hash,
            signature=contract.signature,
            agent_key=contract.agent_key,
            created_at=contract.created_at,
            signed_at=contract.signed_at,
            expires_at=contract.expires_at,
            data=_dump(contract),
        )
    )


def get_contract_row(session: Session, contract_id: str) -> ContractRow | None:
    """Fetch the raw contract row, or None."""
    return session.get(ContractRow, contract_id)


def contract_from_row(row: ContractRow) -> Contract:
    """
    Rebuild the Contract model from a row. Raises pydantic.ValidationError if the stored JSON is invalid.

    The status COLUMN wins over the status inside the JSON, because the
    column is what the atomic double-spend update below changes.
    """
    return Contract.model_validate({**row.data, "status": row.status})


def load_contract(session: Session, contract_id: str) -> Contract | None:
    """Fetch and rebuild a Contract, or None if the id is unknown."""
    row = session.get(ContractRow, contract_id)
    if row is None:
        return None
    return contract_from_row(row)


def update_contract_status(session: Session, contract_id: str, status: ContractStatus) -> None:
    """Change a contract's status in both the column and the JSON copy."""
    row = session.get(ContractRow, contract_id)
    row.status = status.value
    # Assign a new dict (not mutate in place) so SQLAlchemy notices the change.
    row.data = {**row.data, "status": status.value}


def claim_single_use_contract(session: Session, contract_id: str) -> bool:
    """
    Atomically flip a contract from ACTIVE to USED. Returns False if it was not ACTIVE at that instant.

    This is the double-spend guard. The naive version is two steps:
        1. SELECT status        -> "active"
        2. UPDATE status='used'
    Two purchases running at the same moment could both do step 1, both see
    "active", and both get authorized. That is a double spend.

    Instead we do ONE conditional statement:
        UPDATE contracts SET status='used' WHERE id=:id AND status='active'
    The database runs a single UPDATE atomically. If two run at once, the
    first one changes the row; the second one's WHERE no longer matches, so
    it changes 0 rows. We check `rowcount` to see whether we won. This works
    the same way on SQLite and PostgreSQL.

    The caller must commit (to keep it) or roll back (to undo it).
    """
    statement = (
        update(ContractRow)
        .where(ContractRow.id == contract_id)
        .where(ContractRow.status == ContractStatus.ACTIVE.value)
        .values(status=ContractStatus.USED.value)
        .execution_options(synchronize_session=False)
    )
    outcome = session.execute(statement)
    if outcome.rowcount != 1:
        return False  # someone else already used, revoked, or expired it

    # We won. Keep the JSON copy's status in sync too. refresh() re-reads the
    # row because the UPDATE above went straight to the database.
    row = session.get(ContractRow, contract_id)
    session.refresh(row)
    row.data = {**row.data, "status": ContractStatus.USED.value}
    return True


def claim_purchase_status(session: Session, purchase_id: str, from_status: str, to_status: str) -> bool:
    """
    Atomically move a purchase from one status to another. Returns False if it was not in `from_status`.

    Same idea as claim_single_use_contract, applied to the purchase itself:
        UPDATE purchases SET status=:to WHERE id=:id AND status=:from
    Human approval uses it (ESCALATED -> AUTHORIZED) so two approve clicks
    arriving at the same moment cannot both issue a credential. That matters
    most for multi-use contracts, where there is no ACTIVE -> USED flip on
    the contract to stop the second one.

    The caller must commit (to keep it) or roll back (to undo it).
    """
    statement = (
        update(PurchaseRow)
        .where(PurchaseRow.id == purchase_id)
        .where(PurchaseRow.status == from_status)
        .values(status=to_status)
        .execution_options(synchronize_session=False)
    )
    outcome = session.execute(statement)
    if outcome.rowcount != 1:
        return False  # already approved, rejected, or never escalated

    row = session.get(PurchaseRow, purchase_id)
    session.refresh(row)
    row.data = {**row.data, "status": to_status}
    return True


def list_contracts(session: Session, owner: str | None = None) -> list[ContractRow]:
    """All signed contracts (of `owner`, if given), newest first."""
    query = select(ContractRow).order_by(ContractRow.signed_at.desc())
    if owner is not None:
        query = query.where(ContractRow.owner == owner)
    return list(session.scalars(query))


# ============================================================
# Proposals and decisions
# ============================================================


def save_proposal(
    session: Session,
    proposal: TransactionProposal,
    raw_payload: dict[str, Any],
    fields_set: set[str],
    purchase_id: str | None,
) -> None:
    """Insert a parsed proposal, the raw payload as received, and which fields were explicitly sent."""
    session.add(
        ProposalRow(
            id=proposal.id,
            contract_id=proposal.contract_id,
            purchase_id=purchase_id,
            merchant_name=proposal.merchant.name,
            total=proposal.total,
            currency=proposal.currency,
            data=_dump(proposal),
            raw_payload=raw_payload,
            fields_set=sorted(fields_set),  # sorted so the stored list is deterministic
        )
    )


def get_proposal_row(session: Session, proposal_id: str) -> ProposalRow | None:
    """Fetch the raw proposal row, or None."""
    return session.get(ProposalRow, proposal_id)


def save_decision(session: Session, decision: ValidationDecision, outcome: str) -> None:
    """Insert a decision with every individual result, plus the purchase outcome it mapped to."""
    session.add(
        DecisionRow(
            id=decision.id,
            contract_id=decision.contract_id,
            proposal_id=decision.proposal_id,
            verdict=decision.verdict.value,
            outcome=outcome,
            evaluated_at=decision.evaluated_at,
            data=_dump(decision),
        )
    )


def load_decision(session: Session, decision_id: str | None) -> ValidationDecision | None:
    """Fetch and rebuild a ValidationDecision, or None."""
    if not decision_id:
        return None
    row = session.get(DecisionRow, decision_id)
    if row is None:
        return None
    return ValidationDecision.model_validate(row.data)


# ============================================================
# Purchases
# ============================================================


def _purchase_columns(purchase: Purchase) -> dict[str, Any]:
    """The column values for a purchase row (shared by insert and update)."""
    return {
        "contract_id": purchase.contract_id,
        "proposal_id": purchase.proposal_id,
        "decision_id": purchase.decision_id,
        "credential_id": purchase.credential_id,
        "status": purchase.status.value,
        "merchant_name": purchase.merchant_name,
        "authorized_amount": purchase.authorized_amount,
        "charged_amount": purchase.charged_amount,
        "currency": purchase.currency,
        "created_at": purchase.created_at,
        "completed_at": purchase.completed_at,
        "error": purchase.error,
        "data": _dump(purchase),
    }


def save_purchase(session: Session, purchase: Purchase, owner: str = "") -> None:
    """Insert a new purchase owned by `owner` (always the contract's owner)."""
    session.add(PurchaseRow(id=purchase.id, owner=owner, **_purchase_columns(purchase)))


def update_purchase(session: Session, purchase: Purchase) -> None:
    """Overwrite an existing purchase row with the given model's values."""
    row = session.get(PurchaseRow, purchase.id)
    for column, value in _purchase_columns(purchase).items():
        setattr(row, column, value)


def load_purchase(session: Session, purchase_id: str) -> Purchase | None:
    """Fetch and rebuild a Purchase, or None."""
    row = session.get(PurchaseRow, purchase_id)
    if row is None:
        return None
    return Purchase.model_validate(row.data)


# ============================================================
# Credentials
# ============================================================


def _credential_columns(credential: Credential) -> dict[str, Any]:
    """The column values for a credential row (shared by insert and update)."""
    return {
        "contract_id": credential.contract_id,
        "proposal_id": credential.proposal_id,
        "merchant_name": credential.merchant_name,
        "max_amount": credential.max_amount,
        "currency": credential.currency,
        "status": credential.status.value,
        "created_at": credential.created_at,
        "expires_at": credential.expires_at,
        "provider_reference": credential.provider_reference,
        "data": _dump(credential),
    }


def save_credential(session: Session, credential: Credential) -> None:
    """Insert a new credential."""
    session.add(CredentialRow(id=credential.id, **_credential_columns(credential)))


def update_credential(session: Session, credential: Credential) -> None:
    """Overwrite an existing credential row (used for status changes)."""
    row = session.get(CredentialRow, credential.id)
    for column, value in _credential_columns(credential).items():
        setattr(row, column, value)


def load_credential(session: Session, credential_id: str | None) -> Credential | None:
    """Fetch and rebuild a Credential, or None."""
    if not credential_id:
        return None
    row = session.get(CredentialRow, credential_id)
    if row is None:
        return None
    return Credential.model_validate(row.data)


def list_credentials_for_contract(session: Session, contract_id: str) -> list[Credential]:
    """Every credential ever issued under a contract."""
    query = select(CredentialRow).where(CredentialRow.contract_id == contract_id)
    return [Credential.model_validate(row.data) for row in session.scalars(query)]


# ============================================================
# Evidence ledger (insert + read only; see the EvidenceRow comment)
# ============================================================


def _event_hash(event_json: dict[str, Any], prev_hash: str | None) -> str:
    """
    SHA-256 over an event's contents plus the previous event's hash.

    sort_keys + fixed separators give one exact byte string for the same
    data every time; without that, {"a":1,"b":2} and {"b":2,"a":1} would hash
    differently even though they mean the same thing.
    """
    payload = json.dumps(
        {"event": event_json, "prev_hash": prev_hash},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append_evidence(session: Session, event: EvidenceEvent) -> EvidenceEvent:
    """Append one event to the ledger, linking it to the previous event for the same contract."""
    # Push any earlier un-flushed events to the database first so the two
    # queries below can see them.
    session.flush()

    # Next global sequence number. The UNIQUE constraint on `sequence` means a
    # rare concurrent collision fails loudly instead of silently duplicating.
    current_max = session.scalar(select(func.max(EvidenceRow.sequence)))
    next_sequence = (current_max or 0) + 1

    # The hash of the latest event for this contract (None for the first one).
    prev_hash = session.scalar(
        select(EvidenceRow.event_hash)
        .where(EvidenceRow.contract_id == event.contract_id)
        .order_by(EvidenceRow.sequence.desc())
        .limit(1)
    )

    event_json = _dump(event)
    session.add(
        EvidenceRow(
            id=event.id,
            sequence=next_sequence,
            contract_id=event.contract_id,
            purchase_id=event.purchase_id,
            event_type=event.event_type.value,
            timestamp=event.timestamp,
            message=event.message,
            data=event_json,
            prev_hash=prev_hash,
            event_hash=_event_hash(event_json, prev_hash),
        )
    )
    session.flush()
    return event


def event_from_row(row: EvidenceRow) -> dict[str, Any]:
    """An evidence event as a plain dict for the API, including its sequence and chain hashes."""
    return {
        **row.data,
        "sequence": row.sequence,
        "prev_hash": row.prev_hash,
        "event_hash": row.event_hash,
    }


def list_evidence_for_purchase(session: Session, purchase_id: str) -> list[EvidenceRow]:
    """All events attached to a purchase, in sequence order."""
    query = select(EvidenceRow).where(EvidenceRow.purchase_id == purchase_id).order_by(EvidenceRow.sequence)
    return list(session.scalars(query))


def list_evidence_for_contract(session: Session, contract_id: str) -> list[EvidenceRow]:
    """All events for a contract (or draft) id, in sequence order."""
    query = select(EvidenceRow).where(EvidenceRow.contract_id == contract_id).order_by(EvidenceRow.sequence)
    return list(session.scalars(query))


def verify_evidence_chain(session: Session, contract_id: str) -> bool:
    """Walk a contract's chain and recompute every hash. False means some event was edited."""
    expected_prev: str | None = None
    for row in list_evidence_for_contract(session, contract_id):
        # The link must point at the event before it...
        if row.prev_hash != expected_prev:
            return False
        # ...and the stored hash must match the event's current contents.
        if row.event_hash != _event_hash(row.data, expected_prev):
            return False
        expected_prev = row.event_hash
    return True


def count_credentials_for_contract(session: Session, contract_id: str) -> int:
    """How many credentials exist for a contract (used by tests to prove there was no second credential)."""
    query = select(func.count()).select_from(CredentialRow).where(CredentialRow.contract_id == contract_id)
    return session.scalar(query) or 0


# Connect to DATABASE_URL (or the default SQLite file) as soon as the module
# is imported. Tests call configure() again with a temporary database.
configure()
