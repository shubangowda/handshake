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

## Payment model: fund at signing

1. **Connect Stripe Link** (link_test mode only). Each user connects their *own* Link account: the header's "Connect Stripe Link" button (and a banner on `/contracts` while not connected) calls `POST /link/connect`, shows Link's login button and the phrase to check, and polls `GET /link/status` every 2 s until connected. It then shows "Stripe Link connected" with Disconnect. In stub mode it says "Simulated provider (no Link account needed)".
2. **Sign & fund.** Signing asks the user's Link account for one single-use test card for the contract's all-in hard cap. The contract page's Funding card shows it: awaiting approval ("Approve in Link" opens Link's page; in stub mode "Simulated provider approval"), then funded ("Card ending XXXX stored, encrypted, locked until Handshake approves a checkout", valid for 12 hours), released, used. After a denied, expired, failed, or used card, an active contract offers "Fund again". If Link isn't connected, signing returns `link_not_connected` and the dialog shows the Connect panel.
3. **Purchases need a funded contract.** Once Handshake authorizes a checkout, the contract's card is unlocked for that checkout only; the agent collects it once and pays. "Not this one" declines before the card is released (the card stays locked on the contract).

## Screens

| Route | What it shows |
|---|---|
| `/` | Landing page with the Glyph Portal scroll effect ("HANDSHAKE") that opens into the pitch |
| `/login` | Demo login, then the optional interests step. Honors `?next=` (skips onboarding when you were mid-task) |
| `/contracts` | Connect-Link banner (link_test, not connected), "Describe what to buy" box (compiles a draft via `POST /drafts/compile`; the offline compiler only understands the Pegasus 41 demo request), plus tabs: Pending (signed, funding card waiting for your approval: "Approve funding in Link"), Active, Draft, Used, Rejected (revoked or expired) |
| `/contracts/:draftId` | Contract review: plain-English contract, "Inferred by Handshake", blocking issues, Edit, and "Sign & fund" (restates the all-in cap, deadline, merchant rules and key constraints, and explains the funding card; disabled while blocking issues exist) |
| `/contracts/:id` (signed) | Contract, signature verification, the Funding card (polls every 2.5 s while awaiting approval), Revoke, "Edit (creates new version)", and purchase attempts with their payment state |
| `/purchases/:id` | The agent's attempt: progression (Replay replays it from the evidence), the payment panel, the contract's funding summary, check-by-check comparison, checkout breakdown, and activity. Polls every 2.5 s until the purchase is finished |
| `/connect?code=USER-CODE` | Where an agent's (MCP) login link lands: which agent is asking, what it may and may never do, Approve / Deny |

On a purchase page:

- **Passed every check**: the contract's funded card is unlocked for this checkout. "Not this one" declines while the card hasn't been released.
- **Payment progression**, in plain words: Card unlocked → Card released to agent (or Paying, in executor mode) → Paid → Completed (with order id and last4). Side exits: declined, authorization expired, checkout changed (the card stayed locked), failed, and "Checking with the merchant".
- **Escalated** (something couldn't be verified): 1. accept the exception in Handshake, 2. the contract's funded card is then released once to the agent for this checkout.

Mock mode keeps demo fixtures at `/contracts/draft_shoes01`, `/contracts/contract_shoes_d` (funding awaiting approval), `/contracts/contract_shoes_e` (expired card: "Fund again"), and `/purchases/purchase_pending|purchase_pass|purchase_blocked|purchase_escalated`, linked from the dashboard. Mock mode uses the simulated provider. On `purchase_pending`, the mock "agent" collects the card after a few polls, so the progression is visible and "Not this one" can still be tried first.

## Layout

- `lib/types.ts`: TypeScript copies of `handshake/models.py`, field for field. Shapes the API adds around them (DraftRecord, PurchaseDetail, PaymentInfo, Health, ...) are below an "API-only" line.
- `lib/api.ts`: **the only file that talks to the backend.** Uses an in-memory mock store when `USE_MOCKS` is on.
- `lib/session.ts`: the demo session (token + email).
- `lib/status.ts`: dashboard grouping and payment-state wording.
- `lib/mock-data.ts`: demo fixtures. Times are relative to now.
- `components/handshake/*`: app components (`payment-status.tsx` is the purchase payment panel, `funding.tsx` the contract Funding card and summary, `link-connect.tsx` the Connect Stripe Link panel, `sign-dialog.tsx` the Sign & fund dialog).
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
8. **Funding happens at signing** (see "Payment model" above and API.md): one single-use Link test card for the hard cap, stored encrypted on the contract and released once for an authorized checkout. Declining is `POST /purchases/:id/reject` on an authorized purchase whose card hasn't been released.
9. **Pending** means a signed contract whose `funding.state` is `awaiting_approval`. **Rejected** means the contract is revoked or expired.
