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
for a hackathon demo on localhost and nothing more. It must be replaced by
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

from handshake.config import get_settings

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


def issue_agent_token(email: str, agent_id: str, now: float | None = None) -> tuple[str, int]:
    """
    A signed AGENT token for one user and one agent id (what the device flow hands the MCP server).

    Same format and key as user tokens, but role "agent": it can only do what
    an agent may do (never sign, approve, reject, revoke, or edit).
    """
    issued = int(now if now is not None else time.time())
    expires = issued + get_settings().agent_token_ttl_days * 24 * 3600
    payload = {"sub": normalize_email(email), "role": AGENT, "agent_id": agent_id, "iat": issued, "exp": expires}
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

    # Approved: issue the agent token exactly once.
    row.status = "consumed"
    session.commit()
    token, expires = issue_agent_token(row.owner, row.client_id)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": int(expires - time.time()),
        "scope": "handshake.agent",
        "agent_id": row.client_id,
        "email": row.owner,
    }
