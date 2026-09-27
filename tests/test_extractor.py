"""
test_extractor.py: the mock merchant (Sri's store) and Handshake's independent extractor.

Covers docs/original/HANDSHAKE_BUILD.md sections 7.2 to 7.5:
- every scenario normalizes into a valid models.py TransactionProposal
- SSRF rules: wrong origin, file/ftp schemes, redirects to another origin, internal IPs
- the two readings (JSON feed vs the HTML page's embedded facts) are compared
- missing facts stay absent (so strict mode escalates), dates become timezone-aware
- the merchant's /pay: Luhn, expiry, exact amount, idempotency, last4 only
- POST /purchases with a disallowed URL, idempotent retries, and the agent's key requirement
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from handshake import merchant
from handshake.config import get_settings, override_settings
from handshake.extractor import (
    ExtractionError,
    MockMerchantExtractor,
    check_url_allowed,
    facts_from_html,
    normalize,
)
from handshake.intent_diff import explicit_fields
from handshake.models import TransactionProposal
from conftest import STATIC_EXTRACTOR, api_draft_payload, proposal_payload, funded


# ------------------------------------------------------------
# Helpers: an in-process merchant the extractor can reach
# ------------------------------------------------------------


@pytest.fixture
def merchant_app() -> Any:
    """A fresh mock merchant app (its own in-memory sessions)."""
    return merchant.create_app()


@pytest.fixture
def merchant_client(merchant_app: Any) -> TestClient:
    """A client for the merchant, at the configured merchant URL."""
    return TestClient(merchant_app, base_url=get_settings().merchant_url, follow_redirects=False)


def extractor_for(merchant_app: Any) -> MockMerchantExtractor:
    """A real MockMerchantExtractor whose HTTP calls go to the in-process merchant app."""
    def factory(settings: Any) -> TestClient:
        """A fresh client per extraction, with redirects NOT followed (the extractor checks each hop)."""
        return TestClient(merchant_app, base_url=settings.merchant_url, follow_redirects=False)

    return MockMerchantExtractor(client_factory=factory)


def new_session(merchant_client: TestClient, scenario: str = "valid", **extra: Any) -> dict[str, Any]:
    """Create a checkout session and return {session_id, checkout_url, scenario}."""
    response = merchant_client.post("/api/checkout-sessions", json={"scenario": scenario, **extra})
    assert response.status_code == 201, response.json()
    return response.json()


# ============================================================
# Normalization of every scenario
# ============================================================


@pytest.mark.parametrize("scenario", merchant.SCENARIOS)
def test_every_scenario_normalizes_to_a_valid_proposal(merchant_app: Any, merchant_client: TestClient, scenario: str) -> None:
    """Both readings agree and parse into a TransactionProposal with the right contract binding."""
    session = new_session(merchant_client, scenario)
    extraction = extractor_for(merchant_app).extract(session["checkout_url"], "contract_x")
    proposal = TransactionProposal.model_validate(extraction.raw_proposal)

    assert proposal.contract_id == "contract_x"
    assert proposal.extractors_agreed is True
    assert proposal.extractor_ids == ["mock_merchant.json_feed", "mock_merchant.html_embedded"]
    assert proposal.merchant.name == "Amazon.com"
    assert proposal.merchant.domain == "localhost"
    assert proposal.source_url == session["checkout_url"]
    assert len(extraction.snapshot_hash) == 64
    # Explicitly stated terms are explicitly present (strict mode sees real facts).
    assert {"recurring_billing.detected", "membership_detected", "addons_detected", "line_items[0].quantity"} <= explicit_fields(proposal)


def test_valid_scenario_facts(merchant_app: Any, merchant_client: TestClient) -> None:
    """Sri's numbers: 119.99 + 8.40 tax = 128.39, size moved into attributes, Nike verified, not first party."""
    raw = extractor_for(merchant_app).extract(new_session(merchant_client)["checkout_url"], "c").raw_proposal
    proposal = TransactionProposal.model_validate(raw)
    item = proposal.line_items[0]
    assert (item.name, item.category, item.condition, item.quantity, item.unit_price) == ("Pegasus 41", "running_shoes", "new", 1, 119.99)
    assert item.attributes == {"size": "10"}
    assert (proposal.item_subtotal, proposal.tax, proposal.total) == (119.99, 8.40, 128.39)
    assert proposal.merchant.seller_of_record == "Nike"
    assert (proposal.merchant.is_first_party, proposal.merchant.is_verified) == (False, True)
    assert proposal.recurring_billing.detected is False
    assert (proposal.membership_detected, proposal.addons_detected) == (False, False)


def test_hidden_subscription_facts(merchant_app: Any, merchant_client: TestClient) -> None:
    """Shoe Club: recurring billing (with details), membership true, a second line item."""
    raw = extractor_for(merchant_app).extract(new_session(merchant_client, "hidden_subscription")["checkout_url"], "c").raw_proposal
    proposal = TransactionProposal.model_validate(raw)
    assert proposal.recurring_billing.detected is True
    assert (proposal.recurring_billing.interval, proposal.recurring_billing.amount) == ("month", 14.99)
    assert proposal.membership_detected is True
    assert [item.name for item in proposal.line_items] == ["Pegasus 41", "Shoe Club Membership (Auto-Renew)"]
    assert proposal.line_items[1].attributes == {}  # size None -> absent, not "None"
    assert proposal.total == 143.38


def test_unknown_seller_stays_unknown(merchant_app: Any, merchant_client: TestClient) -> None:
    """SneakerDeals123's verification is published as unknown and must stay null."""
    raw = extractor_for(merchant_app).extract(new_session(merchant_client, "unknown_seller")["checkout_url"], "c").raw_proposal
    proposal = TransactionProposal.model_validate(raw)
    assert proposal.merchant.seller_of_record == "SneakerDeals123"
    assert proposal.merchant.is_verified is None
    assert proposal.merchant.is_first_party is False


def test_prompt_injection_is_inert_text(merchant_app: Any, merchant_client: TestClient) -> None:
    """The injected instruction is carried as a product name and nothing else; the total is 500.00."""
    raw = extractor_for(merchant_app).extract(new_session(merchant_client, "prompt_injection")["checkout_url"], "c").raw_proposal
    proposal = TransactionProposal.model_validate(raw)
    assert "Ignore previous rules" in proposal.line_items[0].name
    assert proposal.total == 500.00


def test_delivery_dates_become_aware_end_of_day(merchant_app: Any, merchant_client: TestClient) -> None:
    """A date-only promise becomes 23:59:59 that day, timezone-aware, with the UTC assumption recorded."""
    extraction = extractor_for(merchant_app).extract(new_session(merchant_client)["checkout_url"], "c")
    promised = TransactionProposal.model_validate(extraction.raw_proposal).delivery.promised_by
    assert promised.tzinfo is not None
    assert (promised.hour, promised.minute, promised.second) == (23, 59, 59)
    assert any("UTC" in note for note in extraction.evidence["assumptions"])


def test_vague_delivery_stays_null(merchant_app: Any, merchant_client: TestClient) -> None:
    """No promised date stays None (the engine escalates it), never an invented date."""
    raw = extractor_for(merchant_app).extract(new_session(merchant_client, "vague_delivery")["checkout_url"], "c").raw_proposal
    assert TransactionProposal.model_validate(raw).delivery.promised_by is None


def test_missing_membership_fact_stays_absent() -> None:
    """If the merchant doesn't state membership, membership_detected is not sent at all."""
    facts = merchant.build_facts("valid")
    facts.pop("membership")
    proposal_dict, _ = normalize(facts, "http://localhost:3001/checkout/cs_x", "c")
    assert "membership_detected" not in proposal_dict
    assert "membership_detected" not in explicit_fields(TransactionProposal.model_validate(proposal_dict))


def test_missing_seller_flags_stay_null() -> None:
    """A merchant that doesn't publish seller flags gets None, not False or True."""
    facts = merchant.build_facts("valid")
    facts["merchant"].pop("seller_verified")
    facts["merchant"].pop("seller_is_first_party")
    proposal_dict, _ = normalize(facts, "http://localhost:3001/checkout/cs_x", "c")
    assert proposal_dict["merchant"]["is_verified"] is None
    assert proposal_dict["merchant"]["is_first_party"] is None


def test_readings_that_disagree_flip_extractors_agreed(merchant_app: Any, merchant_client: TestClient) -> None:
    """If the page shows a different total than the feed, extractors_agreed is False (the engine escalates)."""
    session = new_session(merchant_client)
    stored = merchant_app.state.merchant.sessions[session["session_id"]]
    stored.html_override = {**stored.facts, "total": 99.99}

    extraction = extractor_for(merchant_app).extract(session["checkout_url"], "c")
    assert extraction.raw_proposal["extractors_agreed"] is False
    assert "total" in extraction.evidence["disagreements"]


def test_merchant_page_embeds_facts_safely(merchant_client: TestClient) -> None:
    """The HTML reading equals the feed, and merchant text can't break out of the script tag."""
    session = new_session(merchant_client, "prompt_injection")
    html = merchant_client.get(f"/checkout/{session['session_id']}").text
    feed = merchant_client.get(f"/api/checkout/{session['session_id']}").json()
    page_facts = facts_from_html(html)
    assert page_facts["line_items"] == feed["line_items"]
    facts_tag = html.split('id="handshake-checkout-facts">')[1].split("</script>")[0]
    assert "<" not in facts_tag  # every < is escaped as <


# ============================================================
# SSRF safety
# ============================================================


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/checkout/cs_1",  # not an allowed merchant origin
        "file:///etc/passwd",  # wrong scheme
        "ftp://localhost:3001/checkout/cs_1",  # wrong scheme
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://10.0.0.5/checkout/cs_1",  # internal IP, not allowlisted
        "http://127.0.0.1:8000/checkout/cs_1",  # the backend itself, not the merchant
        "http://user:pass@localhost:3001/checkout/cs_1",  # credentials in URL
        "http://localhost:3002/checkout/cs_1",  # right host, wrong port
    ],
)
def test_ssrf_disallowed_urls(url: str) -> None:
    """Anything but the allowlisted merchant origin is refused before any fetch."""
    with pytest.raises(ExtractionError) as caught:
        check_url_allowed(url, get_settings())
    assert caught.value.code == "unsupported_checkout_url"


def test_link_local_is_refused_even_if_allowlisted() -> None:
    """Metadata addresses stay blocked even if someone misconfigures the allowlist."""
    override_settings(allowed_merchant_origins=("http://169.254.169.254",))
    with pytest.raises(ExtractionError):
        check_url_allowed("http://169.254.169.254/checkout/cs_1", get_settings())


def test_redirect_to_another_origin_is_refused() -> None:
    """A merchant that redirects the checkout to another site gets nothing fetched from there."""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """The feed redirects to an internal service."""
        fetched.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    extractor = MockMerchantExtractor(client_factory=lambda s: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))
    with pytest.raises(ExtractionError) as caught:
        extractor.extract(f"{get_settings().merchant_url}/checkout/cs_1", "c")
    assert caught.value.code == "unsupported_checkout_url"
    assert fetched == [f"{get_settings().merchant_url}/api/checkout/cs_1"]  # never followed


def test_oversized_response_is_refused() -> None:
    """A checkout bigger than HANDSHAKE_MAX_CHECKOUT_BYTES is not read."""
    override_settings(max_checkout_bytes=1000)
    extractor = MockMerchantExtractor(
        client_factory=lambda s: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000)), follow_redirects=False)
    )
    with pytest.raises(ExtractionError) as caught:
        extractor.extract(f"{get_settings().merchant_url}/checkout/cs_1", "c")
    assert caught.value.code == "checkout_too_large"


def test_unreachable_merchant_is_a_clean_error() -> None:
    """A connection failure becomes checkout_unreachable (502), not a crash."""
    def refuse(request: httpx.Request) -> httpx.Response:
        """Simulate the merchant being down."""
        raise httpx.ConnectError("connection refused")

    extractor = MockMerchantExtractor(client_factory=lambda s: httpx.Client(transport=httpx.MockTransport(refuse), follow_redirects=False))
    with pytest.raises(ExtractionError) as caught:
        extractor.extract(f"{get_settings().merchant_url}/checkout/cs_1", "c")
    assert caught.value.code == "checkout_unreachable" and caught.value.status_code == 502


# ============================================================
# The merchant's pay endpoint
# ============================================================


def pay_body(**changes: Any) -> dict[str, Any]:
    """A valid pay request for the valid scenario's total, with the Stripe docs test card."""
    return {"card_number": "4242 4242 4242 4242", "exp_month": 12, "exp_year": datetime.now(timezone.utc).year + 2, "cvc": "123", "amount": 128.39, "currency": "USD", **changes}


def test_pay_succeeds_stores_only_last4_and_is_idempotent(merchant_client: TestClient, merchant_app: Any) -> None:
    """A correct payment creates one order; paying again returns that same order."""
    sid = new_session(merchant_client)["session_id"]
    first = merchant_client.post(f"/api/checkout/{sid}/pay", json=pay_body())
    assert first.status_code == 200
    order = first.json()
    assert order["last4"] == "4242" and order["amount_charged"] == 128.39
    assert "4242424242424242" not in json.dumps(order)

    again = merchant_client.post(f"/api/checkout/{sid}/pay", json=pay_body()).json()
    assert again["order_id"] == order["order_id"] and again["idempotent_replay"] is True
    assert len(merchant_app.state.merchant.orders) == 1
    assert merchant_client.get(f"/api/orders/{order['order_id']}").json()["session_id"] == sid
    assert merchant_client.get(f"/api/checkout/{sid}/order").json()["order"]["order_id"] == order["order_id"]


@pytest.mark.parametrize(
    "changes, status, code",
    [
        ({"amount": 128.38}, 422, "amount_mismatch"),  # one cent short
        ({"amount": 128.40}, 422, "amount_mismatch"),  # one cent over
        ({"card_number": "4242 4242 4242 4241"}, 402, "card_declined"),  # fails Luhn
        ({"exp_year": 2020}, 402, "card_expired"),
        ({"cvc": "12a"}, 402, "card_declined"),
        ({"currency": "EUR"}, 422, "currency_mismatch"),
    ],
)
def test_pay_rejections(merchant_client: TestClient, changes: dict[str, Any], status: int, code: str) -> None:
    """Bad cards, wrong amounts, and wrong currencies are refused, and no order exists afterwards."""
    sid = new_session(merchant_client)["session_id"]
    response = merchant_client.post(f"/api/checkout/{sid}/pay", json=pay_body(**changes))
    assert response.status_code == status and response.json()["error"] == code
    assert merchant_client.get(f"/api/checkout/{sid}/order").json()["order"] is None


def test_link_test_card_is_accepted_though_not_luhn_valid(merchant_client: TestClient) -> None:
    """Stripe's documented Link test card fails Luhn; the mock store accepts it anyway (and only it)."""
    sid = new_session(merchant_client)["session_id"]
    response = merchant_client.post(f"/api/checkout/{sid}/pay", json=pay_body(card_number="4000009990001984"))
    assert response.status_code == 200 and response.json()["last4"] == "1984"


def test_dev_scenario_change_changes_the_snapshot(merchant_app: Any, merchant_client: TestClient) -> None:
    """The dev route changes a cart after creation; the extractor sees a new snapshot hash."""
    session = new_session(merchant_client)
    extractor = extractor_for(merchant_app)
    before = extractor.extract(session["checkout_url"], "c").snapshot_hash
    assert merchant_client.post(f"/api/dev/checkout/{session['session_id']}/scenario", json={"scenario": "price_bump"}).json()["total"] == 143.39
    after = extractor.extract(session["checkout_url"], "c").snapshot_hash
    assert before != after


def test_dev_route_is_hidden_in_prod(merchant_client: TestClient) -> None:
    """Outside dev, the cart can't be changed through the dev route."""
    session = new_session(merchant_client)
    import base64

    override_settings(env="prod", signing_secret="prod-sign", session_secret="prod-session", agent_token=None, google_client_id="test.apps.googleusercontent.com", cors_origins=("https://app.example",),
                      card_encryption_key=base64.b64encode(b"p" * 32).decode())
    assert merchant_client.post(f"/api/dev/checkout/{session['session_id']}/scenario", json={"scenario": "price_bump"}).status_code == 404


def test_unknown_session_is_404(merchant_client: TestClient) -> None:
    """Checkout pages for sessions that don't exist are 404."""
    assert merchant_client.get("/api/checkout/cs_nope").status_code == 404


# ============================================================
# Through the API
# ============================================================


def sign(client: TestClient) -> str:
    """Create and sign the demo draft; return the contract id."""
    draft = api_draft_payload()
    assert client.post("/contracts", json=draft).status_code == 201
    return funded(client, client.post(f"/contracts/{draft['id']}/sign").json()["id"])


def test_disallowed_checkout_url_is_422_with_evidence(api_app: Any, client: TestClient) -> None:
    """With the real extractor, a URL off the allowlist is refused and the attempt is recorded."""
    from handshake.extractor import get_extractor

    api_app.dependency_overrides.pop(get_extractor)  # use the REAL extractor
    contract_id = sign(client)
    response = client.post("/purchases", json={"contract_id": contract_id, "checkout_url": "https://evil.example/checkout/cs_1"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "unsupported_checkout_url"
    evidence = client.get(f"/evidence/{body['details']['purchase_id']}").json()
    assert evidence["status"] == "blocked"
    assert evidence["events"][-1]["data"]["reason"] == "unsupported_checkout_url"


def test_proposal_field_is_no_longer_accepted(client: TestClient) -> None:
    """The caller can't supply the facts: a 'proposal' key is refused (and recorded against the contract)."""
    contract_id = sign(client)
    response = client.post("/purchases", json={"contract_id": contract_id, "checkout_url": "x", "proposal": proposal_payload(contract_id)})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_idempotent_request_returns_the_same_purchase(client: TestClient) -> None:
    """The same idempotency key twice: one purchase, returned both times, even after the contract is used."""
    contract_id = sign(client)
    STATIC_EXTRACTOR.set(proposal_payload(contract_id))
    body = {"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout", "idempotency_key": "retry-key-0001"}
    first = client.post("/purchases", json=body).json()
    second = client.post("/purchases", json=body).json()
    assert first["status"] == "authorized"
    assert second["purchase_id"] == first["purchase_id"]
    assert second["idempotent_replay"] is True
    assert len(client.get(f"/contracts/{contract_id}/purchases").json()) == 1


def test_agent_must_send_an_idempotency_key(client: TestClient, agent_client: TestClient) -> None:
    """The agent's retries must be safe, so the key is mandatory for it."""
    contract_id = sign(client)
    STATIC_EXTRACTOR.set(proposal_payload(contract_id))
    response = agent_client.post("/purchases", json={"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout"})
    assert response.status_code == 422
    assert response.json()["error"] == "idempotency_key_required"


def test_extractor_receives_url_and_contract_only(client: TestClient) -> None:
    """Handshake calls the extractor with the URL and the contract id; nothing from the agent's claims."""
    contract_id = sign(client)
    STATIC_EXTRACTOR.set(proposal_payload(contract_id))
    client.post("/purchases", json={"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout"})
    assert STATIC_EXTRACTOR.calls[-1] == ("https://mocknike.example/checkout", contract_id)


# ============================================================
# The checkout-link parser (does the agent's link match the contract?)
# ============================================================

from handshake.extractor import checkout_link_results, parse_checkout_link  # noqa: E402
from handshake.models import CandidateProduct, SelectionReport  # noqa: E402
from conftest import make_contract  # noqa: E402


def link_for(url: str) -> dict[str, Any]:
    """Parse a link the way the real extractor does (the page was read from that exact URL)."""
    link = parse_checkout_link(url, get_settings())
    link["fetched_url"] = url
    return link


def amazon_contract(**merchant_changes: Any) -> Any:
    """The fixture contract, allowing the mock store's identity (Amazon.com)."""
    return make_contract(merchants={"allow": ["Amazon.com"], **merchant_changes})


def page(name: str = "Amazon.com", item: str = "Pegasus 41", price: float = 119.99) -> dict[str, Any]:
    """A minimal extracted proposal dict."""
    return {"merchant": {"name": name}, "line_items": [{"name": item, "unit_price": price}]}


def verdicts(results: list[Any]) -> dict[str, str]:
    """constraint name -> verdict value."""
    return {r.constraint: r.verdict.value for r in results}


MERCHANT = "http://localhost:3001"


def test_link_rules_pass_for_the_real_store() -> None:
    """A /checkout/<id> link on the registered store, claiming to be that store, which the contract allows."""
    results = checkout_link_results(amazon_contract(), link_for(f"{MERCHANT}/checkout/cs_abc123"), page(), None)
    assert verdicts(results) == {"checkout_link_format": "pass", "checkout_link_merchant": "pass"}


def test_link_to_a_non_checkout_page_fails() -> None:
    """A product or search page is not a checkout, so there's nothing to authorize."""
    results = checkout_link_results(amazon_contract(), link_for(f"{MERCHANT}/search?q=pegasus"), page(), None)
    assert verdicts(results)["checkout_link_format"] == "fail"


def test_page_claiming_a_different_merchant_fails() -> None:
    """The page says 'Nike Outlet' but the link belongs to Amazon.com: impersonation, blocked."""
    results = checkout_link_results(amazon_contract(), link_for(f"{MERCHANT}/checkout/cs_abc123"), page(name="Nike Outlet"), None)
    result = {r.constraint: r for r in results}["checkout_link_merchant"]
    assert result.verdict.value == "fail" and "belongs to 'Amazon.com'" in result.reason


def test_link_merchant_denied_by_contract_fails() -> None:
    """The link's merchant is on the contract's deny list."""
    contract = make_contract(merchants={"allow": [], "deny": ["amazon.com"]})
    assert verdicts(checkout_link_results(contract, link_for(f"{MERCHANT}/checkout/cs_abc123"), page(), None))["checkout_link_merchant"] == "fail"


def test_unregistered_origin_is_unverifiable() -> None:
    """An origin Handshake can't map to a merchant can't be matched to the contract, so it escalates."""
    override_settings(merchant_identities=(("http://localhost:3999", "Other Store"),))
    results = checkout_link_results(amazon_contract(), link_for(f"{MERCHANT}/checkout/cs_abc123"), page(), None)
    assert verdicts(results)["checkout_link_merchant"] == "unverifiable"


def selection(contract_id: str, **candidate: Any) -> SelectionReport:
    """A selection report where the agent says what it picked."""
    fields = {"name": "Pegasus 41", "merchant": "Amazon.com", "price": 119.99, "url": f"{MERCHANT}/checkout/cs_abc123", **candidate}
    chosen = CandidateProduct(**fields)
    return SelectionReport(contract_id=contract_id, selected_candidate=chosen, candidates=[chosen], reasoning_summary="Best match.")


def test_selection_matching_the_link_passes() -> None:
    """The agent's own report agrees with the link, the merchant, the product, and the price."""
    contract = amazon_contract()
    results = checkout_link_results(contract, link_for(f"{MERCHANT}/checkout/cs_abc123"), page(), selection(contract.id))
    assert verdicts(results)["checkout_link_selection"] == "pass"


@pytest.mark.parametrize(
    "changes, fragment",
    [
        ({"url": "https://other.example/checkout/cs_abc123"}, "not http://localhost:3001"),
        ({"url": f"{MERCHANT}/checkout/cs_different"}, "different checkout"),
        ({"merchant": "Nike Outlet"}, "from 'Nike Outlet'"),
        ({"name": "Air Max 90"}, "doesn't contain the product"),
    ],
)
def test_selection_not_matching_the_link_fails(changes: dict[str, Any], fragment: str) -> None:
    """The agent said it picked one thing, but the link is something else: blocked."""
    contract = amazon_contract()
    results = checkout_link_results(contract, link_for(f"{MERCHANT}/checkout/cs_abc123"), page(), selection(contract.id, **changes))
    result = {r.constraint: r for r in results}["checkout_link_selection"]
    assert result.verdict.value == "fail" and fragment in result.reason


def test_selection_for_another_contract_fails() -> None:
    """A selection report written for a different contract doesn't count."""
    contract = amazon_contract()
    results = checkout_link_results(contract, link_for(f"{MERCHANT}/checkout/cs_abc123"), page(), selection("contract_other"))
    assert verdicts(results)["checkout_link_selection"] == "fail"


def test_selection_price_differs_is_unverifiable() -> None:
    """Same product, but the checkout price isn't what the agent reported: ask the user."""
    contract = amazon_contract()
    results = checkout_link_results(contract, link_for(f"{MERCHANT}/checkout/cs_abc123"), page(price=129.99), selection(contract.id))
    assert verdicts(results)["checkout_link_selection"] == "unverifiable"


@pytest.mark.parametrize("reported", [119.99, 239.98, 128.39])  # unit price, line total (2 pairs), all-in total
def test_selection_price_may_be_the_item_price_or_the_checkout_total(reported: float) -> None:
    """Regression (first live Muse run): the agent reported the all-in total, not the unit price. Both agree with the checkout."""
    contract = amazon_contract()
    checkout = {**page(), "line_items": [{"name": "Pegasus 41", "unit_price": 119.99, "quantity": 2 if reported == 239.98 else 1}], "item_subtotal": 119.99, "total": 128.39}
    results = checkout_link_results(contract, link_for(f"{MERCHANT}/checkout/cs_abc123"), checkout, selection(contract.id, price=reported))
    assert verdicts(results)["checkout_link_selection"] == "pass"


def test_selection_price_matching_nothing_is_still_unverifiable() -> None:
    """A reported price that is neither the item's price nor any checkout total still goes to the user."""
    contract = amazon_contract()
    checkout = {**page(), "item_subtotal": 119.99, "total": 128.39}
    results = checkout_link_results(contract, link_for(f"{MERCHANT}/checkout/cs_abc123"), checkout, selection(contract.id, price=99.00))
    assert verdicts(results)["checkout_link_selection"] == "unverifiable"


def test_link_mismatch_blocks_a_purchase_end_to_end(client: TestClient, agent_client: TestClient, merchant_env: TestClient) -> None:
    """Through the API: the agent claims it picked a different product than the checkout it links to -> BLOCKED."""
    from handshake import compiler as compiler_module

    record = client.post("/drafts/compile", json={"intent": compiler_module.DEMO_INTENT}).json()
    contract_id = funded(client, client.post(f"/contracts/{record['id']}/sign").json()["id"])
    checkout = merchant_env.post("/api/checkout-sessions", json={"scenario": "valid"}).json()
    report = selection(contract_id, name="Air Max 90", url=checkout["checkout_url"]).model_dump(mode="json")
    body = agent_client.post("/purchases", json={
        "contract_id": contract_id, "checkout_url": checkout["checkout_url"], "idempotency_key": "link-mismatch-01", "selection_report": report,
    }).json()
    assert body["status"] == "blocked"
    first = body["decision"]["results"][:3]
    assert [r["constraint"] for r in first] == ["checkout_link_format", "checkout_link_merchant", "checkout_link_selection"]
    assert first[2]["verdict"] == "fail" and first[2]["label"] == "Link matches agent's pick"
