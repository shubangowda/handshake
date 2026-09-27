"""
config.py: every Handshake setting, in one place.

Job in the system
-----------------
This is the ONLY module that reads environment variables. Every other file
asks for settings with `get_settings()`. That gives us three guarantees:

1. No URL, host, port, or secret is hardcoded anywhere else. Going from
   localhost to the real domain means changing environment variables only
   (see docs/DEPLOY.md), never code.
2. Settings are validated once, at startup, with clear errors. A typo like
   HANDSHAKE_PAYMENT_MODE=live makes the app refuse to start instead of
   quietly doing the wrong thing.
3. Tests can swap settings in one call (`override_settings(...)`) without
   touching os.environ or reloading modules.

How it connects
---------------
    api.py, services.py, db.py, compiler.py, extractor.py, payments.py,
    merchant.py, mcp_server.py, scripts/*  --->  get_settings()

Where values come from
----------------------
Real environment variables win. If a `.env` file exists at the repo root,
python-dotenv loads it first WITHOUT overriding variables already set (so a
shell `export` always beats the file). Tests set HANDSHAKE_SKIP_DOTENV=1 so a
developer's personal .env can never change test results.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

# The repo root: config.py lives in <root>/handshake/config.py.
REPO_ROOT = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------
# Development-only fallback secrets
# ------------------------------------------------------------
# These exist so `python scripts/dev.py` works on a fresh clone. They are
# PUBLIC (they are in the source code), so they protect nothing. In prod
# (HANDSHAKE_ENV=prod) the app refuses to start while any of them is in use.
DEV_SIGNING_SECRET = "handshake-dev-signing-secret-do-not-use-in-prod"
DEV_SESSION_SECRET = "handshake-dev-session-secret-do-not-use-in-prod"
# 32 bytes, base64. Encrypts the funded one-time card stored on a contract.
DEV_CARD_KEY = "aGFuZHNoYWtlLWRldi1jYXJkLWtleS0zMmJ5dGVzISE="

# The only payment modes that exist. There is deliberately no "live" value:
# this codebase never requests a real card (see payments.py).
PAYMENT_MODES = ("stub", "link_test")
CREDENTIAL_MODES = ("agent_visible", "executor")
COMPILER_MODES = ("openai", "fixture")
ENVIRONMENTS = ("dev", "prod")


class ConfigError(ValueError):
    """Raised when the environment describes an unsafe or impossible configuration."""


@dataclass(frozen=True)
class Settings:
    """All settings, validated. Frozen so nothing can change them behind our back."""

    # --- Environment and public URLs --------------------------------------
    env: str = "dev"
    api_url: str = "http://localhost:8000"  # the backend's public base URL
    frontend_url: str = "http://localhost:3000"  # Rohan's Next.js app
    merchant_url: str = "http://localhost:3001"  # Sri's mock merchant
    cors_origins: tuple[str, ...] = ()  # empty -> frontend + merchant URLs
    allowed_merchant_origins: tuple[str, ...] = ()  # empty -> merchant URL only
    # Which merchant each checkout origin belongs to, e.g. "http://localhost:3001=Amazon.com".
    # The checkout-link parser uses it to check that a link really is that merchant's.
    merchant_identities: tuple[tuple[str, str], ...] = ()  # empty -> merchant URL = "Amazon.com" (the mock store)

    # --- Storage ------------------------------------------------------------
    database_url: str = "sqlite:///./handshake.db"

    # --- Secrets ------------------------------------------------------------
    signing_secret: str = DEV_SIGNING_SECRET  # HMAC key for contract signatures
    session_secret: str = DEV_SESSION_SECRET  # HMAC key for demo login tokens (must differ)
    agent_token: str | None = None  # static bearer token for the shopping agent
    agent_owner: str = "demo@handshake.dev"  # which user the agent acts for
    agent_id: str = "agent_demo"  # the agent's identity; contracts bind to it
    mcp_http_token: str | None = None  # bearer token for MCP over HTTP
    mcp_http_host: str = "127.0.0.1"  # where `handshake-mcp --transport http` listens
    mcp_http_port: int = 8765
    agent_token_ttl_days: int = 7  # lifetime of an agent token issued through the device (OAuth) flow
    device_code_ttl_minutes: int = 10  # how long the user has to approve a "connect agent" request
    # Where the MCP server caches the agent token it got from the device flow (0600 file).
    mcp_token_file: str = str(Path.home() / ".handshake" / "mcp-agent-token.json")

    # --- Payments -----------------------------------------------------------
    payment_mode: str = "stub"  # "stub" (offline) or "link_test" (Stripe Link TEST MODE)
    credential_mode: str = "agent_visible"  # or "executor"
    link_cli: str = "npx --yes @stripe/link-cli@0.23.0"  # pinned, verified version
    link_max_minor_units: int = 50000  # our own ceiling on a spend request
    link_timeout_seconds: float = 60.0
    link_tmp_dir: str = str(REPO_ROOT / ".link-tmp")  # private dir for card files
    # Each Handshake user gets their OWN Link login: the CLI runs with HOME set
    # to <link_home_root>/<hash of the user's email>, so credentials never mix.
    link_home_root: str = str(REPO_ROOT / ".handshake-state" / "link-homes")
    link_npm_cache: str = str(Path.home() / ".npm")  # shared npx cache (users' homes stay empty)
    card_encryption_key: str = DEV_CARD_KEY  # AES-256-GCM key for stored cards (base64, 32 bytes)

    # --- Compiler -----------------------------------------------------------
    compiler: str = "openai"  # "openai" (falls back to fixture with no key) or "fixture"
    compiler_model: str = "gpt-5.6"  # Ajay's model; configurable, never swapped silently
    compiler_service_tier: str | None = None  # e.g. "flex"; unset by default
    compiler_timeout_seconds: float = 60.0
    openai_api_key: str | None = None

    # --- Behavior -----------------------------------------------------------
    user_timezone: str = "America/New_York"
    escalation_ttl_minutes: int = 30
    session_ttl_minutes: int = 12 * 60
    credential_ttl_minutes: int = 30  # authorization -> Link approval (10 min window) -> agent pays
    http_timeout_seconds: float = 10.0  # extractor/executor calls to the merchant
    max_checkout_bytes: int = 512 * 1024  # extractor refuses bigger responses

    # --- Dev runner ports (derived from the URLs unless set) -------------------
    api_port: int = 8000
    frontend_port: int = 3000
    merchant_port: int = 3001

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------

    @property
    def effective_cors_origins(self) -> tuple[str, ...]:
        """CORS origins actually used: the configured list, or frontend + merchant by default."""
        if self.cors_origins:
            return self.cors_origins
        return (_origin(self.frontend_url), _origin(self.merchant_url))

    @property
    def effective_allowed_merchant_origins(self) -> tuple[str, ...]:
        """Origins the extractor may fetch: the configured list, or the mock merchant by default."""
        if self.allowed_merchant_origins:
            return tuple(_origin(o) for o in self.allowed_merchant_origins)
        return (_origin(self.merchant_url),)

    @property
    def effective_merchant_identities(self) -> dict[str, str]:
        """origin -> merchant name. By default the mock merchant's origin is 'Amazon.com' (Sri's store)."""
        if self.merchant_identities:
            return {_origin(origin): name for origin, name in self.merchant_identities}
        return {_origin(self.merchant_url): "Amazon.com"}

    @property
    def compiler_uses_fixture(self) -> bool:
        """True when the deterministic demo compiler runs (explicitly, or because no key is set)."""
        return self.compiler == "fixture" or not self.openai_api_key

    @property
    def payment_label(self) -> str:
        """What every surface (health, header, MCP) shows about the payment rail."""
        if self.payment_mode == "link_test":
            return "Stripe Link: TEST MODE"
        return "Simulated provider"


def _origin(url: str) -> str:
    """scheme://host[:port] of a URL, lowercased, without a trailing slash."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}".lower()


def _port_of(url: str, fallback: int) -> int:
    """The port in a URL, or the scheme default, or `fallback`."""
    parts = urlsplit(url)
    if parts.port:
        return parts.port
    if parts.scheme == "https":
        return 443
    if parts.scheme == "http":
        return 80
    return fallback


def _split_list(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated env value into a tuple of trimmed, non-empty items."""
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse_identities(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse 'origin=Name;origin=Name' into pairs (a semicolon list, since names may contain commas)."""
    pairs: list[tuple[str, str]] = []
    for item in (raw or "").split(";"):
        if "=" in item:
            origin, name = item.split("=", 1)
            if origin.strip() and name.strip():
                pairs.append((origin.strip(), name.strip()))
    return tuple(pairs)


def _choice(env: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]) -> str:
    """
    Read an enum-like setting. A value that is SET but empty or unknown is an error.

    This matters most for HANDSHAKE_PAYMENT_MODE: "live", "", or a typo must
    never silently fall back to something else. We refuse to start instead.
    """
    if name not in env:
        return default
    value = env[name].strip()
    if value not in allowed:
        raise ConfigError(f"{name}={value!r} is not allowed. Use one of: {', '.join(allowed)}.")
    return value


def _number(env: Mapping[str, str], name: str, default: float, kind: type) -> Any:
    """Read an int/float setting with a clear error on garbage."""
    if not env.get(name, "").strip():
        return default
    try:
        return kind(env[name])
    except ValueError:
        raise ConfigError(f"{name}={env[name]!r} must be a number.")


def _optional(env: Mapping[str, str], name: str) -> str | None:
    """A string setting where empty means unset."""
    value = env.get(name, "").strip()
    return value or None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build and validate Settings from an environment mapping (os.environ by default)."""
    if env is None:
        env = os.environ

    # A blank value (e.g. from "${HANDSHAKE_API_URL:-}" in .mcp.json) means "use the default".
    api_url = (_optional(env, "HANDSHAKE_API_URL") or Settings.api_url).rstrip("/")
    frontend_url = (_optional(env, "HANDSHAKE_FRONTEND_URL") or Settings.frontend_url).rstrip("/")
    merchant_url = (_optional(env, "HANDSHAKE_MERCHANT_URL") or Settings.merchant_url).rstrip("/")

    settings = Settings(
        env=_choice(env, "HANDSHAKE_ENV", "dev", ENVIRONMENTS),
        api_url=api_url,
        frontend_url=frontend_url,
        merchant_url=merchant_url,
        cors_origins=tuple(_origin(o) if o != "*" else "*" for o in _split_list(env.get("HANDSHAKE_CORS_ORIGINS"))),
        allowed_merchant_origins=_split_list(env.get("HANDSHAKE_ALLOWED_MERCHANT_ORIGINS")),
        merchant_identities=_parse_identities(env.get("HANDSHAKE_MERCHANT_IDENTITIES")),
        database_url=env.get("DATABASE_URL", Settings.database_url),
        signing_secret=_optional(env, "HANDSHAKE_SIGNING_SECRET") or DEV_SIGNING_SECRET,
        session_secret=_optional(env, "HANDSHAKE_SESSION_SECRET") or DEV_SESSION_SECRET,
        agent_token=_optional(env, "HANDSHAKE_AGENT_TOKEN"),
        agent_owner=(_optional(env, "HANDSHAKE_AGENT_OWNER") or Settings.agent_owner).lower(),
        agent_id=_optional(env, "HANDSHAKE_AGENT_ID") or Settings.agent_id,
        mcp_http_token=_optional(env, "HANDSHAKE_MCP_HTTP_TOKEN"),
        mcp_http_host=_optional(env, "HANDSHAKE_MCP_HTTP_HOST") or Settings.mcp_http_host,
        mcp_http_port=_number(env, "HANDSHAKE_MCP_HTTP_PORT", Settings.mcp_http_port, int),
        agent_token_ttl_days=_number(env, "HANDSHAKE_AGENT_TOKEN_TTL_DAYS", Settings.agent_token_ttl_days, int),
        device_code_ttl_minutes=_number(env, "HANDSHAKE_DEVICE_CODE_TTL_MINUTES", Settings.device_code_ttl_minutes, int),
        mcp_token_file=_optional(env, "HANDSHAKE_MCP_TOKEN_FILE") or Settings.mcp_token_file,
        payment_mode=_choice(env, "HANDSHAKE_PAYMENT_MODE", "stub", PAYMENT_MODES),
        credential_mode=_choice(env, "HANDSHAKE_CREDENTIAL_MODE", "agent_visible", CREDENTIAL_MODES),
        link_cli=_optional(env, "HANDSHAKE_LINK_CLI") or Settings.link_cli,
        link_max_minor_units=_number(env, "HANDSHAKE_LINK_MAX_MINOR_UNITS", Settings.link_max_minor_units, int),
        link_timeout_seconds=_number(env, "HANDSHAKE_LINK_TIMEOUT_SECONDS", Settings.link_timeout_seconds, float),
        link_tmp_dir=_optional(env, "HANDSHAKE_LINK_TMP_DIR") or Settings.link_tmp_dir,
        link_home_root=_optional(env, "HANDSHAKE_LINK_HOME_ROOT") or Settings.link_home_root,
        link_npm_cache=_optional(env, "HANDSHAKE_LINK_NPM_CACHE") or Settings.link_npm_cache,
        card_encryption_key=_optional(env, "HANDSHAKE_CARD_ENCRYPTION_KEY") or DEV_CARD_KEY,
        compiler=_choice(env, "HANDSHAKE_COMPILER", "openai", COMPILER_MODES),
        compiler_model=_optional(env, "HANDSHAKE_COMPILER_MODEL") or Settings.compiler_model,
        compiler_service_tier=_optional(env, "HANDSHAKE_COMPILER_SERVICE_TIER"),
        compiler_timeout_seconds=_number(env, "HANDSHAKE_COMPILER_TIMEOUT_SECONDS", Settings.compiler_timeout_seconds, float),
        openai_api_key=_optional(env, "OPENAI_API_KEY"),
        user_timezone=_optional(env, "HANDSHAKE_USER_TIMEZONE") or Settings.user_timezone,
        escalation_ttl_minutes=_number(env, "HANDSHAKE_ESCALATION_TTL_MINUTES", Settings.escalation_ttl_minutes, int),
        session_ttl_minutes=_number(env, "HANDSHAKE_SESSION_TTL_MINUTES", Settings.session_ttl_minutes, int),
        credential_ttl_minutes=_number(env, "HANDSHAKE_CREDENTIAL_TTL_MINUTES", Settings.credential_ttl_minutes, int),
        http_timeout_seconds=_number(env, "HANDSHAKE_HTTP_TIMEOUT_SECONDS", Settings.http_timeout_seconds, float),
        max_checkout_bytes=_number(env, "HANDSHAKE_MAX_CHECKOUT_BYTES", Settings.max_checkout_bytes, int),
        api_port=_number(env, "HANDSHAKE_API_PORT", _port_of(api_url, 8000), int),
        frontend_port=_number(env, "HANDSHAKE_FRONTEND_PORT", _port_of(frontend_url, 3000), int),
        merchant_port=_number(env, "HANDSHAKE_MERCHANT_PORT", _port_of(merchant_url, 3001), int),
    )
    validate(settings)
    return settings


def validate(settings: Settings) -> None:
    """Refuse configurations that are unsafe. Called on every load and every override."""
    # The two HMAC keys protect different things (contract signatures vs.
    # login tokens). Sharing one key would let a leaked login token forge
    # contract signatures or vice versa.
    if settings.signing_secret == settings.session_secret:
        raise ConfigError("HANDSHAKE_SESSION_SECRET must differ from HANDSHAKE_SIGNING_SECRET.")

    import base64
    import binascii

    try:
        if len(base64.b64decode(settings.card_encryption_key, validate=True)) != 32:
            raise ValueError
    except (ValueError, binascii.Error):
        raise ConfigError("HANDSHAKE_CARD_ENCRYPTION_KEY must be 32 random bytes, base64-encoded.")

    if settings.link_max_minor_units < 1:
        raise ConfigError("HANDSHAKE_LINK_MAX_MINOR_UNITS must be at least 1.")

    # Validate the timezone name early (a typo would otherwise crash later).
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(settings.user_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"HANDSHAKE_USER_TIMEZONE={settings.user_timezone!r} is not a known timezone.")

    if settings.env == "prod":
        problems: list[str] = []
        if settings.signing_secret == DEV_SIGNING_SECRET:
            problems.append("HANDSHAKE_SIGNING_SECRET is the public dev default")
        if settings.session_secret == DEV_SESSION_SECRET:
            problems.append("HANDSHAKE_SESSION_SECRET is the public dev default")
        if settings.card_encryption_key == DEV_CARD_KEY:
            problems.append("HANDSHAKE_CARD_ENCRYPTION_KEY is the public dev default")
        if not settings.agent_token:
            problems.append("HANDSHAKE_AGENT_TOKEN is not set")
        if "*" in settings.effective_cors_origins:
            problems.append("HANDSHAKE_CORS_ORIGINS contains a wildcard")
        if problems:
            raise ConfigError("Refusing to start in prod: " + "; ".join(problems) + ".")


# ============================================================
# Access for the rest of the app
# ============================================================

_current: Settings | None = None


def _load_dotenv_once() -> None:
    """Load <repo>/.env into os.environ (without overriding real env vars), unless tests opted out."""
    if os.environ.get("HANDSHAKE_SKIP_DOTENV") == "1":
        return
    dotenv_path = REPO_ROOT / ".env"
    if dotenv_path.is_file():
        from dotenv import load_dotenv

        load_dotenv(dotenv_path, override=False)


def get_settings() -> Settings:
    """The current settings. Loaded (and validated) on first use."""
    global _current
    if _current is None:
        _load_dotenv_once()
        _current = load_settings()
    return _current


def set_settings(settings: Settings) -> None:
    """Replace the current settings wholesale (after validating them)."""
    global _current
    validate(settings)
    _current = settings


def override_settings(**changes: Any) -> Settings:
    """Return AND install a copy of the current settings with `changes` applied (tests use this)."""
    updated = replace(get_settings(), **changes)
    set_settings(updated)
    return updated


def reset_settings() -> None:
    """Forget the current settings so the next get_settings() reloads from the environment."""
    global _current
    _current = None
