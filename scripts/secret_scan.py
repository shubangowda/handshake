"""
secret_scan.py: refuse to commit or push anything that looks like a secret.

Why this file exists (it is not in the build spec's scripts/ list): section 13
requires a secret scan before EVERY commit and push. A script is the only way to
run the same scan the same way every time, so it earns its own file.

How it works
------------
- It scans the STAGED version of each file (what `git commit` would record),
  read through `git show :path`, never the working copy and never a .env file.
- For every hit it prints only "path:line: kind". It never prints the matched
  value, so running the scan can't leak the thing it found.
- It exits 1 if anything is found, so it can gate a commit:
      python scripts/secret_scan.py && git commit ...
- With --all it scans every tracked file instead (used before a push).

What it looks for
-----------------
- OpenAI-style keys (sk-...), GitHub/AWS/Stripe secret key prefixes
- "Bearer <long token>" strings
- PEM private keys
- Link CLI session/card material (session tokens, card files)
- 13 to 19 digit card-like numbers that pass the Luhn check, EXCEPT the
  documented public test card numbers (e.g. 4242 4242 4242 4242), which are
  published by card networks and processors specifically for testing.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Documented public test card numbers. They are safe to appear in code and
# tests because processors publish them for exactly that purpose; they can't
# charge a real account.
DOCUMENTED_TEST_CARDS = {
    "4242424242424242",  # Visa (Stripe docs)
    "4000056655665556",  # Visa debit (Stripe docs)
    "5555555555554444",  # Mastercard (Stripe docs)
    "378282246310005",  # American Express (Stripe docs)
    "4111111111111111",  # Visa (widely documented generic test number)
}

# (kind, compiled pattern). Kinds are what gets printed; values never are.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai-style key", re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{20,}")),
    ("stripe secret key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer token", re.compile(r"Bearer\s+[A-Za-z0-9._\-~+/]{24,}")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----")),
    ("link session token", re.compile(r"\"(?:access_token|refresh_token|session_token)\"\s*:\s*\"[^\"]{16,}\"")),
]

# Files that are never read at all (secrets by definition). The scan reports
# them by path if they are staged, because being staged is itself the problem.
FORBIDDEN_PATH = re.compile(r"(^|/)\.env($|\.(?!example$)[^/]*$)|(^|/)link-card[^/]*\.json$")

CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def luhn_ok(digits: str) -> bool:
    """True if a digit string passes the Luhn checksum used by card numbers."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:  # double every second digit from the right
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def git_lines(*args: str) -> list[str]:
    """Run a git command and return its output lines."""
    output = subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout
    return [line for line in output.splitlines() if line]


def staged_text(path: str, scan_all: bool) -> str | None:
    """Content that would be committed (index version), or None for binary/unreadable files."""
    spec = f"HEAD:{path}" if scan_all else f":{path}"
    result = subprocess.run(["git", "show", spec], capture_output=True)
    if result.returncode != 0:
        return None
    raw = result.stdout
    if b"\x00" in raw[:8192]:
        return None  # binary (images); secrets of the kinds above are text
    return raw.decode("utf-8", errors="replace")


def scan(scan_all: bool) -> list[str]:
    """Return 'path:line: kind' strings for every hit."""
    if scan_all:
        paths = git_lines("ls-files")
    else:
        paths = git_lines("diff", "--cached", "--name-only", "--diff-filter=ACMR")

    hits: list[str] = []
    for path in paths:
        if FORBIDDEN_PATH.search(path):
            hits.append(f"{path}:0: forbidden file (env/card file) is staged")
            continue  # never read it
        text = staged_text(path, scan_all)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in PATTERNS:
                if pattern.search(line):
                    hits.append(f"{path}:{number}: {kind}")
            for match in CARD_CANDIDATE.finditer(line):
                digits = re.sub(r"[ -]", "", match.group(0))
                if 13 <= len(digits) <= 19 and luhn_ok(digits) and digits not in DOCUMENTED_TEST_CARDS:
                    hits.append(f"{path}:{number}: card-like number")
    return hits


def main() -> int:
    """Scan staged files (or all tracked files with --all); exit 1 on any hit."""
    scan_all = "--all" in sys.argv
    hits = scan(scan_all)
    if hits:
        print("SECRET SCAN FAILED. Nothing was committed. Hits (values not shown):", file=sys.stderr)
        for hit in hits:
            print("  " + hit, file=sys.stderr)
        return 1
    print(f"secret scan: clean ({'all tracked files' if scan_all else 'staged files'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
