# Decisions

This file explains why the integrated Handshake works the way it does. Most entries resolve a conflict between the teammates' specs; some record a fact verified against a real tool. Section numbers refer to `HANDSHAKE_BUILD.md`.

## Owner decisions after the integration (these override parts of the build spec)

**Each user connects their own Stripe Link account from the website.**
There is no longer one Link login shared by the whole server. The backend runs the Link CLI with `HOME` set to a private per-user directory, `.handshake-state/link-homes/<hash of email>` (mode 0700). The user's Link login lives there, and every spend request on their behalf uses it.

The website's "Connect Stripe Link" starts `auth login --format jsonl`. That output streams (verified): the verification URL and phrase arrive in about a second, and the CLI keeps polling in the background until the user approves in the Link app. The access token is never returned by any API. In stub mode the connection is simulated.

**Sign = fund. This reverses decision 3.2's "no held funds".**
Signing a contract now asks the user's Link account for one single-use **test** card for the contract's all-in hard cap, and the user approves it in Link. Link's own limits apply: the approval window is 10 minutes, and a card is valid for 12 hours, after which the contract must be funded again.

**The funded card is stored on the contract, encrypted. This reverses section 6.6's "never store a PAN".**
It is encrypted with AES-256-GCM (`HANDSHAKE_CARD_ENCRYPTION_KEY`; `dev.py` generates one, and prod refuses the public dev key). The contract and funding ids are the authenticated data, so a ciphertext copied onto another contract fails to decrypt. The card is decrypted only at release (agent-visible mode) or at payment (executor mode), and is **wiped from the database in the same transaction**. Revoking the contract also wipes it. So the plaintext card exists only in memory, and the stored copy exists only between funding and use.

**The card unlocks only for a checkout Handshake authorized.**
The owner chose this over releasing the card as soon as the contract is funded, so Handshake still decides. The agent requests a purchase; the extractor, the link parser, and the engine run. Only an AUTHORIZED checkout makes `get_payment_credential` available. Even then, Handshake re-reads the checkout just before releasing the card. If it changed for the worse, the card stays locked and the purchase stops at `checkout_changed`. A declined purchase also leaves the card stored for the agent's next try.

**The per-purchase Link request is gone.**
The owner chose to replace it rather than keep both. Purchases against an unfunded contract are refused with `contract_not_funded`. `POST /purchases/{id}/payment/simulate-approval` became `POST /contracts/{id}/funding/simulate-approval`.

## Conflicts decided in the build spec (section 3)

**3.1 Credential exposure: the agent receives the Link TEST card by default.**
The older docs said the agent never sees payment details. That line is superseded: Shuban's spec, Ajay's original `mcp.md`, the product overview, and the `Credential` docstring in models.py all said it.

The default is now **agent-visible mode**. After Handshake authorizes the purchase and the user approves it in Link, the agent calls `get_payment_credential` **once**. It receives the single-use Link **TEST** card and pays the merchant itself.

Every protection from Ajay's handoff is kept:

- a dedicated sensitive response model (`services.CredentialRelease`)
- a check at release time that the caller is the contract's bound agent and that the purchase is the right one
- one-time release, enforced by an atomic `credential_ready → paying` claim
- the `HANDSHAKE_AGENT_INSTRUCTIONS` text
- card values kept out of every other response, log, error, and evidence record

A test sweeps the database rows, the API responses, and the captured logs to prove the last point.

**Executor mode** (`HANDSHAKE_CREDENTIAL_MODE=executor`) is built too. The backend retrieves the card and pays the merchant itself, and the card never enters an agent's context.

*The one real tradeoff:* once a card is in a model's context, the MCP server can't control what the host keeps in its transcript. That's acceptable here because every card is a Link test card. models.py is unchanged.

**3.2 Payment rail: Stripe Link in test mode is the only rail.**
Rohan's held-funds design is gone: the PaymentIntent that held the cap at signing, `Contract.funding`, the refund events, and the placeholder card form. Signing is now a plain review-and-confirm step. His "Pending, waiting for your one-tap approval" state now means the Link approval step.

**3.3 The compiler runs in the backend.**
Ajay's compiler moved from the MCP process into `handshake/compiler.py`. The backend holds the OpenAI key, lints each draft, and persists it. The MCP server is a thin client that needs only the backend URL and an agent token.

**3.4 Handshake produces the TransactionProposal.**
`POST /purchases` no longer accepts a proposal. The agent sends only the `checkout_url` (plus an idempotency key and an optional selection report). `extractor.py` then reads the checkout **twice**, from the JSON feed and from the embedded page facts, and normalizes Sri's format. The agent's claims are never an input to the decision. The API tests now load their proposals through a `StaticExtractor`, with identical assertions.

**3.5 Endpoint names follow the backend.**
The frontend adapts to the backend's names: resolve became approve and reject, and decline became reject. The backend added the capabilities that were genuinely missing: drafts, compile, PATCH, amend, purchase lists, payment refresh, simulated approval, credential release, and the `field` and `label` keys on results.

**3.6 Two separate consents.**
`POST /purchases/{id}/approve` means "I accept this escalated purchase's unverifiable facts". Approving the **payment** happens separately, in Link, or with the "Simulated provider approval" button in stub mode. An approved escalation becomes AUTHORIZED and then goes to Link approval like any other purchase.

**6.4 Policies without a source.**
`TermsPolicy` and `MerchantPolicy` have no `source` field in models.py, so the UI labels them "Default". This answers Rohan's open question 4.

## Decisions made during integration (not specified in the build spec)

**The single-use contract is reserved at authorization, not at completion.** *(Still true under fund-at-signing: the reservation is released if the purchase ends without any money moving.)*
Section 9.4 says to mark the contract used at completion. But if that waited, two purchases could both be authorized against one single-use contract while waiting for Link approval. So the backend keeps its atomic `ACTIVE → USED` claim at authorization; the completion step is then a no-op.

If the payment ends in a state where **no money moved**, the reservation is released back to ACTIVE with a conditional update and an evidence event. Those states are denied, expired, checkout changed, failed before any order, and declined by the user. An uncertain outcome (`unknown`) or an overcharge never releases it.

**The models.py `Credential` row is Handshake's authorization grant.** *(The card itself is now the contract's stored funding card; see above.)*
It is created at authorization: exact amount, single use, a 30-minute TTL. It never holds card data. The card itself comes from Link or from the stub provider at release time, and exists only in memory.

**Contracts bind to an agent by default.**
If the user signs without naming an `agent_key`, the contract is bound to `HANDSHAKE_AGENT_ID`. Otherwise no agent could use it (and "any agent" would be unsafe).

**Agents connect through the user's login (OAuth 2.0 device authorization, RFC 8628 style).** *(Requested by the owner during the build.)*
When the MCP server has no agent token, a tool call returns a login link. The user logs in to Handshake at `/connect`, sees what the agent may and may never do, and approves. The MCP server then gets a signed **agent** token bound to that user and agent id, and caches it in a 0600 file. Only a user can approve; an agent token calling `/oauth/device/approve` gets 403.

Agent tokens are stateless HMAC tokens (7-day TTL). There is no revocation list yet, so rotating `HANDSHAKE_SESSION_SECRET` is the way to revoke them. The static `HANDSHAKE_AGENT_TOKEN` still works for scripts.

**A deterministic checkout-link parser runs before the engine.** *(Requested by the owner during the build.)*
Three hard rules, `checkout_link_format`, `checkout_link_merchant`, and `checkout_link_selection`, check that the link the agent found actually matches the contract:

- the link is the merchant's checkout page, and the page Handshake read;
- the link's origin belongs to the merchant the page claims to be (`HANDSHAKE_MERCHANT_IDENTITIES`), and the contract allows that merchant;
- the agent's own selection report names this link, merchant, and product.

A price differing from what the agent reported is UNVERIFIABLE (escalate); every other mismatch is FAIL. It is pure Python, with no LLM. It runs on real extractions; the `StaticExtractor` used by the older tests supplies no link facts, so those tests are unchanged.

**`/reject` also declines an authorized purchase.**
Section 16 asked for this, so an older test that expected 409 was updated to the new behavior. It works only while the payment hasn't started. It cancels the provider request, revokes the credential, and frees the contract.

**`POST /purchases/{id}/complete` is internal.**
It returns 403 over HTTP, so nobody can report "the merchant charged X". Reconciliation runs inside the backend after it verifies the merchant's order independently. The old tests call the same service function, with identical assertions.

**Pre-evaluation rejections still leave a record.**
Rejections against a known contract (tampered, revoked, used, expired, wrong agent, disallowed URL, unreadable checkout) create a BLOCKED or FAILED purchase with evidence. The frontend can then show them like any other purchase. A completely unknown contract id is a plain 404.

**Evidence subtypes.**
models.py's `EvidenceEventType` has no payment, Link, or decision steps. Events use the closest existing type and put the precise subtype in `data.kind`. The full list is in `docs/API.md`.

**Drafts live in their own table.**
A draft and a signed contract are different models with different ids. The `drafts` table stores `previous_contract_id` (which ContractDraft lacks), the compiler metadata, and the lint results.

**Lint issues block signing.**
Lint errors appear in `clarifications_needed` with a `lint: ` prefix, and signing re-runs lint. A draft whose deadline has passed is refused (409 `draft_has_blocking_issues`). The API tests therefore use a rolling delivery deadline, 14 days out; the engine tests keep their fixed dates because they evaluate at a fixed time.

**`mcp` is pinned to 1.30.0.**
`mcp` 2.x renamed `FastMCP`, and the build spec says to keep Ajay's `mcp.server.fastmcp` API. 1.30.0 is the newest 1.x release. Ajay's venv wasn't included in the delivery, so there was no older version to match.

**The mock merchant accepts Link's documented test card.**
`4000009990001984` fails the Luhn check (details in the Link section below).

**The simulated provider keeps its requests in memory.**
After a restart, a simulated request reads as `expired`, which fails safe.

**The payable total never under-counts.**
The cap check, the authorized amount, and the credential all use the larger of the claimed total and the total computed from its parts. This was already in the backend and is kept.

**The currency allowlist is USD only.**
That's what the demo pays in. Widening it is a deliberate decision, not a default.

## Stripe Link CLI: what was verified (section 9.2)

**Version.** `@stripe/link-cli` **0.23.0**, the latest on npm on 2026-09-26. It is pinned in config as `HANDSHAKE_LINK_CLI="npx --yes @stripe/link-cli@0.23.0"`.

**Sources.** Everything below was read from three places: `--help` and `--schema` for each command, `--llms-full`, and the package's own `dist/cli.js`. The official README at github.com/stripe/link-cli was read as well. Nothing below is guessed.

**Link account.** This machine is **not logged in to Link**. `auth status` printed `[{"authenticated": false, "credentials_path": "…"}]`. So no spend request was ever created, and `link_test` mode was not exercised against Stripe. The steps to run it are in `docs/DEMO.md`.

| What | Verified shape |
|---|---|
| `auth status --format json` | Returns a **list** of status objects when unauthenticated, e.g. `[{"authenticated": false, "credentials_path": "…"}]`. Ajay's parser, which accepts an object or a list and takes the last entry, is kept. |
| `spend-request create` | Flags: `--credential-type card`, `--merchant-name`, `--merchant-url`, `--amount <cents>`, `--currency usd`, `--context <≥100 chars>`, `--line-item "name:…,unit_amount:…,quantity:…"`, `--total "type:total,display_text:Total,amount:…"`, `--idempotency-key`, `--metadata key:value`, `--request-approval` (on by default; the negation is `--no-request-approval`), `--test`, and `--expires-at`. There is also an **undocumented `--approve` flag**, which Handshake never passes. |
| Behavior of `create --format json` with `--request-approval` | In agent/JSON mode it returns **immediately**, without polling. The response is `{…spend request, "instruction": "Present the approval_url to the user…", "_next": {"command": "spend-request retrieve <id> --interval 2 --max-attempts 300"}}`, and `approval_url` is included when approval was requested. A duplicate idempotency key returns an error that names the existing request, which is then retrieved instead. |
| `spend-request retrieve <id>` | `--include card --output-file <path>` writes the full card to a file the CLI creates with mode **0600**. The CLI refuses to overwrite an existing file unless `--force` is given. Stdout then shows only redacted card fields plus `card_output_file`. The card object has `number`, `cvc`, `exp_month`, `exp_year`, `billing_address`, and `valid_until`. |
| `--test` on retrieve and cancel | **Rejected**: `{"code": "UNKNOWN", "message": "Unknown flag: --test"}`. Test mode is a property a request gets when it is created. |
| Errors | Printed as `{"code": "…", "message": "…"}`. |
| Statuses | `created`, `pending_approval`, `requires_action`, `approved`, `submitted`, `succeeded`, `denied`, `declined`, `canceled`, `expired`, `failed`. These are mapped in `payments.LINK_STATUS_MAP`. Any other value is treated as `unknown`, and an unknown status never advances a payment. |
| Limits (README) | 50,000 cents maximum per request. The approval window is 10 minutes. Cards are valid for 12 hours. There is a $500 daily limit, at most 30 active or 10 approved requests at once, and at most 50 creations per hour. |
| Test mode (README) | Test mode "will return test payment credentials (e.g. test card `4000009990001984`)" and "will not charge the underlying payment method". **That example number fails the Luhn check** (verified). The mock merchant's `/pay` therefore accepts Luhn-valid numbers **plus** the documented Link test numbers (`merchant.LINK_TEST_CARDS`); otherwise a real Link test card would be declined. |

**Capabilities not claimed.** Link issues a single-use test card. Handshake does not claim merchant locking, exact spend limits, or an expiry that the CLI doesn't report. The 50,000-cent limit is enforced by Handshake itself (`HANDSHAKE_LINK_MAX_MINOR_UNITS`) as well as documented by Link.

**How test mode is enforced.** Every spend request is created through `payments.build_create_command()`, which always ends the command with `--test`. No function takes a parameter to remove it. `HANDSHAKE_PAYMENT_MODE` accepts only `stub` or `link_test`; `live`, an empty value, or a typo refuses to start. `retrieve` and `cancel` only accept `lsrq_…` ids that the backend itself created. Tests pin all of this down (`tests/test_payments.py`).

### Real `link-cli` output shapes (found live on 2026-09-26)

- `spend-request create --format json` prints a **JSON list** of streamed updates: `[{"id": "lsrq_…", "status": "pending_approval", "approval_url": …, "_next": …}]`. The same goes for `retrieve`. `cancel` prints a bare object.
- The first adapter expected a bare object, which caused the live failure on purchase_93fb17da748a. The adapter now accepts an object, a list (the last update wins), or JSON lines. A sanitized copy of the real output is the regression fixture at `tests/fixtures/link_cli_0.23.0_spend_request_create.json`.
- `--format <anything>` puts the CLI in agent mode. In agent mode, `--format json` collects every update until the end, which is why `python -m handshake.payments login` now runs **without** `--format`, so the approval link prints straight to the terminal. `--format jsonl` streams line by line (`{"type": "chunk", "data": …}`); the website login uses that.
- The CLI keeps its login in `$HOME/Library/Preferences/link-cli-nodejs/config.json` (via `conf`), so a per-user `HOME` gives each user their own Link login.
