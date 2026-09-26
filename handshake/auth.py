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


def verify_user_token(token: str, now: float | None = None) -> Principal:
    """Check a demo login token's signature and expiry; return the user Principal or raise 401."""
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
    if payload.get("role") != USER or not isinstance(payload.get("sub"), str):
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
