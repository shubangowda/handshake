"""
intent_diff.py: the deterministic rule engine ("Intent Diff").

Job in the system
-----------------
The user signs a Contract (what they are willing to buy). Later the shopping
agent proposes a checkout, and an independent extractor turns that checkout
into a TransactionProposal (structured facts). This file compares the two and
answers one question: "does this checkout match what the user signed?"

    main.py  (HTTP routes)
      -> services.py  (purchase flow, database, evidence ledger)
           -> intent_diff.evaluate(contract, proposal)   <-- this file
           <- (ValidationDecision, PurchaseStatus outcome)

How it connects
---------------
- It imports only the shared Pydantic models from models.py.
- It never touches the database, the network, FastAPI, or an LLM. That makes
  it a set of pure functions: same inputs (contract, proposal, fields-set,
  "now") always give the same results in the same order. That property is
  what lets us trust it with money.
- services.py calls evaluate() and then decides what to store and log.

The three rules this file lives by
----------------------------------
1. No LLM or randomness decides anything. It is plain Python comparisons.
2. Fail closed. If a fact is missing, ambiguous, the wrong type, or cannot
   be parsed, the verdict is UNVERIFIABLE, never PASS. A missing field must
   never look like a passing field. UNVERIFIABLE on a hard rule escalates
   the purchase to the human instead of spending their money.
3. No rule may crash the engine. Every rule runs inside a try/except that
   turns an unexpected error into UNVERIFIABLE for that rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable

from app.models import (
    Constraint,
    ConstraintOperator,
    ConstraintResult,
    ConstraintSeverity,
    ConstraintVerdict,
    Contract,
    MerchantPolicyAction,
    PurchaseStatus,
    SellerRequirement,
    TransactionProposal,
    ValidationDecision,
    utc_now,
)

# ------------------------------------------------------------
# STRICT_EXPLICIT_TERMS
# ------------------------------------------------------------
# TransactionProposal (in models.py) declares:
#     addons_detected: bool = False
#     membership_detected: bool = False
#     recurring_billing.detected: bool = False
# So if the extractor simply forgets to send those fields, Pydantic fills in
# False, which reads as "we checked and found nothing". A naive rule would
# then PASS a checkout that nobody inspected.
#
# With this flag on (the default), a term flag that was NOT explicitly present
# in the incoming JSON is treated as missing -> UNVERIFIABLE -> ESCALATED.
# The same logic applies to line item `quantity` (which silently defaults to 1)
# when a generic constraint checks quantity.
#
# Only turn this off if you fully trust the extractor to always send them.
STRICT_EXPLICIT_TERMS = True

# Short aliases so the rule code below reads more like English.
HARD = ConstraintSeverity.HARD
SOFT = ConstraintSeverity.SOFT
PASS = ConstraintVerdict.PASS
FAIL = ConstraintVerdict.FAIL
UNVERIFIABLE = ConstraintVerdict.UNVERIFIABLE


class _Missing:
    """
    A unique "no value" marker.

    We cannot use None to mean "missing" because None is sometimes a real
    value in the models. A dedicated object is unambiguous: `value is MISSING`
    can only be true when a lookup genuinely found nothing.
    """

    def __repr__(self) -> str:
        """Print as the word 'missing' so it shows up nicely in reasons."""
        return "missing"


MISSING = _Missing()


# ============================================================
# Money: always integer cents, never float comparison
# ============================================================
#
# Why not compare floats? Floats are binary fractions, and most decimal
# amounts cannot be represented exactly. Classic example:
#     0.1 + 0.2 == 0.30000000000000004   (not 0.3)
# So "is 128.39 <= 135.00?" might work, but "is 134.99 + 0.01 <= 135.00?"
# can give the wrong answer. With money, a wrong answer is a wrong charge.
#
# The fix: at the edge of every rule, convert each amount to a whole number
# of cents (an int) and compare ints. Ints are exact.
#
# To convert we go through Decimal(str(amount)), not Decimal(amount).
# str(128.39) is "128.39", which is what the human meant. Decimal(128.39)
# would capture the float's binary error (128.3899999999999863575...).
# Then we round half up (0.125 -> 13 cents), which is how people expect
# money to round.


def to_cents(amount: Any) -> int:
    """Convert a money amount (float, int, str, or Decimal) to exact integer cents."""
    # True/False are technically ints in Python (True == 1). A boolean is
    # never a price, so reject it instead of silently treating it as $0.01.
    if isinstance(amount, bool):
        raise ValueError(f"{amount!r} is not a money amount")

    if isinstance(amount, Decimal):
        value = amount
    else:
        try:
            value = Decimal(str(amount).strip())
        except (InvalidOperation, ValueError):
            raise ValueError(f"{amount!r} is not a money amount")

    # Decimal accepts "NaN" and "Infinity"; neither is a price.
    if not value.is_finite():
        raise ValueError(f"{amount!r} is not a finite money amount")

    cents = (value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def cents_to_amount(cents: int) -> float:
    """Convert integer cents back to a float amount for the models (13500 -> 135.0), via exact Decimal division."""
    return float(Decimal(cents) / Decimal(100))


# ------------------------------------------------------------
# The payable total: never trust a claimed total that is too low
# ------------------------------------------------------------
# models.py's validate_total lets the claimed `total` differ from
#     item_subtotal + tax + shipping + fees - discounts
# by up to 2 cents. So a checkout whose parts really add up to 135.01 can
# claim a total of 135.00, pass the model, and slip under a 135.00 cap. The
# merchant would then charge the real 135.01.
#
# Fix: wherever the total decides something about money (the cap check, the
# authorized amount, the credential's max_amount), use the LARGER of the two
# numbers. If the claim is lower than the parts, we believe the parts. If the
# claim is higher, we believe the claim. Either way we never under-count.


def computed_total_cents(proposal: TransactionProposal) -> int:
    """What the total SHOULD be, in cents: item_subtotal + tax + shipping + fees - discounts."""
    return (
        to_cents(proposal.item_subtotal)
        + to_cents(proposal.tax)
        + to_cents(proposal.shipping)
        + to_cents(proposal.fees)
        - to_cents(proposal.discounts)
    )


def payable_total_cents(proposal: TransactionProposal) -> int:
    """The amount we treat as what will actually be paid: max(claimed total, computed total), in cents."""
    claimed = to_cents(proposal.total)
    computed = computed_total_cents(proposal)
    if computed > claimed:
        return computed
    return claimed


def fmt_money(cents: int, currency: str | None = None) -> str:
    """Format integer cents for humans, e.g. 12839 -> '128.39 USD'."""
    sign = "-" if cents < 0 else ""
    dollars = abs(cents) // 100
    remainder = abs(cents) % 100
    text = f"{sign}{dollars}.{remainder:02d}"
    if currency:
        return f"{text} {currency.upper()}"
    return text


# ============================================================
# Strings: trim and lowercase before comparing
# ============================================================


def norm_str(value: Any) -> str:
    """Trim and lowercase so 'Mock Nike', 'mock nike', and ' MOCK NIKE ' compare equal."""
    # Enums (like ProductCondition.NEW) are compared by their value ("new").
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip().lower()


# ============================================================
# Datetimes: normalize everything to timezone-aware UTC
# ============================================================
#
# Python has two kinds of datetime:
#   - "aware": carries a timezone, e.g. 2026-10-10T23:59:59+00:00
#   - "naive": no timezone at all, e.g. 2026-10-10T23:59:59
# Comparing a naive one with an aware one raises TypeError. The contract's
# deliver_by and the proposal's promised_by can each arrive either way, so a
# direct comparison could crash the engine mid-evaluation.
#
# Fix: every datetime goes through as_utc() before any comparison. Aware
# values are converted to UTC; naive values are ASSUMED to be UTC. We return
# a flag saying whether we assumed, so the rule can record that assumption in
# its evidence and a human can see it.


def as_utc(dt: datetime) -> tuple[datetime, bool]:
    """Return (dt as aware UTC, was_naive). Naive inputs are assumed to already be UTC."""
    is_naive = dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None
    if is_naive:
        return dt.replace(tzinfo=timezone.utc), True
    return dt.astimezone(timezone.utc), False


def parse_datetime(value: Any) -> tuple[datetime, bool]:
    """Parse a datetime object or ISO 8601 string into aware UTC; raise ValueError if impossible."""
    if isinstance(value, datetime):
        return as_utc(value)

    if isinstance(value, str) and value.strip():
        text = value.strip()
        # Some Python versions reject a trailing "Z" (Zulu = UTC); spell it out.
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        return as_utc(datetime.fromisoformat(text))

    raise ValueError(f"{value!r} is not a datetime")


# ============================================================
# Numbers and generic value normalization
# ============================================================


def to_number(value: Any) -> Decimal:
    """Convert a value to an exact Decimal for numeric comparison; raise ValueError if it is not a number."""
    # Booleans, None, lists, and dicts are never numbers, even though
    # Python would happily treat True as 1.
    if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
        raise ValueError(f"{value!r} is not a number")

    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{value!r} is not a number")

    if not number.is_finite():
        raise ValueError(f"{value!r} is not a finite number")
    return number


# Strings we accept as booleans in eq/neq comparisons.
_TRUE_STRINGS = {"true", "yes"}
_FALSE_STRINGS = {"false", "no"}


def normalize_scalar(value: Any) -> Any:
    """
    Turn a single value into a canonical form for equality checks.

    The result is one of: bool, Decimal, aware-UTC datetime, or a trimmed
    lowercase string. After normalizing, "10", 10, and 10.0 all become
    Decimal("10"), so they compare equal. "Yes" and True both become True.
    """
    if isinstance(value, Enum):
        value = value.value

    # Check bool before numbers, because bool is a subclass of int.
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float, Decimal)):
        return to_number(value)

    if isinstance(value, datetime):
        return as_utc(value)[0]

    text = str(value).strip().lower()
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False

    # A string that looks like a number ("10", " 10.0 ") becomes that number.
    try:
        return to_number(text)
    except ValueError:
        return text


def display(value: Any) -> str:
    """Render a value as a short human-readable string for the expected/actual fields."""
    if value is MISSING or value is None:
        return "missing"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime):
        return as_utc(value)[0].isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(display(v) for v in value) + "]"
    return str(value)


def aggregate(verdicts: list[ConstraintVerdict]) -> ConstraintVerdict:
    """
    Combine several verdicts (e.g. one per line item) into one.

    FAIL beats UNVERIFIABLE beats PASS: one bad line item fails the whole
    constraint. An empty list means nothing was checked, and "nothing was
    checked" must never mean PASS, so it is UNVERIFIABLE.
    """
    if not verdicts:
        return UNVERIFIABLE
    if FAIL in verdicts:
        return FAIL
    if UNVERIFIABLE in verdicts:
        return UNVERIFIABLE
    return PASS


# ============================================================
# Detecting fields that were never sent (the model_fields_set trick)
# ============================================================
#
# Every Pydantic v2 model instance has `.model_fields_set`: the set of field
# names that were actually present in the input, as opposed to filled in from
# a default. Example:
#
#     TransactionProposal.model_validate({... no "addons_detected" ...})
#     proposal.addons_detected                          -> False (the default)
#     "addons_detected" in proposal.model_fields_set    -> False (never sent!)
#
# That second line is how we tell "the extractor checked and said False"
# apart from "the extractor never looked". Nested models have their own
# model_fields_set, so we build dotted paths like "recurring_billing.detected".
#
# IMPORTANT: this only works on a proposal freshly parsed from the raw
# request. Once we save it to the database and load it back, every field is
# present in the JSON and so every field looks "set". That is why
# services.py stores this set alongside the proposal and passes it to
# evaluate() as `raw_fields_set`.


def explicit_fields(proposal: TransactionProposal) -> set[str]:
    """Return dotted paths of the fields that were explicitly present in the parsed payload."""
    paths: set[str] = set(proposal.model_fields_set)

    # recurring_billing is itself a model. If the whole object was omitted,
    # none of its inner fields were sent, so we add nothing for it.
    if "recurring_billing" in proposal.model_fields_set:
        for name in proposal.recurring_billing.model_fields_set:
            paths.add(f"recurring_billing.{name}")

    # Each line item tracks its own fields (we care about `quantity`).
    for index, item in enumerate(proposal.line_items):
        for name in item.model_fields_set:
            paths.add(f"line_items[{index}].{name}")

    return paths


# ============================================================
# Evaluation context and the result helper
# ============================================================


@dataclass(frozen=True)
class Ctx:
    """Everything a rule needs, bundled so every rule has the same signature: rule(ctx)."""

    contract: Contract
    proposal: TransactionProposal
    now: datetime
    fields_set: set[str]

    def explicit(self, path: str) -> bool:
        """True if `path` was explicitly sent (or if strict mode is off and we trust defaults)."""
        if not STRICT_EXPLICIT_TERMS:
            return True
        return path in self.fields_set


def result(
    name: str,
    verdict: ConstraintVerdict,
    expected: Any,
    actual: Any,
    reason: str,
    evidence: dict[str, Any] | None = None,
    severity: ConstraintSeverity = HARD,
) -> ConstraintResult:
    """Build one ConstraintResult. Every field is filled so the frontend can display it directly."""
    return ConstraintResult(
        constraint=name,  # stable machine name, e.g. "hard_cap_all_in"
        verdict=verdict,
        expected=expected,  # what the contract required, as a readable string
        actual=actual,  # what the checkout showed, or "missing"
        reason=reason,  # one plain-English sentence for the UI
        severity=severity,
        evidence=evidence or {},  # which proposal field the value came from
    )


# ============================================================
# Built-in rules
# ============================================================
#
# Each rule is a small function: rule(ctx) -> list[ConstraintResult].
# It returns [] when the rule does not apply to this contract (for example,
# no delivery deadline was signed), and usually one result otherwise.


def rule_currency_match(ctx: Ctx) -> list[ConstraintResult]:
    """Hard: the checkout must be priced in the currency the contract was signed in."""
    expected = ctx.contract.spend.currency.strip().upper()
    actual = (ctx.proposal.currency or "").strip().upper()
    ev = {"source": "proposal.currency", "contract_field": "spend.currency"}

    if actual == expected:
        reason = f"Checkout currency {actual} matches the signed currency."
        return [result("currency_match", PASS, expected, actual, reason, ev)]

    # A cap of "135" means nothing if the checkout is in a different currency.
    reason = f"Checkout is priced in {actual} but the contract was signed for {expected}."
    return [result("currency_match", FAIL, expected, actual, reason, ev)]


def rule_subtotal_integrity(ctx: Ctx) -> list[ConstraintResult]:
    """Hard: item_subtotal must equal the sum of unit_price x quantity over the line items (within 1 cent)."""
    # models.py never checks item_subtotal against the line items, so an
    # extractor bug (or a merchant trick) could claim a subtotal that does
    # not match what is actually in the cart. We recompute it ourselves.
    p = ctx.proposal

    line_cents: list[int] = []
    for item in p.line_items:
        # Multiply in Decimal (exact), then convert to cents once.
        line_total = Decimal(str(item.unit_price)) * item.quantity
        line_cents.append(to_cents(line_total))

    computed = sum(line_cents)
    claimed = to_cents(p.item_subtotal)
    difference = claimed - computed

    ev = {
        "source": "proposal.item_subtotal",
        "line_item_subtotals": [fmt_money(c) for c in line_cents],
        "difference_cents": difference,
    }
    expected = f"{fmt_money(computed, p.currency)} (sum of line items)"
    actual = fmt_money(claimed, p.currency)

    # Allow 1 cent for per-line rounding differences between systems.
    if abs(difference) <= 1:
        return [result("subtotal_integrity", PASS, expected, actual, "Item subtotal matches the sum of the line items.", ev)]

    reason = (
        f"Checkout claims an item subtotal of {actual} but the line items add up to "
        f"{fmt_money(computed, p.currency)}."
    )
    return [result("subtotal_integrity", FAIL, expected, actual, reason, ev)]


def rule_hard_cap_all_in(ctx: Ctx) -> list[ConstraintResult]:
    """Hard: the final payable total must be <= the signed all-in cap. Strict, no tolerance."""
    spend = ctx.contract.spend
    claimed = to_cents(ctx.proposal.total)
    computed = computed_total_cents(ctx.proposal)
    # The larger of the two (see payable_total_cents): a claimed total that
    # is a cent or two below its own parts cannot sneak under the cap.
    total = payable_total_cents(ctx.proposal)
    cap = to_cents(spend.hard_cap_all_in)

    # Show the full breakdown so a human can see where the money went.
    ev: dict[str, Any] = {
        "source": "max(proposal.total, computed total)",
        "claimed_total": fmt_money(claimed),
        "computed_total": fmt_money(computed),
        "includes": list(spend.includes),
        "breakdown": {
            "item_subtotal": fmt_money(to_cents(ctx.proposal.item_subtotal)),
            "tax": fmt_money(to_cents(ctx.proposal.tax)),
            "shipping": fmt_money(to_cents(ctx.proposal.shipping)),
            "fees": fmt_money(to_cents(ctx.proposal.fees)),
            "discounts": fmt_money(to_cents(ctx.proposal.discounts)),
        },
    }
    # The target ("I'd like to pay about 120") is shown for context only.
    # It never affects authorization; only the hard cap does.
    if spend.target is not None:
        ev["target"] = fmt_money(to_cents(spend.target), spend.currency)
        ev["target_note"] = "Target is informational only and never affects authorization."

    expected = f"<= {fmt_money(cap, spend.currency)}"
    actual = fmt_money(total, ctx.proposal.currency)

    # Integer comparison: exact. 13501 <= 13500 is False, no float surprises.
    if total <= cap:
        return [result("hard_cap_all_in", PASS, expected, actual, "Final payable total is within signed cap.", ev)]

    over_by = fmt_money(total - cap, spend.currency)
    reason = (
        f"Final payable total {actual} exceeds the signed all-in cap of "
        f"{fmt_money(cap, spend.currency)} by {over_by}."
    )
    # Make the "sneaky rounding" case explicit so nobody is confused about
    # why a checkout that SAYS 135.00 was blocked by a 135.00 cap.
    if computed > claimed:
        reason += (
            f" (The checkout claimed {fmt_money(claimed, ctx.proposal.currency)}, but its subtotal, "
            f"tax, shipping, fees, and discounts add up to {actual}.)"
        )
    return [result("hard_cap_all_in", FAIL, expected, actual, reason, ev)]


def rule_max_shipping(ctx: Ctx) -> list[ConstraintResult]:
    """Hard (only if signed): shipping must not exceed delivery.max_shipping."""
    delivery = ctx.contract.delivery
    if delivery is None or delivery.max_shipping is None:
        return []  # contract said nothing about shipping cost

    limit = to_cents(delivery.max_shipping)
    shipping = to_cents(ctx.proposal.shipping)
    currency = ctx.proposal.currency
    ev = {"source": "proposal.shipping", "contract_field": "delivery.max_shipping"}
    expected = f"<= {fmt_money(limit, currency)}"
    actual = fmt_money(shipping, currency)

    if shipping <= limit:
        return [result("max_shipping", PASS, expected, actual, "Shipping cost is within the signed maximum.", ev)]

    reason = f"Shipping costs {actual}, more than the signed maximum of {fmt_money(limit, currency)}."
    return [result("max_shipping", FAIL, expected, actual, reason, ev)]


def rule_contract_category(ctx: Ctx) -> list[ConstraintResult]:
    """Hard (only if signed): every line item must be in the contract's category."""
    if not ctx.contract.category:
        return []

    expected = norm_str(ctx.contract.category)
    verdicts: list[ConstraintVerdict] = []
    actuals: list[str] = []
    sources: list[str] = []
    mismatched: list[str] = []
    missing: list[str] = []

    # resolve_field returns one (value, source) pair per line item.
    for value, source in resolve_field("category", ctx):
        sources.append(source)
        actuals.append(display(value))

        if value is MISSING:
            # Fail closed: we cannot prove this item is in the right category.
            verdicts.append(UNVERIFIABLE)
            missing.append(source)
        elif norm_str(value) == expected:
            verdicts.append(PASS)
        else:
            verdicts.append(FAIL)
            mismatched.append(repr(display(value)))

    # If one item is the wrong category and another is missing, FAIL wins.
    verdict = aggregate(verdicts)
    if verdict == FAIL:
        reason = f"Checkout contains items in category {', '.join(mismatched)}, but the contract only allows {expected!r}."
    elif verdict == UNVERIFIABLE:
        reason = f"Category could not be verified for {len(missing)} line item(s); the contract requires {expected!r}."
    else:
        reason = f"Every line item is in the signed category {expected!r}."

    return [result("contract_category", verdict, expected, ", ".join(actuals), reason, {"sources": sources})]


def _explicit_flag_rule(
    ctx: Ctx,
    name: str,
    path: str,
    detected: bool,
    label: str,
    detail: str = "",
) -> list[ConstraintResult]:
    """
    Shared logic for the three "no X" terms rules (subscription, membership, add-ons).

    Order matters:
      1. detected=True             -> FAIL (it is there, and it is forbidden)
      2. field never sent (strict) -> UNVERIFIABLE (nobody looked)
      3. explicitly False          -> PASS
    """
    expected = f"no {label}"
    ev = {"source": f"proposal.{path}", "explicitly_provided": path in ctx.fields_set}
    # "an add-on" but "a membership": pick the article so the UI sentence reads correctly.
    article = "an" if label[0].lower() in "aeiou" else "a"

    if detected:
        actual = f"{label} detected{detail}"
        reason = f"Checkout includes {article} {label}{detail}, which the contract forbids."
        return [result(name, FAIL, expected, actual, reason, ev)]

    # This is the fail-closed check from STRICT_EXPLICIT_TERMS. The value is
    # False, but only because models.py defaulted it. Not proof of anything.
    if not ctx.explicit(path):
        reason = f"The extractor did not report whether the checkout includes {article} {label}, so it cannot be ruled out."
        return [result(name, UNVERIFIABLE, expected, "missing", reason, ev)]

    return [result(name, PASS, expected, f"no {label} detected", f"No {label} was detected at checkout.", ev)]


def rule_no_subscription(ctx: Ctx) -> list[ConstraintResult]:
    """Hard (if terms.no_subscription): no recurring billing may be attached to this checkout."""
    if not ctx.contract.terms.no_subscription:
        return []

    billing = ctx.proposal.recurring_billing

    # If a subscription was detected, add its interval and amount to the
    # message, e.g. " (monthly, 9.99)", so the user sees exactly what it was.
    detail = ""
    if billing.detected:
        parts: list[str] = []
        if billing.interval:
            parts.append(billing.interval)
        if billing.amount is not None:
            parts.append(fmt_money(to_cents(billing.amount)))
        if parts:
            detail = f" ({', '.join(parts)})"

    return _explicit_flag_rule(
        ctx, "no_subscription", "recurring_billing.detected", billing.detected, "recurring subscription", detail
    )


def rule_no_membership(ctx: Ctx) -> list[ConstraintResult]:
    """Hard (if terms.no_membership): no membership sign-up may be attached."""
    if not ctx.contract.terms.no_membership:
        return []
    return _explicit_flag_rule(
        ctx, "no_membership", "membership_detected", ctx.proposal.membership_detected, "membership"
    )


def rule_no_addons(ctx: Ctx) -> list[ConstraintResult]:
    """Hard (if terms.no_addons): no add-ons (warranties, protection plans, ...) may be attached."""
    if not ctx.contract.terms.no_addons:
        return []
    return _explicit_flag_rule(ctx, "no_addons", "addons_detected", ctx.proposal.addons_detected, "add-on")


def rule_min_return_days(ctx: Ctx) -> list[ConstraintResult]:
    """Hard (if terms.min_return_days): the return window must be at least that many days."""
    required = ctx.contract.terms.min_return_days
    if required is None:
        return []

    terms = ctx.proposal.return_terms
    expected = f">= {required} days"
    ev = {"source": "proposal.return_terms", "evidence": terms.evidence if terms else None}

    # "Not returnable at all" is a definite FAIL, even if the window is missing.
    if terms is not None and terms.returnable is False:
        reason = f"The item is not returnable, but the contract requires at least {required} days to return it."
        return [result("min_return_days", FAIL, expected, "not returnable", reason, ev)]

    # No return info -> we cannot prove the policy is good enough -> UNVERIFIABLE.
    if terms is None or terms.return_window_days is None:
        reason = f"The return window was not found at checkout; the contract requires at least {required} days."
        return [result("min_return_days", UNVERIFIABLE, expected, "missing", reason, ev)]

    window = terms.return_window_days
    if window < required:
        reason = f"The return window is only {window} days, shorter than the required {required} days."
        return [result("min_return_days", FAIL, expected, f"{window} days", reason, ev)]

    reason = f"The {window}-day return window meets the required {required} days."
    return [result("min_return_days", PASS, expected, f"{window} days", reason, ev)]


def rule_delivery_deadline(ctx: Ctx) -> list[ConstraintResult]:
    """Hard (if delivery.deliver_by is signed): promised delivery must be on or before the deadline."""
    policy = ctx.contract.delivery
    if policy is None or policy.deliver_by is None:
        return []

    # Normalize the deadline to aware UTC so the comparison below cannot crash.
    deadline, deadline_was_naive = as_utc(policy.deliver_by)
    expected = f"by {deadline.isoformat()}"
    ev: dict[str, Any] = {"source": "proposal.delivery.promised_by"}
    if deadline_was_naive:
        ev["deliver_by_timezone"] = "naive datetime, assumed UTC"

    delivery = ctx.proposal.delivery
    if delivery is None or delivery.promised_by is None:
        reason = f"No delivery date was found at checkout; the contract requires delivery by {deadline.date().isoformat()}."
        return [result("delivery_deadline", UNVERIFIABLE, expected, "missing", reason, ev)]

    promised, promised_was_naive = as_utc(delivery.promised_by)
    if promised_was_naive:
        ev["promised_by_timezone"] = "naive datetime, assumed UTC"
    ev["carrier"] = delivery.carrier
    ev["verified"] = delivery.verified
    ev["evidence"] = delivery.evidence
    actual = promised.isoformat()

    # Both are aware UTC now, so this comparison is safe and correct.
    if promised > deadline:
        reason = (
            f"Checkout promises delivery on {promised.date().isoformat()}, "
            f"after the signed deadline of {deadline.date().isoformat()}."
        )
        return [result("delivery_deadline", FAIL, expected, actual, reason, ev)]

    # The date is fine, but if the user demanded a verified estimate and this
    # one is not verified, we cannot trust it.
    if policy.require_verified_estimate and not delivery.verified:
        reason = "The contract requires a verified delivery estimate, but the promised date is not verified."
        return [result("delivery_deadline", UNVERIFIABLE, expected, f"{actual} (unverified)", reason, ev)]

    return [result("delivery_deadline", PASS, expected, actual, "Promised delivery date meets the signed deadline.", ev)]


def rule_merchant_policy(ctx: Ctx) -> list[ConstraintResult]:
    """Hard: deny list -> FAIL, allow list -> PASS, otherwise apply the contract's new_merchant action."""
    policy = ctx.contract.merchants
    merchant = ctx.proposal.merchant

    # We match on both the display name and the domain, normalized.
    identifiers: set[str] = set()
    for raw in (merchant.name, merchant.domain):
        if raw and norm_str(raw):
            identifiers.add(norm_str(raw))

    allow = {norm_str(v) for v in policy.allow}
    deny = {norm_str(v) for v in policy.deny}

    actual = merchant.name
    if merchant.domain:
        actual += f" ({merchant.domain})"
    expected = f"allow={policy.allow}, deny={policy.deny}, new_merchant={policy.new_merchant.value}"
    ev = {"source": "proposal.merchant.name/domain", "matched_against": sorted(identifiers)}

    # Deny is checked FIRST so that a merchant somehow on both lists is denied.
    if identifiers & deny:
        reason = f"Merchant {merchant.name!r} is on the contract's deny list."
        return [result("merchant_policy", FAIL, expected, actual, reason, ev)]

    if identifiers & allow:
        reason = f"Merchant {merchant.name!r} is on the approved merchant list."
        return [result("merchant_policy", PASS, expected, actual, reason, ev)]

    # Not on either list (this includes the case where both lists are empty):
    # it is a "new merchant", and the contract says what to do.
    action = policy.new_merchant
    if action == MerchantPolicyAction.ALLOW:
        reason = f"Merchant {merchant.name!r} is not listed, and the contract allows new merchants."
        return [result("merchant_policy", PASS, expected, actual, reason, ev)]
    if action == MerchantPolicyAction.DENY:
        reason = f"Merchant {merchant.name!r} is not on the approved list, and the contract denies new merchants."
        return [result("merchant_policy", FAIL, expected, actual, reason, ev)]

    # ESCALATE: not a failure, but not provable either -> ask the human.
    reason = f"Merchant {merchant.name!r} is not on the approved merchant list, so the user must approve it."
    return [result("merchant_policy", UNVERIFIABLE, expected, actual, reason, ev)]


def _seller_flag_result(
    flag: bool | None, label: str, expected: str, actual: str, ev: dict[str, Any]
) -> list[ConstraintResult]:
    """Turn one tri-state seller flag (True / False / None=unknown) into PASS / FAIL / UNVERIFIABLE."""
    if flag is True:
        return [result("seller_requirement", PASS, expected, actual, f"Seller is {label}, as required.", ev)]
    if flag is False:
        return [result("seller_requirement", FAIL, expected, actual, f"Seller is not {label}, which the contract requires.", ev)]
    # None means the extractor could not tell. Unknown is not a pass.
    return [result("seller_requirement", UNVERIFIABLE, expected, actual, f"Could not determine whether the seller is {label}.", ev)]


def rule_seller_requirement(ctx: Ctx) -> list[ConstraintResult]:
    """Hard: the seller must satisfy the contract's seller_requirement (first party / verified / either / any)."""
    requirement = ctx.contract.merchants.seller_requirement
    m = ctx.proposal.merchant
    expected = requirement.value
    actual = f"first_party={display(m.is_first_party)}, verified={display(m.is_verified)}"
    ev = {"source": "proposal.merchant.is_first_party/is_verified", "seller_of_record": m.seller_of_record}

    if requirement == SellerRequirement.ANY:
        return [result("seller_requirement", PASS, expected, actual, "Contract accepts any seller.", ev)]

    if requirement == SellerRequirement.FIRST_PARTY:
        return _seller_flag_result(m.is_first_party, "first-party", expected, actual, ev)

    if requirement == SellerRequirement.VERIFIED:
        return _seller_flag_result(m.is_verified, "verified", expected, actual, ev)

    # FIRST_PARTY_OR_VERIFIED:
    #   either one explicitly True        -> PASS
    #   both explicitly False             -> FAIL
    #   anything else (some None/unknown) -> UNVERIFIABLE
    if m.is_first_party is True or m.is_verified is True:
        return [result("seller_requirement", PASS, expected, actual, "Seller is first-party or verified, as required.", ev)]
    if m.is_first_party is False and m.is_verified is False:
        return [result("seller_requirement", FAIL, expected, actual, "Seller is neither first-party nor verified.", ev)]
    return [result("seller_requirement", UNVERIFIABLE, expected, actual, "Could not determine whether the seller is first-party or verified.", ev)]


def rule_extractor_agreement(ctx: Ctx) -> list[ConstraintResult]:
    """Hard UNVERIFIABLE if extractors explicitly disagreed; soft informational PASS if not reported."""
    p = ctx.proposal
    ev = {"source": "proposal.extractors_agreed", "extractor_ids": list(p.extractor_ids)}

    # extractors_agreed defaults to True in models.py. If nobody sent it, we
    # do not know, but single-extractor setups are normal at the hackathon,
    # so we record a SOFT note (display only) instead of blocking.
    if "extractors_agreed" not in ctx.fields_set:
        return [result("extractor_agreement", PASS, "extractors agree", "not reported", "Extractor agreement was not reported (informational only).", ev, SOFT)]

    # Two independent readers of the checkout page disagreed: we cannot
    # trust either one, so a human must look.
    if p.extractors_agreed is False:
        return [result("extractor_agreement", UNVERIFIABLE, "extractors agree", "extractors disagreed", "Independent checkout extractors disagreed about the facts, so they cannot be trusted.", ev)]

    return [result("extractor_agreement", PASS, "extractors agree", "extractors agreed", "Independent checkout extractors agreed on the facts.", ev)]


# The fixed, documented order in which built-in rules run. Changing this
# order changes the order of results, so tests pin it down.
BUILTIN_RULES: list[tuple[str, Callable[[Ctx], list[ConstraintResult]]]] = [
    ("currency_match", rule_currency_match),
    ("subtotal_integrity", rule_subtotal_integrity),
    ("hard_cap_all_in", rule_hard_cap_all_in),
    ("max_shipping", rule_max_shipping),
    ("contract_category", rule_contract_category),
    ("no_subscription", rule_no_subscription),
    ("no_membership", rule_no_membership),
    ("no_addons", rule_no_addons),
    ("min_return_days", rule_min_return_days),
    ("delivery_deadline", rule_delivery_deadline),
    ("merchant_policy", rule_merchant_policy),
    ("seller_requirement", rule_seller_requirement),
    ("extractor_agreement", rule_extractor_agreement),
]


# ============================================================
# Field resolution for generic constraints
# ============================================================
#
# A generic Constraint names a `field` like "size" or "total_price". The
# resolver's job is to find the matching value(s) in the proposal and say
# where each one came from (the "source path"), so the evidence can point at
# it. Item-level fields give one value per line item; proposal-level fields
# give exactly one value.


# Fields compared as money (converted to cents before comparing).
MONEY_FIELDS = {"item_price", "shipping", "fees", "tax", "total_price"}

# Fields that are real attributes on LineItem.
LINE_ITEM_FIRST_CLASS = {"category", "brand", "model", "condition", "quantity", "product_name"}

# Fields that describe the whole checkout, not a single item.
PROPOSAL_LEVEL = {
    "total_price", "shipping", "fees", "tax",
    "merchant_name", "seller_of_record",
    "subscription", "membership", "addons",
    "return_days", "delivery_date",
}


def _lookup(mapping: dict[str, Any], key: str) -> Any:
    """Find `key` in a dict: exact match first, then case/whitespace-insensitive. None counts as MISSING."""
    if mapping.get(key) is not None:
        return mapping[key]
    for candidate_key, value in mapping.items():
        if norm_str(candidate_key) == key and value is not None:
            return value
    return MISSING


def _attribute_fallback(ctx: Ctx, field: str, item_index: int) -> tuple[Any, str]:
    """Look for `field` in the line item's attributes, then in extracted_attributes, else MISSING."""
    item_attributes = ctx.proposal.line_items[item_index].attributes
    value = _lookup(item_attributes, field)
    if value is not MISSING:
        return value, f"line_items[{item_index}].attributes.{field}"

    value = _lookup(ctx.proposal.extracted_attributes, field)
    if value is not MISSING:
        return value, f"extracted_attributes.{field}"

    return MISSING, f"line_items[{item_index}].{field}"


def _resolve_proposal_level(field: str, ctx: Ctx) -> tuple[Any, str]:
    """Return (value, source_path) for a checkout-wide field."""
    p = ctx.proposal

    if field == "total_price":
        # A total_price constraint (e.g. "total_price lte 100") is effectively
        # a second cap, so it gets the same protection as hard_cap_all_in.
        return cents_to_amount(payable_total_cents(p)), "max(total, computed total)"

    if field in ("shipping", "fees", "tax"):
        return getattr(p, field), field

    if field == "merchant_name":
        return p.merchant.name, "merchant.name"

    if field == "seller_of_record":
        if p.merchant.seller_of_record is None:
            return MISSING, "merchant.seller_of_record"
        return p.merchant.seller_of_record, "merchant.seller_of_record"

    # The three terms flags follow the same explicit-presence rule as the
    # built-in no_* rules: a defaulted False is MISSING, not False.
    if field == "subscription":
        path = "recurring_billing.detected"
        if not ctx.explicit(path):
            return MISSING, path
        return p.recurring_billing.detected, path

    if field in ("membership", "addons"):
        path = f"{field}_detected"  # "membership_detected" / "addons_detected"
        if not ctx.explicit(path):
            return MISSING, path
        return getattr(p, path), path

    if field == "return_days":
        path = "return_terms.return_window_days"
        if p.return_terms is None or p.return_terms.return_window_days is None:
            return MISSING, path
        return p.return_terms.return_window_days, path

    if field == "delivery_date":
        path = "delivery.promised_by"
        if p.delivery is None or p.delivery.promised_by is None:
            return MISSING, path
        return p.delivery.promised_by, path

    raise ValueError(f"unknown proposal-level field {field!r}")


def resolve_field(field: str, ctx: Ctx) -> list[tuple[Any, str]]:
    """Map a ConstraintField to a list of (value, source_path) pairs; MISSING marks values not found."""
    if field in PROPOSAL_LEVEL:
        return [_resolve_proposal_level(field, ctx)]

    pairs: list[tuple[Any, str]] = []
    for index, item in enumerate(ctx.proposal.line_items):
        # item_price is always present on a LineItem (required by the model).
        if field == "item_price":
            pairs.append((item.unit_price, f"line_items[{index}].unit_price"))
            continue

        # First-class LineItem fields: use the attribute if it has a value.
        if field in LINE_ITEM_FIRST_CLASS:
            attr_name = "name" if field == "product_name" else field
            value = getattr(item, attr_name)

            # quantity defaults to 1 in models.py. If it was not actually
            # sent, treat it as unknown rather than trusting the default.
            if field == "quantity" and not ctx.explicit(f"line_items[{index}].quantity"):
                value = None

            if value is not None:
                pairs.append((value, f"line_items[{index}].{attr_name}"))
                continue
            # Otherwise fall through and try the attribute dictionaries.

        # Everything else (size, color, refresh_rate_hz, ...) lives in dicts.
        pairs.append(_attribute_fallback(ctx, field, index))

    return pairs


# ============================================================
# Operators
# ============================================================


class Unverifiable(Exception):
    """Raised inside operator code when a comparison cannot be made; becomes an UNVERIFIABLE verdict."""


def _as_list(value: Any) -> list[Any]:
    """Wrap a scalar in a list; leave lists alone. Lets 'in' accept a single value too."""
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _is_list(value: Any) -> bool:
    """True for list-like values."""
    return isinstance(value, (list, tuple))


def _normalize_for_field(field: str, value: Any) -> Any:
    """Normalize one value for comparison, using cents for money fields."""
    if field in MONEY_FIELDS and not isinstance(value, bool):
        try:
            return Decimal(to_cents(value))
        except ValueError:
            pass  # not a number; fall back to generic normalization below
    return normalize_scalar(value)


def _normalized_equal(a: Any, b: Any) -> bool:
    """
    Compare two already-normalized values.

    Booleans only equal booleans. Without this check Python would say
    True == Decimal("1"), so a contract asking for 1 would match "yes".
    """
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _lists_equal(field: str, actual: list[Any], expected: list[Any]) -> bool:
    """Compare two lists as unordered collections (each element matched once) using eq rules."""
    if len(actual) != len(expected):
        return False

    remaining = [_normalize_for_field(field, v) for v in expected]
    for value in actual:
        normalized = _normalize_for_field(field, value)

        # Find a not-yet-used expected element equal to this one.
        match_index = None
        for i, candidate in enumerate(remaining):
            if _normalized_equal(normalized, candidate):
                match_index = i
                break

        if match_index is None:
            return False
        remaining.pop(match_index)  # each expected element can match only once

    return True


def _values_equal(field: str, actual: Any, expected: Any) -> bool:
    """eq semantics: trimmed/lowercased strings, numeric if both are numbers, 'yes'/'no' as booleans."""
    actual_is_list = _is_list(actual)
    expected_is_list = _is_list(expected)

    if actual_is_list and expected_is_list:
        return _lists_equal(field, actual, expected)

    # One list and one scalar cannot be meaningfully "equal".
    if actual_is_list or expected_is_list:
        raise Unverifiable("cannot compare a list with a single value using eq/neq")

    return _normalized_equal(_normalize_for_field(field, actual), _normalize_for_field(field, expected))


def _as_comparable_number(field: str, value: Any, side: str) -> Decimal:
    """Convert a value for lt/lte/gt/gte (cents for money fields); raise Unverifiable if it is not a number."""
    if isinstance(value, (list, tuple, dict)):
        raise Unverifiable(f"{side} value {display(value)} is a list, not a number")
    try:
        if field in MONEY_FIELDS:
            return Decimal(to_cents(value))
        return to_number(value)
    except ValueError:
        raise Unverifiable(f"{side} value {display(value)!r} is not a number")


def _as_comparable_datetime(value: Any, side: str) -> datetime:
    """Parse a value for before/after into aware UTC; raise Unverifiable if it is not a date."""
    try:
        return parse_datetime(value)[0]
    except (ValueError, TypeError):
        raise Unverifiable(f"{side} value {display(value)!r} is not a valid date/time")


def apply_operator(field: str, op: ConstraintOperator, actual: Any, expected: Any) -> bool:
    """Evaluate `actual <op> expected`. Returns True/False, or raises Unverifiable if it makes no sense."""
    if expected is None:
        raise Unverifiable("the contract constraint has no value to compare against")

    # --- eq / neq ------------------------------------------------------
    if op == ConstraintOperator.EQ:
        return _values_equal(field, actual, expected)
    if op == ConstraintOperator.NEQ:
        return not _values_equal(field, actual, expected)

    # --- lt / lte / gt / gte (numbers only) ------------------------------
    if op in (ConstraintOperator.LT, ConstraintOperator.LTE, ConstraintOperator.GT, ConstraintOperator.GTE):
        a = _as_comparable_number(field, actual, "checkout")
        e = _as_comparable_number(field, expected, "contract")
        if op == ConstraintOperator.LT:
            return a < e
        if op == ConstraintOperator.LTE:
            return a <= e
        if op == ConstraintOperator.GT:
            return a > e
        return a >= e  # GTE

    # --- in / not_in ---------------------------------------------------
    # The contract value is the allowed (or forbidden) list. A scalar is
    # treated as a one-element list. If the checkout value is itself a list
    # (e.g. dietary tags), `in` means EVERY element is allowed and `not_in`
    # means NO element is in the list.
    if op in (ConstraintOperator.IN, ConstraintOperator.NOT_IN):
        options = _as_list(expected)
        values = _as_list(actual)
        if not values:
            raise Unverifiable("checkout value is an empty list")

        found_flags: list[bool] = []
        for value in values:
            found = False
            for option in options:
                if _values_equal(field, value, option):
                    found = True
                    break
            found_flags.append(found)

        if op == ConstraintOperator.IN:
            return all(found_flags)
        return not any(found_flags)

    # --- contains / not_contains ---------------------------------------
    # Checkout list: membership (using eq rules). Checkout string: substring.
    # A contract list means `contains` needs ALL of them and `not_contains`
    # needs NONE of them.
    if op in (ConstraintOperator.CONTAINS, ConstraintOperator.NOT_CONTAINS):
        wanted = _as_list(expected)
        present_flags: list[bool] = []

        if _is_list(actual):
            for w in wanted:
                present = False
                for element in actual:
                    if _values_equal(field, element, w):
                        present = True
                        break
                present_flags.append(present)
        elif isinstance(actual, str):
            haystack = norm_str(actual)
            for w in wanted:
                present_flags.append(norm_str(w) in haystack)
        else:
            raise Unverifiable(f"checkout value {display(actual)!r} is neither text nor a list")

        if op == ConstraintOperator.CONTAINS:
            return all(present_flags)
        return not any(present_flags)

    # --- before / after (strict comparisons) ----------------------------
    if op in (ConstraintOperator.BEFORE, ConstraintOperator.AFTER):
        if isinstance(expected, list):
            raise Unverifiable("before/after needs a single date, not a list")
        a = _as_comparable_datetime(actual, "checkout")
        e = _as_comparable_datetime(expected, "contract")
        if op == ConstraintOperator.BEFORE:
            return a < e
        return a > e

    raise Unverifiable(f"unsupported operator {op!r}")


# Human-readable operator symbols for the `expected` string.
_OP_WORDS = {
    ConstraintOperator.EQ: "=",
    ConstraintOperator.NEQ: "!=",
    ConstraintOperator.LT: "<",
    ConstraintOperator.LTE: "<=",
    ConstraintOperator.GT: ">",
    ConstraintOperator.GTE: ">=",
    ConstraintOperator.IN: "in",
    ConstraintOperator.NOT_IN: "not in",
    ConstraintOperator.CONTAINS: "contains",
    ConstraintOperator.NOT_CONTAINS: "does not contain",
    ConstraintOperator.BEFORE: "before",
    ConstraintOperator.AFTER: "after",
}


def constraint_name(index: int, constraint: Constraint) -> str:
    """Stable unique name for a generic constraint, e.g. 'constraint[0]:size:eq'."""
    return f"constraint[{index}]:{constraint.field}:{constraint.operator.value}"


def evaluate_constraint(index: int, constraint: Constraint, ctx: Ctx) -> list[ConstraintResult]:
    """Evaluate one generic Constraint against every resolved value and combine the verdicts."""
    name = constraint_name(index, constraint)
    field = constraint.field
    expected = f"{field} {_OP_WORDS[constraint.operator]} {display(constraint.value)}"

    verdicts: list[ConstraintVerdict] = []
    actuals: list[str] = []
    sources: list[str] = []
    failures: list[str] = []  # reasons for FAIL verdicts
    unknowns: list[str] = []  # reasons for UNVERIFIABLE verdicts

    for value, source in resolve_field(field, ctx):
        sources.append(source)
        actuals.append(display(value))

        # Fail closed: a value we could not find is never a pass.
        if value is MISSING:
            verdicts.append(UNVERIFIABLE)
            unknowns.append(f"{field} is missing ({source})")
            continue

        # A comparison that makes no sense (e.g. "fast" < 5) is UNVERIFIABLE.
        try:
            ok = apply_operator(field, constraint.operator, value, constraint.value)
        except Unverifiable as exc:
            verdicts.append(UNVERIFIABLE)
            unknowns.append(f"{source}: {exc}")
            continue

        if ok:
            verdicts.append(PASS)
        else:
            verdicts.append(FAIL)
            failures.append(f"{source} is {display(value)!r}")

    verdict = aggregate(verdicts)
    if verdict == PASS:
        reason = f"Checkout satisfies {expected}."
    elif verdict == FAIL:
        reason = f"Checkout violates {expected}: {'; '.join(failures)}."
    else:
        reason = f"Could not verify {expected}: {'; '.join(unknowns) or 'no values found'}."

    if constraint.description:
        reason = f"{reason} ({constraint.description})"

    # One value: show it plainly. Several line items: label each one.
    if len(actuals) == 1:
        actual = actuals[0]
    else:
        actual = "; ".join(f"{source}={value}" for source, value in zip(sources, actuals))

    ev = {"sources": sources, "operator": constraint.operator.value, "source_of_value": constraint.source.value}
    return [result(name, verdict, expected, actual, reason, ev, constraint.severity)]


# ============================================================
# Outcome mapping
# ============================================================


def decision_verdict(results: list[ConstraintResult]) -> ConstraintVerdict:
    """
    Compute ValidationDecision.verdict exactly the way its validator in models.py does.

    That validator looks at HARD results only: FAIL beats UNVERIFIABLE beats
    PASS. If we set a different value, constructing the model raises. So we
    mirror it here, and keep the real purchase outcome separate (below).
    """
    hard_verdicts = [r.verdict for r in results if r.severity == HARD]
    if FAIL in hard_verdicts:
        return FAIL
    if UNVERIFIABLE in hard_verdicts:
        return UNVERIFIABLE
    return PASS


def compute_outcome(results: list[ConstraintResult], escalate_if: list[str] | None = None) -> PurchaseStatus:
    """
    Map the individual results to BLOCKED / ESCALATED / AUTHORIZED. The first matching step wins.

    Why not just use decision.verdict? Because it ignores ESCALATING-severity
    results and would call an empty result list "PASS". This function is the
    only thing that can produce AUTHORIZED.

    escalate_if is accepted so callers can pass it, but it is deliberately
    NOT consulted. An unverifiable HARD fact always escalates, even if the
    contract's escalate_if list forgot "any_unverifiable_hard". That is the
    fail-closed rule: we never spend money on a fact we could not check.
    """
    hard = [r for r in results if r.severity == HARD]

    # 1. Any hard rule definitely failed -> block.
    for r in hard:
        if r.verdict == FAIL:
            return PurchaseStatus.BLOCKED

    # 2. The engine evaluated zero hard rules. That should be impossible
    #    (the spend cap always runs) but if it ever happens, "we checked
    #    nothing" must never become "authorized".
    if not hard:
        return PurchaseStatus.ESCALATED

    # 3. Any hard fact could not be verified -> ask the human.
    for r in hard:
        if r.verdict == UNVERIFIABLE:
            return PurchaseStatus.ESCALATED

    # 4. ESCALATING-severity constraints did not pass -> ask the human.
    for r in results:
        if r.severity == ConstraintSeverity.ESCALATING and r.verdict != PASS:
            return PurchaseStatus.ESCALATED

    # 5. Everything that matters passed. SOFT results never get here as
    #    blockers; they are for ranking and display only.
    return PurchaseStatus.AUTHORIZED


# ============================================================
# Entry point
# ============================================================


def _run_rule_safely(
    name: str,
    severity: ConstraintSeverity,
    rule: Callable[..., list[ConstraintResult]],
    *args: Any,
) -> list[ConstraintResult]:
    """
    Run one rule; if it raises anything, return an UNVERIFIABLE result instead.

    One buggy rule must never crash the whole evaluation, and it must never
    be silently skipped either (a skipped rule looks like a passing rule).
    """
    try:
        return rule(*args)
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all: fail closed
        reason = f"Rule could not be evaluated: {type(exc).__name__}: {exc}"
        return [result(name, UNVERIFIABLE, None, "error", reason, {"error": type(exc).__name__}, severity)]


def evaluate_results(
    contract: Contract,
    proposal: TransactionProposal,
    now: datetime,
    fields_set: set[str],
) -> list[ConstraintResult]:
    """Run every built-in rule, then every generic constraint, in fixed order; return all results."""
    ctx = Ctx(contract=contract, proposal=proposal, now=now, fields_set=fields_set)
    results: list[ConstraintResult] = []

    for name, rule in BUILTIN_RULES:
        results.extend(_run_rule_safely(name, HARD, rule, ctx))

    for index, constraint in enumerate(contract.constraints):
        name = constraint_name(index, constraint)
        results.extend(_run_rule_safely(name, constraint.severity, evaluate_constraint, index, constraint, ctx))

    return results


def evaluate(
    contract: Contract,
    proposal: TransactionProposal,
    now: datetime | None = None,
    raw_fields_set: set[str] | None = None,
) -> tuple[ValidationDecision, PurchaseStatus]:
    """
    Evaluate a proposal against a signed contract. Returns (decision, outcome).

    now:            evaluation time. Tests pass a fixed value so results are
                    reproducible; production passes the current UTC time.
    raw_fields_set: dotted paths that were explicitly present in the raw
                    payload (see explicit_fields). If omitted, it is computed
                    from the proposal, which is only correct for a proposal
                    freshly parsed from the request.
    """
    if now is None:
        now = utc_now()
    else:
        now = as_utc(now)[0]

    if raw_fields_set is None:
        fields_set = explicit_fields(proposal)
    else:
        fields_set = raw_fields_set

    results = evaluate_results(contract, proposal, now, fields_set)
    outcome = compute_outcome(results, contract.escalate_if)

    decision = ValidationDecision(
        contract_id=contract.id,
        proposal_id=proposal.id,
        verdict=decision_verdict(results),  # always what the model's validator expects
        results=results,
        evaluated_at=now,
    )
    return decision, outcome
