"""
test_auth.py: roles, ownership, and tokens (HANDSHAKE_BUILD.md section 8).

The rule under test: the agent proposes, the USER decides. The agent may read
its owner's records and request purchases, but it can never sign, patch,
amend, revoke, approve, or reject. Those return 403 agent_not_permitted and
leave an evidence event. Records owned by someone else look exactly like
records that don't exist (404), and bad tokens are refused (401).
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from handshake import auth, db, services
from handshake.config import override_settings
from conftest import STATIC_EXTRACTOR, api_draft_payload, proposal_payload


def sign(client: TestClient, **overrides: Any) -> str:
    """Create and sign the demo draft as the user; return the contract id."""
    draft = {**api_draft_payload(), **overrides}
    assert client.post("/contracts", json=draft).status_code == 201
    response = client.post(f"/contracts/{draft['id']}/sign")
    assert response.status_code == 200, response.json()
    return response.json()["id"]


def submit(client: TestClient, contract_id: str, mutate: Any = None) -> Any:
    """Submit a purchase whose checkout (via the StaticExtractor) holds the passing proposal, optionally edited."""
    proposal = proposal_payload(contract_id)
    if mutate is not None:
        mutate(proposal)
    STATIC_EXTRACTOR.set(proposal)
    body = {"contract_id": contract_id, "checkout_url": "https://mocknike.example/c", "idempotency_key": f"key-{uuid.uuid4().hex}"}
    return client.post("/purchases", json=body)


def escalated(client: TestClient, **overrides: Any) -> tuple[str, str]:
    """A signed contract plus an ESCALATED purchase (addons_detected not reported)."""
    contract_id = sign(client, **overrides)
    body = submit(client, contract_id, lambda p: p.pop("addons_detected")).json()
    assert body["status"] == "escalated"
    return contract_id, body["purchase_id"]


def last_event(contract_or_draft_id: str) -> dict[str, Any]:
    """The newest evidence event (as stored) for a contract or draft id."""
    with db.SessionLocal() as session:
        return db.list_evidence_for_contract(session, contract_or_draft_id)[-1].data


# ============================================================
# The agent cannot perform user-only actions
# ============================================================


def test_agent_cannot_sign(client: TestClient, agent_client: TestClient) -> None:
    """Signing is the user's consent; the agent gets 403 and the attempt is recorded."""
    assert client.post("/contracts", json=api_draft_payload()).status_code == 201
    response = agent_client.post("/contracts/draft_demo_shoes/sign")
    assert response.status_code == 403
    assert response.json()["error"] == "agent_not_permitted"
    assert client.get("/contracts/draft_demo_shoes").json()["signed_contract_id"] is None
    event = last_event("draft_demo_shoes")
    assert event["data"]["kind"] == "agent_action_denied"
    assert event["data"]["action"] == "sign a contract"


def test_agent_cannot_approve_its_own_escalation(client: TestClient, agent_client: TestClient) -> None:
    """The core self-approval hole: the agent may not accept the exceptions on its own purchase."""
    contract_id, purchase_id = escalated(client)
    response = agent_client.post(f"/purchases/{purchase_id}/approve")
    assert response.status_code == 403
    assert response.json()["error"] == "agent_not_permitted"
    assert client.get(f"/purchases/{purchase_id}").json()["status"] == "escalated"
    with db.SessionLocal() as session:
        assert db.count_credentials_for_contract(session, contract_id) == 0
    events = client.get(f"/evidence/{purchase_id}").json()["events"]
    assert events[-1]["data"]["kind"] == "agent_action_denied"


def test_agent_cannot_reject(client: TestClient, agent_client: TestClient) -> None:
    """Rejecting is also a user decision."""
    _, purchase_id = escalated(client)
    response = agent_client.post(f"/purchases/{purchase_id}/reject")
    assert response.status_code == 403
    assert client.get(f"/purchases/{purchase_id}").json()["status"] == "escalated"


def test_agent_cannot_revoke(client: TestClient, agent_client: TestClient) -> None:
    """Revoking is the user's call."""
    contract_id = sign(client)
    response = agent_client.post(f"/contracts/{contract_id}/revoke")
    assert response.status_code == 403
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "active"
    assert last_event(contract_id)["data"]["kind"] == "agent_action_denied"


def test_agent_cannot_create_raw_drafts(agent_client: TestClient) -> None:
    """The agent drafts only through the compiler (POST /drafts/compile), never by posting a raw draft."""
    response = agent_client.post("/contracts", json=api_draft_payload())
    assert response.status_code == 403
    assert response.json()["error"] == "agent_not_permitted"


def test_complete_is_internal_for_everyone(client: TestClient, agent_client: TestClient) -> None:
    """Nobody may report 'the merchant charged X' over HTTP; the backend verifies orders itself."""
    contract_id = sign(client)
    purchase_id = submit(client, contract_id).json()["purchase_id"]
    for caller in (client, agent_client):
        response = caller.post(f"/purchases/{purchase_id}/complete", json={"charged_amount": 128.39})
        assert response.status_code == 403
        assert response.json()["error"] == "internal_only"


def test_agent_can_read_and_request(client: TestClient, agent_client: TestClient) -> None:
    """What the agent IS allowed to do: read its owner's records and request purchases."""
    contract_id = sign(client)
    assert agent_client.get(f"/contracts/{contract_id}").status_code == 200
    assert any(item["id"] == contract_id for item in agent_client.get("/contracts").json())
    body = submit(agent_client, contract_id).json()
    assert body["status"] == "authorized"
    assert agent_client.get(f"/evidence/{body['purchase_id']}").status_code == 200


def test_wrong_agent_key_is_rejected(client: TestClient, agent_client: TestClient) -> None:
    """A contract signed for another agent can't be used by this one, and the refusal is recorded."""
    assert client.post("/contracts", json=api_draft_payload()).status_code == 201
    contract_id = client.post("/contracts/draft_demo_shoes/sign", json={"draft_id": "draft_demo_shoes", "agent_key": "agent_other"}).json()["id"]

    response = submit(agent_client, contract_id)
    assert response.status_code == 403
    assert response.json()["error"] == "agent_not_authorized"
    purchase_id = response.json()["details"]["purchase_id"]
    assert client.get(f"/purchases/{purchase_id}").json()["status"] == "blocked"
    with db.SessionLocal() as session:
        assert db.count_credentials_for_contract(session, contract_id) == 0


def test_signing_binds_the_configured_agent_by_default(client: TestClient) -> None:
    """A contract signed without an agent_key is bound to HANDSHAKE_AGENT_ID, never to 'any agent'."""
    contract_id = sign(client)
    assert client.get(f"/contracts/{contract_id}").json()["contract"]["agent_key"] == "agent_demo"


# ============================================================
# Ownership: foreign records are invisible
# ============================================================


def test_foreign_records_are_404(client: TestClient, stranger_client: TestClient) -> None:
    """Another user's draft, contract, purchase, and evidence all look nonexistent."""
    contract_id = sign(client)
    purchase_id = submit(client, contract_id).json()["purchase_id"]

    assert stranger_client.get("/contracts/draft_demo_shoes").status_code == 404
    assert stranger_client.get(f"/contracts/{contract_id}").status_code == 404
    assert stranger_client.get(f"/purchases/{purchase_id}").status_code == 404
    assert stranger_client.get(f"/evidence/{purchase_id}").status_code == 404
    assert stranger_client.get("/contracts").json() == []


def test_stranger_cannot_act_on_foreign_records(client: TestClient, stranger_client: TestClient) -> None:
    """Writes on foreign ids get the same 404 as reads, and change nothing."""
    assert client.post("/contracts", json=api_draft_payload()).status_code == 201
    assert stranger_client.post("/contracts/draft_demo_shoes/sign").status_code == 404

    contract_id, purchase_id = escalated(client, id="draft_second")
    assert stranger_client.post(f"/contracts/{contract_id}/revoke").status_code == 404
    assert stranger_client.post(f"/purchases/{purchase_id}/approve").status_code == 404
    assert submit(stranger_client, contract_id).status_code == 404
    assert client.get(f"/purchases/{purchase_id}").json()["status"] == "escalated"


# ============================================================
# Tokens
# ============================================================


def test_missing_token_is_401(anon_client: TestClient) -> None:
    """Every route except /health and demo-login needs a token."""
    response = anon_client.get("/contracts")
    assert response.status_code == 401
    assert response.json()["error"] == "not_authenticated"
    assert anon_client.get("/health").status_code == 200


def test_demo_login_issues_a_working_token(anon_client: TestClient) -> None:
    """demo-login returns a bearer token labeled as demo auth."""
    body = anon_client.post("/auth/demo-login", json={"email": "  New.Person@Example.com "}).json()
    assert body["demo_auth"] is True and body["email"] == "new.person@example.com"
    me = anon_client.get("/auth/me", headers={"Authorization": f"Bearer {body['token']}"}).json()
    assert me == {"email": "new.person@example.com", "role": "user", "agent_id": None}


def test_demo_login_rejects_garbage_email(anon_client: TestClient) -> None:
    """An obviously invalid email is refused."""
    assert anon_client.post("/auth/demo-login", json={"email": "not-an-email"}).status_code == 422


def test_expired_token_is_rejected(anon_client: TestClient) -> None:
    """A token past its exp is refused with token_expired."""
    token, _ = auth.issue_user_token("demo@handshake.dev", now=time.time() - 24 * 3600)
    response = anon_client.get("/contracts", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error"] == "token_expired"


def test_tampered_token_is_rejected(anon_client: TestClient) -> None:
    """Changing the payload (e.g. to another email) breaks the HMAC signature."""
    token, _ = auth.issue_user_token("demo@handshake.dev")
    payload_part, signature = token.split(".")
    forged_payload = auth._b64encode(auth._b64decode(payload_part).replace(b"demo@handshake.dev", b"boss@handshake.dev"))
    for bad in (f"{forged_payload}.{signature}", f"{payload_part}.AAAA{signature[4:]}", "garbage", "a.b.c"):
        response = anon_client.get("/contracts", headers={"Authorization": f"Bearer {bad}"})
        assert response.status_code == 401, bad


def test_token_signed_with_another_secret_is_rejected(anon_client: TestClient) -> None:
    """A token made with a different session secret (e.g. the contract signing secret) is worthless."""
    override_settings(session_secret="some-other-session-secret")
    token, _ = auth.issue_user_token("demo@handshake.dev")
    from handshake import config

    config.reset_settings()  # back to the real test secret
    assert anon_client.get("/contracts", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_wrong_agent_token_is_rejected(anon_client: TestClient) -> None:
    """Anything that isn't exactly the configured agent token (or a valid user token) is refused."""
    response = anon_client.get("/contracts", headers={"Authorization": "Bearer test-agent-token-nope"})
    assert response.status_code == 401


# ============================================================
# Stale escalations
# ============================================================


def test_stale_escalation_cannot_be_approved(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past HANDSHAKE_ESCALATION_TTL_MINUTES the checkout facts are too old to approve."""
    contract_id, purchase_id = escalated(client)
    later = services.clock() + timedelta(minutes=31)
    monkeypatch.setattr(services, "clock", lambda: later)

    response = client.post(f"/purchases/{purchase_id}/approve")
    assert response.status_code == 409
    assert response.json()["error"] == "escalation_stale"
    with db.SessionLocal() as session:
        assert db.count_credentials_for_contract(session, contract_id) == 0


def test_escalation_ttl_is_configurable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A longer TTL lets the same 31-minute-old escalation through."""
    override_settings(escalation_ttl_minutes=60)
    _, purchase_id = escalated(client)
    later = services.clock() + timedelta(minutes=31)
    monkeypatch.setattr(services, "clock", lambda: later)
    assert client.post(f"/purchases/{purchase_id}/approve").json()["status"] == "authorized"
