# Handshake: frontend

Next.js 16, TypeScript, Tailwind v4, shadcn/ui (Base UI) and lucide-react.

```bash
npm install
npm run dev          # http://localhost:3000, uses mock data
```

To use the FastAPI backend instead of mocks:

```bash
NEXT_PUBLIC_USE_MOCKS=false NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

## Screens

| Route | What it shows |
|---|---|
| `/` | Landing page with the Glyph Portal scroll effect ("HANDSHAKE") that opens into the pitch |
| `/contracts` | Dashboard with tabs: Pending (match found, waiting for your one-tap approval), Active, Draft, Used, Rejected (revoked or expired, with the reason on each card), plus links to the demo scenarios |
| `/contracts/draft_shoes01` | Contract review: plain-English contract, the "Inferred by Handshake" panel, edit, and sign |
| `/contracts/:id` (signed) | Contract, revoke, "Edit (creates new version)" and its purchase attempts |
| `/purchases/purchase_pending` | Passed every check. Approve (paid from held funds, rest refunded) or "Not this one" |
| `/purchases/purchase_pass` | Authorized purchase, with the check-by-check comparison and timeline |
| `/purchases/purchase_blocked` | Malicious merchant: $149.72 against a $135 cap, plus a hidden add-on |
| `/purchases/purchase_escalated` | UNVERIFIABLE seller, with Reject / Approve exception |

Purchase pages animate the agent's progress (Searching → … → Authorized/Blocked/Needs you). Use **Replay** during the demo.

## Layout

- `lib/types.ts`: TypeScript copies of the backend Pydantic models. Additions the backend doesn't have yet are marked `PROPOSED`.
- `lib/api.ts`: **the only file that talks to the backend.** Uses an in-memory mock store when `USE_MOCKS` is on.
- `lib/mock-data.ts`: demo fixtures. Times are relative to now.
- `components/handshake/*`: app components.
- `components/ui/*`: shadcn primitives and the vendored `glyph-portal.tsx` (MIT, keep its license header).

## Open questions for the backend (Shuban)

1. **How an exception is represented.** Frontend assumes `POST /purchases/:id/resolve {action: "approve"|"reject"}`. It stores `purchase.resolution` and emits `escalation_approved` / `escalation_rejected` events. The decision's verdict stays `unverifiable`.
2. **`GET /purchases/:id` should include `proposal`** so the UI can show what the agent tried to buy.
3. **`ConstraintResult` needs `field` and `label`** so the UI doesn't have to parse the `constraint` string.
4. **`TermsPolicy` and `MerchantPolicy` have no `source`.** The UI shows them as "Default" for now.
5. **Drafts:** frontend assumes `GET /drafts` and `GET /drafts/:id`, returning a `ContractDraft` with the `CompilerOutput` assumptions flattened in.
6. **Editing:** frontend assumes `PATCH /drafts/:id` and `POST /contracts/:id/amend`. The new draft has `previous_contract_id`, and signing it revokes the old version.
7. `POST /contracts/:id/sign` is called with the draft id.
8. **Prepaid funds:** the user pays the contract's maximum total when signing (`Contract.funding`, PROPOSED). When the user approves a match, the total is taken from the held money and the rest is refunded. If the contract is revoked or expires, or a new version replaces it, everything is refunded. The frontend assumes `POST /purchases/:id/approve` and `POST /purchases/:id/decline`. Stripe fits this with a PaymentIntent using `capture_method: "manual"`: hold the maximum at signing, capture the actual total on approval, and the rest is released automatically. The card form in `components/handshake/payment-dialog.tsx` is a placeholder and never sends card data anywhere.
9. **Pending and Rejected are frontend-only groups** (see `lib/status.ts`). Pending means the contract is active and has an `authorized` purchase waiting for approval. Rejected means the contract is revoked or expired.
