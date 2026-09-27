# Handshake

**The agent proposes. Handshake decides.**

Handshake is an authorization layer between an AI shopping agent and payment. You describe what you're willing to buy; Handshake turns that into a contract you review, **sign, and fund**. Funding means approving one single-use **Stripe Link test-mode** card from your own Link account, for the contract's all-in cap. Handshake stores that card encrypted and locked on the contract. When the agent finds a checkout, Handshake reads the checkout itself, checks every fact against your signed rules with deterministic code (no LLM decides anything), blocks violations, and asks you about anything it can't verify. Only an authorized checkout unlocks the card, once, for that exact purchase. Every step lands in a hash-chained evidence timeline.

## How it fits together

- **You** use the **frontend** (Next.js) to describe a purchase, review and sign the contract, decide on exceptions, and approve payments.
- **Your agent** (Claude Code, Claude Desktop, or any MCP client) talks to the **MCP server**. The MCP server is a thin client of the backend. It connects to your account through a login link that you approve.
- The **backend** (FastAPI) holds the pieces that decide:
  - **the compiler**, which only drafts contracts;
  - **the extractor**, which reads the checkout from the merchant twice and runs the checkout-link parser;
  - **the engine**, which applies pure, deterministic rules;
  - **the payment state machine**;
  - **the evidence ledger**.
- The **mock merchant** is Sri's store, with eight red-team checkout scenarios.
- **Stripe Link, in test mode** (or a simulated provider), issues the single-use card when you sign. You connect **your own** Link account from the website and approve the card in Link.

For the state machines and trust boundaries, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quickstart

You need Python 3.11+ and Node 20+ (tested with Python 3.12 and Node 22).

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
(cd frontend && npm install)
cp .env.example .env                  # optional: dev.py does this on first run and generates the secrets
.venv/bin/python scripts/dev.py       # backend :8000, merchant :3001, frontend :3000
.venv/bin/python scripts/demo.py      # runs the 8 scenarios against the live stack and prints a table
```

With no `OPENAI_API_KEY`, the compiler uses its offline demo fixture, which understands the Pegasus 41 demo request. To use the live compiler, paste your key into `.env` yourself. The payment rail defaults to `HANDSHAKE_PAYMENT_MODE=stub` (a simulated provider, offline).

## Plug the MCP server into your agent

The server is `handshake-mcp` (a console script in the repo venv). It needs only `HANDSHAKE_API_URL`. If no `HANDSHAKE_AGENT_TOKEN` is set, it shows the agent a **login link**: you open it, log in to Handshake, and approve the agent (OAuth 2.0 device authorization). Only you can approve.

- **Claude Code, in this repo:** `.mcp.json` registers the server automatically. Run `claude` in the repo and approve the `handshake` server when asked. To add it elsewhere:
  ```bash
  claude mcp add handshake --scope user -e HANDSHAKE_API_URL=http://localhost:8000 -- /path/to/handshake/.venv/bin/handshake-mcp
  ```
- **Claude Desktop:** add this to `claude_desktop_config.json`:
  ```json
  {"mcpServers": {"handshake": {"command": "/path/to/handshake/.venv/bin/handshake-mcp",
                                "env": {"HANDSHAKE_API_URL": "http://localhost:8000"}}}}
  ```
- **Any stdio MCP client:** run `/path/to/handshake/.venv/bin/handshake-mcp` over stdio. For streamable HTTP, run `handshake-mcp --transport http`; it requires `HANDSHAKE_MCP_HTTP_TOKEN`.

Tools: `connect_handshake`, `create_contract_draft`, `get_contract`, `list_contracts` (plus `list_active_contracts`), `request_purchase`, `get_purchase_status`, and `get_payment_credential` (in the default agent-visible mode). The prompt `handshake_purchase_workflow` explains the flow to the agent. Details are in [docs/MCP.md](docs/MCP.md).

## Demo walkthrough

[docs/DEMO.md](docs/DEMO.md) is the judge script. In short: compile and sign the Pegasus 41 contract, then run the eight scenarios. `valid` completes with a verified receipt. `price_bump`, `hidden_subscription`, `product_swap`, `late_delivery`, and `prompt_injection` are blocked, each for its stated reason. `unknown_seller` and `vague_delivery` escalate to you: first you accept the exception in Handshake, then you approve the payment in Link.

## What is real and what is mocked

| Part | Status |
|---|---|
| The engine, contract signing and hashing, the evidence ledger, the payment state machine, revalidation, reconciliation, idempotency, ownership, and roles | **Real**, and tested. |
| Auth | **Demo auth.** Anyone who types an email gets a user token. Agents connect through a real OAuth-style device-login flow, but it rests on that demo login. Replace it with passkeys or OAuth before real users. |
| The merchant | **Mocked.** Sri's fake store, with sessions held in memory. |
| Checkout extraction | Reads the mock store's **structured feed** and embedded page facts. It does not scrape real sites. |
| Payments | **`stub`** (default): a simulated provider, labeled "Simulated provider" everywhere. **`link_test`**: real Stripe Link in **TEST MODE**, where each user connects their own Link account from the website. The adapter now matches the real CLI's output (fixed after a live run), but the end-to-end live run is still waiting on the approvals in docs/DEMO.md. **No real card is ever charged by this codebase.** |
| Cards | The contract's funded single-use **test** card is **stored encrypted** (AES-256-GCM) on the contract from funding until use, and wiped when it is released or the contract is revoked. In the default **agent-visible** mode, the agent receives it once, for one authorized checkout. In **executor** mode, the backend pays with it and the agent never sees it. |
| The contract compiler | The **real** OpenAI structured-output call, if you provide a key. Otherwise an offline fixture for the demo request. It only drafts; it never decides. |

## Tests

```bash
.venv/bin/pytest -q                   # the whole backend: engine, API, auth, compiler, extractor, payments, MCP, end to end
(cd frontend && npm run lint && npm run build)
.venv/bin/python scripts/secret_scan.py --all     # no keys, tokens, or card numbers in tracked files
```

## Docs

- [docs/API.md](docs/API.md): every endpoint
- [docs/DECISIONS.md](docs/DECISIONS.md): why things are the way they are
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): state machines and trust boundaries
- [docs/DEPLOY.md](docs/DEPLOY.md): moving from localhost to the real domain
- [docs/DEMO.md](docs/DEMO.md): the judge demo script
- [docs/MCP.md](docs/MCP.md): the MCP server
- [docs/INTEGRATION_PLAN.md](docs/INTEGRATION_PLAN.md): how the four parts were joined
- `docs/original/`: each teammate's original docs, for reference

## Team

- **Shuban:** backend, the deterministic engine (Intent Diff), the evidence ledger
- **Ajay:** MCP server, contract compiler, Stripe Link wrapper
- **Rohan:** frontend
- **Sri:** mock merchant and the red-team checkout scenarios

## Future work

1. **Real authentication.** Passkey signing of contracts and passkey or OAuth login, replacing demo auth and the server-side HMAC signature.
2. **Real merchant extraction.** Extraction from real merchant sites, behind the same extractor interface.
3. **A production ledger.** Externally anchored, not just hash-chained.
4. **More ways to buy.** Standing budgets with one-tap confirmation, returns and cancellations, and revocation for agent tokens.
