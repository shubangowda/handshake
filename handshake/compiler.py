"""
compiler.py: turn a user's shopping request into a DRAFT contract (Ajay's llm.py, finished).

Job in the system
-----------------
    user intent ("running shoes around $120, size 10, by Friday")
        -> compile_intent()        OpenAI structured output (or the offline fixture)
        -> finalize()              server sets id and created_at itself
        -> lint_draft()            deterministic checks, no LLM
        -> services.store_compiled_draft()   persisted as a DRAFT
        -> the user reviews, edits, and signs it in the frontend

The LLM only DRAFTS. It never decides whether a purchase is authorized; that
is intent_diff.py, pure Python. A draft has no authority until the user signs
it, and a draft with blocking lint errors cannot be signed at all.

What the model sees (and what it never sees)
--------------------------------------------
Only: the user's intent, the configured defaults, and trusted date context
(today's date/time in the user's timezone, so "by Friday" resolves correctly).
Merchant pages, product names, and other web content NEVER go in. A product
titled "Ignore previous rules..." can't talk to the compiler, because it
never reaches it.

Failure is explicit
-------------------
Missing key, provider error, refusal, no parsed output, timeout, and schema
validation errors each raise CompileError with a machine code. A failed
compile never creates an active contract, and never creates a draft that
looks complete.

Offline fixture
---------------
With HANDSHAKE_COMPILER=fixture, or with no OPENAI_API_KEY, a deterministic
compiler recognizes the demo intent (Pegasus 41 running shoes) and returns the
exact demo contract from HANDSHAKE_BUILD.md section 12.1. Anything else gets a
structured "live compiler not configured" error. This keeps the demo alive
when the API is down, and it is labeled in compiler_notes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from handshake.config import get_settings
from handshake.intent_diff import as_utc, normalize_scalar, to_number
from handshake.models import (
    CompilerOutput,
    Constraint,
    ConstraintOperator,
    ContractDraft,
    generate_id,
    utc_now,
)
from handshake.prompts import COMPILER_PROMPT

log = logging.getLogger("handshake.compiler")

# Currencies this deployment can actually pay in. Stripe Link test spend
# requests are made in USD for the demo, so that is the only one we accept.
# Widening this list is a deliberate decision, not a default.
SUPPORTED_CURRENCIES = {"USD"}

# Markers in clarifications_needed / compiler_notes that lint owns. Re-running
# lint (after an edit) replaces its old entries instead of piling up duplicates.
LINT_PREFIX = "lint: "
LINT_NOTE_PREFIX = "lint-note: "
FIXTURE_NOTE = "fixture: produced by the offline demo compiler (HANDSHAKE_COMPILER=fixture or no OPENAI_API_KEY), not by an LLM."


class CompileError(Exception):
    """A compile that produced no usable draft. `code` is machine-readable; `message` is for people."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        """Keep the machine code, human message, and optional details (never secrets)."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# ============================================================
# Trusted context for the model
# ============================================================


def trusted_context(now: datetime, timezone_name: str) -> str:
    """
    The facts we GIVE the model so it can resolve relative dates.

    LLMs don't know what day it is. Without this, "by Friday" would be a
    guess. We state the current date and time in the user's timezone, plus
    Handshake's configured defaults, and nothing from any merchant.
    """
    local = as_utc(now)[0].astimezone(ZoneInfo(timezone_name))
    return (
        "Trusted context from Handshake (not from the user, not from any merchant):\n"
        f"- Current date and time: {local.isoformat()} ({local.strftime('%A')}).\n"
        f"- User timezone: {timezone_name}. Resolve relative dates such as 'by Friday' or 'within 3 days' "
        "in this timezone, as the END of that day, and write datetimes with an explicit UTC offset.\n"
        f"- Supported currencies: {', '.join(sorted(SUPPORTED_CURRENCIES))}.\n"
        "- Handshake defaults: no subscriptions, no memberships, no add-ons, quantity 1 unless stated, "
        "hard_cap_all_in includes item, tax, shipping, and fees.\n"
    )


def user_message(intent: str, now: datetime, timezone_name: str) -> str:
    """The full user-turn text: trusted context, then the user's words, clearly fenced."""
    return (
        trusted_context(now, timezone_name)
        + "\nThe user's shopping request, verbatim, between the markers:\n"
        + "<<<USER_REQUEST\n"
        + intent.strip()
        + "\nUSER_REQUEST>>>"
    )


# ============================================================
# The OpenAI compiler
# ============================================================

_client_cache: dict[tuple[str, float], Any] = {}


def _openai_client(api_key: str, timeout: float) -> Any:
    """
    Build the AsyncOpenAI client lazily, on first use.

    Ajay's llm.py created the client at import time, which meant tests, tool
    discovery, and server startup all needed a key. Now nothing touches
    OpenAI until a compile actually runs.
    """
    key = (api_key, timeout)
    if key not in _client_cache:
        from openai import AsyncOpenAI  # imported here so importing compiler.py stays cheap

        _client_cache[key] = AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=1)
    return _client_cache[key]


def _refusal_text(response: Any) -> str | None:
    """Find a refusal in a Responses API result, if the model declined to answer."""
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return getattr(part, "refusal", None) or "The model refused to compile this request."
    return None


async def compile_with_openai(intent: str, now: datetime, client: Any | None = None) -> CompilerOutput:
    """
    Ask the configured OpenAI model for a CompilerOutput. Raises CompileError on every failure.

    `client` lets tests inject a fake; production builds the real one lazily.
    """
    settings = get_settings()
    if client is None:
        if not settings.openai_api_key:
            raise CompileError("missing_api_key", "OPENAI_API_KEY is not set, so the live compiler can't run.")
        client = _openai_client(settings.openai_api_key, settings.compiler_timeout_seconds)

    request: dict[str, Any] = {
        "model": settings.compiler_model,
        "input": [
            {"role": "system", "content": COMPILER_PROMPT},
            {"role": "user", "content": user_message(intent, now, settings.user_timezone)},
        ],
        "text_format": CompilerOutput,
    }
    # Ajay used service_tier="flex". Flex can be slow or unsupported, so it is
    # opt-in through HANDSHAKE_COMPILER_SERVICE_TIER instead of hardcoded.
    if settings.compiler_service_tier:
        request["service_tier"] = settings.compiler_service_tier

    try:
        # A hard ceiling on top of the SDK's own timeout, so a hung connection
        # can never hang the demo (Ajay's 15 minutes would have).
        response = await asyncio.wait_for(client.responses.parse(**request), timeout=settings.compiler_timeout_seconds + 1)
    except asyncio.TimeoutError:
        raise CompileError("timeout", f"The compiler did not answer within {settings.compiler_timeout_seconds:.0f} seconds.")
    except ValidationError as exc:
        # The SDK validates the model's JSON against CompilerOutput.
        raise CompileError("invalid_output", "The compiler's output did not match the contract schema.", {"errors": exc.errors(include_url=False)[:10]})
    except Exception as exc:  # noqa: BLE001 - every provider failure becomes a structured error
        name = type(exc).__name__
        if "Timeout" in name:
            raise CompileError("timeout", "The compiler request timed out.")
        # Status code and error type only; never echo request contents or keys.
        status = getattr(exc, "status_code", None)
        raise CompileError("provider_error", f"The compiler provider returned an error ({name}).", {"status_code": status, "model": settings.compiler_model})

    refusal = _refusal_text(response)
    if refusal:
        raise CompileError("refusal", "The compiler declined to draft a contract for this request.", {"refusal": refusal[:500]})

    parsed = getattr(response, "output_parsed", None)
    if parsed is None:
        raise CompileError("no_parsed_output", "The compiler returned no structured contract.")
    if not isinstance(parsed, CompilerOutput):
        try:
            parsed = CompilerOutput.model_validate(parsed)
        except ValidationError as exc:
            raise CompileError("invalid_output", "The compiler's output did not match the contract schema.", {"errors": exc.errors(include_url=False)[:10]})
    return parsed


# ============================================================
# The offline demo compiler (fixture)
# ============================================================


def end_of_day_in(days_from_now: int, now: datetime, timezone_name: str) -> datetime:
    """23:59:59 on (today + days) in the user's timezone, as an aware datetime."""
    zone = ZoneInfo(timezone_name)
    local_today = as_utc(now)[0].astimezone(zone).date()
    return datetime.combine(local_today + timedelta(days=days_from_now), time(23, 59, 59), tzinfo=zone)


def demo_contract_draft(now: datetime | None = None) -> ContractDraft:
    """
    The exact demo contract of HANDSHAKE_BUILD.md section 12.1.

    Shared by the fixture compiler and scripts/demo.py so both produce the
    identical contract. No min_return_days: the mock merchant publishes no
    return policy, and a required window would escalate every scenario.
    """
    settings = get_settings()
    now = now or utc_now()
    return ContractDraft.model_validate(
        {
            "id": generate_id("draft"),
            "goal": "Buy Nike Pegasus 41 running shoes",
            "category": "running_shoes",
            "spend": {
                "currency": "USD",
                "target": 120,
                "hard_cap_all_in": 135,
                "target_source": "user",
                "hard_cap_source": "user",
            },
            "delivery": {
                "deliver_by": end_of_day_in(3, now, settings.user_timezone).isoformat(),
                "max_shipping": 10,
                "deliver_by_source": "user",
            },
            "terms": {"no_subscription": True, "no_membership": True, "no_addons": True, "min_return_days": None},
            "merchants": {
                "allow": ["Amazon.com"],
                "deny": [],
                "seller_requirement": "first_party_or_verified",
                "new_merchant": "escalate",
            },
            "constraints": [
                {"field": "size", "operator": "eq", "value": "10", "severity": "hard", "source": "user"},
                {"field": "condition", "operator": "in", "value": ["new"], "severity": "hard", "source": "user"},
                {"field": "product_name", "operator": "contains", "value": "Pegasus 41", "severity": "hard", "source": "user"},
                {"field": "quantity", "operator": "eq", "value": 1, "severity": "hard", "source": "user"},
            ],
            "single_use": True,
            "revocable": True,
            "created_at": now.isoformat(),
        }
    )


DEMO_INTENT = (
    "Buy me Nike Pegasus 41 running shoes, size 10, new. Around $120, but no more than $135 all-in "
    "including tax and shipping. Delivered within 3 days. From Amazon.com. No subscriptions, "
    "memberships, or add-ons."
)


def is_demo_intent(intent: str) -> bool:
    """True for the demo request (Pegasus 41 running shoes). Deliberately narrow."""
    lowered = intent.lower()
    return "pegasus" in lowered or ("running shoe" in lowered and "size 10" in lowered)


def compile_with_fixture(intent: str, now: datetime) -> CompilerOutput:
    """Deterministic offline compiler: the demo contract for the demo intent, a structured error otherwise."""
    if not is_demo_intent(intent):
        raise CompileError(
            "live_compiler_not_configured",
            "The live compiler is not configured, and the offline demo compiler only understands the Pegasus 41 demo request.",
            {"clarifications_needed": ["The live compiler is not configured (set OPENAI_API_KEY). Only the Pegasus 41 demo request can be compiled offline."]},
        )
    return CompilerOutput(
        draft=demo_contract_draft(now),
        assumptions=[
            "The $135 figure is the maximum all-in total, including tax, shipping, and fees.",
            "Delivery 'within 3 days' means by the end of the third day in your timezone.",
            "Quantity is one pair.",
        ],
        clarifications_needed=[],
        compiler_notes=[FIXTURE_NOTE],
    )


# ============================================================
# Entry point used by services.py
# ============================================================


@dataclass
class CompileResult:
    """A successful compile: the finalized output plus which compiler produced it."""

    output: CompilerOutput
    source: str  # "openai" or "fixture"


async def compile_intent(intent: str, now: datetime | None = None, client: Any | None = None) -> CompileResult:
    """Compile an intent with the live model, or the offline fixture when configured or when no key is set."""
    if not intent or not intent.strip():
        raise CompileError("empty_intent", "Describe what you want to buy.")
    if len(intent) > 4000:
        raise CompileError("intent_too_long", "Keep the request under 4000 characters.")

    now = now or utc_now()
    settings = get_settings()
    if client is None and settings.compiler_uses_fixture:
        output = compile_with_fixture(intent, now)
        source = "fixture"
    else:
        output = await compile_with_openai(intent, now, client)
        source = "openai"
    return CompileResult(output=finalize(output, now), source=source)


def finalize(output: CompilerOutput, now: datetime) -> CompilerOutput:
    """
    Server-side overrides: the SERVER sets the draft's id and created_at, never the LLM.

    A model could otherwise pick an id that collides with an existing draft,
    or a created_at in the past to make an expiry look valid. After the
    override we validate through ContractDraft again, so the result is a
    genuine, schema-valid draft.
    """
    data = output.draft.model_dump(mode="json")
    data["id"] = generate_id("draft")
    data["created_at"] = as_utc(now)[0].isoformat()
    try:
        draft = ContractDraft.model_validate(data)
    except (ValidationError, TypeError) as exc:
        details = {"errors": exc.errors(include_url=False)[:10]} if isinstance(exc, ValidationError) else {"error": str(exc)}
        raise CompileError("invalid_output", "The compiled draft is not a valid contract after server checks.", details)
    return output.model_copy(update={"draft": draft})


# ============================================================
# Deterministic lint
# ============================================================


@dataclass
class LintReport:
    """Result of linting a draft: the (possibly timezone-fixed) draft, blocking errors, and notes."""

    draft: ContractDraft
    errors: list[str] = field(default_factory=list)  # block signing
    notes: list[str] = field(default_factory=list)  # informational

    @property
    def blocking(self) -> bool:
        """True if the draft must not be signed yet."""
        return bool(self.errors)


NUMERIC_OPERATORS = {ConstraintOperator.LT, ConstraintOperator.LTE, ConstraintOperator.GT, ConstraintOperator.GTE}
LIST_OPERATORS = {ConstraintOperator.IN, ConstraintOperator.NOT_IN}
DATE_OPERATORS = {ConstraintOperator.BEFORE, ConstraintOperator.AFTER}


def _is_number(value: Any) -> bool:
    """True if the engine would accept this value in lt/lte/gt/gte."""
    if isinstance(value, (bool, list)):
        return False
    try:
        to_number(value)
        return True
    except ValueError:
        return False


def _is_datetime_like(value: Any) -> bool:
    """True if the engine could parse this as a date/time for before/after."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        datetime.fromisoformat(text)
        return True
    except ValueError:
        return False


def _describe(constraint: Constraint) -> str:
    """Short human description of a constraint for lint messages."""
    return f"{constraint.field} {constraint.operator.value} {constraint.value!r}"


def _lint_constraint_shape(constraint: Constraint) -> str | None:
    """Return an error if the operator can't work with the value (the engine would say UNVERIFIABLE forever)."""
    op, value = constraint.operator, constraint.value
    if value is None:
        return f"The rule '{constraint.field} {op.value}' has no value to compare against."
    if op in NUMERIC_OPERATORS and not _is_number(value):
        return f"The rule '{_describe(constraint)}' needs a single number."
    if op in LIST_OPERATORS and not isinstance(value, list):
        return f"The rule '{_describe(constraint)}' should list the allowed values, e.g. [{value!r}]."
    if op in DATE_OPERATORS and not _is_datetime_like(value):
        return f"The rule '{_describe(constraint)}' needs a date like 2026-10-01T23:59:59-04:00."
    return None


def _lint_contradictions(constraints: list[Constraint]) -> list[str]:
    """Find constraints on the same field that can never all be true at once."""
    errors: list[str] = []
    by_field: dict[str, list[Constraint]] = {}
    for constraint in constraints:
        by_field.setdefault(constraint.field, []).append(constraint)

    for field_name, group in by_field.items():
        # Compare normalized values, so "10" and 10 count as the same value.
        equals = {repr(normalize_scalar(c.value)) for c in group if c.operator == ConstraintOperator.EQ and not isinstance(c.value, list)}
        if len(equals) > 1:
            errors.append(f"The contract requires {field_name} to equal several different values at once.")
        for c in group:
            if c.operator == ConstraintOperator.NEQ and repr(normalize_scalar(c.value)) in equals:
                errors.append(f"The contract both requires and forbids {field_name} = {c.value!r}.")
            if c.operator == ConstraintOperator.NOT_IN and isinstance(c.value, list):
                forbidden = {repr(normalize_scalar(v)) for v in c.value}
                if equals & forbidden:
                    errors.append(f"The contract requires a {field_name} value that another rule forbids.")
            if c.operator == ConstraintOperator.IN and isinstance(c.value, list) and equals:
                allowed = {repr(normalize_scalar(v)) for v in c.value}
                if not equals <= allowed:
                    errors.append(f"The required {field_name} is not among the allowed values of another rule.")
    return errors


def lint_draft(draft: ContractDraft, now: datetime | None = None, timezone_name: str | None = None) -> LintReport:
    """
    Deterministic checks on a draft. No LLM, no network. Same draft + same time -> same report.

    Fixes one thing (naive datetimes get the user's timezone, noted) and
    reports the rest as blocking errors for the user to resolve before signing.
    """
    now = as_utc(now or utc_now())[0]
    zone_name = timezone_name or get_settings().user_timezone
    zone = ZoneInfo(zone_name)
    errors: list[str] = []
    notes: list[str] = []

    # --- 1. Datetimes must carry a timezone ---------------------------------
    # A naive deadline is ambiguous by up to a day. We interpret it in the
    # user's timezone (the most likely meaning) and say so, rather than
    # silently assuming UTC the way the engine must at purchase time.
    data = draft.model_dump()
    delivery = data.get("delivery") or None
    if delivery and isinstance(delivery.get("deliver_by"), datetime) and delivery["deliver_by"].tzinfo is None:
        delivery["deliver_by"] = delivery["deliver_by"].replace(tzinfo=zone)
        notes.append(f"The delivery deadline had no timezone; it was interpreted in {zone_name}.")
    if isinstance(data.get("expires_at"), datetime) and data["expires_at"].tzinfo is None:
        data["expires_at"] = data["expires_at"].replace(tzinfo=zone)
        notes.append(f"The contract expiry had no timezone; it was interpreted in {zone_name}.")
    if isinstance(data.get("created_at"), datetime) and data["created_at"].tzinfo is None:
        data["created_at"] = data["created_at"].replace(tzinfo=zone)
    if notes:
        try:
            draft = ContractDraft.model_validate(data)
        except (ValidationError, TypeError) as exc:
            errors.append(f"The draft's dates are inconsistent: {exc}")

    # --- 2. Required fields -------------------------------------------------
    if not draft.goal.strip():
        errors.append("The contract has no goal describing what to buy.")

    # --- 3. Money -----------------------------------------------------------
    spend = draft.spend
    if spend.target is not None and spend.target > spend.hard_cap_all_in:
        errors.append("The target price is above the hard all-in cap.")
    currency = (spend.currency or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        errors.append(f"'{spend.currency}' is not a three-letter currency code.")
    elif currency not in SUPPORTED_CURRENCIES:
        errors.append(f"{currency} is not supported yet; use {', '.join(sorted(SUPPORTED_CURRENCIES))}.")

    # --- 4. Dates must be in the future ------------------------------------
    if draft.delivery and draft.delivery.deliver_by is not None and as_utc(draft.delivery.deliver_by)[0] <= now:
        errors.append("The delivery deadline is already in the past.")
    if draft.expires_at is not None and as_utc(draft.expires_at)[0] <= now:
        errors.append("The contract's expiry time is already in the past.")

    # --- 5. Each constraint's operator must fit its value -------------------
    for constraint in draft.constraints:
        problem = _lint_constraint_shape(constraint)
        if problem:
            errors.append(problem)

    # --- 6. Constraints must not contradict each other ----------------------
    errors.extend(_lint_contradictions(draft.constraints))

    # A merchant can't be both allowed and denied (the model's own check is
    # case-sensitive; this one isn't), nor required by a rule and denied.
    allow = {m.strip().lower() for m in draft.merchants.allow}
    deny = {m.strip().lower() for m in draft.merchants.deny}
    for merchant in sorted(allow & deny):
        errors.append(f"The merchant '{merchant}' is both allowed and denied.")
    for constraint in draft.constraints:
        if constraint.field == "merchant_name" and constraint.operator == ConstraintOperator.EQ:
            if str(constraint.value).strip().lower() in deny:
                errors.append(f"The contract requires merchant '{constraint.value}', which is also on the deny list.")

    # De-duplicate while keeping order, so messages read cleanly.
    return LintReport(draft=draft, errors=list(dict.fromkeys(errors)), notes=list(dict.fromkeys(notes)))


def merge_lint(meta: dict[str, Any], report: LintReport) -> dict[str, Any]:
    """Replace lint's previous entries in the draft metadata with the new report's entries."""
    clarifications = [c for c in meta.get("clarifications_needed", []) if not c.startswith(LINT_PREFIX)]
    notes = [n for n in meta.get("compiler_notes", []) if not n.startswith(LINT_NOTE_PREFIX)]
    return {
        **meta,
        "clarifications_needed": clarifications + [LINT_PREFIX + e for e in report.errors],
        "compiler_notes": notes + [LINT_NOTE_PREFIX + n for n in report.notes],
    }
