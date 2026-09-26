# Handshake backend

The trusted backend that sits between the shopping agent and the payment credential.
It stores signed contracts, checks each proposed checkout against the contract with
deterministic Python rules (no LLM is involved in any decision), logs every step to an
evidence ledger, and only then issues a single-use, exact-amount **stub** credential.

## Run it

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export HANDSHAKE_SIGNING_SECRET=change-me        # optional; a dev secret is used (with a loud warning) if unset
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest -q                              # run the tests
```

- `DATABASE_URL` defaults to `sqlite:///./handshake.db`. For Postgres, set it to e.g. `postgresql+psycopg://...` and install a driver. Tables are created on startup; there are no migrations.
- Interactive docs: http://localhost:8000/docs. `POST /purchases` validates its own body, so the docs page doesn't show its schema; use the description below.
- CORS allows every origin for now.

## Files

| File | What's in it |
|---|---|
| `app/models.py` | The team's shared Pydantic models, copied verbatim. **Do not edit.** Suggested changes are listed under Open questions. |
| `app/intent_diff.py` | The rule engine. Pure functions: `evaluate()` and `compute_outcome()`. |
| `app/services.py` | Hashing and signing, the credential broker, the evidence writer, and the purchase flow. |
| `app/db.py` | SQLAlchemy tables and save/load helpers. The evidence table only supports inserts. |
| `app/main.py` | FastAPI routes, plus the backend-only schemas (`PurchaseSubmission`, `PurchaseResponse`, …). |
| `tests/` | `conftest.py` (fixtures), `test_intent_diff.py` (engine), `test_api.py` (end to end). |

## How a decision is made

Every rule returns `pass`, `fail` or `unverifiable`, along with the severity `hard`, `soft` or `escalating`. The outcome is the first match in this list:

1. Any hard rule failed → **BLOCKED**
2. No hard rules were evaluated → **ESCALATED**
3. Any hard rule is unverifiable → **ESCALATED**. This happens even if `escalate_if` is empty, because the backend is fail-closed.
4. Any escalating rule failed or is unverifiable → **ESCALATED**
5. Otherwise → **AUTHORIZED**

Soft results are shown to the user but never change the outcome. Note that `decision.verdict` (computed by the model from hard rules only) is **not** the purchase outcome. Read `status` on the purchase instead.

The rules run in this order: `currency_match`, `subtotal_integrity`, `hard_cap_all_in`,
`max_shipping`, `contract_category`, `no_subscription`, `no_membership`, `no_addons`,
`min_return_days`, `delivery_deadline`, `merchant_policy`, `seller_requirement`,
`extractor_agreement`. After those, each entry in `contract.constraints` runs in order as
`constraint[i]:<field>:<operator>`. A rule only produces a result if it applies to the contract.

Each result looks like this:

```json
{"constraint": "hard_cap_all_in", "verdict": "pass", "severity": "hard",
 "expected": "<= 135.00 USD", "actual": "128.39 USD",
 "reason": "Final payable total is within signed cap.",
 "evidence": {"source": "proposal.total", "...": "..."}}
```

`reason` is a plain sentence that can be displayed as-is.

## For Ajay: what the extractor must send

- **Always send `recurring_billing.detected`, `addons_detected` and `membership_detected` explicitly**, even when they are `false`. The models default these fields to `false`, so the backend cannot tell "checked, none found" apart from "never checked". If a field is left out and the contract forbids it, that rule becomes `unverifiable` and the purchase escalates. The `STRICT_EXPLICIT_TERMS` flag in `intent_diff.py` controls this and is on by default.
- The same goes for `line_items[].quantity` when a contract has a `quantity` constraint. If it's omitted, the model silently fills in 1, so the backend treats it as missing.
- `total` must equal `item_subtotal + tax + shipping + fees - discounts` to within 2 cents. If it doesn't, the purchase is recorded as **BLOCKED** with a `total_integrity` failure; the request is not rejected with a 422.
- Within that 2-cent window, the backend uses **the larger of the claimed total and the computed total** for the cap check, any `total_price` constraint, the authorized amount, and the credential. If the checkout claims 135.00 but its parts add up to 135.01, the backend treats it as 135.01.
- `item_subtotal` must equal the sum of `unit_price × quantity` over the line items, to within 1 cent (`subtotal_integrity`).
- Category-specific attributes (size, color, refresh_rate_hz, …) go in `line_items[].attributes`, or in `extracted_attributes` for proposal-level values. The lookup checks the item first, then `extracted_attributes`, and matching on key names ignores case.
- If you run several extractors, send `extractors_agreed: false` when they disagree; that escalates the purchase. If you leave the field out, it's recorded as a soft note only.
- Send datetimes in ISO 8601 with a timezone. Naive values are assumed to be UTC, and the evidence records that assumption.
- Strings (merchant names, categories, sizes, colors) are compared after trimming and lowercasing. Numbers sent as strings (`"10"`) equal the number (`10`). Booleans only equal booleans, or the words `"yes"`, `"no"`, `"true"` and `"false"`.

## Endpoints

All errors share one shape: `{"error": "<code>", "message": "<sentence>", "details": {...}}`.

### `POST /contracts` (create a draft)
The body is the contract draft Ajay's compiler produces. It can be a bare `ContractDraft`, or the full `CompilerOutput` wrapper (`{"draft": {...}, "assumptions": [...], "clarifications_needed": [...], "compiler_notes": [...]}`). Nothing is signed yet.
The reply (201) is `{"draft_id", "draft", "assumptions", "clarifications_needed", "compiler_notes"}`. If `clarifications_needed` is not empty, the frontend should ask the user those questions before they sign.
Errors: 422 `invalid_draft` (details list each bad field), 409 `draft_exists`.

### `GET /contracts` (list everything)
No body. Returns a list, newest first. Each item is `{"id", "kind": "draft" or "contract", "status", "goal", "created_at", "signed_at", "signed_contract_id", "draft_id"}`. A draft that has been signed points to its contract through `signed_contract_id`.

### `GET /contracts/{id}` (one contract or draft)
The id can be a contract id or a draft id.
- **Signed contract:** `{"kind": "contract", "id", "status", "draft_id", "contract": {...}, "verification": {"valid", "hash_matches", "signature_matches", "stored_hash", "recomputed_hash"}}`. The verification is recomputed on every request, so `valid: false` means the stored contract was changed after it was signed.
- **Draft:** `{"kind": "draft", "id", "status": "draft", "signed_contract_id", "draft": {...}, "verification": null, "assumptions", "clarifications_needed", "compiler_notes"}`.

Errors: 404 `contract_not_found`.

### `POST /contracts/{draft_id}/sign` (user signs)
The body is optional: `{"draft_id", "agent_key", "signature"}`. If you include `draft_id`, it must match the id in the URL. Returns the signed `Contract`. It has a **new** `contract_…` id, status `active`, a `contract_hash` (SHA-256), and a `signature`. The signature is a **demo** HMAC made with a server secret; it is not a signature from a key the user holds. If you send your own `signature`, it is kept in the evidence as `client_signature`, but the server's signature is the one that counts.
Errors: 400 `draft_id_mismatch`, 404 `draft_not_found`, 409 `already_signed`, 409 `draft_not_signable` (e.g. the draft has already expired).

### `POST /contracts/{id}/revoke` (user cancels)
No body. This only works on an `active` contract that was signed as revocable. It returns the `Contract` with status `revoked`, and also revokes any credentials on that contract that haven't been used yet.
Errors: 409 `not_revocable`, 409 `contract_not_active`.

### `POST /purchases` (agent proposes a checkout)
> **TODO: confirm this payload with Ajay and Rohan.** `PurchaseRequest` in models.py has no proposal field, so the backend adds one.

```json
{"contract_id": "contract_…", "checkout_url": "https://…",
 "selection_report": null,
 "proposal": { /* TransactionProposal JSON, the extracted checkout facts */ }}
```

The backend checks the contract, runs every rule, and replies with a `PurchaseResponse`:

```json
{"purchase_id": "purchase_…", "status": "authorized | blocked | escalated",
 "decision": { "verdict": "...", "results": [ ...one per rule... ] },
 "contract_id": "…", "proposal_id": "…",
 "credential": {"credential_id", "merchant_name", "merchant_id", "max_amount",
                "currency", "single_use", "status", "expires_at"} or null,
 "summary": "Authorized 128.39 USD at Mock Nike. A single-use credential was issued."}
```

- **`authorized`:** a credential is included. Its `max_amount` is the exact payable checkout total (the larger of the claimed and computed totals), not the contract cap, and it expires after 15 minutes. It never includes a card number, CVV or reusable token. A single-use contract becomes `used` in the same database transaction, so it can't be spent twice.
- **`blocked`:** a hard rule failed. `summary` and each result's `reason` say why. No credential is issued.
- **`escalated`:** something could not be verified, so the user has to decide. No credential is issued until they approve (see `/approve` and `/reject` below).
- A checkout whose total doesn't add up gets **`blocked`** with a `total_integrity` result. It is not rejected with a 422.

Rejections: every rejection against a **known** contract still creates a purchase record, and `details.purchase_id` points at its evidence.
- 409 `contract_tampered`, `contract_used`, `contract_revoked` or `contract_expired`: the purchase is recorded as `blocked`.
- 422 `invalid_proposal`: the proposal is missing required fields or has the wrong types. The purchase is recorded as `failed`.
- 422 `invalid_request`: the submission itself is malformed. If `contract_id` names a known contract, the purchase is recorded as `failed`.
- 404 `contract_not_found`: an unknown contract id. Nothing is recorded, because there is no contract to attach the record to.

### `GET /purchases/{id}` (check a purchase)
No body. Returns the same `PurchaseResponse` shape as above, with the current status. That status can also be `completed` or `failed` after reconciliation.
Errors: 404 `purchase_not_found`.

### `POST /purchases/{id}/approve` (user says yes to an escalated purchase)
The body is optional: `{"note": "..."}`. The note is kept in the evidence. This only works when all of these hold:
- the purchase is `escalated`
- its decision contains **no hard failures**
- the contract is still `active`, isn't expired, and still passes verification

Approval can only accept results that were `unverifiable` or `escalating`. It can **never** override a `fail`; for that, the user needs to sign a new contract.

On success it returns a `PurchaseResponse` with status `authorized` and a credential issued exactly as it would be automatically: same amount rule, same 15-minute expiry. A single-use contract becomes `used` in the same atomic step. The evidence gains a `purchase_authorized` event with `data.human_approval: true` and `data.accepted_constraints` (which uncertain results the user accepted), followed by `credential_created`.

Errors, all 409:
- `purchase_not_escalated`: the purchase is blocked or authorized, was already approved or rejected, or lost a race with a simultaneous click
- `cannot_override_fail`
- `contract_revoked`, `contract_used`, `contract_expired` or `contract_tampered`

404 `purchase_not_found` if the id is unknown.

### `POST /purchases/{id}/reject` (user says no to an escalated purchase)
The body is optional: `{"note": "..."}`. It returns a `PurchaseResponse` with status `blocked` and summary "Blocked: Rejected by the user after escalation.", and logs a `purchase_blocked` event with `data.human_rejection: true`. This is final: a later approve or reject gets 409 `purchase_not_escalated`. The contract is left untouched, so the agent can try a different checkout.

### `POST /purchases/{id}/complete` (report the actual charge)
Body: `{"charged_amount": 128.39}`. This compares the real charge with the authorized amount and returns a `PurchaseResponse`.
- Charge is at or below the authorized amount → `completed`, and the credential becomes `used`.
- Charge is above it (even by one cent) → `failed`, the credential is `revoked`, and a `payment_mismatch` event is logged.

Errors: 409 `purchase_not_authorized`, `credential_not_active`, `credential_expired`.

### `GET /evidence/{purchase_id}` (the whole story, for the timeline UI)
No body. Returns everything about one purchase in a single response:

```json
{"purchase": {...}, "status": "…", "summary": "…",
 "contract": {...}, "contract_verification": {"valid": true, ...},
 "proposal": {...} or null, "proposal_raw_payload": {...} or null,
 "decision": {...} or null, "credential": {...} or null,
 "ledger_intact": true,
 "events": [{"id", "sequence", "event_type", "timestamp", "message", "data",
             "contract_id", "purchase_id", "prev_hash", "event_hash"}, …]}
```

`events` are in order of `sequence`, and each `message` is a readable sentence. The events always start with `contract_created` and `contract_signed`. What comes next depends on how the purchase went:
- **Authorized:** `shopping_started`, `proposal_created`, `validation_started`, `validation_completed` (its `data.results` holds every rule result), `purchase_authorized`, `credential_created`. After reconciliation, `credential_used` and `payment_completed` (or `payment_mismatch`) are added.
- **Blocked or escalated:** the same events up to `validation_completed`, then `purchase_blocked` or `purchase_escalated`. After a human decision on an escalated purchase, either `purchase_authorized` (with `human_approval: true`) and `credential_created` are added, or a `purchase_blocked` event with `human_rejection: true`.
- **Rejected before evaluation** (tampered, used, revoked, expired): `shopping_started` then `purchase_blocked`, whose `data.reason` holds the error code. `proposal` and `decision` are null.

`ledger_intact` is false if any stored event has been edited, which the hash chain detects.

### `GET /health`
Returns `{"status": "ok", "database": "sqlite"}`.

## Open questions about `models.py`

We deliberately haven't edited the shared file. These are changes to discuss as a team:

1. **The defaults of `addons_detected`, `membership_detected` and `recurring_billing.detected` look like verified results.** They default to `False`, which reads as "checked, nothing found". Making them `bool | None = None` would say "unknown" honestly. The backend works around this today with `model_fields_set`.
2. **`TransactionProposal.validate_total` raises instead of returning a verdict.** The backend works around this by subclassing and overriding the validator by name. If the method is ever renamed, that workaround silently stops working (a test would catch it).
3. **The total tolerance is 2 cents.** A claimed total up to 2 cents below the real sum of its parts gets past the model. The backend now closes this gap by always using the larger of the claimed and computed totals, but a tolerance of 0 in the model would make that workaround unnecessary.
4. **Money is `float`.** The backend converts every amount to integer cents before comparing. Using `Decimal`, or storing integer cents in the model, would remove the trap entirely.
5. **`ValidationDecision.verdict` ignores escalating and soft results, and an empty result list validates as `pass`.** This makes it easy to misread `verdict` as the purchase outcome.
6. **`LineItem.quantity` defaults to 1**, which is another value that looks extracted when it wasn't.
7. **`extractors_agreed` defaults to `True`**, which assumes agreement that nobody reported.
8. **`PurchaseRequest` has no `proposal` field.** Adding one would let us drop the backend-only `PurchaseSubmission`.
9. **`escalate_if` defaults differ:** `["any_unverifiable_hard"]` on the draft, but `[]` on `Contract`. The backend ignores the list and always escalates on an unverifiable hard fact.
10. **`MerchantPolicy`'s allow/deny overlap check is case-sensitive.** `"Mock Nike"` in allow and `"mock nike"` in deny passes validation. The engine checks the deny list first, so this fails safe.
11. **Mixing naive and aware datetimes in the contract validators raises `TypeError`**, not a validation error. The backend catches it.
12. **There are no event types for "rejected before evaluation" or "contract expired"**, so the backend reuses `purchase_blocked` with a `data.reason`. Adding `purchase_rejected` and `contract_expired` would be clearer.
13. **`Contract` has no `draft_id`.** The backend stores the link in its own database column instead.

## Not done yet (P2)
- **Authentication for `/approve` and `/reject`.** Right now anyone who can reach the API, including the shopping agent itself, could approve an escalated purchase. Before any real use these must require proof that the *user* is the caller (a session or passkey the agent doesn't have).
- **An expiry on approvals.** An escalated purchase can currently be approved at any time. Prices and stock can change, so approvals older than a few minutes should probably require a fresh checkout.
- User-held key signatures, which would replace the demo HMAC.
- A real payment or network-token sandbox, which plugs in at `issue_credential` in services.py.
- Anchoring the evidence ledger externally.
- Tightening CORS.
