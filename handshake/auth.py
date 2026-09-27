"""
auth.py: who is calling, and what they are allowed to do.

Job in the system
-----------------
Handshake's whole point is "the agent proposes, the USER decides". Before
this file existed, anyone who could reach the API could sign a contract or
approve an escalated purchase, including the shopping agent itself. This
file closes that hole with two roles:

    user   a person, logged in with a demo token from POST /auth/demo-login
    agent  the shopping agent, holding the static HANDSHAKE_AGENT_TOKEN

    api.py route --Depends(current_principal)--> Principal(role, email, agent_id)
                 --require_user(...)-----------> 403 for agents on user-only actions

How the agent gets a token (OAuth 2.0 device authorization, RFC 8628 style)
-------------------------------------------------------------------------
The shopping agent never sees the user's password or login token. When the
MCP server has no agent token, it asks the backend to start a "connect"
request and gets back a short user code and a login link:

    MCP  -> POST /oauth/device_authorization         -> {device_code, user_code, verification_uri_complete}
    agent tells the user: "Open <link> and approve Handshake for this agent"
    user -> frontend /connect?code=KDQM-TXBW -> logs in -> approves   (POST /oauth/device/approve, USER ONLY)
    MCP  -> POST /oauth/token {device_code}          -> {access_token}  (an agent token bound to that user)

Approving is a USER action: an agent token calling /oauth/device/approve gets
403, so an agent can't connect itself. The device_code is a secret held by the
MCP server (only its hash is stored); the issued token works once per
device_code and expires after HANDSHAKE_AGENT_TOKEN_TTL_DAYS. The static
HANDSHAKE_AGENT_TOKEN still works for scripts and tests.

Every record (draft, contract, purchase) has an owner email. services.py
checks it on every read and write, and a record you don't own is reported as
404 "not found" (not 403), so ids can't be probed.

DEMO AUTH, NOT PRODUCTION AUTH
------------------------------
demo-login hands a token to anyone who types an email address. That is fine
for a hackathon demo on a developer's own machine and nothing more. It must be replaced by
passkeys or OAuth before real users (see README, "Future work").

Token format
------------
    base64url(JSON payload) + "." + base64url(HMAC-SHA256(payload, SESSION_SECRET))

The payload is {"sub": email, "role": "user", "iat": ..., "exp": ...}. The
HMAC means the server can tell whether it issued the token: change one byte
of the payload (say, the email) and the signature no longer matches. The
key is HANDSHAKE_SESSION_SECRET, deliberately different from the contract
signing secret (config.py enforces that).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Header

from handshake.config import LOOPBACK_HOSTS, get_settings

USER = "user"
AGENT = "agent"

# A deliberately loose email check: enough to reject garbage, not an RFC parser.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of one request."""

    role: str  # USER or AGENT
    email: str  # the user this caller acts for (the owner of their records)
    agent_id: str | None = None  # set only for the agent role

    @property
    def is_agent(self) -> bool:
        """True for the shopping agent."""
        return self.role == AGENT

    @property
    def label(self) -> str:
        """Short description for evidence events, e.g. 'agent agent_demo' or 'user a@b.c'."""
        return f"agent {self.agent_id}" if self.is_agent else f"user {self.email}"


class AuthError(Exception):
    """Authentication or permission failure; api.py renders it in the standard error shape."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        """Keep the HTTP status, machine code, and human message."""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# ============================================================
# User tokens (demo login)
# ============================================================


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 without '=' padding (tokens travel in headers and URLs)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """Inverse of _b64encode (re-adds the padding)."""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_part: str) -> str:
    """HMAC-SHA256 of the encoded payload, keyed with the SESSION secret."""
    key = get_settings().session_secret.encode("utf-8")
    return _b64encode(hmac.new(key, payload_part.encode("ascii"), hashlib.sha256).digest())


def normalize_email(email: str) -> str:
    """Trim and lowercase an email; raise AuthError if it doesn't look like one."""
    cleaned = (email or "").strip().lower()
    if not EMAIL_PATTERN.match(cleaned) or len(cleaned) > 254:
        raise AuthError(422, "invalid_email", "Enter a valid email address.")
    return cleaned


def issue_user_token(email: str, now: float | None = None) -> tuple[str, int]:
    """Create a signed demo login token for `email`. Returns (token, expires_at_unix_seconds)."""
    issued = int(now if now is not None else time.time())
    expires = issued + get_settings().session_ttl_minutes * 60
    payload = {"sub": normalize_email(email), "role": USER, "iat": issued, "exp": expires}
    payload_part = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{payload_part}.{_sign(payload_part)}", expires


def issue_agent_token(
    email: str, agent_id: str, now: float | None = None, grant_id: str | None = None, ttl_seconds: int | None = None
) -> tuple[str, int]:
    """
    A signed AGENT token for one user and one agent id (what the device flow hands the MCP server).

    Same format and key as user tokens, but role "agent": it can only do what
    an agent may do (never sign, approve, reject, revoke, or edit). With a
    grant_id ("gid"), the token dies as soon as the user revokes that grant.
    """
    issued = int(now if now is not None else time.time())
    expires = issued + (ttl_seconds if ttl_seconds is not None else get_settings().agent_token_ttl_days * 24 * 3600)
    payload = {"sub": normalize_email(email), "role": AGENT, "agent_id": agent_id, "iat": issued, "exp": expires}
    if grant_id:
        payload["gid"] = grant_id
    payload_part = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{payload_part}.{_sign(payload_part)}", expires


def verify_user_token(token: str, now: float | None = None) -> Principal:
    """Check a signed token's signature and expiry; return its user or agent Principal, or raise 401."""
    try:
        payload_part, signature = token.split(".", 1)
    except ValueError:
        raise AuthError(401, "invalid_token", "The login token is malformed. Log in again.")

    # compare_digest: constant-time comparison, so response timing can't be used
    # to guess a valid signature one character at a time.
    if not hmac.compare_digest(_sign(payload_part), signature):
        raise AuthError(401, "invalid_token", "The login token is not valid. Log in again.")

    try:
        payload = json.loads(_b64decode(payload_part))
    except (ValueError, UnicodeDecodeError):
        raise AuthError(401, "invalid_token", "The login token is malformed. Log in again.")

    current = now if now is not None else time.time()
    if not isinstance(payload.get("exp"), int) or payload["exp"] <= current:
        raise AuthError(401, "token_expired", "Your session has expired. Log in again.")
    if not isinstance(payload.get("sub"), str):
        raise AuthError(401, "invalid_token", "The login token is not valid. Log in again.")
    if payload.get("role") == AGENT and isinstance(payload.get("agent_id"), str):
        if payload.get("gid") is not None and not grant_is_active(str(payload["gid"])):
            raise AuthError(401, "agent_revoked", "This agent's access was revoked or has expired. Connect it again.")
        return Principal(role=AGENT, email=payload["sub"], agent_id=payload["agent_id"])
    if payload.get("role") != USER:
        raise AuthError(401, "invalid_token", "The login token is not valid. Log in again.")
    return Principal(role=USER, email=payload["sub"])


# ============================================================
# Resolving the caller of a request
# ============================================================


def principal_from_authorization(authorization: str | None) -> Principal:
    """Turn an 'Authorization: Bearer ...' header into a Principal, or raise 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(401, "not_authenticated", "Send 'Authorization: Bearer <token>'. Log in first.")
    token = authorization[7:].strip()
    settings = get_settings()

    # The agent token is a static secret. Compare in constant time, and only if
    # one is configured (an unset agent token means "no agent may connect").
    if settings.agent_token and hmac.compare_digest(token.encode("utf-8"), settings.agent_token.encode("utf-8")):
        return Principal(role=AGENT, email=settings.agent_owner, agent_id=settings.agent_id)

    return verify_user_token(token)


def current_principal(authorization: str | None = Header(default=None)) -> Principal:
    """FastAPI dependency: the authenticated caller. Every route except /health and demo-login uses it."""
    return principal_from_authorization(authorization)


# ============================================================
# Device authorization (connecting an agent through the user's login)
# ============================================================

# User codes avoid look-alike characters (0/O, 1/I/L) so they read aloud cleanly.
USER_CODE_ALPHABET = "BCDFGHJKMNPQRSTVWXZ"
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")
POLL_INTERVAL_SECONDS = 3


def _hash_code(device_code: str) -> str:
    """Only the SHA-256 of a device_code is stored, so a database leak can't be used to mint tokens."""
    return hashlib.sha256(device_code.encode("utf-8")).hexdigest()


def _new_user_code() -> str:
    """8 unambiguous letters shown as XXXX-XXXX."""
    import secrets

    letters = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return f"{letters[:4]}-{letters[4:]}"


def _aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; they are stored in UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def start_device_authorization(session: Any, client_id: str, client_name: str | None) -> dict[str, Any]:
    """Begin connecting an agent: returns the device_code (for the agent) and the login link (for the user)."""
    import secrets

    from handshake import db

    if not CLIENT_ID_PATTERN.match(client_id or ""):
        raise AuthError(400, "invalid_client", "client_id must be 3-64 letters, digits, '_' or '-'.")
    settings = get_settings()
    now = datetime.now(timezone.utc)
    device_code = secrets.token_urlsafe(32)
    user_code = _new_user_code()
    session.add(
        db.DeviceAuthorizationRow(
            id="devauth_" + secrets.token_hex(8), device_code_hash=_hash_code(device_code), user_code=user_code,
            client_id=client_id, client_name=(client_name or client_id)[:80], status="pending",
            created_at=now, expires_at=now + timedelta(minutes=settings.device_code_ttl_minutes),
        )
    )
    session.commit()
    verification_uri = f"{settings.frontend_url}/connect"
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": f"{verification_uri}?code={user_code}",
        "expires_in": settings.device_code_ttl_minutes * 60,
        "interval": POLL_INTERVAL_SECONDS,
    }


def _row_by_user_code(session: Any, user_code: str) -> Any:
    """The pending-or-decided request for a user code, or 404."""
    from sqlalchemy import select

    from handshake import db

    code = (user_code or "").strip().upper().replace(" ", "")
    if len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    row = session.scalar(select(db.DeviceAuthorizationRow).where(db.DeviceAuthorizationRow.user_code == code))
    if row is None or _aware(row.expires_at) <= datetime.now(timezone.utc):
        raise AuthError(404, "unknown_code", "That code is unknown or has expired. Ask the agent for a new link.")
    return row


def describe_device_authorization(session: Any, user_code: str) -> dict[str, Any]:
    """What the approval screen shows: which agent is asking, and whether it's still pending."""
    row = _row_by_user_code(session, user_code)
    return {
        "user_code": row.user_code,
        "client_id": row.client_id,
        "client_name": row.client_name,
        "status": row.status,
        "expires_at": _aware(row.expires_at).isoformat(),
        "permissions": [
            "compile draft contracts for you to review",
            "read your contracts, purchases, and evidence",
            "request purchases under contracts you sign for this agent",
            "collect a single-use test card for a purchase you approved",
        ],
        "never": ["sign contracts", "approve or reject purchases", "edit or revoke contracts", "approve payments"],
    }


def decide_device_authorization(session: Any, principal: Principal, user_code: str, approve: bool) -> dict[str, Any]:
    """The USER approves or denies connecting the agent. An agent calling this gets 403."""
    if principal.is_agent:
        raise AuthError(403, "agent_not_permitted", "Only the user can connect an agent to their account.")
    row = _row_by_user_code(session, user_code)
    if row.status != "pending":
        raise AuthError(409, "already_decided", f"This request was already {row.status}.")
    row.status = "approved" if approve else "denied"
    row.owner = principal.email if approve else None
    row.decided_at = datetime.now(timezone.utc)
    session.commit()
    return {"user_code": row.user_code, "status": row.status, "client_id": row.client_id}


def exchange_device_code(session: Any, device_code: str, client_id: str) -> dict[str, Any]:
    """
    The agent polls with its device_code. RFC 8628 answers:
      authorization_pending (keep polling), slow_down, access_denied, expired_token,
      or the access token (once; the device_code is then consumed).
    """
    from sqlalchemy import select

    from handshake import db

    row = session.scalar(select(db.DeviceAuthorizationRow).where(db.DeviceAuthorizationRow.device_code_hash == _hash_code(device_code or "")))
    if row is None or row.client_id != client_id:
        raise AuthError(400, "invalid_grant", "Unknown device code.")
    now = datetime.now(timezone.utc)
    if _aware(row.expires_at) <= now and row.status in ("pending", "approved"):
        raise AuthError(400, "expired_token", "The connect request expired. Start again.")
    if row.status == "denied":
        raise AuthError(400, "access_denied", "The user declined to connect this agent.")
    if row.status == "consumed":
        raise AuthError(400, "invalid_grant", "This device code was already used.")
    if row.status == "pending":
        too_fast = row.last_polled_at is not None and (now - _aware(row.last_polled_at)).total_seconds() < POLL_INTERVAL_SECONDS - 1
        row.last_polled_at = now
        session.commit()
        raise AuthError(400, "slow_down" if too_fast else "authorization_pending", "Waiting for the user to approve.")

    # Approved: issue the agent token exactly once, under a revocable grant.
    row.status = "consumed"
    ttl = get_settings().agent_token_ttl_days * 24 * 3600
    grant = create_grant(session, row.owner, agent_id=row.client_id, kind="device", name=row.client_name, client_id=row.client_id, ttl_seconds=ttl)
    session.commit()
    token, expires = issue_agent_token(row.owner, row.client_id, grant_id=grant.id, ttl_seconds=ttl)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": int(expires - time.time()),
        "scope": "handshake.agent",
        "agent_id": row.client_id,
        "email": row.owner,
    }


# ============================================================
# Agent grants: every connected agent, and revoking it
# ============================================================
#
# An agent token is self-contained (signed, with an expiry), which is why it
# is fast to check, and also why, on its own, it can't be taken back. Each
# token issued through OAuth, the device flow, or an agent key therefore
# names its grant ("gid"), and a token whose grant is revoked or expired is
# refused on the very next request.

GRANT_TOUCH_SECONDS = 300  # update last_used_at at most every 5 minutes (not on every request)
AGENT_SCOPE = "handshake.agent"


def create_grant(
    session: Any, owner: str, agent_id: str, kind: str, name: str, client_id: str | None, ttl_seconds: int | None
) -> Any:
    """Record a newly connected agent. The caller commits."""
    import secrets

    from handshake import db

    now = datetime.now(timezone.utc)
    grant = db.AgentGrantRow(
        id="agt_" + secrets.token_hex(8), owner=normalize_email(owner), agent_id=agent_id, kind=kind,
        name=(name or agent_id)[:80], client_id=client_id, created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
    )
    session.add(grant)
    return grant


def grant_is_active(grant_id: str) -> bool:
    """True if the grant exists, isn't revoked, and hasn't expired. Also notes when it was last used."""
    from handshake import db

    if db.SessionLocal is None:
        db.configure()
    with db.SessionLocal() as session:
        grant = session.get(db.AgentGrantRow, grant_id)
        now = datetime.now(timezone.utc)
        if grant is None or grant.revoked_at is not None:
            return False
        if grant.expires_at is not None and _aware(grant.expires_at) <= now:
            return False
        if grant.last_used_at is None or (now - _aware(grant.last_used_at)).total_seconds() > GRANT_TOUCH_SECONDS:
            grant.last_used_at = now
            session.commit()
        return True


def _grant_view(grant: Any) -> dict[str, Any]:
    """A grant as the Agents page shows it. Never a token."""
    from handshake import db

    iso = lambda value: _aware(value).isoformat() if value else None  # noqa: E731
    client_name = None
    if grant.kind == "oauth" and grant.client_id:
        from sqlalchemy.orm import object_session

        client = object_session(grant).get(db.OAuthClientRow, grant.client_id)
        client_name = client.client_name if client else None
    return {
        "agent_id": grant.id, "name": grant.name, "kind": grant.kind, "client_name": client_name,
        "created_at": iso(grant.created_at), "last_used_at": iso(grant.last_used_at),
        "expires_at": iso(grant.expires_at), "revoked_at": iso(grant.revoked_at),
    }


def list_grants(session: Any, principal: Principal) -> dict[str, Any]:
    """The user's connected agents, active first, newest first."""
    from sqlalchemy import select

    from handshake import db

    _require_user(principal, "list connected agents")
    rows = session.scalars(select(db.AgentGrantRow).where(db.AgentGrantRow.owner == principal.email)).all()
    rows = sorted(rows, key=lambda g: (g.revoked_at is not None, -_aware(g.created_at).timestamp()))
    return {"agents": [_grant_view(g) for g in rows]}


def create_agent_key(session: Any, principal: Principal, name: str) -> dict[str, Any]:
    """
    The user makes a long-lived key for an agent that can't do OAuth (e.g. a
    Muse custom connector pasted through its secure credential prompt). The
    token is returned ONCE and never stored; only its grant is.
    """
    import secrets

    _require_user(principal, "create agent keys")
    label = (name or "").strip()
    if not 1 <= len(label) <= 80:
        raise AuthError(422, "invalid_name", "Give the key a name of 1 to 80 characters (e.g. 'Muse').")
    ttl = get_settings().agent_key_ttl_days * 24 * 3600
    # Each key is its own agent, so contracts bind to exactly this key.
    grant = create_grant(session, principal.email, agent_id="key_" + secrets.token_hex(6), kind="key", name=label, client_id=None, ttl_seconds=ttl)
    session.commit()
    token, expires = issue_agent_token(principal.email, grant.agent_id, grant_id=grant.id, ttl_seconds=ttl)
    return {"agent_id": grant.id, "name": grant.name, "token": token, "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat()}


def revoke_grant(session: Any, principal: Principal, grant_id: str) -> dict[str, Any]:
    """The user disconnects an agent: its tokens (and refresh tokens) stop working immediately."""
    from handshake import db

    _require_user(principal, "revoke agents")
    grant = session.get(db.AgentGrantRow, grant_id)
    if grant is None or grant.owner != principal.email:
        raise AuthError(404, "agent_not_found", "No connected agent with that id.")
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(timezone.utc)
        session.commit()
    return _grant_view(grant)


def _require_user(principal: Principal, action: str) -> None:
    """Agents can't manage agents (or they could mint themselves new keys)."""
    if principal.is_agent:
        raise AuthError(403, "agent_not_permitted", f"Only the user can {action}.")


# ============================================================
# Sign in with Google
# ============================================================
#
# The frontend uses Google Identity Services, which hands it a signed ID token
# (a JWT). We check that Google signed it (Google's published keys), that it
# was issued for OUR client id (aud), by Google (iss), that it hasn't expired,
# and that Google verified the email address. Only then does the email become
# a Handshake user. We never see or store a Google password or access token.

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_google_jwks: Any = None


def _google_signing_key(id_token: str) -> Any:
    """Google's public key for this token (cached by PyJWT). Tests replace this function."""
    global _google_jwks
    import jwt

    if _google_jwks is None:
        _google_jwks = jwt.PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600)
    return _google_jwks.get_signing_key_from_jwt(id_token).key


def verify_google_credential(id_token: str) -> str:
    """The verified email in a Google ID token, or AuthError 401."""
    import jwt

    client_id = get_settings().google_client_id
    if not client_id:
        raise AuthError(404, "google_login_disabled", "Sign in with Google isn't configured on this server.")
    try:
        claims = jwt.decode(
            id_token, _google_signing_key(id_token), algorithms=["RS256"], audience=client_id,
            options={"require": ["exp", "iat", "iss", "aud", "email"]}, leeway=30,
        )
    except Exception:  # any decode/signature/network failure is a failed login, never a pass
        raise AuthError(401, "google_token_invalid", "Google sign-in could not be verified. Try again.")
    if claims.get("iss") not in GOOGLE_ISSUERS or claims.get("email_verified") is not True:
        raise AuthError(401, "google_token_invalid", "Google sign-in needs a verified email address.")
    return normalize_email(claims["email"])


# ============================================================
# OAuth 2.1 authorization code flow (how Muse and other MCP clients connect)
# ============================================================
#
#   client --register (RFC 7591)--> POST /oauth/register            -> client_id
#   client sends the user's browser to GET /oauth/authorize?client_id&redirect_uri&code_challenge(S256)&state&resource
#   backend checks the client + redirect_uri, stores a pending request, and redirects to the FRONTEND /authorize page
#   user logs in (Google) and approves  -> POST /oauth/authorize/decision (USER ONLY) -> redirect_uri?code&state&iss
#   client --code + code_verifier--> POST /oauth/token -> access token (1 h) + refresh token (rotating)
#
# Fail-closed choices: PKCE S256 is mandatory; redirect URIs are matched
# exactly; codes and refresh tokens are stored only as hashes and work once;
# reusing a refresh token revokes the whole grant (it was probably stolen).

ACCESS_TOKEN_SECONDS = 3600
CUSTOM_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*$")
FORBIDDEN_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob"}


class OAuthError(AuthError):
    """An OAuth protocol error (RFC 6749 section 5.2): `code` is the OAuth "error" value."""


def _valid_redirect_uri(uri: str) -> bool:
    """https anywhere; http only to this machine (native apps); private-use schemes like cursor://; nothing else."""
    from urllib.parse import urlsplit

    if not isinstance(uri, str) or len(uri) > 2000 or "#" in uri:
        return False
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    if scheme == "https":
        return bool(parts.netloc)
    if scheme == "http":
        return (parts.hostname or "").lower() in LOOPBACK_HOSTS
    return bool(CUSTOM_SCHEME.match(scheme)) and scheme not in FORBIDDEN_SCHEMES


CLIENT_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")


def register_client(session: Any, body: dict[str, Any]) -> dict[str, Any]:
    """
    Dynamic client registration. Public clients ("none") are the norm for MCP;
    a client that asks for a secret gets one (hash stored), since refusing it
    would just break that client. PKCE is required for everyone regardless.
    """
    import secrets

    from handshake import db

    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= 10 or not all(_valid_redirect_uri(u) for u in uris):
        raise OAuthError(400, "invalid_redirect_uri", "redirect_uris must be 1-10 https URLs (plain http only for this machine's loopback address).")
    method = body.get("token_endpoint_auth_method") or "none"
    if method not in CLIENT_AUTH_METHODS:
        raise OAuthError(400, "invalid_client_metadata", f"token_endpoint_auth_method must be one of {', '.join(CLIENT_AUTH_METHODS)}.")
    for grant in body.get("grant_types") or ["authorization_code"]:
        if grant not in ("authorization_code", "refresh_token"):
            raise OAuthError(400, "invalid_client_metadata", f"Unsupported grant type {grant!r}.")
    name = str(body.get("client_name") or "An AI agent").strip()[:80] or "An AI agent"
    now = datetime.now(timezone.utc)
    secret = "hss_" + secrets.token_urlsafe(32) if method != "none" else None
    client = db.OAuthClientRow(
        client_id="hsc_" + secrets.token_urlsafe(16), client_name=name, redirect_uris=list(uris), auth_method=method,
        client_secret_hash=_hash_code(secret) if secret else None, created_at=now,
    )
    session.add(client)
    session.commit()
    registered = {
        "client_id": client.client_id, "client_name": name, "redirect_uris": list(uris),
        "token_endpoint_auth_method": method, "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": AGENT_SCOPE, "client_id_issued_at": int(now.timestamp()),
    }
    if secret:
        registered.update(client_secret=secret, client_secret_expires_at=0)
    return registered


def authenticate_client(session: Any, client_id: str, client_secret: str | None) -> None:
    """At the token endpoint: the client exists, and a client registered with a secret must present it."""
    from handshake import db

    client = session.get(db.OAuthClientRow, client_id or "")
    if client is None:
        raise OAuthError(401, "invalid_client", "Unknown client_id.")
    if client.client_secret_hash is not None and not (
        client_secret and hmac.compare_digest(_hash_code(client_secret), client.client_secret_hash)
    ):
        raise OAuthError(401, "invalid_client", "Client authentication failed.")


def _valid_resource(resource: str | None) -> bool:
    """The token is for this service: the MCP server's URL (any path under it) or the API itself."""
    if not resource:
        return True
    settings = get_settings()
    allowed = [u for u in (settings.mcp_public_url, settings.api_url) if u]
    return any(resource.rstrip("/") == u or resource.startswith(u + "/") for u in allowed)


def start_authorization(session: Any, params: dict[str, str]) -> dict[str, Any]:
    """
    Validate an /oauth/authorize request. Returns {"request_id"} to send the
    user to the consent page, or {"redirect_error": url} for errors that may
    safely go back to the client (only once the redirect_uri is proven).
    """
    import secrets
    from urllib.parse import urlencode

    from handshake import db

    client = session.get(db.OAuthClientRow, params.get("client_id") or "")
    if client is None:
        raise OAuthError(400, "invalid_client", "Unknown client_id. The client must register first.")
    redirect_uri = params.get("redirect_uri") or (client.redirect_uris[0] if len(client.redirect_uris) == 1 else "")
    if redirect_uri not in client.redirect_uris:
        # Never redirect to an unregistered URI: that would make us an open redirector.
        raise OAuthError(400, "invalid_request", "redirect_uri doesn't match any registered for this client.")

    def back(error: str, description: str) -> dict[str, Any]:
        """An error the client should see, sent to its (verified) redirect_uri."""
        query = {"error": error, "error_description": description, "iss": get_settings().api_url}
        if params.get("state"):
            query["state"] = params["state"]
        return {"redirect_error": f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(query)}"}

    if params.get("response_type") != "code":
        return back("unsupported_response_type", "Only response_type=code is supported.")
    if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
        return back("invalid_request", "PKCE is required: send code_challenge with code_challenge_method=S256.")
    scopes = set((params.get("scope") or AGENT_SCOPE).split())
    if not scopes <= {AGENT_SCOPE, "offline_access"}:
        return back("invalid_scope", f"The only scope is {AGENT_SCOPE}.")
    if not _valid_resource(params.get("resource")):
        return back("invalid_target", "That resource isn't this Handshake server.")

    now = datetime.now(timezone.utc)
    row = db.OAuthRequestRow(
        id="oar_" + secrets.token_urlsafe(16), client_id=client.client_id, redirect_uri=redirect_uri,
        state=params.get("state"), code_challenge=params["code_challenge"], scope=AGENT_SCOPE,
        resource=params.get("resource"), status="pending", created_at=now,
        expires_at=now + timedelta(minutes=get_settings().oauth_request_ttl_minutes),
    )
    session.add(row)
    session.commit()
    return {"request_id": row.id}


def _pending_request(session: Any, request_id: str) -> Any:
    """A pending, unexpired authorization request, or 404/410."""
    from handshake import db

    row = session.get(db.OAuthRequestRow, request_id or "")
    if row is None:
        raise AuthError(404, "authorization_request_not_found", "This sign-in link is unknown. Start again from your agent.")
    if row.status != "pending" or _aware(row.expires_at) <= datetime.now(timezone.utc):
        raise AuthError(410, "authorization_request_expired", "This sign-in link has expired or was already used. Start again from your agent.")
    return row


def describe_authorization(session: Any, principal: Principal, request_id: str) -> dict[str, Any]:
    """What the consent page shows: who is asking, and where the user will be sent back."""
    from urllib.parse import urlsplit

    from handshake import db

    _require_user(principal, "approve agents")
    row = _pending_request(session, request_id)
    client = session.get(db.OAuthClientRow, row.client_id)
    parts = urlsplit(row.redirect_uri)
    return {
        "request_id": row.id, "client_id": row.client_id, "client_name": client.client_name if client else row.client_id,
        "redirect_host": parts.netloc or f"{parts.scheme}:", "scopes": [row.scope], "expires_at": _aware(row.expires_at).isoformat(),
    }


def decide_authorization(session: Any, principal: Principal, request_id: str, approve: bool) -> dict[str, Any]:
    """The USER approves or denies; returns where to send the browser (the client's redirect_uri)."""
    import secrets
    from urllib.parse import urlencode

    _require_user(principal, "approve agents")
    row = _pending_request(session, request_id)
    query: dict[str, str] = {"iss": get_settings().api_url}
    if approve:
        code = secrets.token_urlsafe(32)
        row.status, row.owner, row.code_hash = "approved", principal.email, _hash_code(code)
        row.code_expires_at = datetime.now(timezone.utc) + timedelta(seconds=get_settings().oauth_code_ttl_seconds)
        query["code"] = code
    else:
        row.status = "denied"
        query.update(error="access_denied", error_description="The user declined to connect this agent.")
    if row.state:
        query["state"] = row.state
    session.commit()
    return {"redirect_to": f"{row.redirect_uri}{'&' if '?' in row.redirect_uri else '?'}{urlencode(query)}"}


def _pkce_matches(verifier: str, challenge: str) -> bool:
    """S256: BASE64URL(SHA256(verifier)) == challenge (RFC 7636)."""
    if not verifier or not 43 <= len(verifier) <= 128:
        return False
    digest = _b64encode(hashlib.sha256(verifier.encode("ascii", "ignore")).digest())
    return hmac.compare_digest(digest, challenge)


def _issue_oauth_tokens(session: Any, grant: Any, client_id: str) -> dict[str, Any]:
    """A 1-hour access token plus a fresh single-use refresh token for this grant."""
    import secrets

    from handshake import db

    refresh = "hsr_" + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session.add(db.OAuthRefreshTokenRow(
        token_hash=_hash_code(refresh), grant_id=grant.id, client_id=client_id, created_at=now,
        expires_at=now + timedelta(days=get_settings().refresh_token_ttl_days),
    ))
    session.commit()
    access, _ = issue_agent_token(grant.owner, grant.agent_id, grant_id=grant.id, ttl_seconds=ACCESS_TOKEN_SECONDS)
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TOKEN_SECONDS, "refresh_token": refresh, "scope": AGENT_SCOPE}


def exchange_authorization_code(session: Any, code: str, client_id: str, redirect_uri: str | None, verifier: str) -> dict[str, Any]:
    """Code + PKCE verifier -> tokens, exactly once."""
    from sqlalchemy import select

    from handshake import db

    row = session.scalar(select(db.OAuthRequestRow).where(db.OAuthRequestRow.code_hash == _hash_code(code or "")))
    if row is None or row.status != "approved" or row.client_id != client_id:
        raise OAuthError(400, "invalid_grant", "The authorization code is invalid or was already used.")
    if row.code_expires_at is None or _aware(row.code_expires_at) <= datetime.now(timezone.utc):
        raise OAuthError(400, "invalid_grant", "The authorization code has expired.")
    if redirect_uri is not None and redirect_uri != row.redirect_uri:
        raise OAuthError(400, "invalid_grant", "redirect_uri doesn't match the one used to get the code.")
    if not _pkce_matches(verifier, row.code_challenge):
        raise OAuthError(400, "invalid_grant", "The PKCE code_verifier doesn't match.")
    row.status = "consumed"
    client = session.get(db.OAuthClientRow, client_id)
    # The client id is the agent's identity: contracts it drafts (and the user signs) bind to it.
    grant = create_grant(session, row.owner, agent_id=client_id, kind="oauth", name=client.client_name if client else client_id,
                         client_id=client_id, ttl_seconds=get_settings().refresh_token_ttl_days * 24 * 3600)
    return _issue_oauth_tokens(session, grant, client_id)


def refresh_access_token(session: Any, refresh_token: str, client_id: str) -> dict[str, Any]:
    """Rotate: an unused refresh token -> new access + refresh tokens. A reused one revokes the grant."""
    from handshake import db

    row = session.get(db.OAuthRefreshTokenRow, _hash_code(refresh_token or ""))
    if row is None or row.client_id != client_id:
        raise OAuthError(400, "invalid_grant", "Unknown refresh token.")
    grant = session.get(db.AgentGrantRow, row.grant_id)
    now = datetime.now(timezone.utc)
    if row.used_at is not None:
        # Someone replayed an old refresh token: assume it leaked and cut the agent off.
        if grant is not None and grant.revoked_at is None:
            grant.revoked_at = now
            session.commit()
        raise OAuthError(400, "invalid_grant", "This refresh token was already used; the agent was disconnected for safety.")
    if _aware(row.expires_at) <= now or grant is None or not grant_is_active(grant.id):
        raise OAuthError(400, "invalid_grant", "The refresh token has expired or the agent was disconnected.")
    row.used_at = now
    grant.expires_at = now + timedelta(days=get_settings().refresh_token_ttl_days)  # sliding: active agents stay connected
    return _issue_oauth_tokens(session, grant, client_id)
