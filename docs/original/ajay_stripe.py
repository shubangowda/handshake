"""Create temporary virtual cards using Stripe's Link CLI (no Python SDK needed).

Setup:
    npm install -g @stripe/link-cli
    python stripe.py login
    python stripe.py payment-methods

Create a request (amount is in minor units, e.g. 3500 = USD 35.00):
    python stripe.py create --merchant-name "Stripe Press" \
        --merchant-url "https://press.stripe.com" --amount 3500 \
        --context "Purchase one copy of Working in Public from Stripe Press for my personal reading. The total includes all shipping and taxes." --test

Approve using the URL returned by Link, then use the returned request ID:
    python stripe.py retrieve lsrq_REPLACE_ME --output-file /tmp/link-card.json

Remove --test to request a real card. Use this for merchants accepting cards.
Link controls approval, eligibility, spend limits, and credential expiration.
Retrieval saves credentials with mode 0600 and refuses to overwrite a file.
Only redacted card details are printed. This script does not submit a payment.
Reference: https://github.com/stripe/link-cli
"""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


def link_command():
    """Prefer an installed CLI; fall back to the official npm package."""
    executable = shutil.which("link-cli")
    if executable:
        return [executable]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "@stripe/link-cli"]
    raise RuntimeError("Install Node.js, then run: npm install -g @stripe/link-cli")


def run_link(arguments, *, capture=False):
    # Pass arguments directly, never through a shell. Stream approval URLs.
    return subprocess.run(
        [*link_command(), *arguments, "--format", "json"],
        text=True,
        stdout=subprocess.PIPE if capture else None,
        check=True,
    )


def positive_amount(value):
    amount = int(value)
    if not 1 <= amount <= 50000:
        raise argparse.ArgumentTypeError("amount must be between 1 and 50000 minor units")
    return amount


def parse_auth_status(output):
    """Accept a single status or the CLI's array of streamed status updates."""
    status = json.loads(output)
    if isinstance(status, list):
        if not status:
            raise ValueError("Link CLI returned an empty authentication status list")
        status = status[-1]
    if not isinstance(status, dict) or type(status.get("authenticated")) is not bool:
        raise ValueError("Link CLI returned an unrecognized authentication status")
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Connect your Link account in the browser")
    commands.add_parser("payment-methods", help="List available funding methods")
    create = commands.add_parser("create", help="Request a temporary card and Link approval")
    create.add_argument("--merchant-name", required=True)
    create.add_argument("--merchant-url", required=True)
    create.add_argument("--amount", type=positive_amount, required=True, help="Final total including tax/shipping, in minor units")
    create.add_argument("--currency", default="usd")
    create.add_argument("--context", required=True, help="Purchase description of at least 100 characters")
    create.add_argument("--payment-method-id", help="Omit to use Link's default eligible method")
    create.add_argument("--test", action="store_true", help="Request test credentials instead of a real card")
    retrieve = commands.add_parser("retrieve", help="Retrieve an approved card into a private JSON file")
    retrieve.add_argument("request_id")
    retrieve.add_argument("--output-file", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "create":
        if len(args.context.strip()) < 100:
            parser.error("--context must describe the purchase in at least 100 characters")
        url = urlsplit(args.merchant_url)
        if url.scheme not in ("https", "http") or not url.hostname:
            parser.error("--merchant-url must be a valid HTTP(S) URL")
        if not args.merchant_name.strip():
            parser.error("--merchant-name must not be empty")
        if not re.fullmatch(r"[A-Za-z]{3}", args.currency):
            parser.error("--currency must be a three-letter ISO currency code")
    if args.command == "retrieve":
        if not re.fullmatch(r"lsrq_[A-Za-z0-9_]+", args.request_id):
            parser.error("request_id must be a Link spend request ID (lsrq_...)")
        args.output_file = args.output_file.expanduser().absolute()
        if args.output_file.exists() or args.output_file.is_symlink():
            parser.error("--output-file already exists; choose a new filename")
        if not args.output_file.parent.is_dir():
            parser.error("--output-file parent directory must exist")

    try:
        status = parse_auth_status(run_link(["auth", "status"], capture=True).stdout)
        if not status.get("authenticated"):
            if args.command != "login":
                raise RuntimeError("Not authenticated. Run: python stripe.py login")
            run_link(["auth", "login", "--client-name", "Handshake Temporary Card", "--interval", "5", "--timeout", "300"])
            return 0
        if args.command == "login":
            print("Already authenticated with Link.")
        elif args.command == "payment-methods":
            run_link(["payment-methods", "list"])
        elif args.command == "create":
            command = [
                "spend-request", "create", "--credential-type", "card",
                "--merchant-name", args.merchant_name,
                "--merchant-url", args.merchant_url,
                "--amount", str(args.amount), "--currency", args.currency.lower(),
                "--context", args.context, "--request-approval",
            ]
            if args.payment_method_id:
                command.extend(["--payment-method-id", args.payment_method_id])
            if args.test:
                command.append("--test")
            run_link(command)
            print("Approve in Link if requested, then run: python stripe.py retrieve <request-id> --output-file <new-file.json>", file=sys.stderr)
        else:
            # Link handles masking and exclusive, permission-restricted file creation.
            # Pending requests can be retrieved again after the user approves.
            run_link(["spend-request", "retrieve", args.request_id,
                      "--include", "card", "--output-file", str(args.output_file)])
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"Link CLI failed (exit {exc.returncode}). Resolve its error before retrying; do not recreate an existing spend request unnecessarily.", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. An existing spend request may still be active in Link.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
