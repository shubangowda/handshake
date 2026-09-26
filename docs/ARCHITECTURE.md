# Architecture

## The pieces

```
 ┌──────────┐   review, sign, approve    ┌──────────────┐        ┌────────────────────────── backend (handshake/api.py) ──────────────────────────┐
 │   User   │ ─────────────────────────▶ │   Frontend   │ ─────▶ │ auth.py        who is calling (user / agent), ownership, device login          │
 └──────────┘                            │ (Next.js)    │  HTTP  │ compiler.py    intent -> DRAFT (LLM or offline fixture) + deterministic lint   │
      ▲  login link / review / Link      └──────────────┘        │ extractor.py   reads the checkout itself (feed + page), SSRF-safe, link parser │
      │                                                          │ intent_diff.py the engine: pure deterministic rules, PASS/FAIL/UNVERIFIABLE    │
 ┌──────────┐   MCP (stdio or HTTP)      ┌──────────────┐  HTTP  │ services.py    contracts, purchases, payment state machine, evidence ledger    │
 │  Agent   │ ─────────────────────────▶ │ mcp_server.py│ ─────▶ │ payments.py    Stripe Link TEST MODE adapter / simulated provider / executor   │
 └──────────┘                            └──────────────┘        │ db.py          SQLite (Postgres-ready), hash-chained append-only ledger       │
      │                                                          └───────────────────────────────┬───────────────────────────────┬────────────────┘
      │  checkout + (agent-visible mode) pay with the TEST card                                  │ reads checkout, verifies order │ spend requests
      ▼                                                                                          ▼                                ▼
 ┌──────────────────────────────┐                                                   ┌──────────────────────┐          ┌──────────────────────┐
 │ Mock merchant (merchant.py)  │ ◀──────────────────────────────────────────────── │  (same merchant)     │          │ Stripe Link (TEST)   │
 │ Sri's store, 8 scenarios     │                                                   └──────────────────────┘          │ or simulated provider│
 └──────────────────────────────┘                                                                                     └──────────────────────┘
```

**The rule.** The agent proposes; Handshake decides. The LLM compiler only drafts contracts. Nothing in the purchase path calls an LLM.

## The flow

1. **Compile.** The user or the agent compiles an intent into a DRAFT. Lint problems block signing.
2. **Sign.** The user reviews and signs, and the contract is bound to one agent (its `agent_key`). The hash covers the canonical JSON, excluding `status`, `contract_hash`, and `signature`. The signature is a demo HMAC.
3. **Request.** The agent requests a purchase with `checkout_url` and an idempotency key. Handshake then:
   - checks ownership, then idempotency, then the contract (tampered, revoked, used, or expired, and whether this agent is the bound one);
   - **extracts** the checkout itself;
   - runs the **checkout-link parser**, then the **engine**;
   - maps the result to BLOCKED, ESCALATED, or AUTHORIZED.
4. **Escalated.** The **user** accepts the exception (consent #1) or rejects it.
5. **Authorized.** The single-use contract is **reserved** (atomically marked USED), the Handshake credential grant is created, and a **Link TEST** spend request is created, which the user approves in Link (consent #2).
6. **Refresh** runs the payment state machine:
   - after approval, it re-checks the contract and **re-extracts the checkout**; if the cart changed, it re-runs the engine;
   - in agent-visible mode, the card is released once; in executor mode, the backend pays;
   - it verifies the merchant order independently, reconciles (an overcharge by even one cent fails the purchase), and marks the purchase COMPLETED.

## State machines

### Contract

```
draft (drafts table) ──sign──▶ ACTIVE ──authorize (reservation)──▶ USED
                                  │   ◀──release (no money moved)──┘
                                  ├──revoke (user)──▶ REVOKED
                                  ├──expires_at passes──▶ EXPIRED
                                  └──a signed amendment replaces it──▶ REVOKED (kind contract_amended)
```

### Purchase (models.py `PurchaseStatus`)

```
PENDING ─▶ VALIDATING ─▶ BLOCKED            (a hard FAIL; or the user rejected; or declined; or the checkout changed)
                     ├─▶ ESCALATED ──user approves──▶ AUTHORIZED
                     │             └─user rejects──▶ BLOCKED
                     └─▶ AUTHORIZED ─▶ COMPLETED   (the order is verified and the charge is within the authorization)
                                    └─▶ FAILED     (payment denied, expired, failed, or overcharged)
```

Rejections before evaluation create BLOCKED or FAILED purchases directly, so each one has evidence.

### Payment (the `payments` table; models.py is unchanged)

```
awaiting_approval ─(Link approved)─▶ approved ─▶ revalidating ─┬─ agent_visible ─▶ credential_ready ─(card released once)─▶ paying ─(merchant has an order)─▶ paid ─(order verified)─▶ completed
                                                              └─ executor ──────────────────────────────────────────────▶ paying ─(backend paid)────────────▶ paid
side exits:   denied · expired · checkout_changed · failed · unknown (pay outcome uncertain: check the merchant, never retry blindly)
```

## Trust boundaries

| Boundary | How it's enforced |
|---|---|
| The agent vs. the user's decisions | `auth.py` roles. Every user-only route refuses the agent with 403, and the attempt is written to the evidence. |
| Merchant content | It is untrusted data. It is fetched only from allowlisted origins (SSRF rules), read twice and compared, never interpreted as instructions, never shown to an LLM, and always rendered as text. |
| Money | Integer cents everywhere. The cap check uses the larger of the claimed and computed totals. The credential covers the exact authorized amount. The contract is reserved atomically. |
| Card data | It exists only in the one-time `CredentialRelease` response (agent-visible mode) or inside the executor function. It is never stored, logged, or put in evidence. |
| Test mode | Only `stub` and `link_test` exist, and every Link create command ends in `--test`. |
| Tampering | The contract's hash and HMAC are re-verified on every purchase and before paying. The evidence ledger is hash-chained (`ledger_intact`). |
