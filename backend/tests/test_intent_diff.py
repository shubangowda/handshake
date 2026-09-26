"""
test_intent_diff.py: unit tests for the rule engine (app/intent_diff.py).

These tests never touch the database or HTTP. Each one builds the demo
contract and a (possibly edited) proposal, calls evaluate() with a FIXED
evaluation time (EVAL_TIME), and checks individual results and the outcome.

The block marked "Section 12 checklist" maps one-to-one to the required
engine cases; the rest are extra edge cases.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from app import intent_diff
from app.intent_diff import compute_outcome, decision_verdict, evaluate, explicit_fields, to_cents
from app.models import (
    Constraint,
    ConstraintResult,
    ConstraintSeverity,
    ConstraintVerdict,
    Contract,
    PurchaseStatus,
    ValidationDecision,
)
from conftest import EVAL_TIME, Mutator, make_contract, make_proposal

PASS, FAIL, UNV = ConstraintVerdict.PASS, ConstraintVerdict.FAIL, ConstraintVerdict.UNVERIFIABLE


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------


def run(contract: Contract | None = None, mutate: Mutator | None = None) -> tuple[ValidationDecision, PurchaseStatus]:
    """Evaluate (demo contract or `contract`) against the passing proposal edited by `mutate`, at EVAL_TIME."""
    if contract is None:
        contract = make_contract()
    proposal, _ = make_proposal(mutate)
    return evaluate(contract, proposal, now=EVAL_TIME)


def by_name(decision: ValidationDecision) -> dict[str, ConstraintResult]:
    """Index a decision's results by constraint name."""
    return {r.constraint: r for r in decision.results}


def with_constraints(*constraints: dict[str, Any]) -> Contract:
    """The demo contract with its generic constraints replaced by `constraints`."""
    return make_contract(constraints=[Constraint.model_validate(c) for c in constraints])


def set_totals(raw: dict[str, Any], item_price: float, tax: float = 0.0, shipping: float = 0.0) -> None:
    """Edit a raw proposal so one item costs `item_price` and every total stays consistent."""
    raw["line_items"][0]["unit_price"] = item_price
    raw["item_subtotal"] = item_price
    raw["tax"] = tax
    raw["shipping"] = shipping
    raw["total"] = round(item_price + tax + shipping, 2)


# ============================================================
# Section 12 checklist (engine)
# ============================================================


# 1. 128.39 under a 135 cap -> cap PASS, AUTHORIZED
def test_passing_proposal_is_authorized() -> None:
    """The demo checkout (128.39 under a 135 cap) passes every rule and is AUTHORIZED."""
    decision, outcome = run()
    assert by_name(decision)["hard_cap_all_in"].verdict == PASS
    assert outcome == PurchaseStatus.AUTHORIZED
    assert decision.verdict == PASS
    not_passing = [r for r in decision.results if r.verdict != PASS]
    assert not_passing == []


# 2. 140.00 over a 135 cap -> FAIL, BLOCKED
def test_total_140_over_135_cap_blocks() -> None:
    """A 140.00 total against a 135.00 cap fails the cap rule and is BLOCKED."""
    decision, outcome = run(mutate=lambda r: set_totals(r, 131.60, 8.40))
    cap = by_name(decision)["hard_cap_all_in"]
    assert cap.actual == "140.00 USD"
    assert cap.verdict == FAIL
    assert "by 5.00 USD" in cap.reason
    assert outcome == PurchaseStatus.BLOCKED


# 3. Hidden subscription -> FAIL, BLOCKED
def test_hidden_subscription_blocks() -> None:
    """recurring_billing.detected=True fails no_subscription and is BLOCKED."""
    def mutate(raw: dict[str, Any]) -> None:
        """Edit the raw proposal for this test case."""
        raw["recurring_billing"] = {"detected": True, "interval": "monthly", "amount": 9.99}

    decision, outcome = run(mutate=mutate)
    rule = by_name(decision)["no_subscription"]
    assert rule.verdict == FAIL
    assert "monthly" in rule.reason
    assert outcome == PurchaseStatus.BLOCKED


# 4. Wrong shoe size (9 instead of 10) -> FAIL, BLOCKED
def test_wrong_shoe_size_9_blocks() -> None:
    """Size 9 against a contract size of 10 fails and is BLOCKED."""
    decision, outcome = run(mutate=lambda r: r["line_items"][0]["attributes"].update(size=9))
    size = by_name(decision)["constraint[0]:size:eq"]
    assert size.verdict == FAIL
    assert "'9'" in size.reason
    assert outcome == PurchaseStatus.BLOCKED


# 5. Seller first-party and verified both unknown -> UNVERIFIABLE, ESCALATED
def test_unknown_seller_verification_escalates() -> None:
    """With is_first_party and is_verified both unknown (None), the seller rule is UNVERIFIABLE -> ESCALATED."""
    decision, outcome = run(mutate=lambda r: r["merchant"].update(is_first_party=None, is_verified=None))
    assert by_name(decision)["seller_requirement"].verdict == UNV
    assert outcome == PurchaseStatus.ESCALATED


# 6. Delivery promised after the deadline -> FAIL, BLOCKED
def test_delivery_after_deadline_blocks() -> None:
    """A promised date one second after deliver_by fails and is BLOCKED."""
    decision, outcome = run(mutate=lambda r: r["delivery"].update(promised_by="2026-10-11T00:00:00Z"))
    assert by_name(decision)["delivery_deadline"].verdict == FAIL
    assert outcome == PurchaseStatus.BLOCKED


# 7. No delivery object at all -> UNVERIFIABLE, ESCALATED
def test_missing_delivery_object_escalates() -> None:
    """No delivery information at all means the deadline cannot be verified -> ESCALATED."""
    decision, outcome = run(mutate=lambda r: r.pop("delivery"))
    assert by_name(decision)["delivery_deadline"].verdict == UNV
    assert outcome == PurchaseStatus.ESCALATED


# 8. All three terms fields omitted under strict mode -> UNVERIFIABLE, ESCALATED
def test_all_three_terms_fields_omitted_escalates() -> None:
    """Omitting recurring_billing, membership_detected, and addons_detected makes all three rules UNVERIFIABLE."""
    def mutate(raw: dict[str, Any]) -> None:
        """Edit the raw proposal for this test case."""
        raw.pop("recurring_billing")
        raw.pop("membership_detected")
        raw.pop("addons_detected")

    assert intent_diff.STRICT_EXPLICIT_TERMS is True
    decision, outcome = run(mutate=mutate)
    results = by_name(decision)
    for rule in ("no_subscription", "no_membership", "no_addons"):
        assert results[rule].verdict == UNV, rule
        assert results[rule].actual == "missing"
    assert outcome == PurchaseStatus.ESCALATED


# 9a. Merchant on the deny list -> FAIL
def test_merchant_on_deny_list_blocks() -> None:
    """A merchant on the deny list fails merchant_policy (case-insensitively) and is BLOCKED."""
    contract = make_contract(merchants={"allow": [], "deny": ["mock nike"]})
    decision, outcome = run(contract)
    assert by_name(decision)["merchant_policy"].verdict == FAIL
    assert outcome == PurchaseStatus.BLOCKED


# 9b. Merchant not on the allow list, new_merchant=escalate -> ESCALATED
def test_unknown_merchant_escalates_by_default() -> None:
    """A merchant missing from the allow list, with new_merchant=escalate, is ESCALATED."""
    decision, outcome = run(mutate=lambda r: r["merchant"].update(name="Shady Shoes", domain="shady.example"))
    rule = by_name(decision)["merchant_policy"]
    assert rule.verdict == UNV
    assert "not on the approved merchant list" in rule.reason
    assert outcome == PurchaseStatus.ESCALATED


# 10. Currency mismatch -> BLOCKED
def test_currency_mismatch_blocks() -> None:
    """A EUR checkout against a USD contract is BLOCKED."""
    decision, outcome = run(mutate=lambda r: r.update(currency="eur"))
    assert by_name(decision)["currency_match"].verdict == FAIL
    assert outcome == PurchaseStatus.BLOCKED


# 11. Subtotal not matching line items -> BLOCKED
def test_subtotal_integrity_catches_padded_subtotal() -> None:
    """Line items sum to 119.99 but item_subtotal claims 129.99 -> subtotal_integrity FAIL -> BLOCKED."""
    def mutate(raw: dict[str, Any]) -> None:
        """Edit the raw proposal for this test case."""
        raw["item_subtotal"] = 129.99
        raw["total"] = round(129.99 + 8.40, 2)  # keep the total consistent so only the subtotal is wrong

    decision, outcome = run(mutate=mutate)
    assert by_name(decision)["subtotal_integrity"].verdict == FAIL
    assert outcome == PurchaseStatus.BLOCKED


# 12. Size "10" (string) vs contract 10 (integer) -> PASS
def test_size_string_10_matches_contract_integer_10() -> None:
    """The string "10" equals the integer 10 because both parse as numbers."""
    decision, _ = run(mutate=lambda r: r["line_items"][0]["attributes"].update(size="10"))
    result = by_name(decision)["constraint[0]:size:eq"]
    assert result.actual == "10"
    assert result.verdict == PASS


# 13. lt against a non-numeric value -> UNVERIFIABLE, no exception
def test_lt_against_non_numeric_value_is_unverifiable() -> None:
    """refresh_rate_hz='fast' < 240 cannot be compared: UNVERIFIABLE with a reason, not an exception."""
    contract = with_constraints({"field": "refresh_rate_hz", "operator": "lt", "value": 240})
    decision, outcome = run(contract, mutate=lambda r: r["line_items"][0]["attributes"].update(refresh_rate_hz="fast"))
    result = decision.results[-1]
    assert result.verdict == UNV
    assert "'fast' is not a number" in result.reason
    assert outcome == PurchaseStatus.ESCALATED


# 14. Escalating constraint fails, hard rules pass -> ESCALATED
def test_escalating_constraint_fail_escalates() -> None:
    """An ESCALATING-severity failure escalates even though decision.verdict (hard-only) says PASS."""
    contract = with_constraints({"field": "color", "operator": "eq", "value": "red", "severity": "escalating"})
    decision, outcome = run(contract)
    assert decision.results[-1].verdict == FAIL
    assert decision.verdict == PASS  # the model's verdict ignores non-hard results...
    assert outcome == PurchaseStatus.ESCALATED  # ...which is why the outcome is computed separately


# 15. Soft constraint fails, hard rules pass -> AUTHORIZED
def test_soft_constraint_fail_does_not_block() -> None:
    """A SOFT failure is recorded for display but does not change the outcome."""
    contract = with_constraints({"field": "color", "operator": "eq", "value": "red", "severity": "soft"})
    decision, outcome = run(contract)
    assert decision.results[-1].verdict == FAIL
    assert outcome == PurchaseStatus.AUTHORIZED


# 16. Naive deliver_by vs aware promised_by -> no crash, correct verdict
def test_naive_contract_deadline_does_not_crash() -> None:
    """A naive deliver_by (assumed UTC) compared with an aware promised date: no TypeError, PASS."""
    contract = make_contract(delivery={"deliver_by": "2026-10-10T23:59:59", "max_shipping": 10})
    result = by_name(run(contract)[0])["delivery_deadline"]
    assert result.verdict == PASS
    assert result.evidence["deliver_by_timezone"] == "naive datetime, assumed UTC"


def test_naive_deadline_vs_aware_late_promise_fails() -> None:
    """Same naive deadline, but the aware promised date (-05:00) is after it in UTC -> FAIL."""
    contract = make_contract(delivery={"deliver_by": "2026-10-10T23:59:59"})
    # 20:00 at UTC-5 is 01:00 UTC on Oct 11, which is after the deadline.
    decision, outcome = run(contract, mutate=lambda r: r["delivery"].update(promised_by="2026-10-10T20:00:00-05:00"))
    assert by_name(decision)["delivery_deadline"].verdict == FAIL
    assert outcome == PurchaseStatus.BLOCKED


# 17. Determinism
def test_evaluation_is_deterministic() -> None:
    """Same contract, same proposal, same `now`: identical (name, verdict, expected, actual, reason) sequence."""
    first, first_outcome = run()
    second, second_outcome = run()

    def fingerprint(decision: ValidationDecision) -> list[tuple[Any, ...]]:
        """The ordered tuple of the fields the frontend displays."""
        return [(r.constraint, r.verdict, r.expected, r.actual, r.reason) for r in decision.results]

    assert fingerprint(first) == fingerprint(second)
    assert [r.model_dump() for r in first.results] == [r.model_dump() for r in second.results]
    assert first.evaluated_at == second.evaluated_at == EVAL_TIME
    assert first_outcome == second_outcome


# 18. decision.verdict agrees with the hard results
@pytest.mark.parametrize(
    "mutate",
    [
        None,  # all pass
        lambda r: r.update(currency="EUR"),  # hard FAIL
        lambda r: r.pop("addons_detected"),  # hard UNVERIFIABLE
    ],
    ids=["pass", "fail", "unverifiable"],
)
def test_decision_verdict_agrees_with_hard_results(mutate: Mutator | None) -> None:
    """decision.verdict equals what ValidationDecision's validator computes from HARD results only."""
    decision, _ = run(mutate=mutate)
    hard_verdicts = [r.verdict for r in decision.results if r.severity == ConstraintSeverity.HARD]
    if FAIL in hard_verdicts:
        expected = FAIL
    elif UNV in hard_verdicts:
        expected = UNV
    else:
        expected = PASS
    assert decision.verdict == expected
    assert decision_verdict(decision.results) == expected


# ============================================================
# Ordering and result shape
# ============================================================


def test_results_in_documented_order() -> None:
    """Built-in rules run in the documented order, followed by generic constraints."""
    decision, _ = run()
    assert [r.constraint for r in decision.results] == [
        "currency_match",
        "subtotal_integrity",
        "hard_cap_all_in",
        "max_shipping",
        "contract_category",
        "no_subscription",
        "no_membership",
        "no_addons",
        "delivery_deadline",
        "merchant_policy",
        "seller_requirement",
        "extractor_agreement",
        "constraint[0]:size:eq",
        "constraint[1]:condition:in",
    ]


def test_cap_result_shape() -> None:
    """The cap result carries readable expected/actual/reason strings and the target as evidence only."""
    cap = by_name(run()[0])["hard_cap_all_in"]
    assert cap.expected == "<= 135.00 USD"
    assert cap.actual == "128.39 USD"
    assert cap.reason == "Final payable total is within signed cap."
    assert cap.evidence["target"] == "120.00 USD"


# ============================================================
# Money
# ============================================================


def test_to_cents_rounds_half_up() -> None:
    """to_cents rounds half up via Decimal and rejects booleans."""
    assert to_cents(0.125) == 13
    assert to_cents(128.385) == 12839
    assert to_cents("135") == 13500
    with pytest.raises(ValueError):
        to_cents(True)


def test_total_exactly_at_cap_passes() -> None:
    """A total of exactly 135.00 is allowed (<=)."""
    decision, outcome = run(mutate=lambda r: set_totals(r, 125.00, 10.00))
    assert by_name(decision)["hard_cap_all_in"].verdict == PASS
    assert outcome == PurchaseStatus.AUTHORIZED


def test_total_one_cent_over_cap_blocks() -> None:
    """135.01 is over a 135.00 cap: strict, no tolerance."""
    decision, outcome = run(mutate=lambda r: set_totals(r, 125.00, 10.01))
    cap = by_name(decision)["hard_cap_all_in"]
    assert cap.verdict == FAIL
    assert "by 0.01 USD" in cap.reason
    assert outcome == PurchaseStatus.BLOCKED


def test_currency_match_is_case_insensitive() -> None:
    """'usd' matches 'USD'."""
    decision, _ = run(mutate=lambda r: r.update(currency="usd"))
    assert by_name(decision)["currency_match"].verdict == PASS


def test_subtotal_integrity_uses_quantity() -> None:
    """2 x 50.00 = 100.00 passes subtotal_integrity."""
    def mutate(raw: dict[str, Any]) -> None:
        """Edit the raw proposal for this test case."""
        raw["line_items"][0].update(quantity=2, unit_price=50.00)
        raw["item_subtotal"] = 100.00
        raw["tax"] = 0
        raw["total"] = 100.00

    assert by_name(run(mutate=mutate)[0])["subtotal_integrity"].verdict == PASS


def test_shipping_over_max_blocks() -> None:
    """Shipping of 10.01 exceeds the signed 10.00 maximum."""
    decision, outcome = run(mutate=lambda r: set_totals(r, 110.00, 0.0, 10.01))
    assert by_name(decision)["max_shipping"].verdict == FAIL
    assert outcome == PurchaseStatus.BLOCKED


# ============================================================
# Category
# ============================================================


def test_category_mismatch_blocks() -> None:
    """An item in the wrong category fails contract_category."""
    decision, outcome = run(mutate=lambda r: r["line_items"][0].update(category="Socks"))
    assert by_name(decision)["contract_category"].verdict == FAIL
    assert outcome == PurchaseStatus.BLOCKED


def test_category_missing_escalates() -> None:
    """An item with no category cannot be verified."""
    decision, outcome = run(mutate=lambda r: r["line_items"][0].pop("category"))
    assert by_name(decision)["contract_category"].verdict == UNV
    assert outcome == PurchaseStatus.ESCALATED


def test_category_mismatch_beats_missing() -> None:
    """With one wrong-category item and one missing-category item, FAIL wins over UNVERIFIABLE."""
    def mutate(raw: dict[str, Any]) -> None:
        """Edit the raw proposal for this test case."""
        wrong = dict(raw["line_items"][0], category="hats", unit_price=0.0)
        missing = dict(raw["line_items"][0], unit_price=0.0)
        missing.pop("category")
        raw["line_items"] += [wrong, missing]

    assert by_name(run(mutate=mutate)[0])["contract_category"].verdict == FAIL


def test_category_is_case_and_whitespace_insensitive() -> None:
    """'  SHOES ' matches 'shoes'."""
    decision, _ = run(mutate=lambda r: r["line_items"][0].update(category="  SHOES "))
    assert by_name(decision)["contract_category"].verdict == PASS


# ============================================================
# Terms: the default-False trap
# ============================================================


@pytest.mark.parametrize(
    "rule, drop",
    [
        ("no_subscription", lambda r: r.pop("recurring_billing")),
        ("no_subscription", lambda r: r.update(recurring_billing={})),  # object sent, `detected` not
        ("no_membership", lambda r: r.pop("membership_detected")),
        ("no_addons", lambda r: r.pop("addons_detected")),
    ],
)
def test_unreported_terms_are_unverifiable(rule: str, drop: Mutator) -> None:
    """Each terms flag, when omitted, is UNVERIFIABLE (not a default-False PASS)."""
    decision, outcome = run(mutate=drop)
    assert by_name(decision)[rule].verdict == UNV
    assert outcome == PurchaseStatus.ESCALATED


def test_unreported_terms_pass_when_strict_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With STRICT_EXPLICIT_TERMS off, the model default (False) is trusted."""
    monkeypatch.setattr(intent_diff, "STRICT_EXPLICIT_TERMS", False)
    decision, outcome = run(mutate=lambda r: r.pop("addons_detected"))
    assert by_name(decision)["no_addons"].verdict == PASS
    assert outcome == PurchaseStatus.AUTHORIZED


@pytest.mark.parametrize(
    "rule, detect",
    [
        ("no_membership", lambda r: r.update(membership_detected=True)),
        ("no_addons", lambda r: r.update(addons_detected=True)),
    ],
)
def test_detected_terms_block(rule: str, detect: Mutator) -> None:
    """A detected membership or add-on is a FAIL."""
    decision, outcome = run(mutate=detect)
    assert by_name(decision)[rule].verdict == FAIL
    assert outcome == PurchaseStatus.BLOCKED


def test_terms_rules_skipped_when_contract_allows_them() -> None:
    """If the contract does not forbid subscriptions/add-ons, those rules produce no result."""
    contract = make_contract(terms={"no_subscription": False, "no_membership": False, "no_addons": False})
    names = by_name(run(contract, mutate=lambda r: r.pop("addons_detected"))[0])
    assert "no_addons" not in names
    assert "no_subscription" not in names


def test_explicit_fields_tracks_nested_paths() -> None:
    """explicit_fields reports nested paths, and omits recurring_billing.detected when the object is absent."""
    proposal, _ = make_proposal()
    fields = explicit_fields(proposal)
    assert {"recurring_billing.detected", "addons_detected", "line_items[0].quantity"} <= fields

    proposal, _ = make_proposal(lambda r: r.pop("recurring_billing"))
    assert "recurring_billing.detected" not in explicit_fields(proposal)


def test_raw_fields_set_overrides_model_fields_set() -> None:
    """An explicit raw_fields_set (as stored by services.py) is what the engine trusts."""
    proposal, _ = make_proposal()
    decision, outcome = evaluate(make_contract(), proposal, now=EVAL_TIME, raw_fields_set=set())
    assert by_name(decision)["no_addons"].verdict == UNV
    assert outcome == PurchaseStatus.ESCALATED


# ============================================================
# Returns and delivery
# ============================================================


@pytest.mark.parametrize(
    "mutate, verdict",
    [
        (lambda r: None, PASS),
        (lambda r: r.pop("return_terms"), UNV),
        (lambda r: r.update(return_terms={"returnable": True}), UNV),
        (lambda r: r.update(return_terms={"returnable": False}), FAIL),
        (lambda r: r.update(return_terms={"return_window_days": 14}), FAIL),
    ],
)
def test_min_return_days(mutate: Mutator, verdict: ConstraintVerdict) -> None:
    """min_return_days: missing -> UNVERIFIABLE, not returnable or too short -> FAIL."""
    contract = make_contract(terms={"min_return_days": 30})
    assert by_name(run(contract, mutate)[0])["min_return_days"].verdict == verdict


@pytest.mark.parametrize(
    "mutate, verdict",
    [
        (lambda r: r.update(delivery={"carrier": "UPS"}), UNV),  # no promised_by
        (lambda r: r["delivery"].update(promised_by="2026-10-10T23:59:59Z"), PASS),  # exactly on time
        (lambda r: r["delivery"].update(promised_by="2026-10-08T18:00:00"), PASS),  # naive promised date
        (lambda r: r["delivery"].update(promised_by="2026-10-10T20:00:00-05:00"), FAIL),  # = Oct 11 01:00Z
    ],
)
def test_delivery_deadline(mutate: Mutator, verdict: ConstraintVerdict) -> None:
    """Delivery deadline edge cases around timezones and missing dates."""
    assert by_name(run(mutate=mutate)[0])["delivery_deadline"].verdict == verdict


def test_naive_promised_date_assumption_is_recorded() -> None:
    """When the promised date is naive, the evidence says UTC was assumed."""
    result = by_name(run(mutate=lambda r: r["delivery"].update(promised_by="2026-10-08T18:00:00"))[0])["delivery_deadline"]
    assert result.evidence["promised_by_timezone"] == "naive datetime, assumed UTC"


def test_unverified_estimate_escalates_when_required() -> None:
    """require_verified_estimate + unverified delivery -> UNVERIFIABLE."""
    contract = make_contract(delivery={"deliver_by": "2026-10-10T23:59:59Z", "require_verified_estimate": True})
    decision, outcome = run(contract, mutate=lambda r: r["delivery"].update(verified=False))
    assert by_name(decision)["delivery_deadline"].verdict == UNV
    assert outcome == PurchaseStatus.ESCALATED


# ============================================================
# Merchant and seller
# ============================================================


def test_merchant_names_normalized() -> None:
    """' MOCK NIKE ' matches the allow-list entry 'Mock Nike'."""
    decision, _ = run(mutate=lambda r: r["merchant"].update(name=" MOCK NIKE "))
    assert by_name(decision)["merchant_policy"].verdict == PASS


def test_merchant_deny_matches_domain() -> None:
    """The deny list also matches the merchant's domain, and deny beats allow."""
    contract = make_contract(merchants={"allow": ["Mock Nike"], "deny": ["MockNike.example"]})
    assert by_name(run(contract)[0])["merchant_policy"].verdict == FAIL


@pytest.mark.parametrize("action, verdict", [("allow", PASS), ("deny", FAIL), ("escalate", UNV)])
def test_new_merchant_action_with_empty_lists(action: str, verdict: ConstraintVerdict) -> None:
    """With both lists empty, every merchant is 'new' and new_merchant decides."""
    contract = make_contract(merchants={"allow": [], "deny": [], "new_merchant": action})
    assert by_name(run(contract)[0])["merchant_policy"].verdict == verdict


@pytest.mark.parametrize(
    "requirement, first_party, verified, verdict",
    [
        ("any", None, None, PASS),
        ("first_party", True, None, PASS),
        ("first_party", False, True, FAIL),
        ("first_party", None, True, UNV),
        ("verified", None, True, PASS),
        ("verified", None, False, FAIL),
        ("verified", True, None, UNV),
        ("first_party_or_verified", False, True, PASS),
        ("first_party_or_verified", False, False, FAIL),
        ("first_party_or_verified", False, None, UNV),
    ],
)
def test_seller_requirement(
    requirement: str, first_party: bool | None, verified: bool | None, verdict: ConstraintVerdict
) -> None:
    """Every seller_requirement mode against true/false/unknown flags."""
    contract = make_contract(merchants={"allow": ["Mock Nike"], "seller_requirement": requirement})
    decision, _ = run(contract, mutate=lambda r: r["merchant"].update(is_first_party=first_party, is_verified=verified))
    assert by_name(decision)["seller_requirement"].verdict == verdict


# ============================================================
# Extractor agreement
# ============================================================


def test_extractor_disagreement_escalates() -> None:
    """extractors_agreed explicitly False -> hard UNVERIFIABLE -> ESCALATED."""
    decision, outcome = run(mutate=lambda r: r.update(extractors_agreed=False))
    result = by_name(decision)["extractor_agreement"]
    assert result.verdict == UNV
    assert result.severity == ConstraintSeverity.HARD
    assert outcome == PurchaseStatus.ESCALATED


def test_extractor_agreement_not_reported_is_soft_pass() -> None:
    """extractors_agreed not sent -> soft informational PASS (does not block single-extractor setups)."""
    result = by_name(run()[0])["extractor_agreement"]
    assert result.verdict == PASS
    assert result.severity == ConstraintSeverity.SOFT


# ============================================================
# Generic constraints
# ============================================================


def test_missing_size_escalates() -> None:
    """No size anywhere -> UNVERIFIABLE -> ESCALATED."""
    decision, outcome = run(mutate=lambda r: r["line_items"][0]["attributes"].pop("size"))
    assert by_name(decision)["constraint[0]:size:eq"].verdict == UNV
    assert outcome == PurchaseStatus.ESCALATED


def test_size_falls_back_to_extracted_attributes() -> None:
    """If the item has no size, extracted_attributes (with a case-insensitive key) is used."""
    def mutate(raw: dict[str, Any]) -> None:
        """Edit the raw proposal for this test case."""
        raw["line_items"][0]["attributes"].pop("size")
        raw["extracted_attributes"] = {"Size": 10.0}

    assert by_name(run(mutate=mutate)[0])["constraint[0]:size:eq"].verdict == PASS


def test_used_condition_blocks() -> None:
    """condition 'refurbished' is not in ['new']."""
    decision, outcome = run(mutate=lambda r: r["line_items"][0].update(condition="refurbished"))
    assert by_name(decision)["constraint[1]:condition:in"].verdict == FAIL
    assert outcome == PurchaseStatus.BLOCKED


def test_condition_free_string_normalized() -> None:
    """' NEW ' (a free string) matches 'new'."""
    decision, _ = run(mutate=lambda r: r["line_items"][0].update(condition=" NEW "))
    assert by_name(decision)["constraint[1]:condition:in"].verdict == PASS


def test_any_failing_line_item_fails_constraint() -> None:
    """With two items, one in size 11, the size constraint FAILs."""
    def mutate(raw: dict[str, Any]) -> None:
        """Edit the raw proposal for this test case."""
        extra = dict(raw["line_items"][0], unit_price=0.0, attributes={"size": 11})
        raw["line_items"].append(extra)

    assert by_name(run(mutate=mutate)[0])["constraint[0]:size:eq"].verdict == FAIL


@pytest.mark.parametrize(
    "constraint, verdict",
    [
        ({"field": "refresh_rate_hz", "operator": "gte", "value": 120}, PASS),
        ({"field": "refresh_rate_hz", "operator": "gt", "value": 144}, FAIL),
        ({"field": "refresh_rate_hz", "operator": "lt", "value": "fast"}, UNV),  # non-numeric contract value
        ({"field": "refresh_rate_hz", "operator": "lt", "value": [1, 2]}, UNV),  # list with lt
        ({"field": "color", "operator": "neq", "value": "Red"}, PASS),
        ({"field": "color", "operator": "not_in", "value": ["black", "white"]}, FAIL),
        ({"field": "color", "operator": "in", "value": "BLACK"}, PASS),  # scalar treated as 1-element list
        ({"field": "toppings", "operator": "contains", "value": ["cheese", "basil"]}, PASS),
        ({"field": "toppings", "operator": "contains", "value": ["pineapple"]}, FAIL),
        ({"field": "toppings", "operator": "not_contains", "value": ["pineapple", "anchovy"]}, PASS),
        ({"field": "toppings", "operator": "eq", "value": ["basil", "CHEESE"]}, PASS),  # unordered list equality
        ({"field": "product_name", "operator": "contains", "value": "pegasus"}, PASS),
        ({"field": "product_name", "operator": "not_contains", "value": "kids"}, PASS),
        ({"field": "seats_together", "operator": "eq", "value": True}, PASS),  # "yes" -> True
        ({"field": "seats_together", "operator": "eq", "value": 1}, FAIL),  # booleans only equal booleans
        ({"field": "item_price", "operator": "lte", "value": 119.99}, PASS),
        ({"field": "item_price", "operator": "lt", "value": "119.99"}, FAIL),
        ({"field": "total_price", "operator": "eq", "value": "128.39"}, PASS),
        ({"field": "tax", "operator": "lte", "value": 8.0}, FAIL),
        ({"field": "merchant_name", "operator": "eq", "value": "mock nike"}, PASS),
        ({"field": "seller_of_record", "operator": "eq", "value": "Nike"}, UNV),  # missing
        ({"field": "delivery_date", "operator": "before", "value": "2026-10-09"}, PASS),
        ({"field": "delivery_date", "operator": "after", "value": "2026-10-09T00:00:00Z"}, FAIL),
        ({"field": "delivery_date", "operator": "before", "value": "soon"}, UNV),
        ({"field": "return_days", "operator": "gte", "value": 30}, PASS),
        ({"field": "subscription", "operator": "eq", "value": False}, PASS),
        ({"field": "addons", "operator": "eq", "value": "no"}, PASS),
        ({"field": "brand", "operator": "eq", "value": "NIKE"}, PASS),
        ({"field": "model", "operator": "eq", "value": "pegasus 41"}, UNV),  # missing everywhere
        ({"field": "quantity", "operator": "eq", "value": 1}, PASS),
        ({"field": "storage_gb", "operator": "eq", "value": None}, UNV),  # contract value is null
    ],
)
def test_generic_operators(constraint: dict[str, Any], verdict: ConstraintVerdict) -> None:
    """A table of operator/field combinations and the verdict each must produce."""
    def mutate(raw: dict[str, Any]) -> None:
        """Edit the raw proposal for this test case."""
        raw["line_items"][0]["attributes"].update(
            refresh_rate_hz="144", toppings=["Cheese", "basil"], seats_together="yes", storage_gb=256
        )

    decision, _ = run(with_constraints(constraint), mutate)
    result = decision.results[-1]
    assert result.verdict == verdict, result.reason


def test_unsent_quantity_is_missing_for_quantity_constraints() -> None:
    """quantity defaults to 1 in the model; if it was not sent, a quantity constraint is UNVERIFIABLE."""
    contract = with_constraints({"field": "quantity", "operator": "eq", "value": 1})
    decision, _ = run(contract, mutate=lambda r: r["line_items"][0].pop("quantity"))
    assert decision.results[-1].verdict == UNV


def test_constraint_description_appended_to_reason() -> None:
    """A constraint's description is appended to its reason."""
    contract = with_constraints({"field": "color", "operator": "eq", "value": "black", "description": "user hates bright colors"})
    assert "(user hates bright colors)" in run(contract)[0].results[-1].reason


# ============================================================
# Robustness and outcome mapping
# ============================================================


def test_rule_exception_becomes_unverifiable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rule that raises becomes UNVERIFIABLE (never a crash, never a silent skip)."""
    def boom(ctx: Any) -> list[ConstraintResult]:
        """A rule that always crashes."""
        raise RuntimeError("kaboom")

    patched: list[tuple[str, Callable[..., Any]]] = []
    for name, fn in intent_diff.BUILTIN_RULES:
        patched.append((name, boom if name == "merchant_policy" else fn))
    monkeypatch.setattr(intent_diff, "BUILTIN_RULES", patched)

    decision, outcome = run()
    result = by_name(decision)["merchant_policy"]
    assert result.verdict == UNV
    assert "kaboom" in result.reason
    assert outcome == PurchaseStatus.ESCALATED


def _r(verdict: ConstraintVerdict, severity: ConstraintSeverity = ConstraintSeverity.HARD) -> ConstraintResult:
    """A minimal ConstraintResult for outcome-mapping tests."""
    return ConstraintResult(constraint="x", verdict=verdict, reason="r", severity=severity)


@pytest.mark.parametrize(
    "results, outcome",
    [
        ([], PurchaseStatus.ESCALATED),  # nothing evaluated -> never authorize
        ([_r(PASS, ConstraintSeverity.SOFT)], PurchaseStatus.ESCALATED),  # zero HARD results
        ([_r(PASS)], PurchaseStatus.AUTHORIZED),
        ([_r(PASS), _r(UNV), _r(FAIL)], PurchaseStatus.BLOCKED),
        ([_r(PASS), _r(UNV)], PurchaseStatus.ESCALATED),
        ([_r(PASS), _r(UNV, ConstraintSeverity.ESCALATING)], PurchaseStatus.ESCALATED),
        ([_r(PASS), _r(FAIL, ConstraintSeverity.ESCALATING)], PurchaseStatus.ESCALATED),
        ([_r(PASS), _r(FAIL, ConstraintSeverity.SOFT), _r(UNV, ConstraintSeverity.SOFT)], PurchaseStatus.AUTHORIZED),
    ],
)
def test_compute_outcome(results: list[ConstraintResult], outcome: PurchaseStatus) -> None:
    """compute_outcome applies the documented first-match-wins mapping."""
    assert compute_outcome(results) == outcome


def test_unverifiable_escalates_even_without_escalate_if() -> None:
    """Unverifiable hard facts escalate even if the contract's escalate_if list is empty."""
    contract = make_contract(escalate_if=[])
    assert run(contract, mutate=lambda r: r.pop("addons_detected"))[1] == PurchaseStatus.ESCALATED


# ============================================================
# The 2-cent tolerance gap: the cap uses max(claimed, computed)
# ============================================================


def test_claimed_total_one_cent_under_its_parts_at_cap_blocks() -> None:
    """
    Parts add up to 135.01 but the checkout claims 135.00 (right at the cap).

    models.py accepts this (it tolerates 2 cents), so without the fix the
    claimed 135.00 would pass a 135.00 cap while the real charge is 135.01.
    The cap check must use the larger number and FAIL.
    """
    def mutate(raw: dict[str, Any]) -> None:
        """Parts: 126.60 + 8.41 tax = 135.01; claimed total: 135.00."""
        raw["line_items"][0]["unit_price"] = 126.60
        raw["item_subtotal"] = 126.60
        raw["tax"] = 8.41
        raw["total"] = 135.00

    decision, outcome = run(mutate=mutate)
    cap = by_name(decision)["hard_cap_all_in"]
    assert cap.verdict == FAIL
    assert cap.actual == "135.01 USD"
    assert cap.evidence["claimed_total"] == "135.00"
    assert cap.evidence["computed_total"] == "135.01"
    assert "claimed 135.00 USD" in cap.reason
    assert outcome == PurchaseStatus.BLOCKED


def test_claimed_total_above_its_parts_uses_the_claim() -> None:
    """If the claim is the larger number (135.01 vs parts of 135.00), the claim is what gets checked."""
    def mutate(raw: dict[str, Any]) -> None:
        """Parts: 126.60 + 8.40 tax = 135.00; claimed total: 135.01."""
        raw["line_items"][0]["unit_price"] = 126.60
        raw["item_subtotal"] = 126.60
        raw["tax"] = 8.40
        raw["total"] = 135.01

    decision, outcome = run(mutate=mutate)
    assert by_name(decision)["hard_cap_all_in"].actual == "135.01 USD"
    assert outcome == PurchaseStatus.BLOCKED


def test_total_price_constraint_uses_payable_total() -> None:
    """A generic total_price constraint is a second cap, so it also sees the larger total."""
    contract = with_constraints({"field": "total_price", "operator": "lte", "value": 128.38})
    decision, _ = run(contract, mutate=lambda r: r.update(total=128.38))  # parts are 128.39
    assert decision.results[-1].verdict == FAIL
