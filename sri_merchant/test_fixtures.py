#!/usr/bin/env python3
"""
Sends each TransactionProposal fixture to Shuban's backend and checks the
returned decision against what we expect (see README / hackathon spec):

    valid                -> AUTHORIZED
    price_bump           -> BLOCKED
    hidden_subscription  -> BLOCKED
    product_swap         -> BLOCKED
    unknown_seller       -> ESCALATED
    late_delivery        -> BLOCKED

Bonus fixtures (not in the original required list) are checked too:
    vague_delivery       -> ESCALATED
    prompt_injection     -> BLOCKED

This script only reports facts about backend behavior — it never decides
authorization itself. That's Intent Diff's job.

Usage:
    python3 test_fixtures.py                       # posts to http://localhost:8000/purchases
    HANDSHAKE_BACKEND_URL=http://host:port/path python3 test_fixtures.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BACKEND_URL = os.environ.get("HANDSHAKE_BACKEND_URL", "http://localhost:8000/purchases")

EXPECTED = {
    "valid": "AUTHORIZED",
    "price_bump": "BLOCKED",
    "hidden_subscription": "BLOCKED",
    "product_swap": "BLOCKED",
    "unknown_seller": "ESCALATED",
    "late_delivery": "BLOCKED",
    "vague_delivery": "ESCALATED",
    "prompt_injection": "BLOCKED",
}

# Response field the backend is expected to return the decision in.
# Adjust this if Shuban's Intent Diff uses a different key name.
DECISION_FIELD = "decision"


def post_fixture(payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BACKEND_URL, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    if not os.path.isdir(FIXTURES_DIR):
        print(f"No fixtures/ directory found at {FIXTURES_DIR}. Run generate_fixtures.py first.")
        sys.exit(1)

    results = []
    for name, expected in EXPECTED.items():
        path = os.path.join(FIXTURES_DIR, f"{name}.json")
        if not os.path.exists(path):
            print(f"[SKIP] {name}: fixture file missing ({path})")
            continue
        with open(path) as f:
            payload = json.load(f)

        try:
            response = post_fixture(payload)
        except urllib.error.URLError as e:
            print(f"[SKIP] {name}: backend unreachable at {BACKEND_URL} ({e})")
            continue
        except Exception as e:
            print(f"[ERROR] {name}: request failed ({e})")
            results.append((name, expected, None, False))
            continue

        actual = response.get(DECISION_FIELD)
        ok = actual == expected
        results.append((name, expected, actual, ok))
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: expected={expected} actual={actual}")

    if not results:
        print("\nNo fixtures were checked (backend likely not running).")
        return

    passed = sum(1 for *_r, ok in results if ok)
    print(f"\n{passed}/{len(results)} fixtures matched expected decision.")
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
