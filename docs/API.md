# Handshake API

This is the single source of truth for every backend endpoint. The frontend's `lib/api.ts`, the MCP server, and `scripts/demo.py` are all clients of these routes. Base URL: `HANDSHAKE_API_URL` (default `http://localhost:8000`).

**Auth.** Send `Authorization: Bearer <token>` on every route except `GET /health`, `POST /auth/demo-login`, `/.well-known/oauth-authorization-server`, `POST /oauth/device_authorization`, and `POST /oauth/token`. There are two kinds of token:

- **User token** from `POST /auth/demo-login`. This is demo auth.
- **Agent token**, which is either the static `HANDSHAKE_AGENT_TOKEN` or one issued by the device flow.

**Errors.** Every error has the shape `{"error": "<code>", "message": "<sentence>", "details": {...}}`. Authentication and OAuth errors also include `error_description`.

**Ownership.** You only ever see your own records. Someone else's id returns the same 404 as a missing one.

**Agent binding.** A signed contract names one agent (`agent_key`). If the signer names none, it is bound to `HANDSHAKE_AGENT_ID` (default `agent_demo`). An agent's identity is the static token's `HANDSHAKE_AGENT_ID`, or, for device-flow tokens, the `client_id` it connected with. The MCP server uses `HANDSHAKE_AGENT_ID` as its `client_id`. Any other agent gets 403 `agent_not_authorized` on that contract.

**User-only actions.** Signing, editing drafts, amending, revoking, approving, rejecting, approving devices, and simulated approval are user-only. If the agent attempts one, it gets **403 `agent_not_permitted`**, and the attempt is recorded in the evidence (`data.kind = "agent_action_denied"`).

## Auth and connecting agents

| Route | Who | Request | Response |
|---|---|---|---|
| `GET /auth/config` | anyone | – | `{"google_client_id": string or null, "demo_login": bool}`: which sign-in methods the login page offers. |
| `POST /auth/google` | anyone | `{"credential"}` (the Google Identity Services ID token) | Same shape as demo-login, with `"demo_auth": false`. 401 `google_token_invalid` (bad signature, audience, issuer, unverified email, or expired); 404 `google_login_disabled`. |
| `POST /auth/demo-login` | anyone | `{"email"}` | `{"token", "token_type": "bearer", "email", "role": "user", "expires_at", "demo_auth": true}`. `expires_at` is **Unix seconds**; every other time in this API is an ISO 8601 string. **Off in prod** (404 `demo_login_disabled`) unless `HANDSHAKE_ALLOW_DEMO_LOGIN=true`. |
| `GET /auth/me` | any token | – | `{"email", "role": "user" or "agent", "agent_id"}`. The hosted MCP server uses it to check agent tokens. |
| `GET /.well-known/oauth-authorization-server` | anyone | – | OAuth discovery (RFC 8414): authorize, token, register, and device endpoints; `code_challenge_methods_supported: ["S256"]`. |
| `POST /oauth/register` | anyone (an MCP client) | RFC 7591 client metadata: `redirect_uris` (https, loopback http, or a private-use scheme; matched exactly), `client_name`, `token_endpoint_auth_method` (`none`, or `client_secret_post`/`client_secret_basic` to get a secret) | 201 `{"client_id", "client_name", "redirect_uris", "token_endpoint_auth_method", ...}` (+ `client_secret` when asked for) |
| `GET /oauth/authorize` | the user's browser | `response_type=code`, `client_id`, `redirect_uri`, `code_challenge` + `code_challenge_method=S256` (**required**), `state`, `scope=handshake.agent`, `resource`? | 302 to the frontend's `/authorize?request_id=…` consent page. Errors after the redirect URI is verified go back to it (`error`, `state`, `iss`); an unregistered `redirect_uri` gets a plain 400 (never a redirect). |
| `GET /oauth/authorize/request?request_id=` | user | – | `{"request_id", "client_id", "client_name", "redirect_host", "scopes", "expires_at"}`; 404 `authorization_request_not_found`, 410 `authorization_request_expired` |
| `POST /oauth/authorize/decision` | **user only** | `{"request_id", "approve": bool}` | `{"redirect_to"}`: the client's redirect URI with `code`, `state`, `iss` (or `error=access_denied`) |
| `POST /oauth/device_authorization` | anyone (the MCP server) | `{"client_id", "client_name"?}` | `{"device_code", "user_code", "verification_uri", "verification_uri_complete", "expires_in", "interval"}` |
| `POST /oauth/token` | anyone | Form (or JSON). `grant_type=authorization_code` + `code`, `client_id`, `redirect_uri`, `code_verifier`; or `refresh_token` + `refresh_token`, `client_id`; or the device-code grant + `device_code`, `client_id`. Secret clients add `client_secret` or HTTP Basic. | 200 `{"access_token", "token_type": "Bearer", "expires_in", "refresh_token", "scope"}` (`Cache-Control: no-store`). Access tokens last 1 hour; refresh tokens rotate and work once, and **replaying one revokes the agent**. Errors: 400 `invalid_grant`, `unsupported_grant_type`, device-flow `authorization_pending`/`slow_down`/`access_denied`/`expired_token`; 401 `invalid_client`. |
| `GET /agents` | **user only** | – | `{"agents": [{"agent_id", "name", "kind": "oauth"/"device"/"key", "client_name", "created_at", "last_used_at", "expires_at", "revoked_at"}]}`, active first |
| `POST /agents/keys` | **user only** | `{"name"}` | 201 `{"agent_id", "name", "token", "expires_at"}`. The token is shown **once**; it acts as its own agent. |
| `POST /agents/{agent_id}/revoke` | **user only** | – | The agent, with `revoked_at` set. Its tokens fail on the next request (401 `agent_revoked`). |
| `GET /oauth/device?user_code=` | user | – | `{"user_code", "client_id", "client_name", "status", "expires_at", "permissions": [...], "never": [...]}`, for the approval screen |
| `POST /oauth/device/approve` and `/deny` | **user only** | `{"user_code"}` | `{"user_code", "status", "client_id"}` |

## Payment model: fund at signing

1. **Connect Link.** Each user connects their **own** Stripe Link account from the website. Link test mode is the only live rail; stub mode is simulated and always connected.
2. **Sign = fund.** Signing a contract requests one single-use Link **TEST** card for the contract's all-in hard cap. The user approves it in Link, or with "Simulated provider approval" in stub mode. Handshake then retrieves the card and **stores it encrypted (AES-256-GCM) on the contract**. The contract's `funding.state` goes `awaiting_approval → funded`.
3. **No purchase without funding.** Purchases against an unfunded contract are refused with 409 `contract_not_funded`, and the attempt is recorded.
4. **Release only for an authorized checkout.** The stored card stays **locked** until Handshake AUTHORIZES a specific checkout. Then the agent may collect it once (`POST /purchases/{id}/credential`). Handshake re-reads the checkout first, and wipes the stored card as it releases it. Executor mode pays the merchant with it instead, right at authorization.

Card validity is Link's 12 hours from the request. After that the funding expires and the contract must be funded again (`POST /contracts/{id}/funding`).

## The user's Stripe Link account

| Route | Who | Response |
|---|---|---|
| `POST /link/connect` | user | Starts the Link login for **this user's own** Link account. Waits up to 30 s for Link's first update, then returns `{"state", "verification_url", "phrase", "provider_label", "simulated": false, "error"}`, where `state` is `pending` (the user opens `verification_url` and approves in the Link app), `starting` (no update yet; poll `/link/status`), `connected`, `failed`, or `expired`; `error` is set only when it failed. In stub mode it returns `{"state": "connected", "simulated": true}`. |
| `GET /link/status` | user | `{"connected": bool, "simulated": bool, "provider_label", "login": pending-login-or-null}`. The Link access token is never returned. |
| `POST /link/disconnect` | user | Logs this user's Link account out and returns the status above. |

In `link_test` mode, signing without a connected Link account returns 409 `link_not_connected`. In **`link_optional`** mode (the public server), connecting Link is optional: a user who connected it gets real Link **test** cards, and everyone else is funded by the simulated provider. Each funding records its `provider`, and `POST /contracts/{id}/funding/simulate-approval` works only on `stub` fundings.

**Which agent a contract binds to.** A draft compiled by an agent records `proposed_by_agent` (its agent id: the OAuth client id, or `key_…` for an agent key). Signing binds the contract to that agent, else to `HANDSHAKE_AGENT_ID`. Any other agent, even one belonging to the same user, gets 403 `agent_not_authorized`.

## Contract funding

| Route | Who | Response |
|---|---|---|
| `GET /contracts/{id}/funding` | user or agent | **Funding** (refreshed; it polls Link and stores the card once it's approved) |
| `POST /contracts/{id}/funding` | user | Funds an **active** contract again (after a denied, expired, failed, or canceled card; a single-use contract whose card was used is `used` itself, so this applies to multi-use contracts only). Returns Funding. |
| `POST /contracts/{id}/funding/simulate-approval` | user | **Stub mode only.** "Simulated provider approval" for the funding card. Returns Funding (`funded`). |

**Funding** has this shape:

```json
{"funding_id", "state": "not_funded|awaiting_approval|funded|released|used|denied|expired|failed|canceled",
 "provider": "stub|link_test", "provider_label", "approval_url", "provider_reference",
 "amount", "currency", "merchant_name", "card_stored": bool, "card_last4", "valid_until",
 "released_purchase_id", "last_error"}
```

A contract with no funding yet returns only `{"state": "not_funded", "card_stored": false}`. Funding never contains card data (at most `card_last4`). `approval_url` is Link's approval page, or the frontend contract page in stub mode.

## Drafts and contracts

| Route | Who | Request | Response |
|---|---|---|---|
| `POST /drafts/compile` | user or agent | `{"intent"}` | 201 **DraftRecord** + `compiler_source` (`"openai"` or `"fixture"`). Compile failures return 422, 502, 503, or 504 with `details.draft_created: false`. |
| `GET /drafts` | user or agent | – | `[DraftRecord]`, newest first. Signed drafts are still included; their `signed_contract_id` is set. |
| `GET /drafts/{id}` | user or agent | – | DraftRecord |
| `PATCH /drafts/{id}` | user | **DraftPatch** `{goal?, target?, hard_cap_all_in?, max_shipping?, deliver_by?, constraints?}` (unknown keys → 422) | DraftRecord. Edited values get `source: "user"`, and lint re-runs. Returns 409 `already_signed` for a signed draft. |
| `POST /contracts` | user | a `ContractDraft`, or `{"draft": ContractDraft, "assumptions", "clarifications_needed", "compiler_notes"}` | 201 `{"draft_id", "draft", "assumptions", "clarifications_needed", "compiler_notes", "review_url", "blocking_issues", "previous_contract_id", "record": DraftRecord}` |
| `GET /contracts` | user or agent | – | `[{"id", "kind": "draft" or "contract", "status", "goal", "created_at", "signed_at", "signed_contract_id", "draft_id", "funding_state"}]`, newest first |
| `GET /contracts/{id}` | user or agent | – | Contract: `{"kind": "contract", "id", "status", "draft_id", "contract": Contract, "verification": {"valid", "hash_matches", "signature_matches", ...}, "funding": Funding}`. Draft: `{"kind": "draft", "id", "status": "draft", "signed_contract_id", "draft": ContractDraft, "verification": null, "assumptions", "clarifications_needed", "compiler_notes"}` |
| `POST /contracts/{draft_id}/sign` | user | optional `{"draft_id", "agent_key", "signature"}` | **Contract**. Signing also **starts funding** (see above). Errors: 409 `link_not_connected` (link_test mode), 409 `draft_has_blocking_issues` (with `details.blocking_issues`), 409 `already_signed`, 400 `draft_id_mismatch`. If no `agent_key` is given, it defaults to `HANDSHAKE_AGENT_ID`. Signing an amendment draft atomically revokes the old version. |
| `POST /contracts/{id}/revoke` | user | – | Contract with status `revoked`. Its unused credentials are revoked, uncollected payments are cancelled, and the **funding is cancelled, wiping the stored card**. |
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
| `POST /purchases/{id}/approve` | user | `{"note"?}` | PurchaseDetail. Accepts an ESCALATED purchase's unverifiable facts, which unlocks the contract's stored card for that checkout. It never overrides a FAIL (409 `cannot_override_fail`). Other errors: 409 `escalation_stale` after `HANDSHAKE_ESCALATION_TTL_MINUTES`, 409 `contract_not_funded`. |
| `POST /purchases/{id}/reject` | user | `{"note"?}` | PurchaseDetail. Rejects an ESCALATED purchase, or **declines** an AUTHORIZED one whose card hasn't been released yet. Declining frees the contract, and the card stays stored on it. |
| `POST /purchases/{id}/credential` | **the bound agent only** | – | **CredentialRelease** (see below): the contract's stored card, decrypted and then wiped from the database. **Only exists when `HANDSHAKE_CREDENTIAL_MODE=agent_visible`.** Handshake re-reads the checkout first; if it changed for the worse, it returns 409 `checkout_changed` and the card stays locked. The release works once; a second call returns 409 `credential_already_released`. Without an authorized checkout it returns 409 `payment_not_ready`. |
| `POST /purchases/{id}/complete` | nobody | – | Always 403 `internal_only`. Reconciliation is done by the backend after it verifies the merchant order. |
| `GET /evidence/{purchase_id}` | user or agent | – | `{"purchase", "status", "summary", "contract", "contract_verification", "proposal", "proposal_raw_payload", "decision", "credential", "ledger_intact", "events": [...]}` |
| `GET /health` | anyone | – | `{"status", "database", "payment_mode", "payment_label" ("Simulated provider" or "Stripe Link: TEST MODE"), "credential_mode", "compiler_mode"}` |

**Rejections still leave a record.** Rejections against a known contract create a purchase record, and `details.purchase_id` points at its evidence:

- 409 `contract_tampered`, `contract_used`, `contract_revoked`, `contract_expired`, or `contract_not_funded` (recorded as BLOCKED)
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
  "payment_state": "credential_ready|paying|paid|completed|denied|expired|checkout_changed|failed|unknown" | null,
  "funding": Funding,
  "approval_url": "…" | null,
  "resolution": {"action": "approve|reject|decline", "resolved_at", "accepted_constraints": ["no_addons"], "note"} | null,
  "next_action": "wait_for_user_decision|get_payment_credential_and_pay|wait_for_payment|wait_for_merchant_order|wait_for_order_verification|wait_for_reconciliation|blocked_no_action|request_purchase_again|completed|wait",
  "review_url": "<frontend>/purchases/<id>"
}
```

Results whose names start with `checkout_link_` come from the **checkout-link parser**. They are hard rules checking that:

- the link is the merchant's checkout page, and the page Handshake read;
- the link's origin belongs to the merchant the page claims to be, and the contract allows that merchant;
- the link matches the agent's own `selection_report`.

**Evidence events** always use the models.py `event_type`. For steps models.py has no type for, `data.kind` carries the precise subtype: `funding_requested`, `simulated_provider_approval`, `card_stored`, `funding_denied`, `funding_expired`, `funding_failed`, `funding_unavailable`, `card_unavailable`, `authorization_expired`, `checkout_revalidated`, `checkout_changed`, `credential_ready`, `credential_released`, `payment_submitted`, `payment_outcome_unknown`, `payment_request_uncertain`, `payment_request_failed`, `payment_not_made`, `order_not_verified`, `order_mismatch`, `contract_tampered`, `contract_no_longer_valid`, `credential_retrieval_failed`, `payment_refused`, `receipt_verified`, `receipt_mismatch`, `purchase_declined`, `contract_amended`, `draft_edited`, `agent_action_denied`, and `extraction_failed`. Human decisions set `data.human_approval` or `data.human_rejection`. No event ever contains a card number, CVC, full expiry, Link session token, or card file path.

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
