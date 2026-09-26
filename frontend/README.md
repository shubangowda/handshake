# Handshake: frontend

Next.js 16, TypeScript, Tailwind v4, shadcn/ui (Base UI) and lucide-react.

```bash
npm install
npm run dev          # mock data by default, no backend needed
```

## Running against the backend

The backend is the FastAPI app in `../handshake` (routes and shapes: [`../docs/API.md`](../docs/API.md)).
The frontend reads two settings, and nothing else decides where it connects:

| Variable | Meaning |
|---|---|
| `NEXT_PUBLIC_API_URL` | Base URL of the backend. Required when mocks are off; if it's missing the app shows a configuration error instead of guessing. |
| `NEXT_PUBLIC_USE_MOCKS` | `false` talks to the backend. `true` (or unset) uses the in-memory mock store. |

Put them in `.env.local` (git-ignored). `scripts/dev.py` writes it for you; otherwise copy the template:

```bash
cp .env.example .env.local   # then edit, and restart npm run dev
```

`NEXT_PUBLIC_*` values are baked in when `next dev`/`next build` starts, so restart after changing them.

Sign in with the **demo login** (email only, no password; to be replaced by passkeys/OAuth). It calls `POST /auth/demo-login` and keeps the token in `localStorage` (`lib/session.ts`). Every request sends `Authorization: Bearer <token>`; a 401 clears the session and returns to `/login?next=<where you were>`.

The header shows which payment rail the backend uses (`GET /health`): **Stripe Link: TEST MODE** or **Simulated provider**. In mock mode it shows **Mock data**.

## Screens

| Route | What it shows |
|---|---|
| `/` | Landing page with the Glyph Portal scroll effect ("HANDSHAKE") that opens into the pitch |
| `/login` | Demo login, then the optional interests step. Honors `?next=` (skips onboarding when you were mid-task) |
| `/contracts` | "Describe what to buy" box (compiles a draft via `POST /drafts/compile`; the offline compiler only understands the Pegasus 41 demo request), plus tabs: Pending (a purchase waiting for your payment approval), Active, Draft, Used, Rejected (revoked or expired) |
| `/contracts/:draftId` | Contract review: plain-English contract, "Inferred by Handshake", blocking issues, Edit, and "Review & sign" (restates the all-in cap, deadline, merchant rules and key constraints; disabled while blocking issues exist) |
| `/contracts/:id` (signed) | Contract, signature verification, Revoke, "Edit (creates new version)", and purchase attempts with their payment state |
| `/purchases/:id` | The agent's attempt: progression (Replay replays it from the evidence), the payment panel, check-by-check comparison, checkout breakdown, and activity. Polls every 2.5 s until the purchase is finished |
| `/connect?code=USER-CODE` | Where an agent's (MCP) login link lands: which agent is asking, what it may and may never do, Approve / Deny |

On a purchase page:

- **Passed every check**: "Approve in Link" opens Link's approval page in a new tab (`link_test` mode). In `stub` mode the button is "Simulated provider approval" (stands in for Link's approval tap). "Not this one" declines while the payment hasn't started.
- **Payment progression**, in plain words: Awaiting your Link approval → Rechecking checkout → Card released to agent / Paying → Paid → Completed (with order id and last4). Side exits: declined in Link, expired, checkout changed after approval (nothing paid), failed, and "Checking with the merchant".
- **Escalated** (something couldn't be verified) needs two separate consents: 1. accept the exception in Handshake, 2. then approve the payment in Link.

Mock mode keeps demo fixtures at `/contracts/draft_shoes01` and `/purchases/purchase_pending|purchase_pass|purchase_blocked|purchase_escalated`, linked from the dashboard. The mock payment advances one step per poll so the progression is visible.

## Layout

- `lib/types.ts`: TypeScript copies of `handshake/models.py`, field for field. Shapes the API adds around them (DraftRecord, PurchaseDetail, PaymentInfo, Health, ...) are below an "API-only" line.
- `lib/api.ts`: **the only file that talks to the backend.** Uses an in-memory mock store when `USE_MOCKS` is on.
- `lib/session.ts`: the demo session (token + email).
- `lib/status.ts`: dashboard grouping and payment-state wording.
- `lib/mock-data.ts`: demo fixtures. Times are relative to now.
- `components/handshake/*`: app components (`payment-status.tsx` is the payment panel, `sign-dialog.tsx` the review-and-sign dialog).
- `components/ui/*`: shadcn primitives and the vendored `glyph-portal.tsx` (MIT, keep its license header).

## Resolved (was "Open questions for the backend")

All answered by [`docs/API.md`](../docs/API.md):

1. **Exceptions**: `POST /purchases/:id/approve` or `/reject`. The decision keeps its `unverifiable` verdict; `PurchaseDetail.resolution` records who accepted which checks. Approving an exception does not pay: the payment still needs approval in Link.
2. **`proposal`** is included in every purchase response (`PurchaseDetail`).
3. **`ConstraintResult.field` and `label`** are always present in API responses (added when the API serializes a decision; not in models.py).
4. **`TermsPolicy` and `MerchantPolicy` have no `source` field**, so the UI still shows them as "Default".
5. **Drafts**: `GET /drafts` and `GET /drafts/:id` return a DraftRecord (ContractDraft flattened with the compiler metadata, plus `review_url`, `blocking_issues`, `signed_contract_id`). New: `POST /drafts/compile`.
6. **Editing**: `PATCH /drafts/:id` (send only changed keys) and `POST /contracts/:id/amend`. Signing the new draft revokes the old version.
7. **Signing**: `POST /contracts/:id/sign {draft_id}`. `409 draft_has_blocking_issues` carries `details.blocking_issues`.
8. **Prepaid funds are gone.** Nothing is charged at signing. Each purchase is paid separately after the user approves it in Link (or the simulated provider in stub mode). No held funds, no refunds. Declining is `POST /purchases/:id/reject` on an authorized purchase whose payment hasn't started.
9. **Pending** is now based on the purchase's `payment_state === "awaiting_approval"`, because the backend marks a single-use contract `used` while a purchase holds it. **Rejected** means the contract is revoked or expired.
