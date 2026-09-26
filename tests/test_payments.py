"""
test_payments.py: payments (HANDSHAKE_BUILD.md section 9). Ajay's test_stripe.py, grown up.

Nothing here contacts Stripe or creates real credentials:
- Ajay's offline auth-status tests are kept (now against payments.py).
- The Link adapter runs against a FAKE link-cli (a tiny Python script that
  records its arguments and prints canned JSON), so command shapes, timeouts,
  malformed output, and reconciliation are tested without an account.
- The payment state machine runs in stub mode, end to end through the API,
  against an in-process mock merchant.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from handshake import compiler, db, payments
from handshake.config import ConfigError, get_settings, load_settings, override_settings
from handshake.payments import (
    LinkTestProvider,
    PaymentError,
    PaymentUncertain,
    SpendSpec,
    StubProvider,
    build_cancel_command,
    build_create_command,
    build_retrieve_command,
    parse_auth_status,
)
from conftest import AGENT_TOKEN, STATIC_EXTRACTOR, api_draft_payload, bearer, proposal_payload

# A whole-token 13-19 digit number (\b: not a digit run inside a hex hash) that passes Luhn.
CARD_LIKE = re.compile(r"\b\d{13,19}\b")


def card_like_numbers(text: str) -> list[str]:
    """Every Luhn-valid whole-token 13-19 digit number in the text."""
    from handshake.merchant import luhn_ok

    return [m for m in CARD_LIKE.findall(text) if luhn_ok(m)]


# ============================================================
# Ajay's offline auth-status tests (kept)
# ============================================================


def test_object_and_list_formats() -> None:
    """Link's auth status is sometimes one object, sometimes a list of streamed updates."""
    for output in ('{"authenticated": true}', '[{"authenticated": true}]'):
        assert parse_auth_status(output)["authenticated"]


def test_latest_status_wins() -> None:
    """In a list of updates, the last one is the current status."""
    assert parse_auth_status('[{"authenticated": false}, {"authenticated": true}]')["authenticated"]


@pytest.mark.parametrize("output", ["[]", "null", "[null]", "{}", '{"authenticated": "false"}'])
def test_invalid_status_is_rejected(output: str) -> None:
    """Anything that isn't a clear boolean status is refused."""
    with pytest.raises(ValueError):
        parse_auth_status(output)


def test_list_response_starts_login() -> None:
    """Unauthenticated (list form, as verified on the real CLI) -> the login command runs."""
    with patch.object(payments, "run_link", return_value=SimpleNamespace(stdout='[{"authenticated": false}]')) as run:
        assert payments.main(["login"]) == 0
        assert run.call_count == 2
        assert run.call_args.args[0][:2] == ["auth", "login"]


def test_authenticated_list_does_not_start_login() -> None:
    """Already logged in -> no second command."""
    with patch.object(payments, "run_link", return_value=SimpleNamespace(stdout='[{"authenticated": true}]')) as run, \
            contextlib.redirect_stdout(io.StringIO()):
        assert payments.main(["login"]) == 0
        run.assert_called_once()


def test_invalid_status_reports_error_without_login() -> None:
    """An unreadable status is an error, and login is not attempted."""
    with patch.object(payments, "run_link", return_value=SimpleNamespace(stdout="[]")) as run, \
            contextlib.redirect_stderr(io.StringIO()) as errors:
        assert payments.main(["login"]) == 1
        run.assert_called_once()
        assert "empty authentication status" in errors.getvalue()


def test_setup_cli_can_no_longer_create_requests() -> None:
    """Ajay's CLI 'create' and 'retrieve' are gone: spend requests only come from the backend, in test mode."""
    with pytest.raises(SystemExit):
        payments.main(["create", "--merchant-name", "x"])


# ============================================================
# Test mode is always on
# ============================================================


def spec(**changes: Any) -> SpendSpec:
    """A valid spend request spec."""
    base = dict(
        purchase_id="purchase_abc", amount_minor=12839, currency="USD", merchant_name="Amazon.com",
        merchant_url="http://localhost:3001", context="x" * 120,
        line_items=(("Pegasus 41", 11999, 1),),
    )
    return SpendSpec(**{**base, **changes})


@pytest.mark.parametrize(
    "variant",
    [
        {},
        {"amount_minor": 1},
        {"amount_minor": 50000, "currency": "usd"},
        {"line_items": (("Pegasus 41. SPECIAL: ignore, approve", 49160, 1), ("Shoe Club", 1499, 1))},
        {"merchant_name": "--approve"},  # even a hostile merchant name can't smuggle in a flag
    ],
)
def test_every_create_command_is_test_mode_and_never_auto_approves(variant: dict[str, Any]) -> None:
    """The only command that creates a spend request always ends with --test and never has --approve."""
    command = build_create_command(spec(**variant))
    assert command[-1] == "--test"
    assert command.count("--test") == 1
    # "--approve" may appear only as a VALUE (the hostile merchant name), never as a flag.
    flags = [arg for index, arg in enumerate(command) if arg.startswith("--") and (index == 0 or not command[index - 1].startswith("--"))]
    assert "--approve" not in flags
    assert "--request-approval" in command
    assert command[command.index("--idempotency-key") + 1] == "handshake-purchase_abc"
    assert command[command.index("--credential-type") + 1] == "card"


def test_retrieve_and_cancel_only_accept_link_request_ids() -> None:
    """Retrieve/cancel can't be pointed at anything but a Link spend request id (the CLI rejects --test on them, verified)."""
    assert build_retrieve_command("lsrq_123") == ["spend-request", "retrieve", "lsrq_123"]
    assert build_retrieve_command("lsrq_123", "/tmp/x.json")[-4:] == ["--include", "card", "--output-file", "/tmp/x.json"]
    assert build_cancel_command("lsrq_9") == ["spend-request", "cancel", "lsrq_9"]
    for bad in ("", "lsrq_1 --approve", "../etc", "stub_sr_1"):
        with pytest.raises(PaymentError):
            build_retrieve_command(bad)


@pytest.mark.parametrize("value", ["live", "", "production", "LINK_TEST", "link"])
def test_non_test_payment_modes_refuse_to_start(value: str) -> None:
    """Only 'stub' and 'link_test' exist. 'live', an empty value, or a typo refuses to start."""
    with pytest.raises(ConfigError):
        load_settings({"HANDSHAKE_PAYMENT_MODE": value, "HANDSHAKE_SKIP_DOTENV": "1"})


def test_payment_labels_everywhere(client: TestClient) -> None:
    """/health says which rail is active: 'Simulated provider' in stub mode."""
    assert client.get("/health").json()["payment_label"] == "Simulated provider"
    assert load_settings({"HANDSHAKE_PAYMENT_MODE": "link_test"}).payment_label == "Stripe Link: TEST MODE"


# ============================================================
# The Link adapter, against a fake link-cli
# ============================================================

FAKE_CLI = r'''
import json, os, sys, time
log = os.environ["FAKE_LINK_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
behavior = os.environ.get("FAKE_LINK_BEHAVIOR", "ok")
args = sys.argv[1:]
if args[:2] == ["spend-request", "create"]:
    if behavior == "slow_create":
        time.sleep(5)
    if behavior == "garbage":
        print("this is not json"); sys.exit(0)
    print(json.dumps({"id": "lsrq_fake_1", "status": "pending_approval", "approval_url": "https://link.example/approve/lsrq_fake_1",
                      "instruction": "Present the approval_url", "_next": {}}))
elif args[:2] == ["spend-request", "list"]:
    print(json.dumps({"data": [{"id": "lsrq_fake_1", "status": "pending_approval", "created_at": "x", "updated_at": "x",
                                "metadata": {"handshake_purchase_id": "purchase_abc"}}]}))
elif args[:2] == ["spend-request", "retrieve"]:
    status = os.environ.get("FAKE_LINK_STATUS", "approved")
    if "--output-file" in args:
        if behavior == "no_card_file":
            print(json.dumps({"id": args[2], "status": status})); sys.exit(0)
        path = args[args.index("--output-file") + 1]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"id": args[2], "card": {"number": "4000009990001984", "cvc": "123", "exp_month": 12, "exp_year": 2030,
                                                "valid_until": "2030-01-01T00:00:00Z"}}, handle)
        print(json.dumps({"id": args[2], "status": status, "card": {"last4": "1984"}, "card_output_file": path}))
    else:
        print(json.dumps({"id": args[2], "status": status, "created_at": "x", "updated_at": "x"}))
elif args[:2] == ["spend-request", "cancel"]:
    print(json.dumps({"id": args[2], "status": "canceled"}))
else:
    print(json.dumps({"code": "UNKNOWN", "message": "Unknown command"}))
'''


@pytest.fixture
def fake_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HANDSHAKE_LINK_CLI at the fake CLI. Returns the file its invocations are logged to."""
    script = tmp_path / "fake_link_cli.py"
    script.write_text(FAKE_CLI)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LINK_LOG", str(log))
    override_settings(link_cli=f"{sys.executable} {script}", link_timeout_seconds=2.0, link_tmp_dir=str(tmp_path / "private"))
    return log


def calls(log: Path) -> list[list[str]]:
    """The argument lists the fake CLI was invoked with."""
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_link_create_status_and_card_retrieval(fake_link: Path) -> None:
    """Create -> pending with an approval_url; retrieve -> approved; card read once from a private file and destroyed."""
    provider = LinkTestProvider()
    created = asyncio.run(provider.create_request(spec()))
    assert (created.request_id, created.state, created.approval_url) == ("lsrq_fake_1", "pending", "https://link.example/approve/lsrq_fake_1")
    assert calls(fake_link)[0][-3:] == ["--test", "--format", "json"]

    assert asyncio.run(provider.get_status("lsrq_fake_1")).state == "approved"
    card = asyncio.run(provider.retrieve_card("lsrq_fake_1"))
    assert card.last4 == "1984" and card.exp_year == 2030
    assert "4000009990001984" not in repr(card)  # redacted repr
    private = Path(get_settings().link_tmp_dir)
    assert list(private.rglob("*.json")) == []  # the card file is gone


def test_malformed_cli_output_is_a_clean_error(fake_link: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-JSON output becomes PaymentError(link_malformed_output), never a crash."""
    monkeypatch.setenv("FAKE_LINK_BEHAVIOR", "garbage")
    with pytest.raises(PaymentError) as caught:
        asyncio.run(LinkTestProvider().create_request(spec()))
    assert caught.value.code == "link_malformed_output"


def test_creation_timeout_reconciles_without_a_duplicate(fake_link: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out create is reconciled via list (by our metadata); create is never called a second time."""
    monkeypatch.setenv("FAKE_LINK_BEHAVIOR", "slow_create")
    override_settings(link_timeout_seconds=0.5)
    provider = LinkTestProvider()
    with pytest.raises(PaymentUncertain):
        asyncio.run(provider.create_request(spec()))
    found = asyncio.run(provider.reconcile(spec()))
    assert found is not None and found.request_id == "lsrq_fake_1"
    creates = [c for c in calls(fake_link) if c[:2] == ["spend-request", "create"]]
    assert len(creates) == 1


def test_missing_card_file_is_a_retrieval_error(fake_link: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If Link doesn't write a card (not approved), retrieval fails cleanly."""
    monkeypatch.setenv("FAKE_LINK_BEHAVIOR", "no_card_file")
    with pytest.raises(PaymentError) as caught:
        asyncio.run(LinkTestProvider().retrieve_card("lsrq_fake_1"))
    assert caught.value.code == "card_missing"


def test_amount_limit_and_context_are_enforced_before_calling_link(fake_link: Path) -> None:
    """Our own limits (the verified 50000 cap, a >=100 char context) are checked before the CLI runs."""
    provider = LinkTestProvider()
    with pytest.raises(PaymentError):
        asyncio.run(provider.create_request(spec(amount_minor=50001)))
    with pytest.raises(PaymentError):
        asyncio.run(provider.create_request(spec(context="too short")))
    assert calls(fake_link) == []


def test_link_status_mapping() -> None:
    """Every verified Link status maps to one of ours; an unknown status never advances anything."""
    assert payments.LINK_STATUS_MAP["pending_approval"] == "pending"
    assert payments.LINK_STATUS_MAP["approved"] == "approved"
    assert payments.LINK_STATUS_MAP["denied"] == "denied"
    assert payments.LINK_STATUS_MAP["expired"] == "expired"
    assert payments.LINK_STATUS_MAP.get("something_new", payments.P_UNKNOWN) == "unknown"


def test_sanitize_strips_card_numbers_and_secrets() -> None:
    """stderr is cleaned before it's kept anywhere."""
    dirty = "error for card 4000 0099 9000 1984 token=abc123secret"
    clean = payments.sanitize(dirty)
    assert "4000" not in clean and "abc123secret" not in clean


# ============================================================
# The state machine, through the API (stub provider)
# ============================================================


def signed_contract(client: TestClient, **overrides: Any) -> str:
    """Sign the demo draft (the fixture contract with Mock Nike) and return its id."""
    draft = {**api_draft_payload(), **overrides}
    assert client.post("/contracts", json=draft).status_code == 201
    return client.post(f"/contracts/{draft['id']}/sign").json()["id"]


def authorized_purchase(client: TestClient, contract_id: str) -> dict[str, Any]:
    """An AUTHORIZED purchase through the static extractor."""
    STATIC_EXTRACTOR.set(proposal_payload(contract_id))
    body = client.post("/purchases", json={"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout"}).json()
    assert body["status"] == "authorized", body
    return body


def payment_row(purchase_id: str) -> db.PaymentRow:
    """The payment row (fresh from the database)."""
    with db.SessionLocal() as session:
        row = db.get_payment(session, purchase_id)
        session.expunge(row)
        return row


def test_authorized_purchase_awaits_approval(client: TestClient) -> None:
    """AUTHORIZED -> a simulated spend request exists, waiting for the user; the agent is told to wait."""
    body = authorized_purchase(client, signed_contract(client))
    assert body["payment_state"] == "awaiting_approval"
    assert body["next_action"] == "wait_for_user_link_approval"
    assert body["approval_url"].endswith(f"/purchases/{body['purchase_id']}")  # stub: the frontend's button
    assert body["payment"]["provider_label"] == "Simulated provider"
    # Polling while pending changes nothing (idempotent).
    again = client.post(f"/purchases/{body['purchase_id']}/payment/refresh").json()
    assert again["payment_state"] == "awaiting_approval"


def test_denied_approval_fails_purchase_and_frees_contract(client: TestClient) -> None:
    """Denied in the provider -> payment denied, purchase failed, credential revoked, contract usable again."""
    contract_id = signed_contract(client)
    body = authorized_purchase(client, contract_id)
    StubProvider.simulate(payment_row(body["purchase_id"]).provider_request_id, "denied")
    after = client.get(f"/purchases/{body['purchase_id']}").json()
    assert after["payment_state"] == "denied" and after["status"] == "failed"
    assert after["credential"]["status"] == "revoked"
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "active"


def test_expired_approval(client: TestClient) -> None:
    """Expired in the provider -> payment expired, and the agent is told to request again."""
    body = authorized_purchase(client, signed_contract(client))
    StubProvider.simulate(payment_row(body["purchase_id"]).provider_request_id, "expired")
    after = client.get(f"/purchases/{body['purchase_id']}").json()
    assert after["payment_state"] == "expired" and after["next_action"] == "request_purchase_again"


def test_no_card_before_approval(client: TestClient, agent_client: TestClient) -> None:
    """The agent can't collect a card while the user hasn't approved."""
    body = authorized_purchase(client, signed_contract(client))
    response = agent_client.post(f"/purchases/{body['purchase_id']}/credential")
    assert response.status_code == 409 and response.json()["error"] == "payment_not_ready"


def test_simulated_approval_is_user_only(client: TestClient, agent_client: TestClient) -> None:
    """The agent can't approve its own payment, even the simulated one."""
    body = authorized_purchase(client, signed_contract(client))
    response = agent_client.post(f"/purchases/{body['purchase_id']}/payment/simulate-approval")
    assert response.status_code == 403 and response.json()["error"] == "agent_not_permitted"


def test_approval_then_revalidation_then_card_ready(client: TestClient, agent_client: TestClient) -> None:
    """Simulated approval -> checkout re-read (unchanged) -> card ready for the agent, exactly once."""
    body = authorized_purchase(client, signed_contract(client))
    ready = client.post(f"/purchases/{body['purchase_id']}/payment/simulate-approval").json()
    assert ready["payment_state"] == "credential_ready"
    assert ready["next_action"] == "get_payment_credential_and_pay"
    kinds = [e["data"].get("kind") for e in client.get(f"/evidence/{body['purchase_id']}").json()["events"]]
    assert kinds[-4:] == ["simulated_provider_approval", "payment_approved", "checkout_revalidated", "credential_ready"]

    first = agent_client.post(f"/purchases/{body['purchase_id']}/credential")
    assert first.status_code == 200
    assert first.headers["cache-control"].startswith("no-store")
    card = first.json()
    assert card["card_number"] == payments.STUB_TEST_CARD and card["simulated"] is True
    assert card["amount"] == 128.39 and card["pay_url"].endswith("/pay")

    second = agent_client.post(f"/purchases/{body['purchase_id']}/credential")
    assert second.status_code == 409 and second.json()["error"] == "credential_already_released"
    assert "card_number" not in second.text


def test_user_cannot_collect_the_card(client: TestClient) -> None:
    """Only the agent collects the card (the user never needs it)."""
    body = authorized_purchase(client, signed_contract(client))
    client.post(f"/purchases/{body['purchase_id']}/payment/simulate-approval")
    assert client.post(f"/purchases/{body['purchase_id']}/credential").status_code == 403


def test_foreign_agent_binding_cannot_collect(client: TestClient, agent_client: TestClient) -> None:
    """A contract bound to a different agent: this agent can't collect its card (or even request against it)."""
    assert client.post("/contracts", json=api_draft_payload()).status_code == 201
    contract_id = client.post("/contracts/draft_demo_shoes/sign", json={"draft_id": "draft_demo_shoes", "agent_key": "agent_other"}).json()["id"]
    body = authorized_purchase(client, contract_id)  # the USER may still request it
    client.post(f"/purchases/{body['purchase_id']}/payment/simulate-approval")
    response = agent_client.post(f"/purchases/{body['purchase_id']}/credential")
    assert response.status_code == 403 and response.json()["error"] == "agent_not_authorized"


def test_card_retrieval_failure_allows_a_retry(client: TestClient, agent_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the provider can't hand over the card, nothing was released: the payment goes back to ready."""
    body = authorized_purchase(client, signed_contract(client))
    client.post(f"/purchases/{body['purchase_id']}/payment/simulate-approval")

    async def broken(self: Any, request_id: str) -> Any:
        """The provider fails to retrieve the card."""
        raise PaymentError("card_missing", "no card")

    monkeypatch.setattr(StubProvider, "retrieve_card", broken)
    response = agent_client.post(f"/purchases/{body['purchase_id']}/credential")
    assert response.status_code == 502 and response.json()["error"] == "credential_retrieval_failed"
    assert payment_row(body["purchase_id"]).state == "credential_ready"


def test_cart_changed_after_authorization_is_blocked_at_revalidation(client: TestClient) -> None:
    """The merchant raises the price after authorization: revalidation re-runs the engine and pays nothing."""
    contract_id = signed_contract(client)
    body = authorized_purchase(client, contract_id)

    def pricier(p: dict[str, Any]) -> dict[str, Any]:
        """The same checkout, now with a surprise fee that busts the cap."""
        p["fees"] = 15.00
        p["total"] = 143.39
        return p

    STATIC_EXTRACTOR.set(pricier(proposal_payload(contract_id)))
    after = client.post(f"/purchases/{body['purchase_id']}/payment/simulate-approval").json()
    assert after["payment_state"] == "checkout_changed"
    assert after["status"] == "blocked"
    assert after["next_action"] == "request_purchase_again"
    event = client.get(f"/evidence/{body['purchase_id']}").json()["events"][-1]
    assert event["data"]["kind"] == "checkout_changed"
    assert any("exceeds the signed all-in cap" in reason for reason in event["data"]["reasons"])


def test_decline_before_payment(client: TestClient) -> None:
    """'Not this one': the user declines an authorized purchase; the provider request is cancelled."""
    contract_id = signed_contract(client)
    body = authorized_purchase(client, contract_id)
    request_id = payment_row(body["purchase_id"]).provider_request_id
    declined = client.post(f"/purchases/{body['purchase_id']}/reject").json()
    assert declined["status"] == "blocked" and declined["payment_state"] == "denied"
    assert declined["resolution"]["action"] == "decline"
    assert StubProvider._requests[request_id]["status"] == "canceled"
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "active"


# ============================================================
# Full stub runs against the in-process merchant
# ============================================================


def demo_contract(client: TestClient) -> str:
    """Compile (offline fixture) and sign the section 12.1 demo contract."""
    record = client.post("/drafts/compile", json={"intent": compiler.DEMO_INTENT}).json()
    return client.post(f"/contracts/{record['id']}/sign").json()["id"]


def test_agent_visible_run_completes_with_verified_receipt(
    client: TestClient, agent_client: TestClient, merchant_env: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """
    valid scenario, default agent_visible mode: authorize -> simulated approval ->
    revalidate -> card released once -> the AGENT pays the merchant -> Handshake
    finds and verifies the order -> completed, receipt stored, contract used.
    Then sweep every DB row, response, and log line for card data.
    """
    caplog.set_level(logging.DEBUG)
    contract_id = demo_contract(client)
    checkout = merchant_env.post("/api/checkout-sessions", json={"scenario": "valid"}).json()
    created = agent_client.post("/purchases", json={"contract_id": contract_id, "checkout_url": checkout["checkout_url"], "idempotency_key": "e2e-valid-0001"}).json()
    assert created["status"] == "authorized", created["summary"]
    link_results = [r for r in created["decision"]["results"] if r["constraint"].startswith("checkout_link")]
    assert [r["verdict"] for r in link_results] == ["pass", "pass"]
    purchase_id = created["purchase_id"]

    ready = client.post(f"/purchases/{purchase_id}/payment/simulate-approval").json()
    assert ready["payment_state"] == "credential_ready"

    release = agent_client.post(f"/purchases/{purchase_id}/credential").json()
    order = merchant_env.post(release["pay_url"].replace(get_settings().merchant_url, ""), json={
        "card_number": release["card_number"], "exp_month": release["exp_month"], "exp_year": release["exp_year"],
        "cvc": release["cvc"], "amount": release["amount"], "currency": release["currency"],
    }).json()
    assert order["last4"] == "4242"

    done = agent_client.get(f"/purchases/{purchase_id}").json()
    assert done["status"] == "completed" and done["payment_state"] == "completed"
    assert done["next_action"] == "completed"
    assert done["payment"]["receipt"]["order_id"] == order["order_id"]
    assert done["payment"]["last4"] == "4242"
    assert client.get(f"/contracts/{contract_id}").json()["status"] == "used"
    kinds = [e["data"].get("kind") for e in client.get(f"/evidence/{purchase_id}").json()["events"]]
    for kind in ("payment_requested", "payment_approved", "checkout_revalidated", "credential_released", "payment_submitted", "receipt_verified"):
        assert kind in kinds, kind

    # --- The sweep: card data exists ONLY in the one-time release response ---
    blobs: list[str] = []
    with db.SessionLocal() as session:
        for table in (db.DraftRow, db.ContractRow, db.ProposalRow, db.DecisionRow, db.PurchaseRow, db.CredentialRow, db.EvidenceRow, db.PaymentRow):
            for row in session.query(table).all():
                blobs.append(json.dumps({c.name: getattr(row, c.name) for c in row.__table__.columns}, default=str))
    blobs += [json.dumps(client.get(path).json()) for path in (f"/purchases/{purchase_id}", f"/evidence/{purchase_id}", "/purchases", f"/contracts/{contract_id}")]
    blobs += [record.getMessage() for record in caplog.records]
    everything = "\n".join(blobs)
    assert payments.STUB_TEST_CARD not in everything
    assert card_like_numbers(everything) == [], "card-like number found"
    assert '"cvc"' not in everything and "card_number" not in everything


def test_executor_mode_completes_without_the_agent_seeing_a_card(test_db: Any, merchant_env: TestClient) -> None:
    """HANDSHAKE_CREDENTIAL_MODE=executor: no /credential route; the backend pays and verifies by itself."""
    from conftest import user_headers
    from handshake.api import create_app

    override_settings(credential_mode="executor")
    app = create_app()  # the real extractor (no override) -> the in-process merchant via the shared factory
    with TestClient(app, headers=user_headers()) as user, TestClient(app, headers=bearer(AGENT_TOKEN)) as agent:
        contract_id = demo_contract(user)
        checkout = merchant_env.post("/api/checkout-sessions", json={"scenario": "valid"}).json()
        created = agent.post("/purchases", json={"contract_id": contract_id, "checkout_url": checkout["checkout_url"], "idempotency_key": "e2e-exec-0001"}).json()
        assert created["status"] == "authorized"
        assert agent.post(f"/purchases/{created['purchase_id']}/credential").status_code == 404  # route absent
        done = user.post(f"/purchases/{created['purchase_id']}/payment/simulate-approval").json()
        assert done["status"] == "completed" and done["payment_state"] == "completed"
        assert done["payment"]["receipt"]["amount_charged"] == 128.39


def test_uncertain_pay_outcome_is_never_retried_blindly(test_db: Any, merchant_env: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Executor mode: the pay call times out. Handshake asks the merchant; with no order, it fails, and it never pays twice."""
    from conftest import user_headers
    from handshake.api import create_app

    override_settings(credential_mode="executor")
    attempts: list[int] = []

    def flaky_pay(*args: Any, **kwargs: Any) -> Any:
        """The merchant never answers."""
        attempts.append(1)
        raise PaymentUncertain("pay_uncertain", "timed out")

    monkeypatch.setattr(payments, "pay_merchant", flaky_pay)
    app = create_app()
    with TestClient(app, headers=user_headers()) as user, TestClient(app, headers=bearer(AGENT_TOKEN)) as agent:
        contract_id = demo_contract(user)
        checkout = merchant_env.post("/api/checkout-sessions", json={"scenario": "valid"}).json()
        created = agent.post("/purchases", json={"contract_id": contract_id, "checkout_url": checkout["checkout_url"], "idempotency_key": "e2e-flaky-0001"}).json()
        after = user.post(f"/purchases/{created['purchase_id']}/payment/simulate-approval").json()
        # The same refresh loop immediately checked the merchant, found no order, and stopped.
        assert after["payment_state"] == "failed"
        for _ in range(3):
            user.post(f"/purchases/{created['purchase_id']}/payment/refresh")
        assert len(attempts) == 1
