"""
test_api.py: end-to-end tests through the real HTTP API (FastAPI TestClient).

Every test gets a fresh temporary SQLite database (the `client` fixture in
conftest.py), creates and signs the demo contract, submits checkouts, and
checks the responses, the database, and the evidence chain.

The block marked "Section 12 checklist" maps one-to-one to the required API
cases; "Pre-purchase rejections" covers the part B change.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from handshake import db, services
from handshake.config import get_settings
from conftest import STATIC_EXTRACTOR, Mutator, api_draft_payload, fund, proposal_payload


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------


def sign(client: TestClient, **draft_overrides: Any) -> str:
    """Create the demo draft (with optional overrides), sign it, and return the new contract id."""
    draft = {**api_draft_payload(), **draft_overrides}
    assert client.post("/contracts", json=draft).status_code == 201
    response = client.post(f"/contracts/{draft['id']}/sign", json={"draft_id": draft["id"], "agent_key": "agent_1"})
    assert response.status_code == 200, response.json()
    fund(client, response.json()["id"])  # sign = fund: approve the contract's card (simulated)
    return response.json()["id"]


def purchase(client: TestClient, contract_id: str, mutate: Mutator | None = None, **extra: Any) -> Response:
    """
    Submit a purchase whose checkout contains the passing proposal (optionally edited by `mutate`).

    The proposal is no longer posted by the caller: it is loaded into the
    StaticExtractor, which plays the part of Handshake reading the checkout.
    """
    proposal = proposal_payload(contract_id)
    if mutate is not None:
        mutate(proposal)
    STATIC_EXTRACTOR.set(proposal)
    body = {"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout", **extra}
    return client.post("/purchases", json=body)


def event_types(client: TestClient, purchase_id: str) -> list[str]:
    """The ordered list of evidence event types for a purchase."""
    return [e["event_type"] for e in client.get(f"/evidence/{purchase_id}").json()["events"]]


def credential_count(contract_id: str) -> int:
    """How many credentials exist in the database for a contract."""
    with db.SessionLocal() as session:
        return db.count_credentials_for_contract(session, contract_id)


def purchase_count() -> int:
    """How many purchase rows exist in the database."""
    with db.SessionLocal() as session:
        return session.query(db.PurchaseRow).count()


def evidence_count() -> int:
    """How many evidence rows exist in the database."""
    with db.SessionLocal() as session:
        return session.query(db.EvidenceRow).count()


def tamper_with_cap(contract_id: str, new_cap: float) -> None:
    """Edit the stored contract JSON directly, bypassing the app, as an attacker with DB access would."""
    with db.SessionLocal() as session:
        row = db.get_contract_row(session, contract_id)
        row.data = {**row.data, "spend": {**row.data["spend"], "hard_cap_all_in": new_cap}}
        session.commit()


class ServiceResult:
    """A tiny stand-in for an HTTP response, for service-layer calls (same .status_code / .json())."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        """Keep the status code and JSON body."""
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        """The JSON body, like httpx.Response.json()."""
        return self._body


def complete(purchase_id: str, charged_amount: float) -> ServiceResult:
    """
    Reconcile a purchase through the SERVICE layer.

    POST /purchases/{id}/complete is internal now (403 over HTTP; the payment
    flow calls it after verifying the merchant order). These tests keep their
    exact assertions by calling the same service function and rendering the
    result exactly as the old route did.
    """
    from handshake.api import _purchase_response
    from handshake.services import ServiceError

    with db.SessionLocal() as session:
        try:
            outcome = services.complete_purchase(session, purchase_id, charged_amount)
            return ServiceResult(200, _purchase_response(outcome))
        except ServiceError as exc:
            return ServiceResult(exc.status_code, {"error": exc.code, "message": exc.message, "details": exc.details})


# The full expected evidence story for a successful purchase.
HAPPY_PATH_EVENTS = [
    "contract_created",
    "contract_signed",
    # Sign = fund (contract-level): the card is requested, approved (simulated), and stored.
    "contract_signed",  # data.kind funding_requested
    "contract_signed",  # data.kind simulated_provider_approval
    "credential_created",  # data.kind card_stored (encrypted, locked)
    "shopping_started",
    "proposal_created",
    "validation_started",
    "validation_completed",
    "purchase_authorized",
    "credential_created",
    # The authorized checkout unlocks the stored card for the agent: data.kind "credential_ready".
    "purchase_authorized",
]


# ============================================================
# Section 12 checklist (API)
# ============================================================


# 1. Full happy path
def test_full_happy_path_end_to_end(client: TestClient) -> None:
    """Create -> sign -> purchase: AUTHORIZED, exact-amount credential, contract USED, full evidence story."""
    created = client.post("/contracts", json=api_draft_payload())
    assert created.status_code == 201
    signed = client.post("/contracts/draft_demo_shoes/sign", json={"draft_id": "draft_demo_shoes"})
    assert signed.status_code == 200
    contract_id = signed.json()["id"]
    fund(client, contract_id)

    response = purchase(client, contract_id)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "authorized"

    # The credential is for the proposal total (128.39), NOT the contract cap (135.00).
    assert body["credential"]["max_amount"] == 128.39
    assert body["credential"]["max_amount"] != signed.json()["spend"]["hard_cap_all_in"]

    # The single-use contract has been consumed.
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "used"

    # Every expected event, in order.
    assert event_types(client, body["purchase_id"]) == HAPPY_PATH_EVENTS


# 2. Blocked path
def test_over_cap_is_blocked(client: TestClient) -> None:
    """Over the cap: BLOCKED, no credential anywhere, contract stays ACTIVE, chain ends in purchase_blocked."""
    contract_id = sign(client)
    body = purchase(client, contract_id, lambda p: p.update(item_subtotal=130.00, total=138.40, line_items=[{**p["line_items"][0], "unit_price": 130.00}])).json()

    assert body["status"] == "blocked"
    assert body["credential"] is None
    assert credential_count(contract_id) == 0
    assert "exceeds the signed all-in cap" in body["summary"]
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "active"
    assert event_types(client, body["purchase_id"])[-1] == "purchase_blocked"


# 3. Total does not add up
def test_total_mismatch_becomes_logged_block(client: TestClient) -> None:
    """A fudged total is recorded as BLOCKED with a total_integrity FAIL, not a bare 422."""
    contract_id = sign(client)
    response = purchase(client, contract_id, lambda p: p.update(total=100.00))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"

    results = body["decision"]["results"]
    assert [r["constraint"] for r in results] == ["total_integrity"]
    assert results[0]["verdict"] == "fail"
    assert results[0]["expected"] == "128.39 USD"
    assert results[0]["actual"] == "100.00 USD"

    evidence = client.get(f"/evidence/{body['purchase_id']}").json()
    assert evidence["proposal"]["total"] == 100.0
    assert "proposal_created" in [e["event_type"] for e in evidence["events"]]


# 4. Second purchase on a used single-use contract
def test_second_purchase_on_single_use_contract_rejected(client: TestClient) -> None:
    """The second purchase is rejected, and exactly one credential exists in the database."""
    contract_id = sign(client)
    assert purchase(client, contract_id).json()["status"] == "authorized"

    second = purchase(client, contract_id, lambda p: p.update(id="proposal_second"))
    assert second.status_code == 409
    assert second.json()["error"] == "contract_used"
    assert credential_count(contract_id) == 1


# 5. Revoked contract
def test_revoked_contract_rejects_purchase(client: TestClient) -> None:
    """After revocation, purchases are rejected (and revoking twice is a conflict)."""
    contract_id = sign(client)
    assert client.post(f"/contracts/{contract_id}/revoke").json()["status"] == "revoked"

    response = purchase(client, contract_id)
    assert response.status_code == 409
    assert response.json()["error"] == "contract_revoked"
    assert credential_count(contract_id) == 0
    assert client.post(f"/contracts/{contract_id}/revoke").status_code == 409


# 6. Tampered contract
def test_tampered_contract_is_rejected(client: TestClient) -> None:
    """Raising the cap in the stored JSON breaks verification, and purchases are rejected."""
    contract_id = sign(client)
    tamper_with_cap(contract_id, 5000.0)

    detail = client.get(f"/contracts/{contract_id}").json()
    assert detail["verification"]["valid"] is False
    assert detail["verification"]["hash_matches"] is False

    response = purchase(client, contract_id)
    assert response.status_code == 409
    assert response.json()["error"] == "contract_tampered"
    assert credential_count(contract_id) == 0


# 7. No raw payment credential anywhere in any response
FORBIDDEN_KEY_TOKENS = {"pan", "cvv", "cvv2", "cvc", "cvc2", "cid", "pin", "secret", "password"}
FORBIDDEN_KEY_PHRASES = (
    "cardnumber", "accountnumber", "routingnumber", "securitycode",
    "cardverification", "track1", "track2", "providerreference", "rawtoken",
)
CARD_NUMBER_PATTERN = re.compile(r"\b\d{13,19}\b")  # a bare 13-19 digit run looks like a PAN


# The ONE allowed "provider_reference": the payment's spend-request id (section 6.4),
# which identifies a request and can't be used to pay. Its value is checked.
PROVIDER_REQUEST_ID = re.compile(r"^(lsrq_|stub_sr_)[A-Za-z0-9_]+$")


def find_credential_leaks(value: Any, path: str = "$") -> list[str]:
    """Walk JSON recursively; return the paths of keys/values that look like raw payment credentials."""
    leaks: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            lowered = str(key).lower()
            tokens = set(re.split(r"[_\-\s.]+", lowered))
            squashed = re.sub(r"[_\-\s.]", "", lowered)
            if (path.endswith(".payment") or path.endswith(".funding") or path == "$") and lowered == "provider_reference" and (inner is None or PROVIDER_REQUEST_ID.match(str(inner))):
                continue
            if tokens & FORBIDDEN_KEY_TOKENS or any(p in squashed for p in FORBIDDEN_KEY_PHRASES):
                leaks.append(f"{path}.{key}")
            leaks.extend(find_credential_leaks(inner, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            leaks.extend(find_credential_leaks(inner, f"{path}[{index}]"))
    elif isinstance(value, str) and CARD_NUMBER_PATTERN.search(value):
        leaks.append(f"{path} = {value!r}")
    return leaks


def test_no_raw_credential_in_any_happy_path_response(client: TestClient) -> None:
    """Every JSON body from every endpoint in the happy path is free of card-number/CVV-like keys and values."""
    bodies: dict[str, Any] = {}
    bodies["POST /contracts"] = client.post("/contracts", json=api_draft_payload()).json()
    bodies["GET /contracts/{draft}"] = client.get("/contracts/draft_demo_shoes").json()
    signed = client.post("/contracts/draft_demo_shoes/sign", json={"draft_id": "draft_demo_shoes"}).json()
    bodies["POST /contracts/{id}/sign"] = signed
    contract_id = signed["id"]
    bodies["POST /contracts/{id}/funding/simulate-approval"] = fund(client, contract_id)
    bodies["GET /contracts"] = client.get("/contracts").json()
    bodies["GET /contracts/{id}"] = client.get(f"/contracts/{contract_id}").json()

    created = purchase(client, contract_id).json()
    assert created["status"] == "authorized"
    bodies["POST /purchases"] = created
    purchase_id = created["purchase_id"]
    bodies["GET /purchases/{id}"] = client.get(f"/purchases/{purchase_id}").json()
    bodies["GET /evidence/{id} (authorized)"] = client.get(f"/evidence/{purchase_id}").json()
    bodies["POST /purchases/{id}/complete"] = complete(purchase_id, 128.39).json()
    bodies["GET /evidence/{id} (completed)"] = client.get(f"/evidence/{purchase_id}").json()
    bodies["GET /health"] = client.get("/health").json()

    for endpoint, body in bodies.items():
        assert find_credential_leaks(body) == [], endpoint

    # Sanity-check the scanner itself so this test cannot pass vacuously.
    assert find_credential_leaks({"card_number": "x"}) == ["$.card_number"]
    assert find_credential_leaks({"a": {"CVV": 1}}) == ["$.a.CVV"]
    assert find_credential_leaks({"note": "4111111111111111"}) != []
    assert find_credential_leaks({"credential": {"provider_reference": "stub_abc"}}) != []  # the internal one still counts
    assert find_credential_leaks({"payment": {"provider_reference": "not-a-request-id"}}) != []


# ============================================================
# Pre-purchase rejections (part B): a BLOCKED purchase with evidence
# ============================================================


def _make_tampered(client: TestClient) -> str:
    """A signed contract whose stored cap was then edited."""
    contract_id = sign(client)
    tamper_with_cap(contract_id, 5000.0)
    return contract_id


def _make_revoked(client: TestClient) -> str:
    """A signed, then revoked, contract."""
    contract_id = sign(client)
    client.post(f"/contracts/{contract_id}/revoke")
    return contract_id


def _make_used(client: TestClient) -> str:
    """A single-use contract that has already been spent."""
    contract_id = sign(client)
    assert purchase(client, contract_id).json()["status"] == "authorized"
    return contract_id


def _make_expired(client: TestClient) -> str:
    """A contract whose expires_at will be in the past once the test moves the clock forward."""
    return sign(client, expires_at="2026-12-31T00:00:00Z")


@pytest.mark.parametrize(
    "make_contract, error_code",
    [
        (_make_tampered, "contract_tampered"),
        (_make_revoked, "contract_revoked"),
        (_make_used, "contract_used"),
        (_make_expired, "contract_expired"),
    ],
)
def test_rejection_creates_blocked_purchase_with_evidence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, make_contract: Any, error_code: str
) -> None:
    """A rejected known contract yields a BLOCKED purchase whose evidence chain ends in purchase_blocked."""
    contract_id = make_contract(client)
    if error_code == "contract_expired":
        monkeypatch.setattr(services, "clock", lambda: datetime(2027, 1, 1, tzinfo=timezone.utc))

    response = purchase(client, contract_id, lambda p: p.update(id="proposal_rejected"))
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == error_code
    purchase_id = body["details"]["purchase_id"]
    assert body["details"]["status"] == "blocked"

    # The purchase is a normal, fetchable record...
    fetched = client.get(f"/purchases/{purchase_id}").json()
    assert fetched["status"] == "blocked"
    assert fetched["credential"] is None
    assert fetched["summary"].startswith("Blocked:")

    # ...and its evidence chain tells the story, ending in the rejection.
    evidence = client.get(f"/evidence/{purchase_id}").json()
    types = [e["event_type"] for e in evidence["events"]]
    assert types[:2] == ["contract_created", "contract_signed"]
    assert types[-2:] == ["shopping_started", "purchase_blocked"]
    assert evidence["events"][-1]["data"]["reason"] == error_code
    assert evidence["purchase"]["error"]


def test_expired_contract_status_is_persisted(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading an expired contract for a purchase persists status EXPIRED before rejecting."""
    contract_id = _make_expired(client)
    monkeypatch.setattr(services, "clock", lambda: datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert purchase(client, contract_id).status_code == 409
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "expired"


def test_contract_tampered_into_invalid_json(client: TestClient) -> None:
    """If the stored JSON no longer even parses as a Contract, it is treated as tampered, not a crash."""
    contract_id = sign(client)
    tamper_with_cap(contract_id, -5.0)  # hard_cap_all_in must be > 0, so this JSON is invalid

    assert client.get(f"/contracts/{contract_id}").json()["verification"]["valid"] is False
    response = purchase(client, contract_id)
    assert response.status_code == 409
    assert response.json()["error"] == "contract_tampered"
    evidence = client.get(f"/evidence/{response.json()['details']['purchase_id']}").json()
    assert evidence["contract_verification"]["valid"] is False
    assert evidence["events"][-1]["event_type"] == "purchase_blocked"


def test_unknown_contract_is_plain_404_with_no_records(client: TestClient) -> None:
    """An unknown contract id returns 404 and writes nothing at all."""
    before_purchases, before_events = purchase_count(), evidence_count()
    response = purchase(client, "contract_nope")
    assert response.status_code == 404
    assert response.json()["error"] == "contract_not_found"
    assert "purchase_id" not in response.json()["details"]
    assert (purchase_count(), evidence_count()) == (before_purchases, before_events)


def test_malformed_submission_for_known_contract_leaves_trail(client: TestClient) -> None:
    """A body that fails PurchaseSubmission validation (proposal is not an object) still gets a FAILED purchase."""
    contract_id = sign(client)
    response = client.post("/purchases", json={"contract_id": contract_id, "checkout_url": "x", "proposal": "oops"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid_request"
    evidence = client.get(f"/evidence/{body['details']['purchase_id']}").json()
    assert evidence["status"] == "failed"
    assert evidence["events"][-1]["data"]["reason"] == "invalid_request"


def test_malformed_submission_without_known_contract_is_plain_422(client: TestClient) -> None:
    """A malformed body with no usable contract_id is a plain 422 in the standard error shape."""
    response = client.post("/purchases", json={"checkout_url": "x"})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert "purchase_id" not in response.json()["details"]


# ============================================================
# Contracts
# ============================================================


def test_health(client: TestClient) -> None:
    """/health reports ok and the database type."""
    body = client.get("/health").json()
    # The original two keys, unchanged...
    assert {"status": body["status"], "database": body["database"]} == {"status": "ok", "database": "sqlite"}
    # ...plus the modes section 6.1 adds (a superset, never a changed shape).
    assert body["payment_mode"] == "stub" and body["payment_label"] == "Simulated provider"
    assert body["credential_mode"] == "agent_visible" and body["compiler_mode"] in ("fixture", "openai")


def test_create_draft_returns_id(client: TestClient, draft_json: dict[str, Any]) -> None:
    """POST /contracts with a bare draft returns its id and an empty clarifications list."""
    response = client.post("/contracts", json=draft_json)
    assert response.status_code == 201
    body = response.json()
    assert body["draft_id"] == "draft_demo_shoes"
    assert body["draft"]["spend"]["hard_cap_all_in"] == 135.0
    assert body["clarifications_needed"] == []


def test_create_draft_from_compiler_output(client: TestClient, draft_json: dict[str, Any]) -> None:
    """A CompilerOutput wrapper is unwrapped and its assumptions/clarifications are kept."""
    body = {"draft": draft_json, "assumptions": ["size is US men's"], "clarifications_needed": ["Which color?"]}
    response = client.post("/contracts", json=body)
    assert response.status_code == 201
    assert response.json()["clarifications_needed"] == ["Which color?"]
    detail = client.get(f"/contracts/{draft_json['id']}").json()
    assert detail["kind"] == "draft"
    assert detail["assumptions"] == ["size is US men's"]


def test_invalid_draft_has_standard_error_shape(client: TestClient, draft_json: dict[str, Any]) -> None:
    """An invalid draft returns 422 in the standard {error, message, details} shape."""
    draft_json["spend"]["hard_cap_all_in"] = -5
    response = client.post("/contracts", json=draft_json)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_draft"
    assert response.json()["details"]["errors"]


def test_duplicate_draft_rejected(client: TestClient, draft_json: dict[str, Any]) -> None:
    """Posting the same draft id twice is a conflict."""
    client.post("/contracts", json=draft_json)
    assert client.post("/contracts", json=draft_json).status_code == 409


def test_sign_produces_verifiable_contract(client: TestClient) -> None:
    """A freshly signed contract is ACTIVE and verifies."""
    contract_id = sign(client)
    detail = client.get(f"/contracts/{contract_id}").json()
    assert detail["kind"] == "contract"
    assert detail["status"] == "active"
    assert detail["contract"]["agent_key"] == "agent_1"
    assert detail["verification"]["valid"] is True


def test_sign_twice_is_conflict(client: TestClient) -> None:
    """A draft can only be signed once."""
    sign(client)
    response = client.post("/contracts/draft_demo_shoes/sign", json={"draft_id": "draft_demo_shoes"})
    assert response.status_code == 409
    assert response.json()["error"] == "already_signed"


def test_sign_draft_id_mismatch_is_400(client: TestClient, draft_json: dict[str, Any]) -> None:
    """A body draft_id that disagrees with the path is a 400."""
    client.post("/contracts", json=draft_json)
    assert client.post("/contracts/draft_demo_shoes/sign", json={"draft_id": "draft_other"}).status_code == 400


def test_client_signature_recorded_but_server_signature_authoritative(client: TestClient, draft_json: dict[str, Any]) -> None:
    """A caller-supplied signature is kept as evidence, but the stored signature is the server's own."""
    client.post("/contracts", json=draft_json)
    signed = client.post("/contracts/draft_demo_shoes/sign", json={"draft_id": "draft_demo_shoes", "signature": "user-sig"}).json()
    assert signed["signature"] != "user-sig"
    with db.SessionLocal() as session:
        event = db.list_evidence_for_contract(session, signed["id"])[0]
    assert event.event_type == "contract_signed"
    assert event.data["data"]["client_signature"] == "user-sig"


def test_list_contracts_newest_first(client: TestClient) -> None:
    """GET /contracts lists the signed contract before the (older) draft."""
    contract_id = sign(client)
    items = client.get("/contracts").json()
    assert [(i["kind"], i["id"]) for i in items] == [("contract", contract_id), ("draft", "draft_demo_shoes")]


def test_hash_survives_status_change(client: TestClient) -> None:
    """Revoking changes status, which is excluded from the hash, so the contract still verifies."""
    contract_id = sign(client)
    client.post(f"/contracts/{contract_id}/revoke")
    detail = client.get(f"/contracts/{contract_id}").json()
    assert detail["status"] == "revoked"
    assert detail["verification"]["valid"] is True


def test_unknown_contract_get_is_404(client: TestClient) -> None:
    """GET on an unknown id is a 404."""
    assert client.get("/contracts/nope").status_code == 404


def test_non_revocable_contract(client: TestClient) -> None:
    """A contract signed as non-revocable cannot be revoked."""
    contract_id = sign(client, revocable=False)
    assert client.post(f"/contracts/{contract_id}/revoke").json()["error"] == "not_revocable"


def test_revoke_revokes_outstanding_credentials(client: TestClient) -> None:
    """Revoking a multi-use contract also revokes its still-active credential."""
    contract_id = sign(client, single_use=False)
    body = purchase(client, contract_id).json()
    assert body["status"] == "authorized"
    client.post(f"/contracts/{contract_id}/revoke")
    assert client.get(f"/purchases/{body['purchase_id']}").json()["credential"]["status"] == "revoked"


# ============================================================
# Purchase details
# ============================================================


def test_authorized_credential_has_only_safe_fields(client: TestClient) -> None:
    """The credential summary has exactly the documented safe fields."""
    contract_id = sign(client)
    body = purchase(client, contract_id).json()
    credential = body["credential"]
    assert set(credential) == {"credential_id", "merchant_name", "merchant_id", "max_amount", "currency", "single_use", "status", "expires_at"}
    assert credential["single_use"] is True
    assert credential["merchant_name"] == "Mock Nike"
    assert "Authorized 128.39 USD" in body["summary"]


def test_single_use_claim_is_atomic(client: TestClient) -> None:
    """The conditional UPDATE lets exactly one session claim a single-use contract."""
    contract_id = sign(client)
    with db.SessionLocal() as first, db.SessionLocal() as second:
        assert db.claim_single_use_contract(first, contract_id) is True
        first.commit()
        assert db.claim_single_use_contract(second, contract_id) is False


def test_concurrent_consumption_blocks_at_authorization(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract passes step 1, but another purchase consumes it just before our claim: we end up BLOCKED."""
    contract_id = sign(client)
    original_claim = db.claim_single_use_contract

    def consumed_by_someone_else(session: Any, cid: str) -> bool:
        """Simulate a concurrent purchase winning the claim a moment before ours."""
        with db.SessionLocal() as other:
            original_claim(other, cid)
            other.commit()
        return original_claim(session, cid)

    monkeypatch.setattr(db, "claim_single_use_contract", consumed_by_someone_else)
    body = purchase(client, contract_id).json()
    assert body["status"] == "blocked"
    assert body["credential"] is None
    assert credential_count(contract_id) == 0
    assert "no longer active" in body["summary"]


def test_unreported_addons_escalates(client: TestClient) -> None:
    """Omitting addons_detected escalates through the API as well."""
    contract_id = sign(client)
    body = purchase(client, contract_id, lambda p: p.pop("addons_detected")).json()
    assert body["status"] == "escalated"
    assert body["decision"]["verdict"] == "unverifiable"
    assert event_types(client, body["purchase_id"])[-1] == "purchase_escalated"


def test_invalid_proposal_is_422_with_evidence(client: TestClient) -> None:
    """A proposal missing required fields is a 422, but still leaves a FAILED purchase with the raw payload."""
    contract_id = sign(client)
    response = purchase(client, contract_id, lambda p: p.pop("line_items"))
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid_proposal"

    evidence = client.get(f"/evidence/{body['details']['purchase_id']}").json()
    rejected = evidence["events"][-1]
    assert rejected["event_type"] == "purchase_blocked"
    assert rejected["data"]["reason"] == "proposal_invalid"
    assert "line_items" not in rejected["data"]["raw_payload"]
    assert evidence["status"] == "failed"


def test_proposal_for_other_contract_is_blocked(client: TestClient) -> None:
    """A proposal extracted for a different contract gets a contract_binding FAIL first."""
    contract_id = sign(client)
    body = purchase(client, contract_id, lambda p: p.update(contract_id="contract_other")).json()
    assert body["status"] == "blocked"
    assert body["decision"]["results"][0]["constraint"] == "contract_binding"


def test_missing_selection_report_is_flagged_not_blocked(client: TestClient) -> None:
    """No selection report is noted in the evidence but does not block the purchase."""
    contract_id = sign(client)
    body = purchase(client, contract_id).json()
    assert body["status"] == "authorized"
    started = next(e for e in client.get(f"/evidence/{body['purchase_id']}").json()["events"] if e["event_type"] == "shopping_started")
    assert started["event_type"] == "shopping_started"
    assert started["data"]["selection_disclosure_missing"] is True


def test_get_purchase(client: TestClient) -> None:
    """GET /purchases/{id} returns the same shape; unknown ids are 404."""
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id).json()["purchase_id"]
    body = client.get(f"/purchases/{purchase_id}").json()
    assert body["status"] == "authorized"
    assert body["credential"]["max_amount"] == 128.39
    assert client.get("/purchases/purchase_nope").status_code == 404


# ============================================================
# Evidence chain
# ============================================================


def test_evidence_chain_includes_objects_inline(client: TestClient) -> None:
    """GET /evidence returns contract, proposal, decision, and ordered events in one response."""
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id).json()["purchase_id"]
    evidence = client.get(f"/evidence/{purchase_id}").json()

    assert [e["event_type"] for e in evidence["events"]] == HAPPY_PATH_EVENTS
    sequences = [e["sequence"] for e in evidence["events"]]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)

    assert evidence["contract"]["id"] == contract_id
    assert evidence["proposal"]["id"] == "proposal_demo_pass"
    assert len(evidence["decision"]["results"]) == 14
    completed = next(e for e in evidence["events"] if e["event_type"] == "validation_completed")
    assert len(completed["data"]["results"]) == 14  # validation_completed carries every result
    assert evidence["ledger_intact"] is True
    assert evidence["contract_verification"]["valid"] is True


def test_evidence_hash_chain_detects_edits(client: TestClient) -> None:
    """Editing any stored evidence event makes ledger_intact false."""
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id).json()["purchase_id"]
    with db.SessionLocal() as session:
        row = db.list_evidence_for_purchase(session, purchase_id)[0]
        row.data = {**row.data, "message": "rewritten"}
        session.commit()
    assert client.get(f"/evidence/{purchase_id}").json()["ledger_intact"] is False


# ============================================================
# Reconciliation
# ============================================================


def test_complete_purchase(client: TestClient) -> None:
    """Charging the authorized amount completes the purchase and uses the credential once."""
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id).json()["purchase_id"]
    body = complete(purchase_id, 128.39).json()
    assert body["status"] == "completed"
    assert body["credential"]["status"] == "used"
    assert event_types(client, purchase_id)[-2:] == ["credential_used", "payment_completed"]
    assert complete(purchase_id, 1).status_code == 409


def test_overcharge_revokes_credential(client: TestClient) -> None:
    """Charging one cent more than authorized fails the purchase and revokes the credential."""
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id).json()["purchase_id"]
    body = complete(purchase_id, 128.40).json()
    assert body["status"] == "failed"
    assert body["credential"]["status"] == "revoked"
    assert event_types(client, purchase_id)[-1] == "payment_mismatch"


def test_complete_after_credential_expiry(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A charge after the credential's 15-minute window is rejected."""
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id).json()["purchase_id"]
    later = datetime.now(timezone.utc) + timedelta(minutes=get_settings().credential_ttl_minutes + 1)
    monkeypatch.setattr(services, "clock", lambda: later)
    response = complete(purchase_id, 128.39)
    assert response.status_code == 409
    assert response.json()["error"] == "credential_expired"


def test_complete_blocked_purchase_is_conflict(client: TestClient) -> None:
    """Only AUTHORIZED purchases can be completed."""
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id, lambda p: p.update(currency="EUR")).json()["purchase_id"]
    assert complete(purchase_id, 1).status_code == 409


# ============================================================
# The 2-cent tolerance gap: authorized amount and credential use max(claimed, computed)
# ============================================================


def test_credential_uses_computed_total_when_claim_is_lower(client: TestClient) -> None:
    """Claimed 128.38 but parts add up to 128.39: authorize and lock the credential to 128.39, not 128.38."""
    contract_id = sign(client)
    body = purchase(client, contract_id, lambda p: p.update(total=128.38)).json()
    assert body["status"] == "authorized"
    assert body["credential"]["max_amount"] == 128.39
    purchase_row = client.get(f"/evidence/{body['purchase_id']}").json()["purchase"]
    assert purchase_row["authorized_amount"] == 128.39

    # The merchant charging the real 128.39 is therefore NOT a mismatch.
    completed = complete(body['purchase_id'], 128.39).json()
    assert completed["status"] == "completed"


def test_claimed_total_one_cent_under_parts_at_cap_is_blocked_via_api(client: TestClient) -> None:
    """End to end: parts 135.01, claim 135.00, cap 135.00 -> BLOCKED, no credential."""
    contract_id = sign(client)

    def mutate(p: dict[str, Any]) -> None:
        """Parts: 126.60 + 8.41 = 135.01; claimed total 135.00."""
        p["line_items"][0]["unit_price"] = 126.60
        p["item_subtotal"] = 126.60
        p["tax"] = 8.41
        p["total"] = 135.00

    body = purchase(client, contract_id, mutate).json()
    assert body["status"] == "blocked"
    assert credential_count(contract_id) == 0


# ============================================================
# Human approval and rejection of escalated purchases
# ============================================================


def escalated_purchase(client: TestClient, **draft_overrides: Any) -> tuple[str, str]:
    """Sign a contract and submit a checkout that escalates (addons_detected not reported). Returns (contract_id, purchase_id)."""
    contract_id = sign(client, **draft_overrides)
    body = purchase(client, contract_id, lambda p: p.pop("addons_detected")).json()
    assert body["status"] == "escalated"
    return contract_id, body["purchase_id"]


def test_approve_escalated_purchase_issues_credential(client: TestClient) -> None:
    """Approving an escalated purchase authorizes it exactly like the automatic path, and records the human decision."""
    contract_id, purchase_id = escalated_purchase(client)

    response = client.post(f"/purchases/{purchase_id}/approve", json={"note": "I checked, no add-ons."})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["status"] == "authorized"
    assert body["credential"]["max_amount"] == 128.39  # locked to the checkout total, not the 135 cap
    assert find_credential_leaks(body) == []

    # Single-use contract consumed; exactly one credential.
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "used"
    assert credential_count(contract_id) == 1

    # Evidence: escalated, then the human approval, then the credential.
    evidence = client.get(f"/evidence/{purchase_id}").json()
    types = [e["event_type"] for e in evidence["events"]]
    # ...then the authorized checkout unlocks the contract's stored card.
    assert types[-4:] == ["purchase_escalated", "purchase_authorized", "credential_created", "purchase_authorized"]
    assert evidence["events"][-1]["data"]["kind"] == "credential_ready"  # the stored card is unlocked for this checkout
    approval = evidence["events"][-3]["data"]
    assert approval["human_approval"] is True
    assert approval["note"] == "I checked, no add-ons."
    assert [c["constraint"] for c in approval["accepted_constraints"]] == ["no_addons"]
    assert evidence["ledger_intact"] is True


def test_approve_refused_on_blocked_purchase(client: TestClient) -> None:
    """A BLOCKED purchase (hard FAIL) cannot be approved."""
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id, lambda p: p.update(currency="EUR")).json()["purchase_id"]

    response = client.post(f"/purchases/{purchase_id}/approve")
    assert response.status_code == 409
    assert response.json()["error"] == "purchase_not_escalated"
    assert credential_count(contract_id) == 0


def test_approve_refused_after_contract_revoked(client: TestClient) -> None:
    """If the user revoked the contract after the escalation, approval is refused."""
    contract_id, purchase_id = escalated_purchase(client)
    assert client.post(f"/contracts/{contract_id}/revoke").status_code == 200

    response = client.post(f"/purchases/{purchase_id}/approve")
    assert response.status_code == 409
    assert response.json()["error"] == "contract_revoked"
    assert credential_count(contract_id) == 0
    assert client.get(f"/purchases/{purchase_id}").json()["status"] == "escalated"


def test_approve_refused_when_contract_tampered(client: TestClient) -> None:
    """A contract whose stored JSON was edited after signing cannot be used for approval."""
    contract_id, purchase_id = escalated_purchase(client)
    tamper_with_cap(contract_id, 5000.0)

    response = client.post(f"/purchases/{purchase_id}/approve")
    assert response.status_code == 409
    assert response.json()["error"] == "contract_tampered"
    assert credential_count(contract_id) == 0


def test_double_approve_issues_one_credential(client: TestClient) -> None:
    """Approving twice: the second is refused and only one credential exists."""
    contract_id, purchase_id = escalated_purchase(client)
    assert client.post(f"/purchases/{purchase_id}/approve").status_code == 200

    second = client.post(f"/purchases/{purchase_id}/approve")
    assert second.status_code == 409
    assert second.json()["error"] == "purchase_not_escalated"
    assert credential_count(contract_id) == 1


def test_double_approve_multi_use_contract_issues_one_credential(client: TestClient) -> None:
    """Even with no single-use flip to protect it, the purchase's own conditional update stops a second credential."""
    contract_id, purchase_id = escalated_purchase(client, single_use=False)
    assert client.post(f"/purchases/{purchase_id}/approve").status_code == 200
    assert client.post(f"/purchases/{purchase_id}/approve").status_code == 409
    assert credential_count(contract_id) == 1
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "active"  # multi-use stays active


def test_purchase_status_claim_is_atomic(client: TestClient) -> None:
    """Two sessions racing to move the same purchase out of ESCALATED: exactly one wins."""
    _, purchase_id = escalated_purchase(client)
    with db.SessionLocal() as first, db.SessionLocal() as second:
        assert db.claim_purchase_status(first, purchase_id, "escalated", "authorized") is True
        first.commit()
        assert db.claim_purchase_status(second, purchase_id, "escalated", "blocked") is False


def test_approve_never_overrides_a_hard_fail(client: TestClient) -> None:
    """Defense in depth: even if a stored decision somehow holds a hard FAIL, approval refuses it."""
    contract_id, purchase_id = escalated_purchase(client)
    decision_id = client.get(f"/purchases/{purchase_id}").json()["decision"]["id"]

    # Plant a hard FAIL in the stored decision (the engine itself would have BLOCKED this).
    with db.SessionLocal() as session:
        row = session.get(db.DecisionRow, decision_id)
        results = list(row.data["results"])
        results[0] = {**results[0], "verdict": "fail"}
        row.data = {**row.data, "results": results, "verdict": "fail"}
        session.commit()

    response = client.post(f"/purchases/{purchase_id}/approve")
    assert response.status_code == 409
    assert response.json()["error"] == "cannot_override_fail"
    assert credential_count(contract_id) == 0


def test_reject_escalated_purchase(client: TestClient) -> None:
    """Rejecting an escalated purchase blocks it, logs the human decision, and it can no longer be approved."""
    contract_id, purchase_id = escalated_purchase(client)

    response = client.post(f"/purchases/{purchase_id}/reject", json={"note": "Not sure about add-ons."})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["credential"] is None
    assert body["summary"] == "Blocked: Rejected by the user after escalation."

    evidence = client.get(f"/evidence/{purchase_id}").json()
    last = evidence["events"][-1]
    assert last["event_type"] == "purchase_blocked"
    assert last["data"]["human_rejection"] is True
    assert last["data"]["note"] == "Not sure about add-ons."

    # Rejected is final: no approve, no second reject, no credential, contract untouched.
    assert client.post(f"/purchases/{purchase_id}/approve").status_code == 409
    assert client.post(f"/purchases/{purchase_id}/reject").status_code == 409
    assert credential_count(contract_id) == 0
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "active"


def test_reject_declines_authorized_purchase_before_payment(client: TestClient) -> None:
    """
    Section 16 changed this: /reject on an AUTHORIZED purchase whose payment
    has not started now DECLINES it (it used to be a 409). The pending payment
    request is cancelled and the single-use contract is freed for another try.
    """
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id).json()["purchase_id"]
    body = client.post(f"/purchases/{purchase_id}/reject").json()
    assert body["status"] == "blocked"
    assert body["credential"]["status"] == "revoked"
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "active"
    events = client.get(f"/evidence/{purchase_id}").json()["events"]
    assert events[-1]["data"]["kind"] == "purchase_declined"


def test_reject_refused_once_payment_is_done(client: TestClient) -> None:
    """A purchase that already completed can't be rejected."""
    contract_id = sign(client)
    purchase_id = purchase(client, contract_id).json()["purchase_id"]
    assert complete(purchase_id, 128.39).json()["status"] == "completed"
    assert client.post(f"/purchases/{purchase_id}/reject").status_code == 409


def test_approve_unknown_purchase_is_404(client: TestClient) -> None:
    """Approving a purchase id that does not exist is a 404."""
    assert client.post("/purchases/purchase_nope/approve").status_code == 404


def test_concurrent_reject_wins_race_against_approve(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Approval passes its ESCALATED check, but a concurrent reject lands just before its claim.

    Uses a multi-use contract so the contract's USED flip cannot save us:
    only the purchase's own conditional update stands between the race and
    a credential being issued for a rejected purchase.
    """
    contract_id, purchase_id = escalated_purchase(client, single_use=False)
    original_claim = db.claim_purchase_status

    def rejected_by_someone_else(session: Any, pid: str, from_status: str, to_status: str) -> bool:
        """Simulate another request rejecting the purchase a moment before our claim runs."""
        with db.SessionLocal() as other:
            original_claim(other, pid, "escalated", "blocked")
            other.commit()
        return original_claim(session, pid, from_status, to_status)

    monkeypatch.setattr(db, "claim_purchase_status", rejected_by_someone_else)
    response = client.post(f"/purchases/{purchase_id}/approve")
    assert response.status_code == 409
    assert response.json()["error"] == "purchase_not_escalated"
    assert credential_count(contract_id) == 0
