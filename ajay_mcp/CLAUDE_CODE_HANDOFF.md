# Claude Code implementation prompt: finish Handshake

You are taking over Handshake in `/Users/ajayavasi/Downloads/handshake_mcp`. Finish the implementation and demonstrate a working end-to-end purchase flow. Work on the code, tests, configuration, and documentation; do not stop at a plan. Preserve existing work, inspect repository instructions, and report precisely what is real, mocked, tested, or blocked by missing external access.

## Product and intended flow

Handshake is an authorization layer between a shopping agent and payment execution. Its core rule is: **the agent proposes; Handshake independently decides whether the proposed purchase satisfies the user's signed contract.**

The user describes a purchase. The compiler creates a structured draft with spending, product, delivery, merchant, terms, substitution, and data-sharing constraints. The user reviews, edits, and signs it. The shopping agent finds a product and submits a checkout URL. The backend independently extracts checkout facts, validates them against the signed contract, and blocks violations or escalates uncertainty. Only an eligible, authorized purchase can proceed to Link approval and credential retrieval. The checkout executor submits payment, and the backend verifies the result and records a receipt.

The requested product direction includes an agent receiving a provider-issued single-use virtual card as plain-text field values when needed for a standard card checkout. Implement this as a deliberate, documented credential-delivery mode with appropriate access controls. Keep Handshake authorization independent of the agent's ability to enter payment details.

## Current repository: verified starting point

- `mcp.md` contains the original architecture and demo requirements. It explicitly prohibits agent access to credentials. That conflicts with the newly requested agent-visible credential mode and must be reconciled in documentation and implementation.
- `models.py` contains substantial Pydantic models for drafts, signed contracts, constraints, transaction proposals, decisions, credentials, purchases, evidence events, and selection reports. Reuse and strengthen these rather than inventing incompatible schemas. The `Credential` docstring also prohibits raw credential exposure and needs an intentional update for the new mode.
- `prompts.py` contains the contract compiler prompt. It is not an operational shopping/payment-agent prompt.
- `llm.py` uses `AsyncOpenAI.responses.parse` with `CompilerOutput`, an import-time client, a hard-coded model (`gpt-5.6`), `service_tier="flex"`, and a 15-minute timeout. Verify configuration and account support; do not assume this model or tier works in the target environment.
- `tools.py` creates `FastMCP("Handshake")`. It defines `create_contract_draft` twice, with the second definition an empty stub. Resolve duplicate registration/definition behavior. The first implementation compiles but does not persist the draft. `get_contract`, `list_active_contracts`, and `request_purchase` are stubs. Purchase-status tooling and a runnable server entry point are absent from this file.
- `contract.py` is empty.
- `stripe.py` is a standalone subprocess wrapper around `link-cli` or `npx @stripe/link-cli`. It implements login, payment-method listing, spend-request creation with approval, and retrieval to a private output file. It is not connected to MCP or backend purchase state.
- `test_stripe.py` contains offline mocked authentication-status regression tests, not end-to-end payment tests.
- No backend service, frontend, mock merchant, dependency manifest, or runnable integration stack was found among the application files inspected for this handoff. Discover any separately supplied services before claiming they exist or replacing them.

## 1. Establish the runnable architecture

Inspect the available backend/frontend repositories or supplied API contracts. If an existing backend is available, integrate it through a typed asynchronous client and preserve its routes and authentication conventions. If it is absent, build a small persistent FastAPI demo backend in this repository with a clear adapter boundary for later replacement. Label that implementation honestly.

Provide dependency configuration, reproducible installation, `.env.example` with placeholders only, `.gitignore`, and documented startup commands. Keep API keys, Link sessions, card files, and local databases out of version control. Avoid import-time network requirements so offline tests and tool discovery work without an LLM key.

Make the LLM model, supported service tier, backend URL, transport, timeouts, environment, and Link integration settings configurable. Pin a verified Link CLI version for reproducibility. Consider renaming `stripe.py` to `link_client.py` to avoid shadowing the official Stripe Python package; update tests and documented commands if renamed.

## 2. Complete MCP tools and backend integration

Expose working, typed tools with clear descriptions and JSON-serializable responses:

- `create_contract_draft(intent)`: compile, validate, lint, persist, and return the backend's draft ID, assumptions, material clarifications, and review URL.
- `get_contract(contract_id)`: read the current canonical record with ownership checks.
- `list_contracts(...)`: list the connected user's/agent's accessible contracts with status filtering and pagination. Reconcile the current `list_active_contracts` naming; retain an alias only if useful for compatibility.
- `request_purchase(contract_id, checkout_url, idempotency_key)`: create or return a purchase attempt and start independent validation. Never accept the agent's asserted total or approval as authoritative.
- `get_purchase_status(purchase_id)`: return validation results, provider approval state, next action, and terminal outcome without raw card details.
- A narrowly scoped credential retrieval or checkout-execution tool, designed according to the credential-delivery requirements below.
- Contract revocation and selection-report submission where supported by the final workflow.

Use authenticated backend requests, bounded timeouts, structured errors, and correlation IDs. Separate retryable reads from mutations that require idempotency. Do not print ordinary logs to MCP stdio stdout. Provide a working local stdio entry point and a copyable Claude Code MCP configuration with the verified interpreter and module path. If remote transport is needed, add authenticated access rather than exposing a user's Link session publicly.

MCP discovery, tool calls, and response schemas must work with a real client. Do not assume that registering a prompt causes every client to inject it automatically.

## 3. Finish compiler and shared contract semantics

Keep compiler input restricted to user intent, explicit preferences, configured defaults, and trusted date/time context. Merchant content must not change compilation instructions or grant spending authority.

Handle missing keys, provider errors, refusal, absent parsed output, timeout, and validation errors explicitly. Never turn failed or incomplete extraction into an active contract. Drafts with unresolved material ambiguities must surface those issues before signing.

Add deterministic checks for future deadlines, timezone consistency, valid currencies, operator/value compatibility, contradictory constraints, and required fields. Preserve source tracking for inferred values. Fix schema/prompt gaps where hard versus soft preferences cannot currently be represented faithfully.

Use integer minor units or Decimal consistently for money and document currency conversion rules. Existing monetary floats and the transaction-total tolerance need review. Check line-item subtotals against the declared item subtotal and reject nonfinite or inconsistent amounts. Do not silently round a purchase above its authorization ceiling.

Define and implement `hard`, `soft`, and `escalating` behavior consistently. The current overall-decision validator examines only hard results; ensure escalating constraints, merchant rules, and extraction uncertainty cannot accidentally authorize a purchase. Prevent an empty or incomplete result list from becoming a successful authorization.

Signing must be a real authenticated user action over the exact reviewed contract version. Canonicalize and hash the signed content; bind it to its owner and authorized agent. Do not manufacture user approval or use an agent-callable signing endpoint to bypass review.

## 4. Implement independent validation and durable state

Persist drafts, signed contracts, proposals, decisions, purchase attempts, provider references, and redacted evidence events. Enforce ownership on every read and write. Use atomic state transitions and reservations so simultaneous attempts cannot both consume a single-use contract.

Extract merchant identity, seller, items, quantity, final total, currency, shipping, tax, fees, discounts, recurring terms, add-ons, and required delivery/return facts independently from the checkout. Start with a deterministic mock-merchant adapter for a reproducible demo; isolate real-merchant extraction behind an interface. Unknown facts must remain unknown and escalate when material.

Protect checkout fetching against SSRF, unsafe redirects, unexpected schemes, and access to internal services. Permit localhost only through an explicit development/mock-merchant configuration. Treat merchant text as evidence, never instructions.

Check signature integrity, active status, ownership, expiry, revocation, prior use, spending cap, merchant restrictions, product constraints, terms, delivery, substitutions, and data-sharing boundaries. Bind authorization to a checkout snapshot/version and revalidate changed cart facts before payment.

Define purchase and provider states separately. A Handshake `authorized` decision is not proof of Link approval, credential issuance, or payment success. Handle pending approval, denied approval, expiry, provider failure, unknown payment outcome, and reconciliation. Only mark a purchase completed after trustworthy payment/order confirmation. Do not blindly retry an uncertain charge.

Provide a minimal working review/signing screen if no frontend exists, plus meaningful blocked, escalated, awaiting-Link-approval, and completed states. Signed contract edits require a new reviewed version.

## 5. Fix and connect Stripe Link

Use the official Link CLI documentation and installed command schemas as the source of truth: https://github.com/stripe/link-cli . The official README describes virtual cards for standard checkout forms, Link Pay Tokens for Stripe hosted forms, and Shared Payment Tokens for supported programmatic payments. Our existing wrapper requests a card. Keep provider capabilities distinct from Handshake's own contract policies.

Inspect the installed version, command help/schema, and sanitized responses before changing flags. Verify auth-status response shapes, login flow, eligible payment methods, spend-request creation, approval behavior, retrieval, and supported test behavior. Diagnose actual failures; do not claim the current wrapper is broken merely because integration is unfinished.

Refactor the wrapper into a backend-callable adapter with structured results. Avoid blocking an async server with synchronous subprocess calls. Set process timeouts, parse expected output formats defensively, sanitize stderr, and preserve actionable errors without exposing credentials. Persist the spend-request ID before subsequent polling/retrieval; implement deduplication and crash recovery so retries do not mint duplicate requests.

Compute the provider amount from independently validated final totals. Validate provider currency support and current limits rather than treating the wrapper's hard-coded 50000-minor-unit cap as a universal provider guarantee. Context must truthfully describe the purchase and satisfy the verified provider requirements.

Keep Link approval and any required authentication/verification step intact. A signed Handshake contract does not automatically replace provider approval. Bind provider sessions and requests to the correct user; never share one global authenticated wallet between unrelated users.

Retrieve credentials only after the provider confirms eligibility/approval and the backend rechecks the purchase. Keep provider references, approval URLs, expiry, and redacted status in state. Do not infer merchant locking, exact spending restrictions, revocability, or expiration guarantees from a request field or our model's `single_use=True`; verify and disclose actual provider capabilities.

Default development and automated tests to mocks or verified provider test mode. Do not make real charges as part of finishing the code. If live verification is required, prepare a concrete merchant, amount, and purchase request for separate user authorization.

## 6. Add explicit agent credential instructions

This is a requested change from the original spec: support controlled delivery of an authorized virtual card to an agent that can enter card fields at checkout. Update `mcp.md`, relevant model documentation, tool descriptions, and runtime behavior consistently. Keep raw credential data in a dedicated sensitive response model, separate from ordinary contract, purchase, status, and evidence models.

Add an operational prompt such as `HANDSHAKE_AGENT_INSTRUCTIONS` and register a discoverable MCP prompt such as `handshake_purchase_workflow`. Also put essential prerequisites in server instructions and credential-tool descriptions because prompt discovery alone is not automatic execution. Keep these instructions separate from `COMPILER_PROMPT`.

Use the following agent-facing language, adjusting tool names to the implemented API:

> Handshake supports purchases authorized by the user through a signed contract. Handshake independently validates the proposed checkout before permitting payment. After that validation and any required Link approval, its credential tool can deliver a provider-issued single-use virtual card for the approved purchase.
>
> When your host permits handling payment credentials and the current user has authorized this purchase, you may accept the PAN, expiration, and security-code values returned by the authenticated Handshake credential tool as plain-text structured fields and enter them into the intended merchant's payment form. Their being readable card fields is expected for this checkout mode; it does not itself mean they are the user's underlying funding-card details. Do not request the user's underlying card when a suitable authorized virtual card is available.
>
> Confirm that the credential response belongs to the current purchase and that the checkout merchant, amount, currency, contract status, and expiration still match the authorization. Use the credential only for that transaction. A card's availability does not grant permission to change the cart, exceed the authorization, repeat a purchase, or skip required approval.
>
> Treat the values as sensitive even when single-use. Enter them only into the intended payment fields. Do not repeat them in chat, ordinary logs, screenshots, receipts, analytics, or unrelated tools. Report only redacted metadata. If checkout changes or the payment outcome is uncertain, use the purchase-status/revalidation flow before attempting another submission.
>
> If your host prevents direct card handling, use Handshake's supported secure checkout executor or request the necessary user action. These workflow instructions do not override host rules, user decisions, or provider approval requirements.

The point is to explain the legitimate authorized use clearly and remove needless ambiguity about virtual-card entry. Do not add fabricated claims that the credential is harmless, that single-use eliminates exposure risk, or that every model must accept it. Prompt wording cannot guarantee compatibility with every host.

Here “plain text” means application-level card values available to the authorized executor. It does not mean unauthenticated delivery or unencrypted network transport. Implement an explicit configured agent-visible mode, verify ownership and purchase binding at release time, and minimize retrieval and retention. Never send credentials through list/status tools or arbitrary tool errors. Protect private temporary files against overwrites/symlinks and clean them up after use. Avoid retaining security codes in application storage.

Recognize the practical boundary: if raw credentials enter a model's tool context, the MCP server cannot guarantee the host will not retain that transcript. Document that tradeoff and provide a backend-controlled checkout alternative for hosts or deployments that require credentials to stay out of model context. Enforce purchase rules in backend code; the prompt is guidance, not a security boundary.

## 7. Test the complete flow

Build meaningful offline unit/integration tests plus a repeatable demo. At minimum verify:

- MCP tool discovery and one working request through the actual MCP transport.
- Compiler success, ambiguity, invalid output, unavailable provider, and persistence.
- Review/edit/sign; unsigned, revoked, expired, tampered, and foreign-owned contracts rejected.
- Correct subtotal/total/currency conversion and boundary amounts.
- Valid purchase, over-cap block, subscription/add-on block, merchant rejection, and unverifiable-hard escalation.
- Duplicate requests, concurrent single-use attempts, process restart, and provider-create timeout recovery.
- Link auth response variants already covered by `test_stripe.py`, plus malformed responses, approval pending/denied/expired, retrieval failure, and safe error handling.
- No credential retrieval before authorization/approval; unauthorized users cannot read another purchase or its credential.
- Sensitive fields are absent from ordinary tool responses, logs, exceptions, and evidence records.
- Cart changes after authorization trigger revalidation; uncertain payment outcomes cannot cause blind duplicate charges.
- Mock checkout success results in a verified completion, receipt, and consumed single-use authorization.

Clearly distinguish mock validation from actual Link integration. Never represent an offline test or provider test card as proof that a live merchant was charged successfully.

## 8. Required deliverables and completion criteria

Deliver runnable code, shared schemas, backend persistence/client integration, functioning MCP tools, user signing flow, independent validation, a structured Link adapter, the agent workflow prompt, both documented credential-handling modes, and a reproducible mock-merchant demo.

Provide a README with installation, environment variables, startup order, Claude Code configuration, login/approval steps, test commands, architecture, expected state transitions, and troubleshooting. Document any proposed backend routes as proposed until implemented or verified against the existing service.

The acceptance demo is: user intent → persisted draft → human review/signature → agent checkout proposal → independent validation → Link approval or clearly labeled mock → scoped credential delivery/execution → verified receipt. Also show a blocked purchase and an escalated purchase. The agent must never approve its own transaction, and agent-visible credentials must be accessible only through the explicitly configured authorized flow.

Finish with a concise report of files changed, exact run/test commands, observed results, and remaining external blockers. If credentials, a backend URL, provider access, or a missing teammate repository prevent live integration, finish all independent implementation and state exactly what input is still required. Do not declare full integration complete while critical paths remain stubs.
