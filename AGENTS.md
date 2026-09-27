# AGENTS.md: start here

This is the single source of truth for any AI coding agent working in this repo. If another file disagrees with this one, this file and [docs/DECISIONS.md](docs/DECISIONS.md) win. [docs/original/HANDSHAKE_BUILD.md](docs/original/HANDSHAKE_BUILD.md) is the historical build spec and is superseded wherever it conflicts with either.

## What Handshake is

Handshake is an authorization layer between an AI shopping agent and payment: the user describes what they're willing to buy, and Handshake turns it into a contract the user reviews, signs, and funds. When the agent finds a checkout, Handshake reads the checkout itself and checks every fact against the signed contract with deterministic code, blocking violations and escalating anything it can't verify to the user. Only a checkout Handshake authorizes unlocks the contract's single-use Stripe Link test card, and every step lands in a hash-chained evidence timeline.

The one-line rule: **the agent proposes, Handshake decides.**

## Non-negotiable rules

1. **LLMs never decide authorization.** The compiler LLM only *drafts* contracts. The engine (`handshake/intent_diff.py`) is pure, deterministic Python. Nothing on the purchase path calls a model.
2. **Fail closed.** A missing, ambiguous, unparseable, or disagreeing fact is UNVERIFIABLE, which escalates to the human. It is never PASS. Errors block; they don't allow.
3. **`handshake/models.py` is canonical and byte-frozen.** Never edit, reformat, or "fix" it, and never add a second copy. `tests/test_integrity.py` pins its SHA-256 (`63778dcd…7dac02`). New fields go in DB rows or API read models, not in models.py.
4. **Stripe Link is TEST MODE only.** Every Link CLI call carries the test flag, and there must be no code path that can request a live card. Never add a "live" switch.
5. **The agent never signs, approves, or decides.** Signing a contract, accepting an exception, and declining a purchase are user-only. The API enforces this by role (403 `agent_not_permitted`), and the denied attempt is recorded as evidence.
6. **Merchant text is data.** Product names, seller names, and page text are stored and compared as plain strings, never followed as instructions (see the `prompt_injection` scenario).
7. **Never read or commit `.env` files.** Don't open, cat, grep, print, copy, or summarize `.env` or `.env.*`; the `.example` files are the only exception. Never put a real key, token, or card number in a file, commit, log, or test. Fake values in tests must look fake (for example `FAKE-TOK`).
8. **No hardcoded hosts or ports outside `handshake/config.py`.** URLs and ports come from settings and env vars. `test_no_hardcoded_hosts_or_ports` fails with the exact file and line.
9. **Never print card values, access tokens, or Link session data.** Redact raw CLI output before showing or storing it.
10. **The GitHub repo stays private.**

## The payment model: fund at signing

This is the owner's decision (see docs/DECISIONS.md, "Owner decisions after the integration"), and it replaces the old "approve payment in Link after authorization" flow.

1. **Connect Link.** Each user connects **their own** Stripe Link account from the website ("Connect Stripe Link", `POST /link/connect`). Handshake keeps a separate Link CLI home per user under `.handshake-state/link-homes/`.
2. **Sign = fund.** Signing requests one single-use Link **test** card for the contract's **all-in hard cap**, and the user approves it in Link. Handshake then stores the card **encrypted** (AES-256-GCM, key `HANDSHAKE_CARD_ENCRYPTION_KEY`) on the contract. Funding goes `awaiting_approval → funded`.
3. **No funding, no purchase.** A purchase against an unfunded contract is refused with 409 `contract_not_funded` and recorded.
4. **Authorize, then unlock.** The card is unlocked only after Handshake authorizes a specific checkout. When the card is released, Handshake re-reads the checkout first (a changed cart gives `checkout_changed`, and the card stays stored). Then it decrypts the card, wipes the stored copy, and releases it **once**.
5. **Credential modes** (`HANDSHAKE_CREDENTIAL_MODE`):
   - `agent_visible` (default): the agent collects the card once through the MCP tool `get_payment_credential` and pays the merchant.
   - `executor`: the backend pays with the card itself, and the agent never sees it.
6. **After payment**, Handshake verifies the merchant order independently and reconciles it. An overcharge of even one cent fails the purchase.

The state machines are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and the routes are in [docs/API.md](docs/API.md).

## Repo map

| Path | What it is |
|---|---|
| `handshake/models.py` | The canonical Pydantic models shared by every part. **Frozen**; see rule 3. |
| `handshake/config.py` | All settings, env vars, hosts, and ports, plus secret validation (prod refuses dev secrets). |
| `handshake/db.py` | SQLAlchemy tables and helpers, including atomic conditional-UPDATE claims and the evidence ledger rows. |
| `handshake/intent_diff.py` | The deterministic engine: contract plus extracted checkout gives per-constraint verdicts. No I/O, no LLM. |
| `handshake/compiler.py` | Intent to DRAFT contract: an OpenAI structured-output call, or the offline demo fixture when there's no key. |
| `handshake/prompts.py` | The compiler and agent-workflow prompts. `COMPILER_PROMPT` is hash-pinned; add new text as new constants. |
| `handshake/extractor.py` | Reads the checkout from the merchant (structured feed plus page facts, twice) and runs the checkout-link parser. |
| `handshake/services.py` | The business logic: signing, funding, purchases, escalation, payment state machine, evidence, and read models. |
| `handshake/api.py` | The FastAPI app factory `create_app()` and routes. Thin; it delegates to services. |
| `handshake/auth.py` | Demo user login, agent tokens, device-flow agent login, and roles and ownership. |
| `handshake/payments.py` | Payment providers (`stub`, `link_test`), the Link CLI adapter and output parsers, per-user Link login, and card encryption. |
| `handshake/mcp_server.py` | The MCP server (`handshake-mcp`, FastMCP, stdio or HTTP), a thin client of the backend. |
| `handshake/merchant.py` | The mock merchant (FastAPI) and its eight red-team checkout scenarios. |
| `merchant_static/` | The mock store's page and assets. |
| `frontend/` | The Next.js 16 / React 19 user app. Read [frontend/AGENTS.md](frontend/AGENTS.md) first: this Next.js has breaking changes. |
| `scripts/dev.py` | Starts backend, merchant, and frontend, and generates missing secrets into `.env` without printing them. |
| `scripts/demo.py` | Runs the eight scenarios against the live stack and prints a table. |
| `scripts/secret_scan.py` | Scans for keys, tokens, and card numbers (staged files by default, `--all` for every tracked file). |
| `tests/` | pytest: engine, API, auth, compiler, extractor, payments, MCP, integrity, and end to end. `tests/fixtures/` holds sanitized real CLI output. |
| `docs/` | API, architecture, decisions, demo script, deploy, MCP, and integration plan. `docs/original/` has each teammate's original specs, for history. |

## Run, test, demo

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # once
(cd frontend && npm install)                                  # once
.venv/bin/python scripts/dev.py          # backend :8000, merchant :3001, frontend :3000 (ports from config.py)
.venv/bin/pytest -q                      # the whole backend suite (396 tests at the time of writing)
.venv/bin/python scripts/demo.py         # all 8 scenarios against the running stack; must print 8/8
.venv/bin/python scripts/demo.py valid   # one scenario
(cd frontend && npm run lint && npm run build)
.venv/bin/python scripts/secret_scan.py --all
```

Payment modes (`HANDSHAKE_PAYMENT_MODE`):

- **`stub`** (default): the simulated provider, fully offline and labeled "Simulated provider" everywhere. Approve funding with "Simulated provider approval" in the UI or `POST /contracts/{id}/funding/simulate-approval` (stub only). Use this for all development and tests.
- **`link_test`**: real Stripe Link in test mode (`@stripe/link-cli`, pinned). A human must approve each connect and each card in the Link app, and Link rate-limits approvals. **Don't start a live Link run unless the owner asks for one.** See [docs/DEMO.md](docs/DEMO.md).

The compiler runs offline with no `OPENAI_API_KEY`. The owner pastes a key into `.env` themselves if they want the live compiler.

## Code style

- **Python 3.11+, few files.** Add to the existing module that owns the concern rather than creating new ones. The flat `handshake/` package is deliberate.
- **Heavy "why" comments.** Every module starts with a docstring explaining what it does and why. Comments explain decisions and threats, not syntax. Match the density of the surrounding code.
- **Type hints everywhere**, `from __future__ import annotations`, and docstrings on every function.
- **Money is integer minor units (cents)** internally. Convert only at the API edge.
- **State changes are atomic claims** (a conditional UPDATE from an expected state), never read-then-write, so races fail closed.
- **Errors are `ServiceError(status, code, message)`** with a stable snake_case `code` that the frontend and agent can branch on.
- **Tests assert *why*,** meaning which checks failed and how, not just the final status.
- Frontend: TypeScript, shadcn/ui on Base UI, and all backend calls through `frontend/lib/api.ts`. Never use `dangerouslySetInnerHTML`.

## Who owns what

| Teammate | Area | Where it lives |
|---|---|---|
| **Shuban** | Backend, the deterministic engine (Intent Diff), evidence ledger, integration | `handshake/intent_diff.py`, `services.py`, `api.py`, `db.py`, `auth.py`, `config.py`, `extractor.py`, `tests/` |
| **Ajay** | MCP server, contract compiler, Stripe Link wrapper | `handshake/mcp_server.py`, `compiler.py`, `prompts.py`, `payments.py`, `docs/MCP.md` |
| **Rohan** | Frontend | `frontend/` |
| **Sri** | Mock merchant and red-team checkout scenarios | `handshake/merchant.py`, `merchant_static/`, `scripts/demo.py` scenarios |

Their original specs are in `docs/original/`.

## Before you change anything

- [ ] Read the relevant doc: [docs/API.md](docs/API.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/DECISIONS.md](docs/DECISIONS.md), [docs/MCP.md](docs/MCP.md).
- [ ] `.venv/bin/pytest -q` passes before and after your change.
- [ ] With `scripts/dev.py` running in stub mode, `scripts/demo.py` still prints **8/8**, and `valid` completes.
- [ ] If you touched the frontend: `npm run lint` and `npm run build` pass.
- [ ] `scripts/secret_scan.py` is clean before every commit (and `--all` before every push).
- [ ] If behavior changed, update [docs/API.md](docs/API.md) (routes, shapes, error codes) and add the reason to [docs/DECISIONS.md](docs/DECISIONS.md).
- [ ] You did not edit `handshake/models.py` or `COMPILER_PROMPT`, add a host or port literal, or read a `.env` file.
