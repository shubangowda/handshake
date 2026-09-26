# Handshake API

This is the single source of truth for every backend endpoint. The frontend's `lib/api.ts`, the MCP server, and `scripts/demo.py` are all clients of these routes. Base URL: `HANDSHAKE_API_URL` (default `http://localhost:8000`).

**Auth.** Send `Authorization: Bearer <token>` on every route except `GET /health`, `POST /auth/demo-login`, `/.well-known/oauth-authorization-server`, `POST /oauth/device_authorization`, and `POST /oauth/token`. There are two kinds of token:

- **User token** from `POST /auth/demo-login`. This is demo auth.
- **Agent token**, which is either the static `HANDSHAKE_AGENT_TOKEN` or one issued by the device flow.

**Errors.** Every error has the shape `{"error": "<code>", "message": "<sentence>", "details": {...}}`. Authentication and OAuth errors also include `error_description`.

**Ownership.** You only ever see your own records. Someone else's id returns the same 404 as a missing one.

**User-only actions.** Signing, editing drafts, amending, revoking, approving, rejecting, approving devices, and simulated approval are user-only. If the agent attempts one, it gets **403 `agent_not_permitted`**, and the attempt is recorded in the evidence (`data.kind = "agent_action_denied"`).

## Auth and connecting agents

| Route | Who | Request | Response |
|---|---|---|---|
| `POST /auth/demo-login` | anyone | `{"email"}` | `{"token", "token_type": "bearer", "email", "role": "user", "expires_at", "demo_auth": true}` |
| `GET /auth/me` | any token | – | `{"email", "role": "user" or "agent", "agent_id"}` |
| `GET /.well-known/oauth-authorization-server` | anyone | – | OAuth discovery metadata (the device and token endpoints) |
| `POST /oauth/device_authorization` | anyone (the MCP server) | `{"client_id", "client_name"?}` | `{"device_code", "user_code", "verification_uri", "verification_uri_complete", "expires_in", "interval"}` |
| `POST /oauth/token` | anyone (the MCP server) | `{"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code", "client_id"}` | 200 `{"access_token", "token_type": "Bearer", "expires_in", "scope", "agent_id", "email"}`, or 400 with `error` = `authorization_pending`, `slow_down`, `access_denied`, `expired_token`, or `invalid_grant` |
| `GET /oauth/device?user_code=` | user | – | `{"user_code", "client_id", "client_name", "status", "expires_at", "permissions": [...], "never": [...]}`, for the approval screen |
| `POST /oauth/device/approve` and `/deny` | **user only** | `{"user_code"}` | `{"user_code", "status", "client_id"}` |

## Drafts and contracts

| Route | Who | Request | Response |
|---|---|---|---|
| `POST /drafts/compile` | user or agent | `{"intent"}` | 201 **DraftRecord** + `compiler_source` (`"openai"` or `"fixture"`). Compile failures return 422, 502, 503, or 504 with `details.draft_created: false`. |
| `GET /drafts` | user or agent | – | `[DraftRecord]`, newest first |
| `GET /drafts/{id}` | user or agent | – | DraftRecord |
| `PATCH /drafts/{id}` | user | **DraftPatch** `{goal?, target?, hard_cap_all_in?, max_shipping?, deliver_by?, constraints?}` (unknown keys → 422) | DraftRecord. Edited values get `source: "user"`, and lint re-runs. Returns 409 `already_signed` for a signed draft. |
| `POST /contracts` | user | a `ContractDraft`, or `{"draft": ContractDraft, "assumptions", "clarifications_needed", "compiler_notes"}` | 201 `{"draft_id", "draft", "assumptions", "clarifications_needed", "compiler_notes", "review_url", "blocking_issues", "previous_contract_id", "record": DraftRecord}` |
| `GET /contracts` | user or agent | – | `[{"id", "kind": "draft" or "contract", "status", "goal", "created_at", "signed_at", "signed_contract_id", "draft_id"}]`, newest first |
| `GET /contracts/{id}` | user or agent | – | Contract: `{"kind": "contract", "id", "status", "draft_id", "contract": Contract, "verification": {"valid", "hash_matches", "signature_matches", ...}}`. Draft: `{"kind": "draft", "id", "status": "draft", "signed_contract_id", "draft": ContractDraft, "verification": null, "assumptions", "clarifications_needed", "compiler_notes"}` |
| `POST /contracts/{draft_id}/sign` | user | optional `{"draft_id", "agent_key", "signature"}` | **Contract**. Errors: 409 `draft_has_blocking_issues` (with `details.blocking_issues`), 409 `already_signed`, 400 `draft_id_mismatch`. If no `agent_key` is given, it defaults to `HANDSHAKE_AGENT_ID`. Signing an amendment draft atomically revokes the old version. |
| `POST /contracts/{id}/revoke` | user | – | Contract with status `revoked`. Its unused credentials are revoked and pending payment requests cancelled. |
| `POST /contracts/{id}/amend` | user | – | A new DraftRecord whose `previous_contract_id` points at the contract. The old contract stays in force until the new draft is signed. |
| `GET /contracts/{id}/purchases` | user or agent | – | `[PurchaseDetail]` |

A **DraftRecord** has every `ContractDraft` field flattened, plus these API-only fields: `status: "draft"`, `assumptions`, `clarifications_needed` (lint problems prefixed with `lint: `), `compiler_notes`, `previous_contract_id`, `signed_contract_id`, `review_url`, and `blocking_issues`.

## Purchases

| Route | Who | Request | Response |
|---|---|---|---|
| `POST /purchases` | user or agent | `{"contract_id", "checkout_url", "idempotency_key" (**required for the agent**, 8–128 chars), "selection_report"?: SelectionReport}`. There is **no proposal field**: Handshake reads the checkout itself, and unknown keys → 422. | PurchaseDetail. The same `(contract, idempotency_key)` returns the same purchase with `idempotent_replay: true`. |
| `GET /purchases` | user or agent | – | `[PurchaseDetail]`, newest first |
| `GET /purchases/{id}` | user or agent | – | PurchaseDetail. **Runs the payment refresh first.** |
| `POST /purchases/{id}/payment/refresh` | user or agent | – | PurchaseDetail. Advances the payment state machine; idempotent. |
| `POST /purchases/{id}/approve` | user | `{"note"?}` | PurchaseDetail. Accepts an ESCALATED purchase's unverifiable facts. It never overrides a FAIL (409 `cannot_override_fail`), and returns 409 `escalation_stale` after `HANDSHAKE_ESCALATION_TTL_MINUTES`. Afterwards the payment still needs the user's approval in Link. |
| `POST /purchases/{id}/reject` | user | `{"note"?}` | PurchaseDetail. Rejects an ESCALATED purchase, or **declines** an AUTHORIZED one whose payment hasn't started. Declining cancels the provider request and frees the contract. |
| `POST /purchases/{id}/payment/simulate-approval` | user | – | PurchaseDetail. **Only exists when `HANDSHAKE_PAYMENT_MODE=stub`.** It is labeled "Simulated provider approval" and stands in for tapping Approve in Link. |
| `POST /purchases/{id}/credential` | **the bound agent only** | – | **CredentialRelease** (see below). **Only exists when `HANDSHAKE_CREDENTIAL_MODE=agent_visible`.** It works once; a second call returns 409 `credential_already_released`. Before the payment is ready it returns 409 `payment_not_ready`. |
| `POST /purchases/{id}/complete` | nobody | – | Always 403 `internal_only`. Reconciliation is done by the backend after it verifies the merchant order. |
| `GET /evidence/{purchase_id}` | user or agent | – | `{"purchase", "status", "summary", "contract", "contract_verification", "proposal", "proposal_raw_payload", "decision", "credential", "ledger_intact", "events": [...]}` |
| `GET /health` | anyone | – | `{"status", "database", "payment_mode", "payment_label" ("Simulated provider" or "Stripe Link: TEST MODE"), "credential_mode", "compiler_mode"}` |

**Rejections still leave a record.** Rejections against a known contract create a purchase record, and `details.purchase_id` points at its evidence:

- 409 `contract_tampered`, `contract_used`, `contract_revoked`, or `contract_expired` (recorded as BLOCKED)
- 403 `agent_not_authorized`: the contract is bound to a different agent (BLOCKED)
- 422 `unsupported_checkout_url`: the URL is off the merchant allowlist (BLOCKED)
- 502 `checkout_unreachable` (FAILED)
- 422 `invalid_proposal` or `invalid_request` (FAILED)

### PurchaseDetail

```json
{
  "purchase_id": "purchase_…", "status": "pending|validating|authorized|blocked|escalated|completed|failed",
  "decision": {"id", "verdict", "evaluated_at", "results": [
      {"constraint": "hard_cap_all_in", "field": "total_price", "label": "Total price",
       "verdict": "pass|fail|unverifiable", "severity": "hard|soft|escalating",
       "expected": "<= 135.00 USD", "actual": "128.39 USD", "reason": "…", "evidence": {…}}]},
  "contract_id": "…", "proposal_id": "…",
  "credential": {"credential_id", "merchant_name", "merchant_id", "max_amount", "currency", "single_use", "status", "expires_at"} | null,
  "summary": "Authorized 128.39 USD at Amazon.com. …",
  "idempotent_replay": false,
  "purchase": Purchase, "proposal": TransactionProposal | null,
  "payment": {"state", "provider": "stub|link_test", "provider_label", "approval_url", "provider_reference",
              "amount", "pay_amount", "currency", "last4", "credential_released", "order_id", "receipt", "last_error", "updated_at"} | null,
  "payment_state": "awaiting_approval|approved|revalidating|credential_ready|paying|paid|completed|denied|expired|checkout_changed|failed|unknown" | null,
  "approval_url": "…" | null,
  "resolution": {"action": "approve|reject|decline", "resolved_at", "accepted_constraints": ["no_addons"], "note"} | null,
  "next_action": "wait_for_user_decision|wait_for_user_link_approval|wait_for_revalidation|get_payment_credential_and_pay|wait_for_payment|wait_for_merchant_order|wait_for_order_verification|wait_for_reconciliation|blocked_no_action|request_purchase_again|completed|wait",
  "review_url": "<frontend>/purchases/<id>"
}
```

Results whose names start with `checkout_link_` come from the **checkout-link parser**. They are hard rules checking that:

- the link is the merchant's checkout page, and the page Handshake read;
- the link's origin belongs to the merchant the page claims to be, and the contract allows that merchant;
- the link matches the agent's own `selection_report`.

**Evidence events** always use the models.py `event_type`. For steps models.py has no type for, `data.kind` carries the precise subtype: `payment_requested`, `simulated_provider_approval`, `payment_approved`, `payment_denied`, `payment_expired`, `checkout_revalidated`, `checkout_changed`, `credential_ready`, `credential_released`, `payment_submitted`, `payment_outcome_unknown`, `receipt_verified`, `receipt_mismatch`, `purchase_declined`, `contract_amended`, `draft_edited`, `agent_action_denied`, and `extraction_failed`. Human decisions set `data.human_approval` or `data.human_rejection`. No event ever contains a card number, CVC, full expiry, Link session token, or card file path.

### CredentialRelease (the only response that contains card values)

```json
{"purchase_id", "contract_id", "merchant_name", "checkout_url", "pay_url", "pay_method": "POST",
 "amount": 128.39, "currency": "USD", "card_number", "exp_month", "exp_year", "cvc",
 "card_valid_until", "authorization_expires_at", "simulated": true, "mode_label", "instructions"}
```

It is sent with `Cache-Control: no-store`. The agent submits the card to `pay_url` as `{"card_number", "exp_month", "exp_year", "cvc", "amount", "currency"}`.

## Mock merchant (`HANDSHAKE_MERCHANT_URL`, default port 3001)

| Route | Purpose |
|---|---|
| `GET /` | The product page (Sri's design) |
| `POST /api/checkout-sessions` `{"scenario", "size", "quantity"}` | Creates a cart. Returns `{"session_id", "checkout_url", "scenario"}`. Scenarios: valid, price_bump, hidden_subscription, product_swap, unknown_seller, late_delivery, vague_delivery, prompt_injection. |
| `GET /checkout/{id}` | The checkout page, with the facts in `<script type="application/json" id="handshake-checkout-facts">` |
| `GET /api/checkout/{id}` | The same facts as JSON, plus `pay_url` |
| `POST /api/checkout/{id}/pay` | Takes the card and the exact total. Test cards only (a Luhn-valid number, or the documented Link test number). Only last4 is stored. Idempotent per session. |
| `GET /api/checkout/{id}/order` | `{"session_id", "order": Order or null}` |
| `GET /api/orders/{order_id}` | The order, for independent verification |
| `POST /api/dev/checkout/{id}/scenario` `{"scenario"}` | **Dev only.** Changes the cart after creation, to demo revalidation. |
