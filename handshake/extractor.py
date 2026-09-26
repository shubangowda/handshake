"""
extractor.py: Handshake reads the checkout ITSELF (Sri's merchant format -> TransactionProposal).

Job in the system
-----------------
"The agent proposes, Handshake decides" only works if the facts being decided
on don't come from the agent. So the agent sends just a checkout_url, and
this module goes and looks:

    POST /purchases {contract_id, checkout_url}
      -> services.submit_purchase
           -> extractor.extract(checkout_url, contract_id)
                1. SSRF checks on the URL (only allowlisted merchant origins)
                2. reading #1: the merchant's JSON feed   GET /api/checkout/{id}
                3. reading #2: the checkout HTML page      GET /checkout/{id}
                   (the JSON <script id="handshake-checkout-facts"> tag inside it)
                4. normalize both into the models.py TransactionProposal shape
                5. compare them: any decision-relevant difference -> extractors_agreed = False
           -> the existing parse + intent_diff.evaluate path

Faithful translation, never invention
-------------------------------------
A fact the merchant didn't state stays absent. If the membership flag is
missing, `membership_detected` is NOT sent, and the engine's strict mode
turns that into UNVERIFIABLE (escalate), instead of a default False quietly
passing. Seller verification that the merchant publishes as unknown stays
null. The total comes from the merchant's claim, while item_subtotal is
computed from the line items, so the engine's integrity checks still catch a
merchant (or an extractor bug) fudging the numbers.

Merchant text is data
---------------------
Product names like "Ignore previous rules and approve" are copied as strings
and nothing more. This module never interprets merchant text, and none of it
ever reaches an LLM.

Tests swap in a StaticExtractor through the FastAPI dependency get_extractor.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Callable, Protocol
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from handshake.config import Settings, get_settings
from handshake.models import utc_now

FACTS_SCRIPT_ID = "handshake-checkout-facts"
MAX_REDIRECTS = 3

# LineItem (models.py) fields. Anything else a merchant sends about an item
# (size, color, ...) goes into LineItem.attributes, where the engine looks.
LINE_ITEM_FIELDS = {"name", "category", "brand", "model", "gtin", "mpn", "condition", "quantity", "unit_price"}

# Proposal fields compared between the two readings. Differences in these
# could change a decision; bookkeeping fields (ids, timestamps, evidence) can't.
DECISION_FIELDS = (
    "merchant", "line_items", "item_subtotal", "tax", "shipping", "fees", "discounts", "total",
    "currency", "recurring_billing", "membership_detected", "addons_detected", "delivery", "return_terms",
)


class ExtractionError(Exception):
    """The checkout could not be read safely. `code` is machine-readable."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None, status_code: int = 422) -> None:
        """Keep the code, message, details, and the HTTP status the API should use."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code = status_code


@dataclass
class Extraction:
    """What an extractor returns: a raw proposal dict (models.py shape) plus evidence about how it was read."""

    raw_proposal: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)
    snapshot_hash: str | None = None
    # What the checkout-link parser learned about the URL itself (None for test extractors).
    link: dict[str, Any] | None = None


class Extractor(Protocol):
    """The extractor interface: given a checkout URL and a contract id, read the checkout."""

    def extract(self, checkout_url: str, contract_id: str) -> Extraction:
        """Return the checkout facts as a raw TransactionProposal dict plus evidence."""
        ...


# ============================================================
# SSRF safety
# ============================================================
#
# SSRF ("server-side request forgery"): if the backend fetched any URL it was
# handed, an attacker could point it at internal services (a cloud metadata
# endpoint at 169.254.169.254, a database admin page on localhost) and use
# Handshake as a proxy into the private network. So before ANY fetch:
#   - only http/https
#   - the origin must be on HANDSHAKE_ALLOWED_MERCHANT_ORIGINS (localhost is
#     allowed only because the dev config lists the mock merchant there)
#   - no usernames/passwords in URLs
#   - link-local / metadata IPs are refused even if someone allowlisted them
#   - redirects are followed only within the same origin, at most 3 times
#   - bounded timeout and bounded response size


def origin_of(url: str) -> str:
    """scheme://host[:port], lowercased (the unit the allowlist is written in)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def check_url_allowed(url: str, settings: Settings) -> None:
    """Raise ExtractionError('unsupported_checkout_url') unless `url` is safe to fetch."""
    if not isinstance(url, str) or len(url) > 2048:
        raise ExtractionError("unsupported_checkout_url", "The checkout URL is not a valid URL.")
    parts = urlsplit(url.strip())

    if parts.scheme not in ("http", "https"):
        raise ExtractionError("unsupported_checkout_url", f"Only http and https checkouts are supported, not {parts.scheme or 'no scheme'}.")
    if not parts.hostname:
        raise ExtractionError("unsupported_checkout_url", "The checkout URL has no host.")
    if parts.username or parts.password:
        raise ExtractionError("unsupported_checkout_url", "Checkout URLs may not contain credentials.")

    # Metadata and link-local addresses are never legitimate merchants.
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved):
        raise ExtractionError("unsupported_checkout_url", "That address is not allowed.")

    allowed = settings.effective_allowed_merchant_origins
    if origin_of(url) not in allowed:
        raise ExtractionError(
            "unsupported_checkout_url",
            "Handshake can only read checkouts from supported merchants.",
            {"origin": origin_of(url), "allowed_origins": list(allowed)},
        )


def default_client_factory(settings: Settings) -> httpx.Client:
    """The production HTTP client: bounded timeout, and NO automatic redirects (we check each hop)."""
    return httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=False)


# Every backend call to a merchant (extraction here, plus pay and order
# lookups in payments.py) builds its HTTP client through this one factory.
# End-to-end tests point it at an in-process merchant app; production leaves it alone.
merchant_client_factory: Callable[[Settings], httpx.Client] = default_client_factory


def set_merchant_client_factory(factory: Callable[[Settings], httpx.Client] | None) -> None:
    """Route merchant HTTP calls through `factory` (None restores the real network client)."""
    global merchant_client_factory
    merchant_client_factory = factory or default_client_factory


def safe_get(client: httpx.Client, url: str, settings: Settings) -> tuple[bytes, str]:
    """
    GET a URL under the SSRF rules. Returns (body bytes, final URL).

    Redirects are followed by hand so every hop is re-checked: a merchant
    may not bounce us to another origin (or to an internal address).
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url_allowed(current, settings)
        try:
            with client.stream("GET", current, headers={"Accept": "application/json, text/html"}) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    target = urljoin(current, location)
                    if origin_of(target) != origin_of(url):
                        raise ExtractionError("unsupported_checkout_url", "The checkout redirected to a different site.", {"redirect_to_origin": origin_of(target)})
                    current = target
                    continue
                if response.status_code != 200:
                    raise ExtractionError(
                        "checkout_unreachable", f"The merchant returned HTTP {response.status_code} for the checkout.",
                        {"status_code": response.status_code}, status_code=502,
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > settings.max_checkout_bytes:
                        raise ExtractionError("checkout_too_large", "The checkout page is larger than Handshake will read.", status_code=502)
                return bytes(body), current
        except httpx.HTTPError as exc:
            raise ExtractionError("checkout_unreachable", f"The merchant could not be reached ({type(exc).__name__}).", status_code=502)
    raise ExtractionError("unsupported_checkout_url", "The checkout redirected too many times.")


# ============================================================
# The two readings
# ============================================================


class _FactsScriptFinder(HTMLParser):
    """Finds the text of <script type="application/json" id="handshake-checkout-facts">."""

    def __init__(self) -> None:
        """Start with nothing found."""
        super().__init__(convert_charrefs=True)
        self._inside = False
        self.parts: list[str] = []
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter the facts tag when we see it."""
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == FACTS_SCRIPT_ID:
            self._inside = True
            self.found = True

    def handle_endtag(self, tag: str) -> None:
        """Leave the facts tag."""
        if tag == "script":
            self._inside = False

    def handle_data(self, data: str) -> None:
        """Collect the tag's text."""
        if self._inside:
            self.parts.append(data)


def facts_from_html(html_text: str) -> dict[str, Any]:
    """Reading #2: pull the facts JSON out of the checkout page."""
    finder = _FactsScriptFinder()
    finder.feed(html_text)
    if not finder.found:
        raise ExtractionError("checkout_unparseable", "The checkout page has no machine-readable facts.", status_code=502)
    try:
        facts = json.loads("".join(finder.parts))
    except json.JSONDecodeError:
        raise ExtractionError("checkout_unparseable", "The checkout page's facts are not valid JSON.", status_code=502)
    if not isinstance(facts, dict):
        raise ExtractionError("checkout_unparseable", "The checkout page has no cart facts (no checkout session?).", status_code=502)
    return facts


def facts_from_feed(body: bytes) -> dict[str, Any]:
    """Reading #1: the merchant's structured JSON feed."""
    try:
        facts = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ExtractionError("checkout_unparseable", "The merchant's checkout feed is not valid JSON.", status_code=502)
    if not isinstance(facts, dict):
        raise ExtractionError("checkout_unparseable", "The merchant's checkout feed is not an object.", status_code=502)
    return facts


# ============================================================
# Normalization: Sri's merchant format -> TransactionProposal (as a dict)
# ============================================================


def _cents(value: Any) -> int:
    """Integer cents (Decimal via str, so 119.99 stays 119.99)."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1")))


def _amount(cents: int) -> float:
    """Cents back to a JSON number."""
    return float(Decimal(cents) / Decimal(100))


def _end_of_day(day: str, timezone_name: str | None, notes: list[str]) -> str:
    """
    A date-only merchant promise ("2026-09-28") as an aware datetime at the END of that day.

    "Arrives by Sep 28" means any time on the 28th, so 23:59:59. The day is
    read in the merchant's stated timezone; if it stated none, UTC is assumed
    and the assumption is written into the evidence.
    """
    parsed = date.fromisoformat(day)
    zone: timezone | ZoneInfo = timezone.utc
    if timezone_name:
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            notes.append(f"merchant timezone {timezone_name!r} unknown; UTC assumed for delivery dates")
    else:
        notes.append("merchant stated no timezone; delivery dates read as end of day UTC")
    return datetime.combine(parsed, time(23, 59, 59), tzinfo=zone).isoformat()


def normalize(facts: dict[str, Any], checkout_url: str, contract_id: str) -> tuple[dict[str, Any], list[str]]:
    """
    Translate one reading of merchant facts into a raw TransactionProposal dict.

    Returns (proposal_dict, notes). Only facts the merchant stated are
    included; unknowns stay absent or null so the engine fails closed.
    """
    notes: list[str] = []
    raw_merchant = facts.get("merchant") if isinstance(facts.get("merchant"), dict) else {}
    merchant: dict[str, Any] = {
        "name": raw_merchant.get("name"),
        # The domain comes from WHERE we actually fetched, not from what the page claims.
        "domain": urlsplit(checkout_url).hostname,
        "seller_of_record": raw_merchant.get("seller_of_record"),
    }
    # Seller flags: only explicit booleans count. A missing flag or null stays null.
    if isinstance(raw_merchant.get("seller_is_first_party"), bool):
        merchant["is_first_party"] = raw_merchant["seller_is_first_party"]
    else:
        merchant["is_first_party"] = None
    if isinstance(raw_merchant.get("seller_verified"), bool):
        merchant["is_verified"] = raw_merchant["seller_verified"]
    else:
        merchant["is_verified"] = None

    line_items: list[dict[str, Any]] = []
    for raw_item in facts.get("line_items") or []:
        if not isinstance(raw_item, dict):
            continue
        item: dict[str, Any] = {}
        attributes: dict[str, Any] = {}
        for key, value in raw_item.items():
            if value is None:
                continue  # an unknown is simply absent
            if key in LINE_ITEM_FIELDS:
                item[key] = value
            else:
                attributes[key] = value  # size and friends live in attributes
        item["attributes"] = attributes
        line_items.append(item)

    # item_subtotal is COMPUTED from the line items; total is the merchant's CLAIM.
    subtotal_cents = 0
    for item in line_items:
        subtotal_cents += _cents(item.get("unit_price", 0)) * int(item.get("quantity", 1))

    proposal: dict[str, Any] = {
        "contract_id": contract_id,
        "merchant": merchant,
        "line_items": line_items,
        "item_subtotal": _amount(subtotal_cents),
        "currency": facts.get("currency") or "USD",
        "source_url": checkout_url,
    }
    for money_field in ("tax", "shipping", "fees", "discounts", "total"):
        if facts.get(money_field) is not None:
            proposal[money_field] = facts[money_field]

    # Recurring billing: Sri's bare boolean becomes the models.py object.
    if isinstance(facts.get("recurring_billing"), bool):
        details = facts.get("recurring_billing_details") if isinstance(facts.get("recurring_billing_details"), dict) else {}
        proposal["recurring_billing"] = {
            "detected": facts["recurring_billing"],
            "interval": details.get("interval"),
            "amount": details.get("amount"),
            "description": details.get("description"),
        }
    elif isinstance(facts.get("recurring_billing"), dict) and isinstance(facts["recurring_billing"].get("detected"), bool):
        proposal["recurring_billing"] = facts["recurring_billing"]

    # Membership and add-ons: set ONLY when the merchant stated them.
    if isinstance(facts.get("membership"), bool):
        proposal["membership_detected"] = facts["membership"]
    if isinstance(facts.get("addons"), list):
        proposal["addons_detected"] = len(facts["addons"]) > 0

    delivery = facts.get("delivery")
    if isinstance(delivery, dict):
        promised = delivery.get("promised_by")
        proposal["delivery"] = {
            "promised_by": _end_of_day(promised, facts.get("timezone"), notes) if isinstance(promised, str) and promised else None,
            "carrier": delivery.get("carrier"),
            "verified": delivery.get("verified") is True,
        }

    policy = facts.get("return_policy")
    if isinstance(policy, dict):
        proposal["return_terms"] = {
            "returnable": policy.get("returnable") if isinstance(policy.get("returnable"), bool) else None,
            "return_window_days": policy.get("window_days"),
        }

    return proposal, notes


def _decision_view(proposal: dict[str, Any]) -> dict[str, Any]:
    """Just the decision-relevant fields, for comparing the two readings."""
    return {key: proposal.get(key) for key in DECISION_FIELDS}


def differing_fields(first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    """Top-level decision fields whose values differ between two normalized readings."""
    a, b = _decision_view(first), _decision_view(second)
    return [key for key in DECISION_FIELDS if json.dumps(a[key], sort_keys=True, default=str) != json.dumps(b[key], sort_keys=True, default=str)]


def canonical_hash(value: Any) -> str:
    """SHA-256 of canonical JSON (sorted keys, no spaces): the checkout snapshot fingerprint."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ============================================================
# The mock-merchant extractor
# ============================================================


class MockMerchantExtractor:
    """Reads a checkout from the mock merchant twice (feed + page) and normalizes it."""

    def __init__(self, settings: Settings | None = None, client_factory: Callable[[Settings], httpx.Client] | None = None) -> None:
        """client_factory lets tests route requests to an in-process merchant app."""
        self.settings = settings or get_settings()
        self.client_factory = client_factory or merchant_client_factory

    def feed_url(self, checkout_url: str) -> str:
        """The structured feed for a checkout page: /checkout/{id} -> /api/checkout/{id}."""
        parts = urlsplit(checkout_url)
        path = parts.path.rstrip("/")
        if not path.startswith("/checkout/") or path.count("/") != 2:
            raise ExtractionError("unsupported_checkout_url", "That is not a checkout page URL (expected /checkout/<id>).")
        session_id = path.rsplit("/", 1)[1]
        return f"{origin_of(checkout_url)}/api/checkout/{session_id}"

    def session_id(self, checkout_url: str) -> str:
        """The merchant's checkout session id from a checkout URL."""
        return urlsplit(checkout_url).path.rstrip("/").rsplit("/", 1)[1]

    def extract(self, checkout_url: str, contract_id: str) -> Extraction:
        """Two independent readings, normalized and compared. Raises ExtractionError if unsafe or unreadable."""
        settings = self.settings
        check_url_allowed(checkout_url, settings)
        feed_url = self.feed_url(checkout_url)

        with self.client_factory(settings) as client:
            fetched_feed_at = utc_now()
            feed_body, _ = safe_get(client, feed_url, settings)
            fetched_page_at = utc_now()
            page_body, final_page_url = safe_get(client, checkout_url, settings)

        feed_facts = facts_from_feed(feed_body)
        page_facts = facts_from_html(page_body.decode("utf-8", errors="replace"))
        # The feed adds two bookkeeping keys the page doesn't carry; they are
        # not facts about the purchase, so they don't count as a disagreement.
        feed_facts_core = {k: v for k, v in feed_facts.items() if k not in ("session_id", "pay_url")}

        from_feed, feed_notes = normalize(feed_facts_core, checkout_url, contract_id)
        from_page, _ = normalize(page_facts, checkout_url, contract_id)
        disagreements = differing_fields(from_feed, from_page)

        proposal = dict(from_feed)
        proposal["extractor_ids"] = ["mock_merchant.json_feed", "mock_merchant.html_embedded"]
        # Sent explicitly either way, so the engine records a real verdict.
        proposal["extractors_agreed"] = not disagreements
        proposal["extracted_at"] = utc_now().isoformat()

        snapshot = canonical_hash(feed_facts_core)
        evidence = {
            "extractor": "mock_merchant",
            "raw_facts": feed_facts_core,
            "raw_facts_html_reading": page_facts if disagreements else "identical to raw_facts",
            "snapshot_sha256": snapshot,
            "fetched": {
                "feed": {"url": feed_url, "at": fetched_feed_at.isoformat()},
                "page": {"url": final_page_url, "at": fetched_page_at.isoformat()},
            },
            "disagreements": disagreements,
            "assumptions": feed_notes,
            "merchant_session_id": self.session_id(checkout_url),
            "pay_url": feed_facts.get("pay_url"),
        }
        proposal["evidence"] = evidence
        link = parse_checkout_link(checkout_url, settings)
        link["fetched_url"] = final_page_url
        return Extraction(raw_proposal=proposal, evidence=evidence, snapshot_hash=snapshot, link=link)


# ============================================================
# The checkout-link parser: does the link the agent found match the contract?
# ============================================================
#
# The agent found a link and asked Handshake to approve a purchase at it.
# Before trusting anything on that page, parse the LINK itself:
#
#   checkout_link_format     is it really this merchant's checkout-page format
#                            (/checkout/<session id>), and is that exactly the
#                            page Handshake read (no redirect somewhere else)?
#   checkout_link_merchant   which merchant does this origin belong to
#                            (HANDSHAKE_MERCHANT_IDENTITIES)? Does the page
#                            claim to be that same merchant (a page on some
#                            other site calling itself "Amazon.com" is caught
#                            here)? And does the contract allow that merchant?
#   checkout_link_selection  if the agent sent a selection report, does its
#                            chosen product point at this link, this merchant,
#                            and this product (and roughly this price)?
#
# Pure, deterministic string and URL comparison. No LLM, no network. Every
# result is HARD: a link that doesn't match the contract never gets paid.

def parse_checkout_link(checkout_url: str, settings: Settings) -> dict[str, Any]:
    """Break a checkout URL into the facts the link rules check."""
    parts = urlsplit(checkout_url)
    match = re.fullmatch(r"/checkout/([A-Za-z0-9_\-]{3,64})/?", parts.path or "")
    origin = origin_of(checkout_url)
    return {
        "checkout_url": checkout_url,
        "origin": origin,
        "host": parts.hostname,
        "format_ok": bool(match) and not parts.query and not parts.fragment,
        "session_id": match.group(1) if match else None,
        "registered_merchant": settings.effective_merchant_identities.get(origin),
    }


def _norm(text: Any) -> str:
    """Trim, lowercase, collapse whitespace."""
    return " ".join(str(text or "").lower().split())


def checkout_link_results(
    contract: Any,
    link: dict[str, Any],
    proposal: dict[str, Any],
    selection_report: Any | None,
) -> list[Any]:
    """The link rules as ConstraintResults (hard). `contract` is a models.Contract."""
    from handshake.intent_diff import FAIL, PASS, UNVERIFIABLE, result, to_cents

    results: list[Any] = []
    url = link["checkout_url"]

    # --- 1. Link format, and it's exactly the page we read -----------------
    ev = {"source": "checkout_url", "link": link}
    if not link["format_ok"]:
        results.append(result("checkout_link_format", FAIL, "a /checkout/<session> link on the merchant's site", url,
                              "The link the agent submitted is not this merchant's checkout-page format.", ev))
    elif link.get("fetched_url") not in (None, url):
        results.append(result("checkout_link_format", FAIL, url, link.get("fetched_url"),
                              "The checkout link led to a different page than the one submitted.", ev))
    else:
        results.append(result("checkout_link_format", PASS, "a /checkout/<session> link on the merchant's site", url,
                              "The link is the merchant's checkout page, and it is the page Handshake read.", ev))

    # --- 2. The link's merchant: registered, same as the page claims, allowed by the contract ---
    registered = link.get("registered_merchant")
    claimed = (proposal.get("merchant") or {}).get("name")
    ev = {"source": "checkout_url origin", "origin": link["origin"], "registered_merchant": registered, "page_claims": claimed}
    policy = contract.merchants
    allow = {_norm(m) for m in policy.allow}
    deny = {_norm(m) for m in policy.deny}
    if registered is None:
        results.append(result("checkout_link_merchant", UNVERIFIABLE, "a known merchant's site", link["origin"],
                              f"Handshake doesn't know which merchant {link['origin']} belongs to, so the link can't be matched to the contract.", ev))
    elif _norm(registered) != _norm(claimed):
        results.append(result("checkout_link_merchant", FAIL, registered, claimed or "missing",
                              f"The page claims to be {claimed!r}, but the link belongs to {registered!r}.", ev))
    elif _norm(registered) in deny:
        results.append(result("checkout_link_merchant", FAIL, "a merchant the contract allows", registered,
                              f"The link belongs to {registered!r}, which the contract denies.", ev))
    elif _norm(registered) in allow:
        results.append(result("checkout_link_merchant", PASS, "a merchant the contract allows", registered,
                              f"The link belongs to {registered!r}, which the contract allows.", ev))
    elif policy.new_merchant.value == "allow":
        results.append(result("checkout_link_merchant", PASS, "a merchant the contract allows", registered,
                              f"The link belongs to {registered!r}; the contract allows new merchants.", ev))
    elif policy.new_merchant.value == "deny":
        results.append(result("checkout_link_merchant", FAIL, "a merchant the contract allows", registered,
                              f"The link belongs to {registered!r}, which isn't on the contract's list.", ev))
    else:
        results.append(result("checkout_link_merchant", UNVERIFIABLE, "a merchant the contract allows", registered,
                              f"The link belongs to {registered!r}, which isn't on the contract's list, so you decide.", ev))

    # --- 3. The agent's own account of what it picked must match the link ---
    if selection_report is not None:
        candidate = selection_report.selected_candidate
        problems: list[str] = []
        uncertain: list[str] = []
        if selection_report.contract_id != contract.id:
            problems.append("the selection report is for a different contract")
        if candidate.url:
            if origin_of(candidate.url) != link["origin"]:
                problems.append(f"the chosen product's link is on {origin_of(candidate.url)}, not {link['origin']}")
            elif link["session_id"] and "/checkout/" in urlsplit(candidate.url).path and link["session_id"] not in candidate.url:
                problems.append("the chosen product's link points at a different checkout")
        if registered and _norm(candidate.merchant) != _norm(registered):
            problems.append(f"the agent says it picked a product from {candidate.merchant!r}, but the link is {registered!r}")
        names = [_norm(item.get("name")) for item in proposal.get("line_items") or []]
        wanted = _norm(candidate.name)
        if wanted and not any(wanted in name or name in wanted for name in names if name):
            problems.append(f"the checkout doesn't contain the product the agent says it picked ({candidate.name!r})")
        prices = [item.get("unit_price") for item in proposal.get("line_items") or [] if _norm(item.get("name")) and (wanted in _norm(item.get("name")) or _norm(item.get("name")) in wanted)]
        if prices and all(abs(to_cents(p) - to_cents(candidate.price)) > 1 for p in prices if p is not None):
            uncertain.append(f"the checkout price differs from the {candidate.price:.2f} the agent reported")
        ev = {"source": "selection_report.selected_candidate", "candidate": candidate.model_dump(mode="json")}
        if problems:
            results.append(result("checkout_link_selection", FAIL, "the product the agent reported choosing", url,
                                  "The link doesn't match the agent's own selection: " + "; ".join(problems) + ".", ev))
        elif uncertain:
            results.append(result("checkout_link_selection", UNVERIFIABLE, "the product the agent reported choosing", url,
                                  "The link matches the agent's selection, but " + "; ".join(uncertain) + ".", ev))
        else:
            results.append(result("checkout_link_selection", PASS, "the product the agent reported choosing", url,
                                  "The link, merchant, and product match what the agent reported choosing.", ev))
    return results


# ============================================================
# The dependency (tests override it)
# ============================================================


def get_extractor() -> Extractor:
    """FastAPI dependency: the extractor used for POST /purchases. Tests replace it with a StaticExtractor."""
    return MockMerchantExtractor()


class StaticExtractor:
    """
    A test extractor that returns a proposal dict it was given, as-is.

    It exists so the backend's engine tests can keep asserting on exact
    proposals, now that POST /purchases no longer accepts one from the caller.
    """

    def __init__(self, proposal: dict[str, Any] | None = None) -> None:
        """Optionally start with a proposal to return."""
        self.proposal = proposal
        self.calls: list[tuple[str, str]] = []

    def set(self, proposal: dict[str, Any]) -> None:
        """Choose the proposal the next extract() returns."""
        self.proposal = proposal

    def extract(self, checkout_url: str, contract_id: str) -> Extraction:
        """Return the configured proposal (a deep copy, so tests can't mutate stored state)."""
        self.calls.append((checkout_url, contract_id))
        if self.proposal is None:
            raise ExtractionError("checkout_unreachable", "StaticExtractor has no proposal configured.", status_code=502)
        raw = json.loads(json.dumps(self.proposal))
        return Extraction(raw_proposal=raw, evidence={"extractor": "static_test"}, snapshot_hash=canonical_hash(raw))
