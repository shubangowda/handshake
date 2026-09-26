"""
demo.py: run Sri's eight red-team scenarios against the LIVE stack (replaces his test_fixtures.py).

    python scripts/dev.py            (in another terminal first)
    python scripts/demo.py           all 8 scenarios
    python scripts/demo.py valid     just one, for the live demo
    python scripts/demo.py --executor   expect the backend to be in executor mode

For each scenario, with a FRESH contract (contracts are single use):
    1. log in as the user (demo auth) and seed + sign the section 12.1 demo contract
    2. create a checkout session at the mock merchant
    3. request the purchase AS THE AGENT, with an idempotency key
    4. for valid: approve the payment (stub: "Simulated provider approval";
       link_test: print the Link approval URL and wait), then either collect the
       card once and pay the merchant's pay API as the agent (agent_visible), or
       let the backend pay (executor), and poll until completed
It prints a table (scenario, expected, actual, main reason, purchase page) and
exits nonzero on any mismatch. URLs come from config.py, never from literals.

Sri's test_fixtures.py expected an uppercase "decision" field that the real
API never had; this script reads the real PurchaseDetail `status` instead.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from handshake import compiler, config  # noqa: E402

EXPECTED = {
    "valid": "completed",
    "price_bump": "blocked",
    "hidden_subscription": "blocked",
    "product_swap": "blocked",
    "unknown_seller": "escalated",
    "late_delivery": "blocked",
    "vague_delivery": "escalated",
    "prompt_injection": "blocked",
}


class Demo:
    """A small client for the backend (as the user and as the agent) and the merchant."""

    def __init__(self, settings: config.Settings, email: str) -> None:
        """Log in as the user; the agent uses the static token from .env (dev.py generates it)."""
        self.settings = settings
        self.api = httpx.Client(base_url=settings.api_url, timeout=30)
        self.merchant = httpx.Client(base_url=settings.merchant_url, timeout=30)
        login = self.api.post("/auth/demo-login", json={"email": email})
        login.raise_for_status()
        self.user = {"Authorization": f"Bearer {login.json()['token']}"}
        if not settings.agent_token:
            raise SystemExit("HANDSHAKE_AGENT_TOKEN is not set. Run `python scripts/dev.py` once (it generates one into .env).")
        me = self.api.get("/auth/me", headers={"Authorization": f"Bearer {settings.agent_token}"}).json()
        if me.get("email") != email:
            raise SystemExit(f"The agent token acts for {me.get('email')!r}; run with --email {me.get('email')}.")
        self.agent = {"Authorization": f"Bearer {settings.agent_token}"}

    def sign_demo_contract(self) -> str:
        """Seed the exact section 12.1 contract (same builder as the fixture compiler) and sign it as the user."""
        draft = compiler.demo_contract_draft()
        created = self.api.post("/contracts", json=draft.model_dump(mode="json"), headers=self.user)
        created.raise_for_status()
        signed = self.api.post(f"/contracts/{draft.id}/sign", headers=self.user)
        signed.raise_for_status()
        return signed.json()["id"]

    def purchase(self, contract_id: str, scenario: str) -> dict[str, Any]:
        """Open a checkout for the scenario and request it as the agent."""
        checkout = self.merchant.post("/api/checkout-sessions", json={"scenario": scenario})
        checkout.raise_for_status()
        body = {"contract_id": contract_id, "checkout_url": checkout.json()["checkout_url"], "idempotency_key": "demo-" + secrets.token_hex(8)}
        response = self.api.post("/purchases", json=body, headers=self.agent)
        if response.status_code >= 400:
            raise RuntimeError(f"{scenario}: HTTP {response.status_code} {response.json().get('message')}")
        return response.json()

    def status(self, purchase_id: str, who: dict[str, str]) -> dict[str, Any]:
        """Refresh and read a purchase."""
        response = self.api.post(f"/purchases/{purchase_id}/payment/refresh", headers=who)
        response.raise_for_status()
        return response.json()

    def complete_valid(self, detail: dict[str, Any], credential_mode: str, payment_mode: str) -> dict[str, Any]:
        """Take an AUTHORIZED purchase all the way to completed."""
        purchase_id = detail["purchase_id"]
        if payment_mode == "stub":
            self.api.post(f"/purchases/{purchase_id}/payment/simulate-approval", headers=self.user).raise_for_status()
        else:
            print(f"    Approve the payment in Link (TEST MODE): {detail.get('approval_url')}")
        deadline = time.time() + (600 if payment_mode == "link_test" else 30)
        while time.time() < deadline:
            detail = self.status(purchase_id, self.agent)
            if detail["next_action"] == "get_payment_credential_and_pay" and credential_mode == "agent_visible":
                self.pay_as_agent(purchase_id)
            elif detail["status"] in ("completed", "blocked", "failed"):
                return detail
            time.sleep(1.5)
        return detail

    def pay_as_agent(self, purchase_id: str) -> None:
        """Collect the card ONCE and submit it to the merchant's pay API with exactly the returned amount. Never print it."""
        card = self.api.post(f"/purchases/{purchase_id}/credential", headers=self.agent)
        card.raise_for_status()
        c = card.json()
        body = {k: c[k] for k in ("card_number", "exp_month", "exp_year", "cvc", "amount", "currency")}
        paid = httpx.post(c["pay_url"], json=body, timeout=30)
        body.clear()
        paid.raise_for_status()


def main_reason(detail: dict[str, Any]) -> str:
    """The most important reason for the outcome, in plain words."""
    if detail["status"] == "completed":
        receipt = (detail.get("payment") or {}).get("receipt") or {}
        return f"paid {receipt.get('amount_charged')} {receipt.get('currency')}, order {receipt.get('order_id')} verified, card ...{receipt.get('last4')}"
    results = (detail.get("decision") or {}).get("results", [])
    for wanted in ("fail", "unverifiable"):
        for result in results:
            if result["verdict"] == wanted and result["severity"] != "soft":
                return f"{result.get('label', result['constraint'])}: {result['reason']}"
    return detail.get("summary", "")


def main() -> int:
    """Run the scenarios and print the table."""
    parser = argparse.ArgumentParser(description="Run the Handshake red-team scenarios against the live stack.")
    parser.add_argument("scenario", nargs="?", choices=sorted(EXPECTED), help="Run only this scenario")
    parser.add_argument("--executor", action="store_true", help="Expect HANDSHAKE_CREDENTIAL_MODE=executor on the backend")
    parser.add_argument("--email", default=None, help="The user to log in as (default: HANDSHAKE_AGENT_OWNER)")
    args = parser.parse_args()

    settings = config.get_settings()
    try:
        health = httpx.get(f"{settings.api_url}/health", timeout=5).json()
        httpx.get(f"{settings.merchant_url}/health", timeout=5).raise_for_status()
    except httpx.HTTPError:
        print(f"The stack isn't running. Start it with: python scripts/dev.py  (backend {settings.api_url}, merchant {settings.merchant_url})")
        return 2
    expected_mode = "executor" if args.executor else "agent_visible"
    if health["credential_mode"] != expected_mode:
        print(f"The backend is in {health['credential_mode']} mode; run with{'out' if health['credential_mode'] == 'agent_visible' else ''} --executor.")
        return 2
    print(f"{health['payment_label']} | credential mode {health['credential_mode']} | compiler {health['compiler_mode']}\n")

    demo = Demo(settings, args.email or settings.agent_owner)
    rows: list[tuple[str, str, str, str, str]] = []
    mismatches = 0
    for scenario in ([args.scenario] if args.scenario else list(EXPECTED)):
        detail = demo.purchase(demo.sign_demo_contract(), scenario)
        if scenario == "valid" and detail["status"] == "authorized":
            detail = demo.complete_valid(detail, health["credential_mode"], health["payment_mode"])
        actual = detail["status"]
        if actual != EXPECTED[scenario]:
            mismatches += 1
        rows.append((scenario, EXPECTED[scenario], actual, main_reason(detail), detail["review_url"]))

    widths = [max(len(row[i]) for row in rows + [("scenario", "expected", "actual", "reason", "purchase page")]) for i in range(3)]
    header = f"{'scenario':{widths[0]}}  {'expected':{widths[1]}}  {'actual':{widths[2]}}  ok  main reason / purchase page"
    print(header)
    print("-" * len(header))
    for scenario, expected, actual, reason, url in rows:
        mark = "OK" if expected == actual else "XX"
        print(f"{scenario:{widths[0]}}  {expected:{widths[1]}}  {actual:{widths[2]}}  {mark}  {reason[:110]}")
        print(f"{'':{widths[0] + widths[1] + widths[2] + 10}}{url}")
    print(f"\n{len(rows) - mismatches}/{len(rows)} scenarios matched.")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
