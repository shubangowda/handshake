"""
test_oauth.py: going public. Sign in with Google, the OAuth 2.1 authorization
code flow that Muse (or any MCP client) uses to connect an agent, agent keys
and revocation, binding contracts to the agent that proposed them, the
link_optional payment mode, and the hosted multi-user MCP server.

Everything runs in-process: Google's signing key is replaced with a test RSA
key, and the MCP server talks to the real backend app over an ASGI transport.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from handshake import auth, compiler
from handshake.config import get_settings, override_settings
from conftest import TEST_USER, bearer, funded, user_headers

REDIRECT = "https://agent.example/callback"


# ============================================================
# Helpers
# ============================================================


def pkce() -> tuple[str, str]:
    """A PKCE (verifier, S256 challenge) pair."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(anon: TestClient, **extra: Any) -> dict[str, Any]:
    """Register a client the way an MCP client does (dynamic client registration)."""
    response = anon.post("/oauth/register", json={"client_name": "Muse", "redirect_uris": [REDIRECT], **extra})
    assert response.status_code == 201, response.json()
    return response.json()


def authorize(anon: TestClient, client_id: str, challenge: str, **params: str) -> httpx.Response:
    """The browser hits /oauth/authorize; we look at the redirect instead of following it."""
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
             "code_challenge_method": "S256", "state": "st-123", "scope": "handshake.agent", **params}
    return anon.get("/oauth/authorize", params=query, follow_redirects=False)


def approve_and_get_code(anon: TestClient, user: TestClient, client_id: str, challenge: str) -> str:
    """Authorize -> consent page -> the user approves -> the code in the redirect."""
    location = authorize(anon, client_id, challenge).headers["location"]
    assert location.startswith(f"{get_settings().frontend_url}/authorize?request_id=")
    request_id = parse_qs(urlsplit(location).query)["request_id"][0]
    shown = user.get("/oauth/authorize/request", params={"request_id": request_id}).json()
    assert shown["client_name"] == "Muse" and shown["redirect_host"] == "agent.example"
    back = user.post("/oauth/authorize/decision", json={"request_id": request_id, "approve": True}).json()["redirect_to"]
    query = parse_qs(urlsplit(back).query)
    assert back.startswith(REDIRECT) and query["state"] == ["st-123"] and query["iss"] == [get_settings().api_url]
    return query["code"][0]


def exchange(anon: TestClient, client_id: str, code: str, verifier: str, **extra: str) -> httpx.Response:
    """Trade the code for tokens with a FORM body, as OAuth clients do."""
    form = {"grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": REDIRECT, "code_verifier": verifier, **extra}
    return anon.post("/oauth/token", data=form)


def connect_agent(anon: TestClient, user: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """The whole flow; returns (client registration, token response)."""
    client = register(anon)
    verifier, challenge = pkce()
    tokens = exchange(anon, client["client_id"], approve_and_get_code(anon, user, client["client_id"], challenge), verifier)
    assert tokens.status_code == 200, tokens.json()
    assert tokens.headers["cache-control"] == "no-store"
    return client, tokens.json()


# ============================================================
# Sign-in methods
# ============================================================


def test_auth_config_in_dev(anon_client: TestClient) -> None:
    """Dev: demo login on, Google off until configured."""
    assert anon_client.get("/auth/config").json() == {"google_client_id": None, "demo_login": True}


def test_demo_login_is_off_when_disabled(anon_client: TestClient) -> None:
    """With demo login disabled (the prod default), typing an email gets you nothing."""
    override_settings(allow_demo_login=False)
    response = anon_client.post("/auth/demo-login", json={"email": "anyone@example.com"})
    assert response.status_code == 404 and response.json()["error"] == "demo_login_disabled"


def test_prod_defaults_turn_demo_login_off_and_forbid_a_static_agent_token() -> None:
    """In prod, demo login is off unless allowed, a static agent token is refused, and some login must exist."""
    from dataclasses import replace

    from handshake.config import ConfigError, validate

    prod = replace(get_settings(), env="prod", signing_secret="s1", session_secret="s2", agent_token=None,
                   card_encryption_key=base64.b64encode(b"k" * 32).decode(), cors_origins=("https://app.example",),
                   google_client_id="id.apps.googleusercontent.com")
    validate(prod)
    assert prod.demo_login_enabled is False
    with pytest.raises(ConfigError, match="HANDSHAKE_AGENT_TOKEN"):
        validate(replace(prod, agent_token="static"))
    with pytest.raises(ConfigError, match="no way to log in"):
        validate(replace(prod, google_client_id=None))


class FakeGoogle:
    """Signs ID tokens with a test RSA key and stands in for Google's published keys."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, client_id: str = "handshake-test.apps.googleusercontent.com") -> None:
        """Install the fake key and configure the client id."""
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.client_id = client_id
        monkeypatch.setattr(auth, "_google_signing_key", lambda token: self.key.public_key())
        override_settings(google_client_id=client_id)

    def token(self, **claims: Any) -> str:
        """A signed ID token; claims override the defaults."""
        now = int(time.time())
        body = {"iss": "https://accounts.google.com", "aud": self.client_id, "sub": "1234", "email": "Shopper@Example.com",
                "email_verified": True, "iat": now, "exp": now + 600, **claims}
        return jwt.encode(body, self.key, algorithm="RS256")


def test_google_sign_in(anon_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid Google ID token becomes a Handshake user token for the verified email."""
    google = FakeGoogle(monkeypatch)
    response = anon_client.post("/auth/google", json={"credential": google.token()})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["email"] == "shopper@example.com" and body["demo_auth"] is False
    assert anon_client.get("/auth/me", headers=bearer(body["token"])).json()["role"] == "user"
    assert anon_client.get("/auth/config").json()["google_client_id"] == google.client_id


@pytest.mark.parametrize("claims", [
    {"aud": "someone-elses-app.apps.googleusercontent.com"},  # issued for another app
    {"iss": "https://evil.example"},  # not Google
    {"email_verified": False},  # Google didn't verify the address
    {"exp": int(time.time()) - 3600},  # expired
])
def test_google_sign_in_refuses_bad_tokens(anon_client: TestClient, monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any]) -> None:
    """Wrong audience, issuer, unverified email, or expired: 401, never a login."""
    google = FakeGoogle(monkeypatch)
    response = anon_client.post("/auth/google", json={"credential": google.token(**claims)})
    assert response.status_code == 401 and response.json()["error"] == "google_token_invalid"


def test_google_sign_in_refuses_a_token_signed_by_another_key(anon_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token signed with any key but Google's is refused."""
    google = FakeGoogle(monkeypatch)
    forged = jwt.encode({"iss": "https://accounts.google.com", "aud": google.client_id, "email": "victim@example.com",
                         "email_verified": True, "iat": int(time.time()), "exp": int(time.time()) + 600},
                        rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256")
    assert anon_client.post("/auth/google", json={"credential": forged}).status_code == 401


def test_google_sign_in_off_when_not_configured(anon_client: TestClient) -> None:
    """No client id configured: 404."""
    assert anon_client.post("/auth/google", json={"credential": "x" * 40}).json()["error"] == "google_login_disabled"


# ============================================================
# OAuth 2.1 authorization code flow
# ============================================================


def test_full_oauth_flow_gives_the_agent_its_own_revocable_token(anon_client: TestClient, client: TestClient) -> None:
    """Register -> authorize -> user approves -> code + PKCE -> agent token acting for that user, as that client."""
    registered, tokens = connect_agent(anon_client, client)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == auth.ACCESS_TOKEN_SECONDS
    me = anon_client.get("/auth/me", headers=bearer(tokens["access_token"])).json()
    assert me == {"email": TEST_USER, "role": "agent", "agent_id": registered["client_id"]}

    agents = client.get("/agents").json()["agents"]
    assert [(a["kind"], a["client_name"]) for a in agents] == [("oauth", "Muse")]
    client.post(f"/agents/{agents[0]['agent_id']}/revoke")
    revoked = anon_client.get("/auth/me", headers=bearer(tokens["access_token"]))
    assert revoked.status_code == 401 and revoked.json()["error"] == "agent_revoked"


def test_refresh_tokens_rotate_and_a_replay_disconnects_the_agent(anon_client: TestClient, client: TestClient) -> None:
    """A refresh token works once; presenting it again (theft) revokes the whole grant."""
    registered, tokens = connect_agent(anon_client, client)
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": registered["client_id"]}
    fresh = anon_client.post("/oauth/token", data=form).json()
    assert fresh["refresh_token"] != tokens["refresh_token"]
    assert anon_client.get("/auth/me", headers=bearer(fresh["access_token"])).status_code == 200

    replay = anon_client.post("/oauth/token", data=form)
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert anon_client.get("/auth/me", headers=bearer(fresh["access_token"])).status_code == 401


def test_codes_need_the_right_verifier_and_work_once(anon_client: TestClient, client: TestClient) -> None:
    """Wrong PKCE verifier, wrong redirect_uri, or a second use: invalid_grant."""
    registered = register(anon_client)
    verifier, challenge = pkce()
    code = approve_and_get_code(anon_client, client, registered["client_id"], challenge)
    assert exchange(anon_client, registered["client_id"], code, "x" * 50).json()["error"] == "invalid_grant"
    assert exchange(anon_client, registered["client_id"], code, verifier, redirect_uri="https://agent.example/other").json()["error"] == "invalid_grant"

    code = approve_and_get_code(anon_client, client, registered["client_id"], challenge)
    assert exchange(anon_client, registered["client_id"], code, verifier).status_code == 200
    assert exchange(anon_client, registered["client_id"], code, verifier).json()["error"] == "invalid_grant"


def test_authorize_never_redirects_to_an_unregistered_uri(anon_client: TestClient) -> None:
    """An unknown redirect_uri gets a plain 400, not a redirect (no open redirector)."""
    registered = register(anon_client)
    response = authorize(anon_client, registered["client_id"], pkce()[1], redirect_uri="https://attacker.example/cb")
    assert response.status_code == 400 and "location" not in response.headers


def test_authorize_requires_pkce(anon_client: TestClient) -> None:
    """No S256 challenge: the client is sent back with invalid_request."""
    registered = register(anon_client)
    response = authorize(anon_client, registered["client_id"], "", code_challenge_method="plain")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert response.status_code == 302 and query["error"] == ["invalid_request"] and query["state"] == ["st-123"]


def test_user_can_deny(anon_client: TestClient, client: TestClient) -> None:
    """Deny sends the client access_denied, and the request can't be approved afterwards."""
    registered = register(anon_client)
    location = authorize(anon_client, registered["client_id"], pkce()[1]).headers["location"]
    request_id = parse_qs(urlsplit(location).query)["request_id"][0]
    back = client.post("/oauth/authorize/decision", json={"request_id": request_id, "approve": False}).json()["redirect_to"]
    assert parse_qs(urlsplit(back).query)["error"] == ["access_denied"]
    assert client.post("/oauth/authorize/decision", json={"request_id": request_id, "approve": True}).status_code == 410


def test_an_agent_cannot_approve_its_own_connection(anon_client: TestClient, agent_client: TestClient) -> None:
    """Consent is user-only: an agent token gets 403 on the consent endpoints and on agent keys."""
    registered = register(anon_client)
    request_id = parse_qs(urlsplit(authorize(anon_client, registered["client_id"], pkce()[1]).headers["location"]).query)["request_id"][0]
    assert agent_client.post("/oauth/authorize/decision", json={"request_id": request_id, "approve": True}).status_code == 403
    assert agent_client.post("/agents/keys", json={"name": "sneaky"}).status_code == 403
    assert agent_client.get("/agents").status_code == 403


@pytest.mark.parametrize("uri", ["javascript:alert(1)", "http://agent.example/cb", "https://agent.example/cb#frag", "data:text/html,x"])
def test_registration_refuses_unsafe_redirect_uris(anon_client: TestClient, uri: str) -> None:
    """Script schemes, plain http to other hosts, and fragments are refused."""
    response = anon_client.post("/oauth/register", json={"client_name": "x", "redirect_uris": [uri]})
    assert response.status_code == 400 and response.json()["error"] == "invalid_redirect_uri"


def test_confidential_clients_must_present_their_secret(anon_client: TestClient, client: TestClient) -> None:
    """A client that asked for a secret gets one, and the token endpoint then demands it (Basic auth works)."""
    registered = register(anon_client, token_endpoint_auth_method="client_secret_basic")
    assert registered["client_secret"].startswith("hss_")
    verifier, challenge = pkce()
    code = approve_and_get_code(anon_client, client, registered["client_id"], challenge)
    assert exchange(anon_client, registered["client_id"], code, verifier).status_code == 401
    basic = base64.b64encode(f"{registered['client_id']}:{registered['client_secret']}".encode()).decode()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}
    assert anon_client.post("/oauth/token", data=form, headers={"Authorization": f"Basic {basic}"}).status_code == 200


# ============================================================
# Agent keys, and binding contracts to the proposing agent
# ============================================================


def test_agent_keys_are_shown_once_and_revocable(anon_client: TestClient, client: TestClient) -> None:
    """A key acts as its own agent for the user; after revoking, it's dead."""
    key = client.post("/agents/keys", json={"name": "Muse custom connector"}).json()
    me = anon_client.get("/auth/me", headers=bearer(key["token"])).json()
    assert me["role"] == "agent" and me["agent_id"].startswith("key_") and me["email"] == TEST_USER
    listed = client.get("/agents").json()["agents"]
    assert listed[0]["name"] == "Muse custom connector" and "token" not in listed[0]
    client.post(f"/agents/{key['agent_id']}/revoke")
    assert anon_client.get("/auth/me", headers=bearer(key["token"])).status_code == 401


def test_other_users_cannot_revoke_my_agents(client: TestClient, stranger_client: TestClient) -> None:
    """Revoking someone else's agent is a 404 (ids can't be probed)."""
    key = client.post("/agents/keys", json={"name": "mine"}).json()
    assert stranger_client.post(f"/agents/{key['agent_id']}/revoke").status_code == 404


def test_contract_binds_to_the_agent_that_proposed_it(anon_client: TestClient, client: TestClient, agent_client: TestClient) -> None:
    """Muse drafts, the user signs: the contract is Muse's, and another agent of the same user is refused."""
    registered, tokens = connect_agent(anon_client, client)
    muse = bearer(tokens["access_token"])
    draft = anon_client.post("/drafts/compile", json={"intent": compiler.DEMO_INTENT}, headers=muse).json()
    assert draft["proposed_by_agent"] == registered["client_id"]
    contract_id = funded(client, client.post(f"/contracts/{draft['id']}/sign").json()["id"])
    assert client.get(f"/contracts/{contract_id}").json()["contract"]["agent_key"] == registered["client_id"]

    from conftest import STATIC_EXTRACTOR, proposal_payload

    STATIC_EXTRACTOR.set(proposal_payload(contract_id))  # the checkout Handshake will read
    body = {"contract_id": contract_id, "checkout_url": "https://mocknike.example/checkout", "idempotency_key": "bind-test-0001"}
    refused = agent_client.post("/purchases", json=body)  # the static demo agent, same user
    assert refused.status_code == 403 and refused.json()["error"] == "agent_not_authorized"
    allowed = anon_client.post("/purchases", json={**body, "idempotency_key": "bind-test-0002"}, headers=muse)
    # The bound agent gets past the binding check and Handshake evaluates the checkout (this test
    # checkout is a different store than the contract's, so the rules then block it on the merits).
    assert allowed.status_code == 200 and allowed.json()["decision"]["results"]


# ============================================================
# link_optional payments
# ============================================================


def test_link_optional_funds_with_the_simulated_provider_until_link_is_connected(api_app: Any, test_db: Any) -> None:
    """No Link account: signing works, the funding is simulated, and simulated approval is available."""
    from handshake.api import create_app

    override_settings(payment_mode="link_optional")
    app = create_app()
    with TestClient(app, headers=user_headers()) as user:
        health = user.get("/health").json()
        assert health["payment_mode"] == "link_optional" and "Simulated" in health["payment_label"]
        assert user.get("/link/status").json()["connected"] is False  # Link is optional, not assumed
        record = user.post("/drafts/compile", json={"intent": compiler.DEMO_INTENT}).json()
        contract_id = user.post(f"/contracts/{record['id']}/sign").json()["id"]
        funding = user.get(f"/contracts/{contract_id}/funding").json()
        assert funding["provider"] == "stub" and funding["state"] == "awaiting_approval"
        assert funded(user, contract_id)


# ============================================================
# The hosted, multi-user MCP server
# ============================================================


def hosted_app(api_app: Any) -> Any:
    """The hosted MCP ASGI app, talking to the in-process backend."""
    from handshake.mcp_server import build_server

    override_settings(mcp_public_url="https://mcp.handshake.test")
    server = build_server(hosted=True, transport=httpx.ASGITransport(app=api_app))
    return server.streamable_http_app()


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Host": "mcp.handshake.test"}


def test_hosted_mcp_requires_a_token_and_advertises_how_to_get_one(api_app: Any) -> None:
    """No token: 401 pointing at the protected-resource metadata, which names the backend as the authorization server."""
    with TestClient(hosted_app(api_app), base_url="https://mcp.handshake.test") as mcp:
        response = mcp.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        meta = mcp.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == "https://mcp.handshake.test/mcp"
        assert [s.rstrip("/") for s in meta["authorization_servers"]] == [get_settings().api_url]
        assert mcp.get("/.well-known/oauth-protected-resource").json()["authorization_servers"] == [get_settings().api_url]
        assert mcp.get("/.well-known/oauth-authorization-server").json()["registration_endpoint"].endswith("/oauth/register")
        assert mcp.get("/health").json()["status"] == "ok"


def test_hosted_mcp_refuses_user_tokens_and_accepts_agent_tokens(api_app: Any, client: TestClient) -> None:
    """A user's own login token is not an agent token; an agent key is."""
    key = client.post("/agents/keys", json={"name": "Muse"}).json()["token"]
    with TestClient(hosted_app(api_app), base_url="https://mcp.handshake.test") as mcp:
        as_user = mcp.post("/mcp", json=INIT, headers={**MCP_HEADERS, **user_headers()})
        assert as_user.status_code == 401
        as_agent = mcp.post("/mcp", json=INIT, headers={**MCP_HEADERS, **bearer(key)})
        assert as_agent.status_code == 200, as_agent.text


def test_hosted_mcp_tools_act_as_the_calling_user(api_app: Any, client: TestClient, stranger_client: TestClient) -> None:
    """Two users' agents on the same server each see only their own account."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    from handshake.mcp_server import HandshakeBackend

    override_settings(mcp_public_url="https://mcp.handshake.test")
    backend = HandshakeBackend(transport=httpx.ASGITransport(app=api_app), per_request=True)
    mine = client.post("/agents/keys", json={"name": "a"}).json()["token"]
    theirs = stranger_client.post("/agents/keys", json={"name": "b"}).json()["token"]

    async def whoami(token: str) -> str:
        """Call the backend as whichever user's token is in this request's context."""
        auth_context_var.set(AuthenticatedUser(AccessToken(token=token, client_id="x", scopes=["handshake.agent"])))
        return (await backend.call("GET", "/auth/me"))["email"]

    assert asyncio.run(whoami(mine)) == TEST_USER
    assert asyncio.run(whoami(theirs)) != TEST_USER
