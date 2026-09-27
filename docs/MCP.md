# Handshake MCP server

This is Ajay's `mcp.md`, updated for the integrated system. The original is in `docs/original/ajay_mcp.md`.

## Mission

Shopping agents use this server to work with Handshake. The core rule is unchanged: **the shopping agent proposes; Handshake decides.** The agent can draft contracts and request purchases. It can never sign a contract, approve an exception, approve a payment, or change the user's rules. The backend enforces this. The server's instructions describe it too, but they are not what enforces it.

## What changed from the original spec

**The agent may now receive a card (agent-visible mode, the default).** The original spec said the agent must never see payment credentials. The team has since decided that this prototype delivers the single-use Stripe Link **TEST** card to the agent, which enters it at the merchant's checkout. The card is funded **when the user signs**, from the user's own Link account, and stored encrypted on the contract. It is released only:

- after Handshake has authorized a specific checkout,
- after Handshake has re-read that checkout, and
- once, to the agent the contract is bound to, for that exact purchase.

Until the contract is funded, `request_purchase` returns `contract_not_funded`.

The card never appears in any other response, log, error, or evidence record.

**The real tradeoff.** Once a card is in a model's context, this server can't control what the host keeps in its transcript. That's acceptable here because every card is a Link test card.

**Executor mode** (`HANDSHAKE_CREDENTIAL_MODE=executor`) keeps the card out of the agent entirely. The backend pays the merchant itself, `get_payment_credential` does not exist, and the agent only polls.

**Other changes:**

- **Connecting.** The agent connects through the user's own login, as described below. It never needs an OpenAI key, a Link login, or database access.
- **The compiler** now runs in the backend. The MCP server is a thin HTTP client.

## Connecting an agent (OAuth 2.0 device authorization, RFC 8628 style)

1. The agent calls any tool. With no token, the server starts a connect request and returns this:
   ```json
   {"error": "authorization_required", "login_url": "https://<frontend>/connect?code=KDQM-TXBW", "user_code": "KDQM-TXBW"}
   ```
2. The agent gives the user the `login_url`. The user logs in to Handshake, sees which agent is asking and what it can and can't do, and approves. **Only the user can approve.** If an agent token calls `/oauth/device/approve`, it gets a 403.
3. The agent calls the tool again. The server exchanges its secret `device_code` for an **agent token**. That token is bound to the user and to the agent id, and expires after `HANDSHAKE_AGENT_TOKEN_TTL_DAYS`. The server caches it in `HANDSHAKE_MCP_TOKEN_FILE` with mode 0600.

For scripts and CI, the static `HANDSHAKE_AGENT_TOKEN` still works. If it is set, the server uses it and skips the login flow.

## Tools

| Tool | What it does |
|---|---|
| `connect_handshake` | Shows whether the agent is connected (user, agent id, payment rail), or returns the login link. |
| `create_contract_draft(intent)` | Turns the user's words into a DRAFT. Returns `draft_id`, a plain-English summary, assumptions, clarifications, and the `review_url` where the user reviews and signs. The draft has no authority until then. |
| `get_contract(contract_id)` | Returns a draft or a signed contract, including its status and signature verification. |
| `list_contracts(status, limit, cursor)` | A paginated list of contracts. `list_active_contracts` is kept as Ajay's alias. |
| `request_purchase(contract_id, checkout_url, idempotency_key, selection_report?)` | Handshake reads the checkout itself, parses the link against the contract, and decides. The agent's claims about price or approval are ignored. |
| `get_purchase_status(purchase_id)` | Refreshes the purchase, then returns `status`, `payment_state`, `next_action`, the checks that failed or couldn't be verified (with their reasons), and the order id and last4 once completed. It never returns card data. |
| `get_payment_credential(purchase_id)` | **Agent-visible mode only.** Returns the single-use TEST card, the purchase binding, and where to pay (`pay_url`, exact amount and currency). It works once; a second call returns `credential_already_released`. |

**Prompts.** `handshake_purchase_workflow` is also the server's instructions. Its text is `HANDSHAKE_AGENT_INSTRUCTIONS` in `handshake/prompts.py`. The essential card rules are repeated in `get_payment_credential`'s description, because not every host reads prompts.

## `next_action` values

| Value | What it means for the agent |
|---|---|
| `wait_for_user_decision` | The purchase escalated. Send the user the `review_url` and wait. |
| `wait_for_revalidation`, `wait_for_merchant_order`, `wait_for_order_verification`, `wait_for_reconciliation` | Keep polling. |
| `get_payment_credential_and_pay` | Call `get_payment_credential` once, then submit the card to `pay_url` with exactly the amount and currency it gives. |
| `blocked_no_action`, `request_purchase_again` | Stop. The checks explain why. |
| `completed` | Done. Report only the order id and last4. |

## Running it

- **stdio (the default):** `.venv/bin/handshake-mcp`
- **HTTP:** `handshake-mcp --transport http --host 127.0.0.1 --port 8765`. This requires `HANDSHAKE_MCP_HTTP_TOKEN`; clients send it as `Authorization: Bearer …`. Put it behind TLS on the real domain.
- **Claude Code** discovers the server from `.mcp.json` at the repo root. Setup commands for Claude Code, Claude Desktop, and any other stdio client are in the README.
