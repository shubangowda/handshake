# Decisions

This file explains why the integrated Handshake works the way it does. Most entries resolve a conflict between the teammates' specs; some record a fact verified against a real tool. Section numbers refer to `HANDSHAKE_BUILD.md`.

## Stripe Link CLI: what was verified (section 9.2)

**Version.** `@stripe/link-cli` **0.23.0**, the latest on npm on 2026-09-26. It is pinned in config as `HANDSHAKE_LINK_CLI="npx --yes @stripe/link-cli@0.23.0"`.

**Sources.** Everything below was read from three places: `--help` and `--schema` for each command, `--llms-full`, and the package's own `dist/cli.js`. The official README at github.com/stripe/link-cli was read as well. Nothing below is guessed.

**Link account.** This machine is **not logged in to Link**. `auth status` printed `[{"authenticated": false, "credentials_path": "…"}]`. So no spend request was ever created, and `link_test` mode was not exercised against Stripe. The steps to run it are in `docs/DEMO.md`.

| What | Verified shape |
|---|---|
| `auth status --format json` | Returns a **list** of status objects when unauthenticated, e.g. `[{"authenticated": false, "credentials_path": "…"}]`. Ajay's parser, which accepts an object or a list and takes the last entry, is kept. |
| `spend-request create` | Flags: `--credential-type card`, `--merchant-name`, `--merchant-url`, `--amount <cents>`, `--currency usd`, `--context <≥100 chars>`, `--line-item "name:…,unit_amount:…,quantity:…"`, `--total "type:total,display_text:Total,amount:…"`, `--idempotency-key`, `--metadata key:value`, `--request-approval` (on by default; the negation is `--no-request-approval`), `--test`, and `--expires-at`. There is also an **undocumented `--approve` flag**, which Handshake never passes. |
| Behavior of `create --format json` with `--request-approval` | In agent/JSON mode it returns **immediately**, without polling. The response is `{…spend request, "instruction": "Present the approval_url to the user…", "_next": {"command": "spend-request retrieve <id> --interval 2 --max-attempts 300"}}`, and `approval_url` is included when approval was requested. A duplicate idempotency key returns an error that names the existing request, which is then retrieved instead. |
| `spend-request retrieve <id>` | `--include card --output-file <path>` writes the full card to a file the CLI creates with mode **0600**. The CLI refuses to overwrite an existing file unless `--force` is given. Stdout then shows only redacted card fields plus `card_output_file`. The card object has `number`, `cvc`, `exp_month`, `exp_year`, `billing_address`, and `valid_until`. |
| `--test` on retrieve and cancel | **Rejected**: `{"code": "UNKNOWN", "message": "Unknown flag: --test"}`. Test mode is a property a request gets when it is created. |
| Errors | Printed as `{"code": "…", "message": "…"}`. |
| Statuses | `created`, `pending_approval`, `requires_action`, `approved`, `submitted`, `succeeded`, `denied`, `declined`, `canceled`, `expired`, `failed`. These are mapped in `payments.LINK_STATUS_MAP`. Any other value is treated as `unknown`, and an unknown status never advances a payment. |
| Limits (README) | 50,000 cents maximum per request. The approval window is 10 minutes. Cards are valid for 12 hours. There is a $500 daily limit, at most 30 active or 10 approved requests at once, and at most 50 creations per hour. |
| Test mode (README) | Test mode "will return test payment credentials (e.g. test card `4000009990001984`)" and "will not charge the underlying payment method". |

**Capabilities not claimed.** Link issues a single-use test card. Handshake does not claim merchant locking, exact spend limits, or an expiry that the CLI doesn't report. The 50,000-cent limit is enforced by Handshake itself (`HANDSHAKE_LINK_MAX_MINOR_UNITS`) as well as documented by Link.

**How test mode is enforced.** Every spend request is created through `payments.build_create_command()`, which always ends the command with `--test`. No function takes a parameter to remove it. `HANDSHAKE_PAYMENT_MODE` accepts only `stub` or `link_test`; `live`, an empty value, or a typo refuses to start. `retrieve` and `cancel` only accept `lsrq_…` ids that the backend itself created. Tests pin all of this down (`tests/test_payments.py`).
