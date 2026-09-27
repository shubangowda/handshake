"""
payments.py: the payment rail. Stripe Link in TEST MODE, or a simulated provider (Ajay's stripe.py, finished).

Job in the system
-----------------
Handshake's engine decides WHETHER a purchase may happen. This file is the
part that makes it happen, after the decision:

    services.start_payment()     -> provider.create_request()   Link TEST spend request (user approves in Link)
    services.refresh_payment()   -> provider.get_status()       pending / approved / denied / expired
    credential release           -> provider.retrieve_card()    the single-use TEST card, read once, never stored
    executor mode                -> pay_merchant()              submit the card to the merchant's /pay
    both modes                   -> find_session_order() / get_order()   verify the order independently

Renamed from stripe.py so it no longer shadows the official `stripe` package.

TEST MODE, ALWAYS
-----------------
HANDSHAKE_PAYMENT_MODE accepts exactly "stub" and "link_test" (config.py
refuses anything else, including "live", at startup). Every Link spend
request is created through build_create_command(), which ALWAYS appends
--test, and nothing anywhere can remove it. The Link CLI itself only accepts
--test on `create` (verified: `retrieve --test` fails with "Unknown flag:
--test"), so test mode is a property the request gets when it is created.
retrieve and cancel only ever touch request ids this backend created. No
code path in this repository can request a live card. No real card is ever
charged by this codebase.

Card handling
-------------
A card exists in memory only, for the moment it is released to the agent or
submitted to the merchant. It is never written to the database, logs, errors,
or evidence. CardSecret's repr is redacted, so even an accidental print shows
only the last four digits. Link writes the full card to a private temp file
(mode 0600, in a 0700 directory we create), which we read once and then
overwrite and delete in a `finally` block.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import httpx

from handshake.config import Settings, get_settings

# Payment states stored in the payments table (PurchaseStatus in models.py is untouched).
AWAITING_APPROVAL = "awaiting_approval"
APPROVED = "approved"
REVALIDATING = "revalidating"
CREDENTIAL_READY = "credential_ready"  # agent_visible mode only
PAYING = "paying"
PAID = "paid"
COMPLETED = "completed"
DENIED = "denied"
EXPIRED = "expired"
CHECKOUT_CHANGED = "checkout_changed"
FAILED = "failed"
UNKNOWN = "unknown"  # the outcome of a pay call is uncertain: never retried blindly

TERMINAL_STATES = {COMPLETED, DENIED, EXPIRED, CHECKOUT_CHANGED, FAILED}

# Provider-side states (normalized from whatever the provider reports).
P_PENDING = "pending"
P_APPROVED = "approved"
P_DENIED = "denied"
P_EXPIRED = "expired"
P_FAILED = "failed"
P_UNKNOWN = "unknown"

# How the verified Link CLI statuses map onto ours. Anything not listed is
# P_UNKNOWN, and an unknown provider status never advances a payment.
LINK_STATUS_MAP = {
    "created": P_PENDING,
    "pending_approval": P_PENDING,
    "requires_action": P_PENDING,  # e.g. 3-D Secure; Link resolves it or the request expires
    "approved": P_APPROVED,
    "submitted": P_APPROVED,
    "succeeded": P_APPROVED,
    "denied": P_DENIED,
    "declined": P_DENIED,
    "canceled": P_DENIED,
    "expired": P_EXPIRED,
    "failed": P_FAILED,
}

# The simulated provider's well-known test card. It is a published Stripe
# test number, accepted by the mock merchant, and can't charge anything.
STUB_TEST_CARD = "4242424242424242"


class PaymentError(Exception):
    """A provider or executor failure. `code` is machine-readable; the message never contains card data."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        """Keep the code, a safe message, and safe details."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class PaymentUncertain(PaymentError):
    """We don't know whether the merchant took the payment (timeout, dropped connection). Never retry blindly."""


@dataclass(frozen=True)
class SpendSpec:
    """Everything needed to ask a provider for one single-use card."""

    reference_id: str  # the contract funding this card is for (idempotency + metadata)
    amount_minor: int  # integer cents: the contract's all-in hard cap
    currency: str
    merchant_name: str
    merchant_url: str
    context: str  # >= 100 chars, truthful: goal, merchant, item, total
    line_items: tuple[tuple[str, int, int], ...] = ()  # (name, unit_amount_minor, quantity)

    @property
    def idempotency_key(self) -> str:
        """One key per funding: retrying creation can never mint a second request."""
        return f"handshake-{self.reference_id}"


@dataclass(frozen=True)
class ProviderRequest:
    """A provider-side request as we track it."""

    request_id: str
    state: str  # one of the P_* values
    raw_status: str
    approval_url: str | None = None


@dataclass(frozen=True)
class CardSecret:
    """
    A single-use card, in memory only. repr() shows last4 only, so it can't leak by accident.

    `simulated` is True for the stub provider's card.
    """

    number: str = field(repr=False)
    exp_month: int = field(repr=False)
    exp_year: int = field(repr=False)
    cvc: str = field(repr=False)
    valid_until: str | None = None
    simulated: bool = False
    brand: str | None = None

    @property
    def last4(self) -> str:
        """The only part of a card number that may be stored or shown."""
        return self.number[-4:]

    def __repr__(self) -> str:
        """Redacted representation."""
        return f"CardSecret(last4={self.last4!r}, simulated={self.simulated})"

    __str__ = __repr__


class Provider(Protocol):
    """What services.py needs from a payment provider."""

    name: str
    label: str

    async def create_request(self, spec: SpendSpec) -> ProviderRequest:
        """Create a single-use spend request with user approval required."""
        ...

    async def get_status(self, request_id: str) -> ProviderRequest:
        """Read the current status of a request."""
        ...

    async def retrieve_card(self, request_id: str) -> CardSecret:
        """Retrieve the approved card (only after approval)."""
        ...

    async def cancel(self, request_id: str) -> None:
        """Cancel a request that is no longer wanted."""
        ...

    async def reconcile(self, spec: SpendSpec) -> ProviderRequest | None:
        """After an uncertain create, find the request that may already exist."""
        ...


# ============================================================
# Output hygiene
# ============================================================

CARD_LIKE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
SECRETISH = re.compile(r"(?i)(token|secret|cvc|cvv|number|authorization)[\"'=: ]+[^\s,\"']+")


def sanitize(text: str | None, limit: int = 300) -> str:
    """Strip card-like numbers and secret-looking values from provider output before logging or storing it."""
    if not text:
        return ""
    cleaned = CARD_LIKE.sub("[redacted]", text)
    cleaned = SECRETISH.sub(r"\1=[redacted]", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def parse_auth_status(output: str) -> dict[str, Any]:
    """
    Ajay's parser, kept: Link's `auth status` returns ONE object or a LIST of
    streamed updates (verified: an unauthenticated CLI prints a one-item list).
    The latest update wins.
    """
    status = json.loads(output)
    if isinstance(status, list):
        if not status:
            raise ValueError("Link CLI returned an empty authentication status list")
        status = status[-1]
    if not isinstance(status, dict) or type(status.get("authenticated")) is not bool:
        raise ValueError("Link CLI returned an unrecognized authentication status")
    return status


def parse_cli_json(text: str) -> Any:
    """
    Parse Link CLI stdout: one JSON document, or JSON lines (one update per line).

    Verified on link-cli 0.23.0: `spend-request create --format json` prints a
    single JSON document that is a LIST of streamed updates (one item for a
    request awaiting approval), while `cancel` prints a bare object.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        lines = [line for line in text.splitlines() if line.strip()]
        try:
            return [json.loads(line) for line in lines]
        except json.JSONDecodeError:
            raise PaymentError("link_malformed_output", "The Link CLI returned output that isn't JSON.")


def latest_update(data: Any) -> Any:
    """
    The current state from Link output that may be ONE object or a LIST of
    streamed updates. Like auth status: the last update wins.
    """
    if isinstance(data, list):
        updates = [item for item in data if isinstance(item, dict)]
        if not updates:
            raise PaymentError("link_malformed_output", "The Link CLI returned an empty list of updates.")
        return updates[-1]
    return data


def spend_request_from_output(data: Any) -> dict[str, Any]:
    """A spend request object from create/retrieve output, in either shape. Raises if there is none."""
    item = latest_update(data)
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("status"), str):
        raise PaymentError("link_malformed_output", "The Link CLI returned a spend request without an id or status.")
    return item


def spend_requests_from_list_output(data: Any) -> list[dict[str, Any]]:
    """The spend requests from `spend-request list` output: {"data": [...]}, [...], or [{"data": [...]}]."""
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and isinstance(data[0].get("data"), list):
        data = data[0]
    if isinstance(data, dict):
        data = data.get("data", [])
    return [item for item in data or [] if isinstance(item, dict) and "id" in item]


def build_context(goal: str, merchant_name: str, item_names: list[str], total_text: str) -> str:
    """
    The truthful description the user reads when approving in Link (Link requires >= 100 characters).
    Built from the signed contract and the independently extracted checkout, nothing the agent claimed.
    """
    items = ", ".join(item_names[:3]) or "the selected item"
    text = (
        f"Handshake-authorized purchase for the goal '{goal}': {items} from {merchant_name}, "
        f"final all-in total {total_text}. The checkout was independently checked against the contract you signed. "
        "This is a single-use test-mode card."
    )
    return text if len(text) >= 100 else text + " " + "." * (100 - len(text))


def _cli_safe(text: str, limit: int = 80) -> str:
    """Line-item names go into Link's key:value flags; commas and colons would break that format."""
    return re.sub(r"[,:\n\r]", " ", text).strip()[:limit] or "item"


# ============================================================
# Stripe Link, TEST MODE
# ============================================================


def build_create_command(spec: SpendSpec) -> list[str]:
    """
    The ONLY way a Link spend request is ever created. It ALWAYS ends with --test.

    Also always: a card credential (for the merchant's card form), user
    approval requested (--request-approval), and our idempotency key. It
    never passes --approve (which would skip the user's approval).
    """
    args = [
        "spend-request", "create",
        "--credential-type", "card",
        "--merchant-name", spec.merchant_name,
        "--merchant-url", spec.merchant_url,
        "--amount", str(spec.amount_minor),
        "--currency", spec.currency.lower(),
        "--context", spec.context,
        "--idempotency-key", spec.idempotency_key,
        "--metadata", f"handshake_reference_id:{spec.reference_id}",
        "--total", f"type:total,display_text:Total,amount:{spec.amount_minor}",
        "--request-approval",
    ]
    for name, unit_amount, quantity in spec.line_items:
        args += ["--line-item", f"name:{_cli_safe(name)},unit_amount:{unit_amount},quantity:{quantity}"]
    return _test_mode(args)


def _test_mode(args: list[str]) -> list[str]:
    """Append Link's test flag. There is no parameter that turns this off."""
    return [*args, "--test"]


def build_retrieve_command(request_id: str, output_file: str | None = None) -> list[str]:
    """Retrieve a request (optionally with the card, written to `output_file`, never to stdout)."""
    _check_request_id(request_id)
    args = ["spend-request", "retrieve", request_id]
    if output_file is not None:
        args += ["--include", "card", "--output-file", output_file]
    return args


def build_cancel_command(request_id: str) -> list[str]:
    """Cancel a request."""
    _check_request_id(request_id)
    return ["spend-request", "cancel", request_id]


def _check_request_id(request_id: str) -> None:
    """Only real Link spend request ids (lsrq_...) are ever passed to the CLI."""
    if not re.fullmatch(r"lsrq_[A-Za-z0-9_]+", request_id or ""):
        raise PaymentError("invalid_request_id", "Not a Link spend request id.")


def _find_card(data: Any) -> dict[str, Any] | None:
    """Find the card object in Link's card file, whatever envelope it is in."""
    if isinstance(data, dict):
        if isinstance(data.get("number"), str) and data.get("cvc") is not None:
            return data
        for value in data.values():
            found = _find_card(value)
            if found:
                return found
    if isinstance(data, list):
        for value in data:
            found = _find_card(value)
            if found:
                return found
    return None


class LinkTestProvider:
    """Stripe Link through the official link-cli, always in test mode."""

    name = "link_test"
    label = "Stripe Link: TEST MODE"

    def __init__(self, settings: Settings | None = None, home: str | None = None) -> None:
        """
        The CLI command (pinned version) and limits come from config.

        `home` is the user's private Link directory (user_link_home). The CLI
        keeps its login under $HOME, so running it with a per-user HOME is
        what gives every Handshake user their OWN Link account.
        """
        self.settings = settings or get_settings()
        self.home = home

    def _env(self) -> dict[str, str]:
        """Environment for the CLI: the user's HOME, and the shared npx cache (so nothing is re-downloaded per user)."""
        env = dict(os.environ)
        if self.home:
            env["HOME"] = self.home
        env["npm_config_cache"] = self.settings.link_npm_cache
        return env

    def _base_command(self) -> list[str]:
        """The configured CLI invocation, e.g. ['npx', '--yes', '@stripe/link-cli@0.23.0']."""
        parts = shlex.split(self.settings.link_cli)
        if not parts:
            raise PaymentError("link_cli_not_configured", "HANDSHAKE_LINK_CLI is empty.")
        if shutil.which(parts[0]) is None and not os.path.isabs(parts[0]):
            raise PaymentError("link_cli_missing", f"{parts[0]} is not installed (Node.js is needed for the Link CLI).")
        return parts

    async def _run(self, args: list[str], *, log_stdout: bool = True) -> Any:
        """
        Run one CLI command asynchronously and parse its JSON.

        Arguments go straight to the process as a list (never through a
        shell), every call has a timeout, and stderr is sanitized before it
        is kept anywhere.
        """
        command = [*self._base_command(), *args, "--format", "json"]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL, env=self._env()
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.settings.link_timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise PaymentUncertain("link_timeout", "The Link CLI did not answer in time.")
        text = stdout.decode("utf-8", errors="replace")
        try:
            data = parse_cli_json(text)
        except PaymentError as exc:
            exc.details["stderr"] = sanitize(stderr.decode("utf-8", "replace"))
            raise
        # The CLI reports errors as {"code": ..., "message": ...} (verified),
        # possibly as the last item of a list of streamed updates.
        last = data[-1] if isinstance(data, list) and data else data
        if isinstance(last, dict) and "code" in last and "message" in last and "id" not in last:
            raise PaymentError(f"link_{str(last['code']).lower()}", sanitize(str(last["message"])))
        if process.returncode not in (0, None):
            raise PaymentError("link_cli_failed", f"The Link CLI exited with {process.returncode}.", {"stderr": sanitize(stderr.decode("utf-8", "replace"))})
        return data

    async def auth_status(self) -> dict[str, Any]:
        """Whether this machine's Link CLI is logged in."""
        command = [*self._base_command(), "auth", "status", "--format", "json"]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL, env=self._env())
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.settings.link_timeout_seconds)
        return parse_auth_status(stdout.decode("utf-8", errors="replace"))

    def _to_request(self, data: Any) -> ProviderRequest:
        """Normalize Link output (an object or a list of streamed updates) into a ProviderRequest."""
        data = spend_request_from_output(data)
        raw = data["status"]
        return ProviderRequest(
            request_id=data["id"],
            state=LINK_STATUS_MAP.get(raw, P_UNKNOWN),
            raw_status=raw,
            approval_url=data.get("approval_url") if isinstance(data.get("approval_url"), str) else None,
        )

    async def create_request(self, spec: SpendSpec) -> ProviderRequest:
        """Create a TEST spend request with approval requested (returns immediately in agent/JSON mode, verified)."""
        if not 1 <= spec.amount_minor <= self.settings.link_max_minor_units:
            raise PaymentError("amount_out_of_range", f"Link requests are limited to {self.settings.link_max_minor_units} minor units here.")
        if len(spec.context.strip()) < 100:
            raise PaymentError("context_too_short", "Link requires a purchase description of at least 100 characters.")
        if not re.fullmatch(r"[A-Za-z]{3}", spec.currency):
            raise PaymentError("invalid_currency", "Currency must be a three-letter code.")
        data = await self._run(build_create_command(spec))
        return self._to_request(data)

    async def get_status(self, request_id: str) -> ProviderRequest:
        """Current status (no card data is requested)."""
        return self._to_request(await self._run(build_retrieve_command(request_id)))

    async def reconcile(self, spec: SpendSpec) -> ProviderRequest | None:
        """Find a request we may have created before a timeout, by our metadata, without creating anything."""
        data = await self._run(["spend-request", "list", "--include-history"])
        for item in spend_requests_from_list_output(data):
            metadata = item.get("metadata")
            if isinstance(metadata, dict) and metadata.get("handshake_reference_id") == spec.reference_id:
                return self._to_request(item)
        return None

    async def retrieve_card(self, request_id: str) -> CardSecret:
        """
        Retrieve the approved card through a private temp file, read it once, and destroy it.

        - a fresh 0700 directory we create (mkdtemp), inside HANDSHAKE_LINK_TMP_DIR
        - the CLI creates the file itself with 0600 and refuses to overwrite (verified)
        - we refuse symlinks and anything that isn't a regular file we own
        - read once into memory, then overwrite with zeros and delete in `finally`
        - stdout from this call is never logged (it would still be redacted)
        """
        base = Path(self.settings.link_tmp_dir)
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="card-", dir=base))
        os.chmod(workdir, 0o700)
        card_path = workdir / f"{secrets.token_hex(8)}.json"
        try:
            await self._run(build_retrieve_command(request_id, str(card_path)), log_stdout=False)
            info = os.lstat(card_path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise PaymentError("card_file_unsafe", "The card file is not a private regular file.")
            with open(card_path, "rb") as handle:
                raw = handle.read()
            card = _find_card(json.loads(raw.decode("utf-8")))
            if card is None:
                raise PaymentError("card_missing", "Link returned no card for this request (is it approved?).")
            return CardSecret(
                number=str(card["number"]).replace(" ", ""),
                exp_month=int(card["exp_month"]),
                exp_year=int(card["exp_year"]),
                cvc=str(card["cvc"]),
                valid_until=str(card.get("valid_until")) if card.get("valid_until") else None,
                brand=card.get("brand"),
            )
        except FileNotFoundError:
            raise PaymentError("card_missing", "Link did not write a card file (is the request approved?).")
        except (ValueError, KeyError, TypeError):
            raise PaymentError("card_malformed", "The card file could not be read.")
        finally:
            _shred(card_path)
            shutil.rmtree(workdir, ignore_errors=True)

    async def cancel(self, request_id: str) -> None:
        """Cancel a request (from created, pending_approval, or approved)."""
        await self._run(build_cancel_command(request_id))


def _shred(path: Path) -> None:
    """Overwrite a file with zeros, then delete it. Never raises."""
    try:
        if path.is_file() and not path.is_symlink():
            size = path.stat().st_size
            with open(path, "r+b") as handle:
                handle.write(b"\0" * size)
                handle.flush()
                os.fsync(handle.fileno())
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ============================================================
# The simulated provider (stub mode, fully offline)
# ============================================================


class StubProvider:
    """
    Mimics the Link flow offline and deterministically. Everything it produces is labeled SIMULATED.

    Requests live in memory (a restart forgets them; they then read as
    expired, which fails safe). The "approval tap" is the frontend's
    "Simulated provider approval" button (POST .../payment/simulate-approval).
    """

    name = "stub"
    label = "Simulated provider"
    _requests: dict[str, dict[str, Any]] = {}

    async def create_request(self, spec: SpendSpec) -> ProviderRequest:
        """Idempotent like Link: the same purchase always maps to the same request id."""
        request_id = "stub_sr_" + hashlib.sha256(spec.idempotency_key.encode()).hexdigest()[:16]
        entry = self._requests.setdefault(request_id, {"status": "pending_approval", "spec": spec})
        return ProviderRequest(request_id, LINK_STATUS_MAP[entry["status"]], entry["status"], None)

    async def get_status(self, request_id: str) -> ProviderRequest:
        """The simulated status; an id we don't know reads as expired (fail safe)."""
        entry = self._requests.get(request_id)
        raw = entry["status"] if entry else "expired"
        return ProviderRequest(request_id, LINK_STATUS_MAP.get(raw, P_UNKNOWN), raw, None)

    async def reconcile(self, spec: SpendSpec) -> ProviderRequest | None:
        """Nothing is ever uncertain offline."""
        return None

    async def retrieve_card(self, request_id: str) -> CardSecret:
        """The well-known test card, only once the simulated request is approved."""
        entry = self._requests.get(request_id)
        if not entry or entry["status"] != "approved":
            raise PaymentError("not_approved", "The simulated request is not approved.")
        year = datetime.now(timezone.utc).year + 3
        return CardSecret(number=STUB_TEST_CARD, exp_month=12, exp_year=year, cvc="123", simulated=True, brand="visa",
                          valid_until=(datetime.now(timezone.utc) + timedelta(hours=12)).isoformat())

    async def cancel(self, request_id: str) -> None:
        """Mark the simulated request canceled."""
        if request_id in self._requests:
            self._requests[request_id]["status"] = "canceled"

    # --- simulation controls (the stub-only route and tests use these) ---

    @classmethod
    def simulate(cls, request_id: str, status: str) -> None:
        """Set a simulated request's status: 'approved', 'denied', or 'expired'."""
        if request_id not in cls._requests:
            raise PaymentError("unknown_request", "No such simulated request.")
        cls._requests[request_id]["status"] = status


def get_provider(settings: Settings | None = None, owner: str | None = None) -> Provider:
    """The provider for the configured payment mode, acting as `owner`'s own Link account."""
    settings = settings or get_settings()
    if settings.payment_mode == "link_test":
        return LinkTestProvider(settings, home=user_link_home(owner) if owner else None)
    return StubProvider()


def provider_named(name: str, owner: str | None) -> Provider:
    """The provider a stored row was created with (a row never switches providers)."""
    if name == "link_test":
        return LinkTestProvider(home=user_link_home(owner) if owner else None)
    return StubProvider()


# ============================================================
# Each user's own Link account (connected from the website)
# ============================================================


def user_link_home(email: str) -> str:
    """
    The private directory that acts as this user's HOME for the Link CLI.

    Named by a hash of the email (so the path reveals nothing), created 0700.
    The Link login the user approves is stored inside it, and only CLI calls
    made on that user's behalf ever use it.
    """
    root = Path(get_settings().link_home_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = root / hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:24]
    home.mkdir(mode=0o700, exist_ok=True)
    os.chmod(home, 0o700)
    return str(home)


@dataclass
class LinkLogin:
    """One in-progress "connect your Link account" login for one user."""

    email: str
    process: Any = None
    verification_url: str | None = None
    phrase: str | None = None
    state: str = "starting"  # starting / pending / connected / failed / expired
    error: str | None = None
    started_at: float = 0.0
    first_update: Any = None  # threading.Event set when the link (or a failure) is known


_logins: dict[str, LinkLogin] = {}


def _link_cli_for(email: str) -> tuple[list[str], dict[str, str]]:
    """The CLI command prefix and environment for acting as `email`."""
    provider = LinkTestProvider(home=user_link_home(email))
    return shlex.split(get_settings().link_cli), provider._env()


def _read_login_output(login: LinkLogin) -> None:
    """
    Background reader for `auth login --format jsonl` (verified: it STREAMS
    one {"type": "chunk", "data": {...}} line per update, the first within
    about a second, carrying verification_url and phrase).
    """
    try:
        for line in login.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = message.get("data") if isinstance(message, dict) else None
            if isinstance(data, dict):
                if isinstance(data.get("verification_url"), str):
                    login.verification_url = data["verification_url"]
                    login.phrase = data.get("phrase") if isinstance(data.get("phrase"), str) else login.phrase
                    if login.state == "starting":
                        login.state = "pending"
                if data.get("authenticated") is True:
                    login.state = "connected"
                if "code" in data and "message" in data:
                    login.state, login.error = "failed", sanitize(str(data["message"]))
                login.first_update.set()
            if isinstance(message, dict) and message.get("type") == "done" and login.state in ("starting", "pending"):
                login.state = "expired"
                login.first_update.set()
    finally:
        login.process.wait()
        if login.state in ("starting", "pending"):
            login.state = "expired"
        login.first_update.set()


def start_link_login(email: str, wait_seconds: float = 30.0) -> dict[str, Any]:
    """
    Start connecting `email`'s own Link account; return the Link login link and phrase to show them.

    The CLI keeps polling in the background until the user approves in the
    Link app (or it times out), and then saves the login in the user's
    private HOME. Stub mode needs no Link account: it's simulated.
    """
    import threading
    import time as _time

    if get_settings().payment_mode != "link_test":
        return {"state": "connected", "simulated": True, "provider_label": "Simulated provider"}
    existing = _logins.get(email)
    if existing and existing.state == "pending" and existing.process and existing.process.poll() is None:
        return _login_view(existing)

    command, env = _link_cli_for(email)
    process = subprocess.Popen(
        [*command, "auth", "login", "--client-name", "Handshake", "--interval", "3", "--timeout", "600", "--format", "jsonl"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True, env=env,
    )
    login = LinkLogin(email=email, process=process, started_at=_time.time(), first_update=threading.Event())
    _logins[email] = login
    threading.Thread(target=_read_login_output, args=(login,), daemon=True).start()
    login.first_update.wait(timeout=wait_seconds)
    return _login_view(login)


def _login_view(login: LinkLogin) -> dict[str, Any]:
    """What the website shows about a login in progress (to that user only)."""
    return {"state": login.state, "verification_url": login.verification_url, "phrase": login.phrase,
            "error": login.error, "provider_label": "Stripe Link: TEST MODE", "simulated": False}


def link_connection(email: str) -> dict[str, Any]:
    """Whether `email` has a connected Link account (the access token itself is never returned)."""
    if get_settings().payment_mode != "link_test":
        return {"connected": True, "simulated": True, "provider_label": "Simulated provider", "login": None}
    login = _logins.get(email)
    command, env = _link_cli_for(email)
    try:
        result = subprocess.run([*command, "auth", "status", "--format", "json"], capture_output=True, text=True,
                                env=env, timeout=get_settings().link_timeout_seconds)
        connected = bool(parse_auth_status(result.stdout).get("authenticated"))
    except (ValueError, subprocess.TimeoutExpired, OSError):
        connected = False
    return {"connected": connected, "simulated": False, "provider_label": "Stripe Link: TEST MODE",
            "login": _login_view(login) if login and not connected and login.state == "pending" else None}


def disconnect_link(email: str) -> None:
    """Log `email` out of Link (their saved Link login is removed)."""
    if get_settings().payment_mode != "link_test":
        return
    command, env = _link_cli_for(email)
    subprocess.run([*command, "auth", "logout", "--format", "json"], capture_output=True, text=True, env=env,
                   timeout=get_settings().link_timeout_seconds)
    _logins.pop(email, None)


# ============================================================
# The stored card: encrypted at rest, tied to its contract
# ============================================================
#
# The owner decided the funded one-time card is stored on the contract. It
# is stored ENCRYPTED (AES-256-GCM, key HANDSHAKE_CARD_ENCRYPTION_KEY), with
# the contract and funding ids as authenticated data, so a ciphertext copied
# onto another contract fails to decrypt. It is decrypted only at the moment
# of release (agent-visible) or payment (executor), and wiped right after.


def _card_key() -> bytes:
    """The 32-byte AES key from config."""
    import base64

    return base64.b64decode(get_settings().card_encryption_key)


def encrypt_card(card: CardSecret, contract_id: str, funding_id: str) -> str:
    """Encrypt a card for storage. Returns base64(nonce + ciphertext)."""
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    plaintext = json.dumps({"number": card.number, "exp_month": card.exp_month, "exp_year": card.exp_year, "cvc": card.cvc,
                            "valid_until": card.valid_until, "simulated": card.simulated, "brand": card.brand}).encode("utf-8")
    nonce = os.urandom(12)
    sealed = AESGCM(_card_key()).encrypt(nonce, plaintext, f"{contract_id}:{funding_id}".encode("utf-8"))
    return base64.b64encode(nonce + sealed).decode("ascii")


def decrypt_card(blob: str, contract_id: str, funding_id: str) -> CardSecret:
    """Decrypt a stored card (only at release/payment time). Raises PaymentError if it was tampered with or moved."""
    import base64

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.b64decode(blob)
        plaintext = AESGCM(_card_key()).decrypt(raw[:12], raw[12:], f"{contract_id}:{funding_id}".encode("utf-8"))
    except (InvalidTag, ValueError):
        raise PaymentError("card_unreadable", "The stored card could not be decrypted (wrong key, tampered, or moved).")
    data = json.loads(plaintext)
    return CardSecret(number=data["number"], exp_month=int(data["exp_month"]), exp_year=int(data["exp_year"]), cvc=str(data["cvc"]),
                      valid_until=data.get("valid_until"), simulated=bool(data.get("simulated")), brand=data.get("brand"))


# ============================================================
# The merchant side: executor pay + independent order verification
# ============================================================
#
# These calls go to the merchant, so they obey the same SSRF allowlist as the
# extractor, and they reuse its HTTP client factory (tests point it at an
# in-process merchant app).


def merchant_endpoints(checkout_url: str) -> dict[str, str]:
    """Where to pay and where to check orders, for a mock-merchant checkout URL."""
    parts = urlsplit(checkout_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    session_id = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return {
        "pay_url": f"{origin}/api/checkout/{session_id}/pay",
        "session_order_url": f"{origin}/api/checkout/{session_id}/order",
        "order_url_prefix": f"{origin}/api/orders/",
    }


def _merchant_client(settings: Settings) -> httpx.Client:
    """An HTTP client for merchant calls (shared factory with the extractor)."""
    from handshake import extractor

    return extractor.merchant_client_factory(settings)


def pay_merchant(checkout_url: str, card: CardSecret, amount_minor: int, currency: str, settings: Settings | None = None) -> dict[str, Any]:
    """
    Executor mode: submit the card to the merchant's /pay with the EXACT authorized amount.

    Returns the merchant's order. A timeout or dropped connection raises
    PaymentUncertain: we don't know if the charge happened, so the caller
    must look the order up instead of retrying.
    """
    from handshake.extractor import check_url_allowed

    settings = settings or get_settings()
    endpoints = merchant_endpoints(checkout_url)
    check_url_allowed(endpoints["pay_url"], settings)
    body = {
        "card_number": card.number,
        "exp_month": card.exp_month,
        "exp_year": card.exp_year,
        "cvc": card.cvc,
        "amount": float(Decimal(amount_minor) / 100),
        "currency": currency,
    }
    try:
        with _merchant_client(settings) as client:
            response = client.post(endpoints["pay_url"], json=body)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
        raise PaymentUncertain("pay_uncertain", "The merchant did not confirm the payment; its outcome is unknown.")
    finally:
        body.clear()  # drop our copy of the card fields right away
    if response.status_code >= 500:
        raise PaymentUncertain("pay_uncertain", f"The merchant returned HTTP {response.status_code}; the payment outcome is unknown.")
    if response.status_code != 200:
        try:
            code = response.json().get("error", "declined")
        except ValueError:
            code = "declined"
        raise PaymentError(f"merchant_{code}", f"The merchant refused the payment ({code}).")
    return response.json()


def find_session_order(checkout_url: str, settings: Settings | None = None) -> dict[str, Any] | None:
    """Ask the merchant whether this checkout has an order yet (agent-visible mode and uncertain outcomes)."""
    from handshake.extractor import check_url_allowed

    settings = settings or get_settings()
    url = merchant_endpoints(checkout_url)["session_order_url"]
    check_url_allowed(url, settings)
    try:
        with _merchant_client(settings) as client:
            response = client.get(url)
    except httpx.HTTPError:
        raise PaymentUncertain("merchant_unreachable", "The merchant could not be reached to check for an order.")
    if response.status_code != 200:
        raise PaymentUncertain("merchant_unreachable", f"The merchant returned HTTP {response.status_code} for the order check.")
    order = response.json().get("order")
    return order if isinstance(order, dict) else None


def get_order(checkout_url: str, order_id: str, settings: Settings | None = None) -> dict[str, Any]:
    """Independently verify an order by id (never trust an order the agent reported)."""
    from handshake.extractor import check_url_allowed

    settings = settings or get_settings()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,64}", order_id or ""):
        raise PaymentError("invalid_order_id", "Not a valid order id.")
    url = merchant_endpoints(checkout_url)["order_url_prefix"] + order_id
    check_url_allowed(url, settings)
    try:
        with _merchant_client(settings) as client:
            response = client.get(url)
    except httpx.HTTPError:
        raise PaymentUncertain("merchant_unreachable", "The merchant could not be reached to verify the order.")
    if response.status_code != 200:
        raise PaymentError("order_not_found", "The merchant has no such order.")
    return response.json()


# ============================================================
# Link setup CLI (Ajay's stripe.py commands, kept for account setup only)
# ============================================================
#
#   python -m handshake.payments status           is this machine logged in to Link?
#   python -m handshake.payments login            connect a Link account (browser approval)
#   python -m handshake.payments payment-methods  list funding methods
#
# Deliberately NOT here any more: creating or retrieving spend requests. Those
# only happen inside the backend, after Handshake authorizes a purchase, and
# always in test mode. (Ajay's original also said "remove --test to request a
# real card"; that path no longer exists anywhere.)

import argparse  # noqa: E402  (kept next to the CLI it serves)
import subprocess  # noqa: E402
import sys  # noqa: E402


def link_command() -> list[str]:
    """The configured, version-pinned Link CLI invocation."""
    return shlex.split(get_settings().link_cli)


def run_link(arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a Link setup command synchronously (a person is at the keyboard). Arguments as a list, never a shell."""
    return subprocess.run(
        [*link_command(), *arguments, "--format", "json"],
        text=True,
        stdout=subprocess.PIPE if capture else None,
        check=True,
        timeout=get_settings().link_timeout_seconds * 6,
    )


def run_login() -> subprocess.CompletedProcess:
    """
    Log in to Link INTERACTIVELY, on the user's own terminal.

    Deliberately WITHOUT --format json and without capturing output: the CLI
    treats --format as "agent mode" and then collects its streamed updates
    until the end (verified in link-cli 0.23.0), so the approval link and
    code would only appear after login had already timed out. Interactive
    mode prints them immediately.
    """
    return subprocess.run(
        [*link_command(), "auth", "login", "--client-name", "Handshake (test mode)"],
        check=True,
        timeout=get_settings().link_timeout_seconds * 10,
    )


def main(argv: list[str] | None = None) -> int:
    """Link account setup: status, login, payment-methods."""
    parser = argparse.ArgumentParser(description="Handshake: Link TEST MODE account setup")
    parser.add_argument("--email", help="Act as this Handshake user's own Link account (their private Link home)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Check whether this machine is logged in to Link")
    commands.add_parser("login", help="Connect your Link account in the browser")
    commands.add_parser("payment-methods", help="List available funding methods")
    args = parser.parse_args(argv)
    if args.email:
        # The same private HOME the backend uses for this user, so logging in
        # here connects THAT user's Link account (like the website does).
        os.environ["HOME"] = user_link_home(args.email)
        os.environ["npm_config_cache"] = get_settings().link_npm_cache
    try:
        status = parse_auth_status(run_link(["auth", "status"], capture=True).stdout)
        if args.command == "status":
            print("Logged in to Link." if status["authenticated"] else "Not logged in to Link. Run: python -m handshake.payments login")
            return 0
        if not status.get("authenticated"):
            if args.command != "login":
                raise RuntimeError("Not authenticated. Run: python -m handshake.payments login")
            run_login()
            return 0
        if args.command == "login":
            print("Already authenticated with Link.")
        else:
            run_link(["payment-methods", "list"])
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"Link CLI failed (exit {exc.returncode}).", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
