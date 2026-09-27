"""
test_mcp.py: the MCP server (section 10) and the agent login flow (OAuth device authorization).

A real MCP client session (in memory) talks to the real FastMCP server, which
talks over HTTP (an ASGI transport) to the real backend app with the offline
fixture compiler and the StaticExtractor. So tool discovery, tool calls, and
the backend's rules are all exercised together, with no network.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from handshake import compiler, payments
from handshake.config import get_settings, override_settings
from handshake.mcp_server import HandshakeBackend, build_server
from conftest import STATIC_EXTRACTOR, proposal_payload, funded

CARD_FIELDS = ("card_number", "cvc", "exp_month", "exp_year")


def backend_for(api_app: Any, tmp_path: Path) -> HandshakeBackend:
    """An MCP-side backend client wired to the in-process backend app (and a private token cache)."""
    return HandshakeBackend(transport=httpx.ASGITransport(app=api_app), token_file=str(tmp_path / "token.json"))


async def call(session: Any, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call an MCP tool and return its JSON result."""
    result = await session.call_tool(tool, arguments or {})
    if result.structuredContent is not None:
        data = result.structuredContent
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


def run_with_session(api_app: Any, tmp_path: Path, scenario: Any) -> Any:
    """Build the MCP server for the current settings, open a client session, and run `scenario(session, backend)`."""
    backend = backend_for(api_app, tmp_path)
    server = build_server(backend=backend)

    async def main() -> Any:
        """The async part."""
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            return await scenario(session, backend)

    return asyncio.run(main())


def signed_demo_contract(client: TestClient) -> str:
    """The user compiles (offline fixture) and signs the demo contract."""
    record = client.post("/drafts/compile", json={"intent": compiler.DEMO_INTENT}).json()
    return funded(client, client.post(f"/contracts/{record['id']}/sign").json()["id"])


def assert_no_card_data(value: Any) -> None:
    """No tool output other than get_payment_credential may carry card values."""
    text = json.dumps(value)
    assert payments.STUB_TEST_CARD not in text
    for field in CARD_FIELDS:
        assert f'"{field}"' not in text, field


# ============================================================
# Discovery
# ============================================================


def test_tools_and_prompt_are_discoverable(api_app: Any, tmp_path: Path) -> None:
    """Every tool is listed with a description; the credential tool is present by default."""
    async def scenario(session: Any, backend: Any) -> None:
        """List tools and prompts."""
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {
            "connect_handshake", "create_contract_draft", "get_contract", "list_contracts", "list_active_contracts",
            "request_purchase", "get_purchase_status", "get_payment_credential",
        }
        assert "no authority until the user reviews and signs" in tools["create_contract_draft"].description.lower() or \
            "NO authority" in tools["create_contract_draft"].description
        assert "never repeat the values" in tools["get_payment_credential"].description
        prompts = (await session.list_prompts()).prompts
        assert [p.name for p in prompts] == ["handshake_purchase_workflow"]
        text = (await session.get_prompt("handshake_purchase_workflow")).messages[0].content.text
        assert "request_purchase" in text and "get_payment_credential" in text

    run_with_session(api_app, tmp_path, scenario)


def test_credential_tool_absent_in_executor_mode(api_app: Any, tmp_path: Path) -> None:
    """HANDSHAKE_CREDENTIAL_MODE=executor: no get_payment_credential, and no card paragraph in the instructions."""
    override_settings(credential_mode="executor")

    async def scenario(session: Any, backend: Any) -> None:
        """List tools."""
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert "get_payment_credential" not in names
        text = (await session.get_prompt("handshake_purchase_workflow")).messages[0].content.text
        assert "get_payment_credential" not in text and "executor" in text

    run_with_session(api_app, tmp_path, scenario)


# ============================================================
# Tool calls against the backend
# ============================================================


def test_draft_then_purchase_through_mcp(api_app: Any, client: TestClient, tmp_path: Path) -> None:
    """create_contract_draft -> (user signs) -> request_purchase -> get_purchase_status, all through MCP."""
    async def scenario(session: Any, backend: Any) -> dict[str, Any]:
        """Draft, then (after the user signs) purchase."""
        draft = await call(session, "create_contract_draft", {"intent": compiler.DEMO_INTENT})
        assert draft["status"] == "draft" and draft["review_url"].endswith(f"/contracts/{draft['draft_id']}")
        assert "Pegasus 41" in draft["summary"] and "$135.00" in draft["summary"]

        contract_id = funded(client, client.post(f"/contracts/{draft['draft_id']}/sign").json()["id"])  # the USER signs
        STATIC_EXTRACTOR.set(_demo_checkout(contract_id))
        purchase = await call(session, "request_purchase", {"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout", "idempotency_key": "mcp-attempt-0001"})
        assert purchase["status"] == "authorized" and purchase["next_action"] == "get_payment_credential_and_pay"
        status = await call(session, "get_purchase_status", {"purchase_id": purchase["purchase_id"]})
        assert status["payment_state"] == "credential_ready"
        listing = await call(session, "list_contracts", {"status": "used"})
        assert [c["id"] for c in listing["contracts"]] == [contract_id]
        active = await call(session, "list_active_contracts")
        assert active["contracts"] == []
        contract = await call(session, "get_contract", {"contract_id": contract_id})
        assert contract["verification"]["valid"] is True
        for value in (draft, purchase, status, listing, contract):
            assert_no_card_data(value)
        return purchase

    run_with_session(api_app, tmp_path, scenario)


def _demo_checkout(contract_id: str) -> dict[str, Any]:
    """A checkout matching the section 12.1 demo contract (Amazon.com, Pegasus 41, size 10)."""
    proposal = proposal_payload(contract_id)
    proposal["merchant"]["name"] = "Amazon.com"
    proposal["line_items"][0]["name"] = "Pegasus 41"
    proposal["line_items"][0]["category"] = "running_shoes"
    # The demo contract's deadline is 3 days from NOW, so promise delivery tomorrow.
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    proposal["delivery"]["promised_by"] = tomorrow.isoformat()
    return proposal


def test_blocked_purchase_reports_reasons(api_app: Any, client: TestClient, tmp_path: Path) -> None:
    """A blocked purchase comes back with the failing checks and their reasons."""
    contract_id = signed_demo_contract(client)
    checkout = _demo_checkout(contract_id)
    checkout["fees"] = 15.0
    checkout["total"] = 143.39
    STATIC_EXTRACTOR.set(checkout)

    async def scenario(session: Any, backend: Any) -> None:
        """Request, and read the reasons."""
        result = await call(session, "request_purchase", {"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout", "idempotency_key": "mcp-blocked-0001"})
        assert result["status"] == "blocked" and result["next_action"] == "blocked_no_action"
        assert any(check["check"] == "Total price" and "exceeds" in check["reason"] for check in result["checks"])

    run_with_session(api_app, tmp_path, scenario)


def test_credential_tool_rules(api_app: Any, client: TestClient, tmp_path: Path) -> None:
    """get_payment_credential: refused while the checkout isn't authorized (escalated), delivered once when it is, refused the second time."""
    contract_id = signed_demo_contract(client)  # signed AND funded (the card is stored, locked)
    escalating = _demo_checkout(contract_id)
    escalating.pop("addons_detected")
    STATIC_EXTRACTOR.set(escalating)

    async def scenario(session: Any, backend: Any) -> None:
        """Try on an escalated purchase, then on an authorized one, twice."""
        escalated = await call(session, "request_purchase", {"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout", "idempotency_key": "mcp-card-esc1"})
        assert escalated["status"] == "escalated"
        early = await call(session, "get_payment_credential", {"purchase_id": escalated["purchase_id"]})
        assert early["error"] == "payment_not_ready"
        assert_no_card_data(early)

        client.post(f"/purchases/{escalated['purchase_id']}/approve")  # the USER accepts the exception
        status = await call(session, "get_purchase_status", {"purchase_id": escalated["purchase_id"]})
        assert status["next_action"] == "get_payment_credential_and_pay"
        assert_no_card_data(status)

        STATIC_EXTRACTOR.set(escalating)  # the checkout is re-read at release; unchanged
        card = await call(session, "get_payment_credential", {"purchase_id": escalated["purchase_id"]})
        assert card["card_number"] == payments.STUB_TEST_CARD and card["purchase_id"] == escalated["purchase_id"]
        again = await call(session, "get_payment_credential", {"purchase_id": escalated["purchase_id"]})
        assert again["error"] == "credential_already_released"
        assert_no_card_data(again)

    run_with_session(api_app, tmp_path, scenario)

def test_credential_tool_refuses_a_foreign_purchase(api_app: Any, stranger_client: TestClient, tmp_path: Path) -> None:
    """Another user's purchase is invisible to this agent (404), card or not."""
    record = stranger_client.post("/drafts/compile", json={"intent": compiler.DEMO_INTENT}).json()
    contract_id = funded(stranger_client, stranger_client.post(f"/contracts/{record['id']}/sign").json()["id"])
    STATIC_EXTRACTOR.set(_demo_checkout(contract_id))
    purchase_id = stranger_client.post("/purchases", json={"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout"}).json()["purchase_id"]

    async def scenario(session: Any, backend: Any) -> None:
        """Try to take the stranger's card."""
        result = await call(session, "get_payment_credential", {"purchase_id": purchase_id})
        assert result["error"] == "purchase_not_found"
        assert_no_card_data(result)

    run_with_session(api_app, tmp_path, scenario)


def test_backend_unreachable_is_a_clear_error(tmp_path: Path) -> None:
    """If the backend is down, tools say so instead of crashing."""
    def refuse(request: httpx.Request) -> httpx.Response:
        """Nothing is listening."""
        raise httpx.ConnectError("refused")

    backend = HandshakeBackend(transport=httpx.MockTransport(refuse), token_file=str(tmp_path / "t.json"))
    server = build_server(backend=backend)

    async def main() -> dict[str, Any]:
        """One call."""
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            return await call(session, "list_contracts")

    result = asyncio.run(main())
    assert result["error"] == "backend_unreachable"
    assert get_settings().api_url in result["message"]


# ============================================================
# Connecting the agent: OAuth device authorization (the login link)
# ============================================================


def test_agent_without_token_gets_a_login_link_then_works(api_app: Any, client: TestClient, tmp_path: Path) -> None:
    """
    No agent token: the first tool call returns a login link; the USER logs in and approves;
    the next call gets an agent token, caches it privately, and works.
    """
    override_settings(agent_token=None)  # no static token anywhere: the device flow is the only way in

    async def scenario(session: Any, backend: Any) -> None:
        """Call, get the link, approve as the user, call again."""
        first = await call(session, "connect_handshake")
        assert first["error"] == "authorization_required"
        assert first["login_url"].startswith(f"{get_settings().frontend_url}/connect?code=")
        user_code = first["user_code"]
        assert first["login_url"].endswith(user_code)

        # Still waiting: calling again returns the SAME link (no new request each time).
        waiting = await call(session, "list_contracts")
        assert waiting["error"] == "authorization_required" and waiting["user_code"] == user_code

        # The user sees who is asking, and approves.
        described = client.get("/oauth/device", params={"user_code": user_code}).json()
        assert described["client_id"] == get_settings().agent_id and described["status"] == "pending"
        assert "sign contracts" in described["never"]
        assert client.post("/oauth/device/approve", json={"user_code": user_code}).json()["status"] == "approved"

        connected = await call(session, "connect_handshake")
        assert connected["connected"] is True and connected["user"] == "demo@handshake.dev"
        assert connected["agent_id"] == get_settings().agent_id

        token_file = Path(backend.token_file)
        assert token_file.exists()
        assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600

    run_with_session(api_app, tmp_path, scenario)


def test_device_flow_token_is_an_agent_token_not_a_user_token(api_app: Any, client: TestClient, anon_client: TestClient) -> None:
    """The token the agent gets can do agent things, and can't sign, approve, or connect other agents."""
    start = anon_client.post("/oauth/device_authorization", json={"client_id": "agent_demo"}).json()
    client.post("/oauth/device/approve", json={"user_code": start["user_code"]})
    token = anon_client.post("/oauth/token", json={"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": start["device_code"], "client_id": "agent_demo"}).json()["access_token"]
    agent = {"Authorization": f"Bearer {token}"}

    assert anon_client.get("/auth/me", headers=agent).json() == {"email": "demo@handshake.dev", "role": "agent", "agent_id": "agent_demo"}
    draft = client.post("/drafts/compile", json={"intent": compiler.DEMO_INTENT}).json()
    assert anon_client.post(f"/contracts/{draft['id']}/sign", headers=agent).status_code == 403

    # An agent can't approve a connect request (it would connect itself).
    other = anon_client.post("/oauth/device_authorization", json={"client_id": "agent_other"}).json()
    response = anon_client.post("/oauth/device/approve", json={"user_code": other["user_code"]}, headers=agent)
    assert response.status_code == 403 and response.json()["error"] == "agent_not_permitted"


@pytest.mark.parametrize("decision, error", [("deny", "access_denied"), (None, "authorization_pending")])
def test_device_token_endpoint_answers(anon_client: TestClient, client: TestClient, decision: str | None, error: str) -> None:
    """RFC 8628 answers: pending while waiting, access_denied after a deny."""
    start = anon_client.post("/oauth/device_authorization", json={"client_id": "agent_demo"}).json()
    if decision:
        client.post(f"/oauth/device/{decision}", json={"user_code": start["user_code"]})
    body = {"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": start["device_code"], "client_id": "agent_demo"}
    response = anon_client.post("/oauth/token", json=body)
    assert response.status_code == 400
    assert response.json()["error"] == error and response.json()["error_description"]


def test_device_code_works_once_and_only_for_its_client(anon_client: TestClient, client: TestClient) -> None:
    """A device code can't be exchanged by another client id, and can't be exchanged twice."""
    start = anon_client.post("/oauth/device_authorization", json={"client_id": "agent_demo"}).json()
    client.post("/oauth/device/approve", json={"user_code": start["user_code"]})
    grant = "urn:ietf:params:oauth:grant-type:device_code"
    wrong = anon_client.post("/oauth/token", json={"grant_type": grant, "device_code": start["device_code"], "client_id": "agent_evil"})
    assert wrong.json()["error"] == "invalid_grant"
    assert anon_client.post("/oauth/token", json={"grant_type": grant, "device_code": start["device_code"], "client_id": "agent_demo"}).status_code == 200
    assert anon_client.post("/oauth/token", json={"grant_type": grant, "device_code": start["device_code"], "client_id": "agent_demo"}).json()["error"] == "invalid_grant"


def test_oauth_discovery_document(anon_client: TestClient) -> None:
    """The authorization-server metadata points at the device and token endpoints."""
    meta = anon_client.get("/.well-known/oauth-authorization-server").json()
    assert meta["device_authorization_endpoint"].endswith("/oauth/device_authorization")
    assert meta["grant_types_supported"] == ["urn:ietf:params:oauth:grant-type:device_code"]
