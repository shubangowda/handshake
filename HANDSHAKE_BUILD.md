# HANDSHAKE_BUILD.md: Fuse the four parts into one working Handshake repo

## 0. Read this first

You are Claude Code (Opus 5.5) working in the folder that contains this file, the Handshake repo root (normally ~/handshake). Handshake is a hackathon project by a team of four. Each teammate finished a part separately. Your job is to fuse the parts into one cohesive, runnable product in a single git repository, push it to a private GitHub repo, and prove the whole flow works end to end.

This file is long on purpose. It was put in a file rather than pasted because the chat paste limit cut off an earlier prompt. Read the entire file before you change anything, including Appendix A (the canonical models.py). Then write a plan to docs/INTEGRATION_PLAN.md and follow the milestones in section 14. Run tests after every milestone. Do not stop at a plan. Build it.

Handshake in one sentence: the user signs a contract saying what an AI shopping agent may buy; the agent proposes a checkout; Handshake independently extracts the checkout facts, deterministically checks them against the signed contract, and only then lets payment happen through Stripe Link in test mode, logging every step as evidence.

The core rule, from every teammate's spec: the agent proposes, Handshake decides. No LLM ever makes an authorization decision.

## 1. Where the pieces are right now

Before starting, confirm each of these exists. If something is missing, stop and tell me exactly which path.

1. Shuban's backend (FastAPI, SQLAlchemy, deterministic engine, 181 passing tests) is at backend/ inside this repo root. Its files are backend/app/models.py, db.py, intent_diff.py, services.py, main.py, and __init__.py. Tests are in backend/tests/ (conftest.py, test_intent_diff.py, test_api.py). It also has requirements.txt, README.md, and possibly a .venv and handshake.db. Its README has the full endpoint documentation, and an "Open questions" list about models.py. Read both.
2. Ajay's MCP server, contract compiler, and Stripe Link wrapper are in _incoming/handshake_mcp/. The files are mcp.md (his original spec), models.py (identical to the backend copy except one extra blank first line), llm.py (OpenAI compiler call), prompts.py (COMPILER_PROMPT, keep it word for word), tools.py (FastMCP tools, mostly stubs, with create_contract_draft defined twice), contract.py (empty), stripe.py (Link CLI subprocess wrapper), and test_stripe.py (offline auth-status tests). The folder also holds a macOS venv/, a __pycache__/, and a .env containing a REAL OpenAI API key. Never copy venv/, __pycache__/, or .env into the repo, and never print the key. Ajay also wrote _incoming/CLAUDE_CODE_HANDOFF.md, a detailed wish list. Read it. This file folds in its requirements, and where the two disagree, this file wins (section 3 explains each decision).
3. Rohan's frontend is in _incoming/handshake-frontend/: Next.js 16, React 19, TypeScript, Tailwind v4, shadcn/ui on Base UI. Read its README.md (it has an "Open questions for the backend" list that you must resolve), CLAUDE.md, and AGENTS.md. AGENTS.md warns that this Next.js version has breaking changes and that you must read the docs in node_modules/next/dist/docs/ before writing Next code. Obey that. lib/api.ts is the only file that talks to the backend, and lib/types.ts mirrors the Pydantic models with some fields marked PROPOSED. It currently runs on mock data by default.
4. Sri's mock merchant is in _incoming/HackGt13/: mock-amazon.html (a single-page fake store with product, cart, checkout, and confirmation views, plus a hidden dev panel with 8 scenarios), assets/ (images and logos), generate_fixtures.py, fixtures/*.json (static TransactionProposal-like payloads with hardcoded dates), and test_fixtures.py (posts fixtures to the backend and expects a decision field in uppercase, which does not match the real API).
5. A plain-English product overview (the Handshake v0.2 spec summary) may also be in _incoming/. Use it for README wording.

If the zips are not unpacked yet, the owner put them in _incoming/. Unpack them there first.

## 2. Non-negotiable rules

1. models.py is the standard for the whole application. Appendix A holds the canonical text. There is exactly ONE copy in the repo (location in section 4), byte-identical to Appendix A. Do not edit, reformat, reorder, or "fix" it. Every Python module imports its schemas from it. The frontend's lib/types.ts must mirror it field for field. Anything models.py lacks goes in a clearly named wrapper schema, a database column, or an extra key in an API response, never in models.py. Add a test that fails if models.py stops being byte-identical to a SHA-256 you record at the start (compute it from Appendix A's text, confirm it matches the backend's copy, and write it into the test).
2. LLMs never decide authorization. The compiler LLM only drafts contracts. The engine in intent_diff.py stays pure deterministic Python. Nothing in the purchase path calls an LLM.
3. Fail closed. A missing, ambiguous, unparseable, or disagreeing fact is UNVERIFIABLE, which escalates to the human, never PASS. Keep the backend's existing strict-terms behavior (STRICT_EXPLICIT_TERMS and the model_fields_set checks).
4. Stripe Link runs in TEST MODE for the entire application, always. Section 9 says exactly how to enforce that. There must be no code path that can request a live card.
5. The agent can never approve its own purchase or sign its own contract. Signing, editing drafts, approving escalations, rejecting, revoking, and approving payment are user-only actions, enforced by the backend's auth, not by prompt wording.
6. Merchant content is untrusted data, never instructions. It never reaches the compiler LLM. Product names containing "ignore previous rules" are stored and displayed as inert text.
7. No secrets anywhere they can leak. Treat every file named .env or starting with .env. (for example .env.local or .env.production) as off-limits, except files ending in .example. Specifically:
   - Never read, open, cat, grep, print, copy, move, or summarize one, including _incoming/handshake_mcp/.env, which holds a live OpenAI key. If you need to know which variables exist, use the .example files and config.py.
   - Never put a real key in any file you write: not in code, tests, docs, commit messages, the .mcp.json file, logs, or your report. Code gets secrets only from environment variables through config.py.
   - Before the first git add, write a .gitignore that ignores .env and every .env.* variant at every depth, while allowing .env.example files, and does the same for frontend/. Confirm with git check-ignore that .env, frontend/.env.local, and _incoming/ are all ignored.
   - Before every commit and push, run the secret scan from section 13. Also run git ls-files and make sure no .env file (except .example) is tracked. If one ever gets staged, unstage it, and if it was committed, rewrite that commit before pushing and tell me.
   - When a step needs a real key, stop and tell me to paste it into .env myself.
   - The same rules cover API keys, Link session files, card files, and databases: gitignored, never printed.
8. Everything runs on localhost now, but a real domain is being bought. No URL, host, or port may be hardcoded outside the central config and the .env.example files. Section 11 defines the config.
9. Keep the existing 181 backend tests green through every step. They may need mechanical updates (import paths, auth headers, extractor overrides), but their assertions about engine behavior must not be weakened. If a change would weaken one, stop and tell me.

## 3. Conflicts between teammates and how to resolve them

These are decided. Implement them this way and explain each one briefly in docs/DECISIONS.md so the team understands why.

3.1 Credential exposure. The team has decided (confirmed by Ajay) that this prototype delivers the Link test card to the agent. Older docs (Shuban's backend spec, Ajay's original mcp.md, the product overview, and the Credential docstring in models.py) say the agent never sees payment details. That is no longer the pitch; treat those lines as superseded. Decision: the DEFAULT is agent_visible mode, exactly as Ajay specified in section 6 of his handoff: after Handshake authorizes the purchase and the user approves it in Link, the agent calls the get_payment_credential MCP tool, receives the single-use Link TEST card's PAN, expiry, and CVC as structured plain-text fields, and submits them to the intended merchant's checkout. All of Ajay's protections stay: the dedicated sensitive response model, ownership and purchase binding at release time, one-time release, the HANDSHAKE_AGENT_INSTRUCTIONS text, and card data kept out of every other response, log, error, and evidence record. Executor mode, where Handshake's backend submits payment itself and the card never enters the agent's context, is ALSO built, selectable with HANDSHAKE_CREDENTIAL_MODE=executor, for hosts that refuse card handling and for a hardened future version. models.py stays untouched. Update mcp.md, the README, and docs/DECISIONS.md so both modes are described honestly, including the one real tradeoff: once a card enters a model's context, the MCP server cannot control what the host keeps in its transcript. That is acceptable here because every card is a Link test card.

3.2 Payment rail. Rohan's frontend assumes a Stripe PaymentIntent that holds the contract's full cap when the user signs and refunds the difference later (Contract.funding, a card form in payment-dialog.tsx, and the funds_refunded events). Ajay's code and the owner's instruction use Stripe Link virtual cards. Decision: Stripe Link in test mode is the only payment rail. Remove the held-funds concept from the frontend: drop the PROPOSED funding field, the refund events, and the placeholder card form at signing. Signing becomes a plain review-and-confirm step. Rohan's "Pending, waiting for your one-tap approval" state maps directly onto Link's own approval step. After Handshake authorizes a purchase, a Link test spend request is created and the user approves it in Link. Reuse his Funds component for Link status if it fits; otherwise delete it.

3.3 Where the compiler runs. Ajay's tools.py calls the OpenAI compiler inside the MCP process. Decision: the compiler moves into the backend (it holds the OpenAI key server-side, persists the draft, and runs the deterministic lint), and the MCP server becomes a thin authenticated HTTP client of the backend. That makes the MCP server plug-and-play: it needs only the backend URL and an agent token, never an OpenAI key.

3.4 Who produces the TransactionProposal. The backend currently accepts a proposal inside POST /purchases, and Sri's page builds the proposal in browser JavaScript. Both let the proposing side supply its own facts. Decision: the agent sends only contract_id, checkout_url, an idempotency key, and an optional selection report. The backend's extractor fetches the checkout from the merchant, normalizes Sri's format into a models.py TransactionProposal, and evaluates that. The agent's claims are never an input to the decision. The existing route's proposal field is removed from the public API. Tests switch to an injectable in-memory extractor (section 7.6).

3.5 Endpoint names. Rohan's api.ts assumed several routes the backend does not have. The backend is the source of truth for semantics, and the frontend's lib/api.ts adapts to it. The backend adds the capabilities that are genuinely missing (section 6). Where Rohan's name differs only cosmetically (resolve versus approve and reject, decline versus reject), change the frontend.

3.6 Escalation approval versus payment approval. The backend's POST /purchases/{id}/approve means "the human accepts an ESCALATED purchase's unverifiable facts." Rohan's approvePurchase meant "pay for an authorized match." They are different steps and both are kept. An escalated purchase that the human approves becomes AUTHORIZED, and then, like any authorized purchase, goes to Link approval. The UI must make clear these are two separate consents: accept the exception in Handshake, then approve the payment in Link.

## 4. Target repository layout

One repo and one Python package, flat, with no sub-packages. The owner wants few files, and a module only when it owns a real responsibility. Python code lives in a package named handshake at the repo root. The backend's app/ package moves into it. Use git mv so history follows the files, and update every import and the tests.

Python package handshake/ holds:
- models.py: the canonical file from Appendix A, moved from backend/app/models.py. It is the only copy.
- config.py: every setting in one place, loaded from environment variables with safe local defaults (section 11). Nothing else reads os.environ directly.
- intent_diff.py: the backend's engine, unchanged in behavior.
- db.py: the backend's persistence, plus new tables and columns (section 6.4).
- services.py: the backend's contract, purchase, evidence, and credential logic, extended for the extractor, idempotency, payment states, and ownership.
- api.py: the FastAPI app, which was backend/app/main.py, renamed. It owns routes, auth dependencies, CORS from config, and error shapes.
- auth.py: demo user login tokens, agent tokens, roles, and ownership checks (section 8). Fold it into api.py instead if it stays under about 150 lines.
- compiler.py: Ajay's llm.py rebuilt as described in section 5, plus deterministic lint.
- prompts.py: Ajay's COMPILER_PROMPT copied word for word, plus a new HANDSHAKE_AGENT_INSTRUCTIONS constant and the MCP workflow prompt text (section 10). Keep the two prompts clearly separate.
- extractor.py: independent checkout extraction, the normalizer from Sri's merchant format to TransactionProposal, SSRF protection, the two-reading agreement check, and the extractor interface (section 7).
- payments.py: Ajay's stripe.py turned into an async Link adapter plus a clearly labeled stub provider, and the executor that submits payment to the merchant (section 9). The rename also stops the file from shadowing the official stripe package.
- mcp_server.py: the MCP server (section 10), with a main() entry point.
- merchant.py: Sri's mock merchant as a small FastAPI app serving his page and a structured checkout API (section 7.2).

Other top-level folders:
- merchant_static/: Sri's mock-amazon.html and assets/, adjusted as described in section 7.2.
- frontend/: Rohan's Next.js app, moved from _incoming (without node_modules, .next, or env files).
- tests/: the backend's tests moved here, plus new test modules for the compiler, extractor, merchant, payments, MCP, auth, and the end-to-end scenarios. Aim for about 8 test files total.
- scripts/: dev.py (starts backend, merchant, and frontend together with prefixed logs and clean shutdown), demo.py (the scenario runner that replaces Sri's test_fixtures.py, section 12), and check_no_localhost.py (optional; may be a test instead).
- docs/: INTEGRATION_PLAN.md, DECISIONS.md, API.md (the single source of truth for every endpoint's request and response), ARCHITECTURE.md (state machines and the flow), DEPLOY.md (switching localhost to the real domain), DEMO.md (the judge demo script).

Root files: pyproject.toml (one Python project for everything, with a handshake-mcp console script pointing at mcp_server:main and the dependency list merged from the backend requirements and Ajay's imports; pin versions that are installed and verified), .env.example (placeholders only), frontend/.env.example, .gitignore, .mcp.json (section 10.4), README.md (section 15), LICENSE only if the team asks.

Delete backend/ once everything has moved and tests pass. Keep _incoming/ out of git with .gitignore, and do not delete it; it is the owner's copy of the originals.

Code style for every Python file you write or touch: Python 3.11 or newer, type hints everywhere, a docstring at the top of every file explaining its job and how it connects to the other files, a docstring on every function, and HEAVY inline # comments that explain the why. The owner is a student who reads this code to learn. Use plain, readable code over clever one-liners. Keep the backend's existing heavy comments intact. The frontend stays TypeScript because it is Rohan's code, but keep frontend changes as small as the integration allows.

## 5. The contract compiler (Ajay's part, finished)

5.1 Move llm.py into handshake/compiler.py. Do not create the OpenAI client at import time. Build it lazily on first use, so tests, tool discovery, and the server all start with no key. Read the model from HANDSHAKE_COMPILER_MODEL, defaulting to Ajay's gpt-5.6. Read the service tier from HANDSHAKE_COMPILER_SERVICE_TIER, left unset by default because flex can be slow or unsupported. Read the timeout from HANDSHAKE_COMPILER_TIMEOUT_SECONDS, defaulting to 60 (Ajay's 15 minutes would hang the demo). Verify once, with a real call if the key is present, that the model and parse API work. If the model name is rejected, report it and leave it configurable. Do not quietly swap providers.

5.2 Give the model trusted date context. The user message wrapper includes the current date, time, and the user's timezone (from HANDSHAKE_USER_TIMEZONE, default America/New_York), so "by Friday" resolves correctly. The compiler input is only the user intent, explicit preferences, configured defaults, and that context. Merchant content never goes in.

5.3 Handle every failure explicitly: missing key, provider error, refusal, absent parsed output, timeout, and Pydantic validation error. Each becomes a structured compile error returned to the caller. A failed compile never creates an active contract, and never creates a draft that looks complete.

5.4 Server-side overrides. After parsing, the server sets the draft's id and created_at itself and ignores whatever the LLM produced for them. It then validates through ContractDraft again.

5.5 Deterministic lint runs after parsing and before storing. It checks that: hard_cap_all_in is at least the target; the delivery deadline and expires_at are in the future; datetimes carry timezones (naive ones are treated as the user's timezone and noted); the currency is a 3-letter ISO code the system supports; each constraint's operator fits its value (lt with a list, in with a scalar that should be a list, before with a non-date, and similar mismatches); constraints do not contradict each other (eq 10 and eq 11 on the same field, a merchant both required and denied); and required fields exist. Lint problems are appended to clarifications_needed with a lint: prefix, so the review screen shows them before signing, and a draft with unresolved lint errors cannot be signed (the sign endpoint returns 409 draft_has_blocking_issues).

5.6 Offline fallback. When HANDSHAKE_COMPILER=fixture, or when no OpenAI key is set, use a deterministic demo compiler. It recognizes the demo intent (running shoes, Pegasus 41, size 10, around 120 dollars, under 135 all-in, delivered within 3 days, from Amazon.com, no subscriptions) and returns the exact demo contract from section 12.1. For any other intent it returns a draft-less result with a clarification saying the live compiler is not configured. Label fixture drafts clearly in compiler_notes. This keeps the demo alive if the API is down.

5.7 Tests with a mocked OpenAI client: success, refusal, missing parsed output, timeout, validation error, each lint rule, the server overriding id and created_at, persistence, and the fixture compiler.

## 6. Backend API: the final contract

Write docs/API.md first, then implement it. Every route requires auth (section 8) except GET /health and POST /auth/demo-login. Keep the backend's existing error shape everywhere: an error code, a human message, and a details object. Keep every existing route working; extend its responses as supersets rather than changing their shape, so existing tests keep their assertions.

6.1 Existing routes, kept: POST /contracts (create a draft from a ContractDraft or CompilerOutput body; user role, or agent role through compile), GET /contracts, GET /contracts/{id}, POST /contracts/{id}/sign, POST /contracts/{id}/revoke, POST /purchases, GET /purchases/{id}, POST /purchases/{id}/complete (now internal and executor-driven; user and agent tokens get 403, and it stays callable from tests through the service layer), POST /purchases/{id}/approve, POST /purchases/{id}/reject, GET /evidence/{purchase_id}, and GET /health (add payment_mode, credential_mode, and compiler mode to its response).

6.2 Changed: POST /purchases. The body is contract_id, checkout_url, idempotency_key (required for the agent role), and an optional selection_report (a models.py SelectionReport). No proposal field. The flow: auth and ownership, then the idempotency lookup (the same key and contract returns the existing purchase instead of creating another), the existing contract checks (tampered, revoked, used, expired, all still recorded as BLOCKED purchases with evidence), then the extractor fetches and normalizes the checkout, then the existing parse and evaluate path, then the existing outcome branches. On AUTHORIZED it no longer issues the stub credential by itself. It starts the payment flow of section 9 instead. The response adds payment_state, approval_url (when there is one), next_action (a short machine-readable hint such as wait_for_user_link_approval, wait_for_user_decision, blocked_no_action, completed), and review_url (the frontend purchase page).

6.3 Added routes:
- POST /auth/demo-login: body has an email. Returns a signed user token (section 8). Demo only, and labeled so.
- POST /drafts/compile: body has intent. Compiles, lints, persists, and returns the flattened draft record (below) plus review_url. Agent and user roles.
- GET /drafts and GET /drafts/{id}: the draft as Rohan's DraftRecord. The ContractDraft fields are flattened together with status draft, assumptions, clarifications_needed, compiler_notes, and previous_contract_id, which is stored in a DB column because ContractDraft lacks it.
- PATCH /drafts/{id}: Rohan's DraftPatch (goal, target, hard_cap_all_in, max_shipping, deliver_by, constraints). Any edited value's source becomes user. Re-validate through ContractDraft and re-run lint. User only.
- POST /contracts/{id}/amend: creates a new draft copied from a signed contract, with previous_contract_id pointing at it. Signing that draft produces a Contract whose previous_contract_id is set, and atomically revokes the old active version with an evidence event. User only.
- GET /purchases (owner-scoped list, newest first) and GET /contracts/{id}/purchases.
- POST /purchases/{id}/payment/refresh: polls the payment provider and advances the payment state (section 9). User or agent. Idempotent.
- POST /purchases/{id}/payment/simulate-approval: exists only when HANDSHAKE_PAYMENT_MODE=stub, user only. Stands in for the Link approval tap, and is labeled "Simulated provider approval" everywhere it appears.
- POST /purchases/{id}/credential: exists only when HANDSHAKE_CREDENTIAL_MODE=agent_visible, agent only, one-time (section 9.6).

6.4 Response shapes for Rohan. GET /purchases/{id} returns a superset: the existing top-level purchase_id, status, decision, and credential summary, plus purchase (the full models.py Purchase), proposal (the full TransactionProposal), payment (state, provider, approval_url, provider_reference with no secrets, last4 once paid, and order_id and receipt once completed), and resolution (whether a human approved or rejected, when, and which constraints they accepted, taken from the evidence the backend already records). Each ConstraintResult in API responses also gets two extra keys, field and label, so the UI never parses the constraint string. The ConstraintResult model itself is unchanged: the keys are added when serializing. The field comes from the rule or the constraint, and the label is a short human name such as "Total price" or "Shoe size". Rohan's open question about TermsPolicy and MerchantPolicy having no source stays answered as "Default" in the UI. Note it in DECISIONS.md.

6.5 Evidence. models.py's EvidenceEventType has no values for payment approval, Link steps, or escalation decisions, so use the closest existing type and put a precise subtype in data.kind: payment_requested, payment_approval_pending, payment_approved, payment_denied, payment_expired, checkout_revalidated, checkout_changed, credential_released, payment_submitted, receipt_verified, escalation_approved, escalation_rejected, contract_amended. The frontend timeline labels events by data.kind when it exists. Evidence data must never contain a PAN, CVC, full expiry, Link session token, or raw card file path.

6.6 Database additions (SQLAlchemy, created on startup as now): an owner column on drafts, contracts, and purchases; previous_contract_id on drafts; an idempotency_key column on purchases (unique per contract and key); a checkout_snapshot_hash on proposals; and a payments table with purchase_id, provider (stub or link_test), provider_request_id (persisted immediately after creation, before any polling), state, approval_url, amount_minor, currency, created_at, updated_at, last_error (sanitized), card_last4, order_id, and receipt JSON. Never store a PAN, CVC, or full expiry anywhere.

## 7. Independent checkout extraction and the mock merchant (Sri's part, integrated)

7.1 The problem to solve. Sri's scenario payloads use a format that differs from models.py. recurring_billing is a bare boolean instead of an object. size sits directly on the line item instead of in attributes. There is no contract_id, no item_subtotal, no membership_detected or addons_detected, and no seller verification flags. There are extra fields (fulfilled_by_amazon, payment_method). Dates are date-only strings. Against the backend's strict fail-closed rules, even the "valid" scenario would ESCALATE today, because its terms flags and seller verification are missing. The merchant must publish explicit facts, and the extractor must translate them faithfully without inventing any.

7.2 merchant.py, the mock merchant app, served on the merchant port (default 3001). Move Sri's scenario definitions (buildScenarioData in the HTML, and generate_fixtures.py) into Python in merchant.py as the single source of truth for all 8 scenarios: valid, price_bump, hidden_subscription, product_swap, unknown_seller, late_delivery, vague_delivery, prompt_injection. Keep his numbers exactly: Pegasus 41, 119.99, tax 8.40, surprise 15.00 fee, 14.99 Shoe Club Membership, Pegasus 40 in size 11, seller SneakerDeals123, delivery in 2 days (valid) versus 14 days (late) versus null (vague), and the 491.60 injected-name item totaling 500.00. Extend each scenario with explicit facts: seller identity (Nike as a brand-authorized, verified seller, so is_verified true and is_first_party false; SneakerDeals123 as unknown, so is_verified null and is_first_party false), a membership flag (true only for the Shoe Club scenario), an addons list (empty unless a scenario adds one), and a return policy only if Sri's page shows one (do not invent it). Routes:
- GET / serves the product page.
- POST /api/checkout-sessions with scenario, size, and quantity creates a session and returns its id and checkout_url.
- GET /checkout/{session_id} serves Sri's page with that session's cart preloaded and the checkout facts embedded as a JSON script tag with a stable id.
- GET /api/checkout/{session_id} returns the same facts as JSON (the structured feed).
- POST /api/checkout/{session_id}/pay takes the card fields and the amount. It accepts only Luhn-valid test cards with a future expiry, rejects any amount that differs from the checkout total, stores only last4, is idempotent per session (a second pay returns the same order and never double-charges), and returns order_id, amount_charged, currency, and last4.
- GET /api/orders/{order_id} lets the executor verify the order independently.
Sessions live in memory with a TTL, which is enough for the demo. A dev-only route can change a session's scenario after creation, so the demo can show a cart changing after authorization (section 9.4).

7.3 Sri's HTML. Keep his design and dev panel. Make the page load its scenario facts from the server instead of building them in JavaScript, so the page and the API can never disagree. Remove the hardcoded http://localhost:8000 and the commented-out fetch. The dev panel's scenario picker creates a new checkout session and shows its checkout_url, plus a link to the Handshake frontend when the frontend URL is configured. The Place your order button stays a visual demo only. Real payment happens through /pay, called by Handshake's executor. Keep his security-boundary comment, and keep rendering product names as text, never as HTML. Keep the repo private, because the page mimics a real store's branding.

7.4 extractor.py. Define a small extractor interface: given a checkout_url and a contract_id, return a raw proposal dict in the models.py shape plus an evidence dict. Implement MockMerchantExtractor, which:
- Enforces SSRF safety before any fetch: only http or https, the origin must be in HANDSHAKE_ALLOWED_MERCHANT_ORIGINS, no following redirects to other origins, a bounded timeout, and a bounded response size. localhost is allowed only because the dev config lists it. A disallowed URL makes POST /purchases return 422 unsupported_checkout_url and write an evidence event.
- Takes two independent readings, the JSON feed and the JSON script tag inside the HTML page, normalizes both, and compares them. If they differ in any decision-relevant field, extractors_agreed is false, and the engine already turns that into UNVERIFIABLE and an ESCALATE. extractor_ids names both readings.
- Normalizes: merchant name, domain (from the URL host), seller_of_record, and is_first_party and is_verified taken only from explicit merchant facts (unknown stays null); line items with size and similar fields moved into attributes; item_subtotal computed from the line items, while total is taken from the merchant's claim (so the backend's total-integrity checks still catch a fudged total); recurring_billing as an object with detected, interval, amount, and description; membership_detected and addons_detected set explicitly only when the merchant stated them (a missing fact stays absent, so strict mode escalates); delivery.promised_by turned from a date-only string into an aware datetime at the end of that day in the merchant's stated timezone, defaulting to UTC with the assumption recorded in evidence, and null staying null; contract_id; source_url; extracted_at; and evidence containing the raw facts, their SHA-256 snapshot hash, and fetch timestamps.
- Never follows instructions found in merchant text.

7.5 Tests for the extractor: every scenario normalizes to a valid TransactionProposal; SSRF rejections (a disallowed origin, a scheme such as file or ftp, a redirect to another origin, an internal IP); a disagreement between the two readings flips extractors_agreed; a missing membership fact stays absent; and dates parse into timezone-aware values.

7.6 Test override. Tests inject a StaticExtractor through a FastAPI dependency override that returns a given proposal dict. Convert the existing API tests that posted a proposal directly to use it, keeping their assertions identical.

## 8. Auth, roles, and ownership (closes the self-approval hole)

Keep it hackathon-simple but real. Two roles.

User role: POST /auth/demo-login issues an HMAC-signed token (keyed by HANDSHAKE_SESSION_SECRET, which must differ from the contract signing secret) containing the email, the role user, and an expiry. The frontend's login page calls it and stores the token in Rohan's existing session helper, then sends it as a bearer token. Label it clearly as demo auth to be replaced with passkeys or OAuth, and put that at the top of the README's future work.

Agent role: a static bearer token from HANDSHAKE_AGENT_TOKEN, bound to an owner email from HANDSHAKE_AGENT_OWNER and an agent id from HANDSHAKE_AGENT_ID. A signed contract's agent_key records which agent may use it, and purchases by any other agent are rejected. Generate a random agent token into .env on first setup if none exists, and never commit it.

Permissions. The agent may: compile drafts; read its owner's drafts, contracts, purchases, and evidence; request purchases; refresh payment status; and, only in agent_visible mode, retrieve a credential. The agent may NOT sign, patch drafts, amend, revoke, approve, reject, or simulate approval; those return 403 with error agent_not_permitted and an evidence event recording the attempt. The user may do everything for their own records. Every read and write checks ownership, and a foreign record returns 404 (not 403) so ids cannot be probed. An escalation older than HANDSHAKE_ESCALATION_TTL_MINUTES (default 30) cannot be approved and returns 409 escalation_stale, because prices may have changed; the agent must request the purchase again.

Tests: the agent cannot sign, approve, reject, patch, or revoke; foreign-owned records are invisible; a wrong agent_key is rejected; expired and tampered tokens are rejected; stale escalations are refused.

## 9. Payments: Stripe Link in test mode, and the checkout executor

9.1 Test mode enforcement. HANDSHAKE_PAYMENT_MODE accepts exactly two values: stub (the default, fully offline) and link_test. Any other value, including live or an empty string when set, makes the app refuse to start with a clear error. The Link adapter builds every spend-request command through one function that always appends Link's test flag, and there is no parameter anywhere that removes it. Add a test that inspects every command the adapter can build and asserts the test flag is present. /health, the startup log, the frontend header, and the MCP status responses all show "Stripe Link: TEST MODE" or "Simulated provider". The README states that no real card is ever charged by this codebase.

9.2 Before writing the adapter, inspect the real CLI. Check the installed version (link-cli, or npx @stripe/link-cli), read its --help for auth, payment-methods, and spend-request, and read the official README at https://github.com/stripe/link-cli . Record the verified command shapes and a sanitized sample of each JSON response in docs/DECISIONS.md. Pin the CLI version you verified (use npx with an explicit version if it is not installed globally). Do not guess flags. Ajay's stripe.py already found that auth status can come back as either a single object or a list of streamed updates. Keep that parser and his offline tests.

9.3 The adapter in payments.py. Use asyncio subprocesses (never blocking the server), pass arguments as a list (never through a shell), set a timeout on every call, parse output defensively, sanitize stderr before logging or storing it, and never log stdout from a retrieve. Operations: auth status; create a spend request for a purchase (amount in minor units computed with Decimal from the backend's already-validated authorized amount, the currency, the merchant name and URL from the proposal, and a truthful context string of at least 100 characters built from the contract goal, merchant, item, and total; always test mode, always with approval requested); get status (pending, approved, denied, expired, or failed, mapped from whatever the CLI actually returns); and retrieve the card into a private temporary file (a private directory, the file created exclusively with mode 0600, refusing symlinks and overwrites, read once into memory, then overwritten and deleted in a finally block). Persist provider_request_id to the payments table immediately after creation. If creation times out, reconcile before retrying so no duplicate request is ever created, and never create a second spend request for the same purchase. Treat Ajay's 50000 minor-unit cap as a configurable limit you verified, not an assumed provider guarantee. Do not claim merchant locking, exact spend limits, or expiry that the provider does not actually confirm; record the capabilities you actually verified.

9.4 The payment state machine, stored in the payments table (PurchaseStatus in models.py stays as-is and remains authorized until the purchase is completed). States run in this order: awaiting_approval, then approved, then revalidating, then credential_ready (agent_visible mode only), then paying, then paid, then completed. Side exits: denied, expired, checkout_changed, failed, and unknown. Flow after AUTHORIZED: create the spend request and store approval_url (in stub mode, approval_url points to the frontend purchase page, where the user clicks Simulated provider approval). Refresh, which the frontend and MCP poll and which also runs on GET, reads provider status. When approved: re-check that the contract is still active and verifies, re-extract the checkout, and compare its snapshot hash to the authorized one. If anything changed, run the engine again. If the new outcome is not AUTHORIZED, or the total rose at all, stop at checkout_changed with evidence, and do not pay. Otherwise, in agent_visible mode (the default), move to credential_ready: the agent may now call get_payment_credential once, and pays the merchant itself. The payment moves to paying when the card is released. Refresh then asks the merchant, through a merchant route GET /api/checkout/{session_id}/order (add it to section 7.2), whether that session has an order, and moves to paid once an order exists. In executor mode, the backend instead retrieves the card itself and submits it to the merchant's /pay with the exact authorized amount. In both modes, the backend then verifies the order independently through /api/orders. Reconcile through the backend's existing complete logic (an overcharge by even one cent fails it and revokes the credential), mark the payment completed, store order_id, last4, and a receipt, and mark the single-use contract used through the existing atomic update. If the pay call's outcome is uncertain (a timeout or connection error), set unknown and never retry the payment blindly. Refresh must query the merchant's order endpoint and resolve the state before anything else happens.

9.5 Stub provider. It mimics the same interface and state machine, offline and deterministic, and hands the executor a well-known test card number that the mock merchant accepts. Everything it produces is labeled simulated.

9.6 Agent-visible mode (the DEFAULT). Implement Ajay's handoff section 6 faithfully. POST /purchases/{id}/credential and the MCP tool get_payment_credential exist in this mode, and are absent in executor mode. Release happens once, only for the owning agent, only when the payment state is credential_ready, only after the recheck and revalidation, and only for the exact purchase. A second call returns 409 credential_already_released and never re-sends card values. In link_test mode, retrieve the card from Link into the private temp file at release time, read it, delete it, and return it. In stub mode, return the stub provider's well-known test card, labeled simulated. The response also tells the agent exactly where to pay: the checkout's pay endpoint and the exact amount and currency to submit. The response uses a dedicated sensitive response model defined outside models.py, holding the card fields plus purchase_id, merchant, amount, currency, and expiry, and it is never cached. The agent then reports completion through the status and refresh flow, and the backend verifies the merchant order before marking anything completed. Card values never appear in any other response, error, log, or evidence record.

9.7 Tests: the test flag is always present; a live mode value refuses startup; approval pending, denied, and expired paths; malformed CLI output; a creation timeout with no duplicate request; retrieval failure; no retrieval before approval; a cart changed after authorization triggers revalidation and blocks when worse; an uncertain pay outcome is never retried blindly; a successful stub run ends completed with a verified receipt and the contract used; and a sweep over every DB row, every API response except the one-time credential release, and captured logs finds no PAN, CVC, or 13 to 19 digit card-like string (reuse and extend the backend's existing scanner test).

## 10. The Handshake MCP server: plug and play

10.1 mcp_server.py uses the official MCP Python SDK (Ajay used mcp.server.fastmcp; keep that, and pin the version installed in his venv or newer and verified). It is a thin, async, authenticated HTTP client of the backend, using httpx with bounded timeouts, a correlation id header on every request, and retries only on idempotent reads. It needs only HANDSHAKE_API_URL and HANDSHAKE_AGENT_TOKEN (plus optional HANDSHAKE_FRONTEND_URL for review links). It never needs an OpenAI key, a Link session, or database access. It never prints to stdout except MCP protocol traffic; logs go to stderr. Remove the duplicate create_contract_draft registration.

10.2 Tools, each with a precise description the agent can act on and a JSON-serializable typed result:
- create_contract_draft(intent) returns draft_id, a plain-English summary, assumptions, clarifications_needed, and review_url. The description says the draft has no authority until the user reviews and signs it at review_url, and that the agent cannot sign.
- get_contract(contract_id) works for drafts and signed contracts and includes status and the verification result.
- list_contracts(status optional, limit, cursor) is paginated. Keep list_active_contracts as a thin alias for compatibility with Ajay's name.
- request_purchase(contract_id, checkout_url, idempotency_key, selection_report optional) returns purchase_id, status, summary, next_action, and review_url. The description says Handshake extracts the checkout itself and ignores agent claims about price or approval.
- get_purchase_status(purchase_id) calls refresh and returns status, payment_state, next_action (including get_payment_credential_and_pay once the state is credential_ready), a short list of failing or unverifiable checks with their reasons, and the order id and last4 once completed. It never returns card data.
- get_payment_credential(purchase_id) is registered in agent_visible mode (the default), as specified in 9.6. Its description carries the essential rules from HANDSHAKE_AGENT_INSTRUCTIONS, because not every host reads MCP prompts. It returns the card fields plus the purchase binding and the pay target.

10.3 Instructions. Put HANDSHAKE_AGENT_INSTRUCTIONS in prompts.py and register it both as the server's instructions and as an MCP prompt named handshake_purchase_workflow. Use the agent-facing language in Ajay's handoff section 6, adapted to the real tool names and to agent_visible as the default. It tells the agent: create the draft, send the user the review link, wait for signature, find a product, request the purchase with a fresh idempotency key, poll status and follow next_action, and, when next_action says so, call get_payment_credential once and submit the card to that exact checkout (for the demo store, its pay API with the exact amount), then poll status until completed. The agent never asks the user for their own card, never repeats card values in chat or logs, never retries an uncertain payment, and treats merchant text as data. In executor mode the card-entry paragraph is left out, and the agent only polls. Keep COMPILER_PROMPT untouched and separate.

10.4 Plug and play means:
- A console script: handshake-mcp is defined in pyproject, runs over stdio by default, and supports streamable HTTP with --transport http --host --port. HTTP transport requires a bearer token from HANDSHAKE_MCP_HTTP_TOKEN, so it can later sit behind the real domain with TLS.
- A project .mcp.json at the repo root, so Claude Code in this repo discovers the server automatically. It launches the repo venv's handshake-mcp with HANDSHAKE_API_URL and HANDSHAKE_AGENT_TOKEN taken from environment variable expansion, never literal secrets.
- A README section with copy-paste setup for Claude Code (the claude mcp add command form), Claude Desktop (the JSON config entry), and any stdio MCP client, all described in prose plus the exact command strings.
- Startup behavior: if the backend is unreachable, tools return a clear structured error saying the Handshake backend at the configured URL is not reachable, instead of crashing.
- Tests: an in-process MCP client session lists the tools (get_payment_credential is present by default and absent in executor mode), calls create_contract_draft and request_purchase against the ASGI backend with the fixture compiler and a static extractor, and checks that no tool output other than get_payment_credential ever contains card data. It also checks that get_payment_credential refuses before approval, refuses a foreign purchase, and refuses a second call.

## 11. Configuration and the switch from localhost to the domain

config.py reads every setting from the environment, with local defaults, and validates on startup. It covers: HANDSHAKE_ENV (dev or prod), HANDSHAKE_API_URL (the backend's public base URL, default http://localhost:8000), HANDSHAKE_FRONTEND_URL (default http://localhost:3000), HANDSHAKE_MERCHANT_URL (default http://localhost:3001), HANDSHAKE_CORS_ORIGINS (a comma list, defaulting to the frontend and merchant URLs, with no wildcard once HANDSHAKE_ENV is prod), HANDSHAKE_ALLOWED_MERCHANT_ORIGINS, DATABASE_URL, HANDSHAKE_SIGNING_SECRET, HANDSHAKE_SESSION_SECRET, HANDSHAKE_AGENT_TOKEN, HANDSHAKE_AGENT_OWNER, HANDSHAKE_AGENT_ID, HANDSHAKE_MCP_HTTP_TOKEN, HANDSHAKE_PAYMENT_MODE, HANDSHAKE_CREDENTIAL_MODE (agent_visible by default, or executor), HANDSHAKE_LINK_CLI (the command or npx spec with its pinned version), HANDSHAKE_LINK_MAX_MINOR_UNITS, HANDSHAKE_COMPILER, HANDSHAKE_COMPILER_MODEL, HANDSHAKE_COMPILER_SERVICE_TIER, HANDSHAKE_COMPILER_TIMEOUT_SECONDS, OPENAI_API_KEY, HANDSHAKE_USER_TIMEZONE, HANDSHAKE_ESCALATION_TTL_MINUTES, and the ports for dev.py. In prod, the default secrets and a wildcard CORS setting refuse to start.

Frontend: NEXT_PUBLIC_API_URL and NEXT_PUBLIC_USE_MOCKS go in frontend/.env.example. The integrated default is USE_MOCKS=false in frontend/.env.local, which dev.py writes if it is missing. Keep Rohan's mock mode working for UI-only work.

Merchant page: it gets its backend and frontend links from the merchant server, which reads config, never from literals.

Add a test that scans the source tree (the Python package, merchant_static, frontend source, and scripts) for localhost, 127.0.0.1, or hardcoded ports outside config.py, the .env.example files, docs, and tests, and fails if it finds any.

docs/DEPLOY.md is the checklist for going live on the domain: which variables to change, CORS, HTTPS, next build and start, running the MCP server over HTTP behind TLS with its token, rotating every secret, moving to Postgres (DATABASE_URL), the fact that payments stay in Link test mode unless the team makes a separate deliberate decision, and a note that demo auth must be replaced before real users.

## 12. The demo contract and end-to-end scenarios

12.1 The demo contract, produced identically by the fixture compiler and by scripts/demo.py. Goal: buy Nike Pegasus 41 running shoes. Category running_shoes. Spend: USD, target 120, hard_cap_all_in 135 (hard cap source user, target source user). Delivery: deliver_by 3 days from now at the end of the day in the user's timezone, max_shipping 10. Terms: no subscription, no membership, no add-ons, and no min_return_days, because the mock merchant publishes no return policy and a required return window would escalate every scenario. Merchants: allow Amazon.com, seller requirement first_party_or_verified, new merchant escalate. Constraints, all hard and user-sourced: size eq "10", condition in ["new"], product_name contains "Pegasus 41", quantity eq 1. Single use, revocable, agent_key set to the configured agent id.

12.2 Expected outcomes against that contract, using Sri's scenarios through the real extractor path: valid is AUTHORIZED and, in stub mode, goes all the way to completed with a verified receipt (the credential released once to the agent, which then pays); price_bump is BLOCKED (143.39 exceeds the 135 cap); hidden_subscription is BLOCKED (subscription, membership, and a wrong-category line item); product_swap is BLOCKED (Pegasus 40 fails product_name, size 11 fails size); unknown_seller is ESCALATED (seller verification unknown); late_delivery is BLOCKED; vague_delivery is ESCALATED; prompt_injection is BLOCKED (500.00 exceeds the cap, and the injected text is stored inertly). Verify that each outcome happens for the stated reason, not just that the status matches. If the engine produces a different outcome for a legitimate reason, report it and do not bend the engine to fit.

12.3 scripts/demo.py replaces Sri's test_fixtures.py. It is Python, uses httpx, and reads URLs from config. It logs in as the user, compiles or seeds the demo contract, signs it (each scenario gets a fresh contract, because contracts are single use), creates a merchant checkout session per scenario, requests the purchase as the agent with an idempotency key, and for valid drives the stub approval, then acts as the agent (retrieves the credential once and pays the merchant's pay API with the exact amount), then polls until completed. With a flag it runs in executor mode instead. It prints a table: scenario, expected, actual, the main reason, and the purchase page URL. It exits nonzero on any mismatch. It supports running one scenario by name, so the live demo can use it.

12.4 A pytest end-to-end module runs the same 8 scenarios fully in-process, with the backend and merchant as ASGI apps wired together through httpx transports, the fixture compiler, and the stub provider. It also covers: an escalated purchase approved by the user, then stub-approved, then the credential released and paid by the test agent, then completed; the same valid flow in executor mode; the same escalation rejected; the agent trying to approve and getting 403; the cart changing after authorization and being blocked at revalidation; and idempotent request_purchase returning the same purchase.

12.5 Also test with real services once: start everything with scripts/dev.py, run scripts/demo.py against it, click through the frontend for the valid, blocked, and escalated cases, and connect the MCP server to a real MCP client (the claude mcp list and tool call from this Claude Code session is fine). If Link credentials are available on this machine, run one link_test purchase for the valid scenario and record the sanitized result in DEMO.md. If they are not, say so and leave the exact steps. Never make a live charge.

## 13. Git and GitHub

1. If the repo root is not a git repo yet, initialize it on branch main. If it already is one, work on a new branch named integration.
2. Write .gitignore before the first add. Cover: venvs (venv, .venv), node_modules, .next, __pycache__, *.pyc, every .env and .env.local but not the .env.example files, *.db, *.sqlite, the Link temp directory, .DS_Store, and _incoming/.
3. Commit history that shows each teammate's work: one commit importing Shuban's backend as it was, one importing Ajay's MCP and compiler files as delivered (source files only, no venv and no .env), one importing Rohan's frontend as delivered (no node_modules), and one importing Sri's merchant files as delivered. Each commit message names the teammate and says "imported as delivered." After that come the integration commits, one per milestone, each with a clear message.
4. Secret scan before every commit and push (it scans staged file contents, never .env files themselves, and prints only the file path and line number of a hit, never the matched value): grep the staged files for OpenAI-style keys (sk- prefixes), bearer tokens, Link session files, 13 to 19 digit card-like numbers outside test fixtures that use documented test cards, and private keys. Abort on any hit and tell me. Ajay's .env holds a live OpenAI key, and it must never be staged.
5. At the end, if the gh CLI is installed and authenticated, create a PRIVATE GitHub repo named handshake under the account it is logged into, then push main (or the integration branch plus a pull request if main already existed). If gh is not available or not authenticated, stop at that step and give me the exact commands to run. Never make the repo public.

## 14. Milestones (run the full test suite after each; do not continue while red)

1. Inventory. Read everything in section 1, compute models.py's SHA-256, diff Ajay's copy against the backend's (expect only a leading blank line), and write docs/INTEGRATION_PLAN.md with any new conflicts you found beyond section 3. If a conflict you find would change the architecture, stop and ask me. Otherwise proceed.
2. Repo and history. Set up .gitignore and the teammate import commits (section 13, steps 1 to 4).
3. Restructure. Move the backend into the handshake package, create pyproject.toml and the root venv, add config.py, and make all 181 existing tests pass with only mechanical changes. Add the models.py integrity test.
4. Auth and ownership (section 8). Update the existing tests to send tokens.
5. Compiler (section 5) and the draft routes (section 6.3).
6. Mock merchant and extractor (section 7). Convert the proposal-posting tests to the static extractor.
7. Payments (section 9) in stub mode, then link_test after inspecting the real CLI.
8. MCP server (section 10), including .mcp.json and a real client check.
9. Frontend integration (section 16).
10. Scenarios (section 12): the pytest end-to-end module, scripts/demo.py, and the manual run with dev.py.
11. Docs, the localhost scan test, and DEPLOY.md. Then a final full run: pytest, npm run lint, npm run build, and demo.py against live services.
12. Secret scan, then push to the private GitHub repo.

## 15. README.md (root)

Written for judges and for the team. Include: what Handshake is (three sentences, drawing on the product overview); a diagram described in prose (user, frontend, backend with engine and ledger, extractor, merchant, Link test mode, MCP agent); the rule that the agent proposes and Handshake decides; quickstart (prerequisites, one venv, pip install, npm install in frontend, copy the env examples, python scripts/dev.py, python scripts/demo.py); how to plug the MCP server into Claude Code and Claude Desktop; the demo walkthrough; what is real and what is mocked (be exact: demo auth, the mock merchant, the structured feed instead of real-site scraping, the stub versus Link test mode, that in the default mode the agent receives a single-use Link TEST card, and that no real money moves); test commands; team credits (Shuban: backend, deterministic engine, evidence ledger; Ajay: MCP server, contract compiler, Link wrapper; Rohan: frontend; Sri: mock merchant and red-team scenarios); and future work (passkey signing, real merchant extraction, a production ledger, standing budgets, returns). Keep each teammate's original docs under docs/original/ for reference.

## 16. Frontend integration (Rohan's part, finished)

Read frontend/AGENTS.md and the relevant Next docs in node_modules first. Keep lib/api.ts as the only file that talks to the backend.
- api.ts sends the bearer token from the session on every call; on a 401 it clears the session and sends the user to /login.
- Map the calls to the section 6 routes: listContracts uses GET /drafts plus GET /contracts (adapt to the backend's list item shape); getContract uses /drafts/{id} for draft ids; signContract, revokeContract, amendContract, and updateDraft map to their routes; getPurchase maps to the superset shape of GET /purchases/{id}, turned into Rohan's PurchaseDetail; getEvidence takes the events list out of GET /evidence/{id}; resolveEscalation calls /approve or /reject; declinePurchase calls /reject (extend the backend so /reject also works on an authorized purchase whose payment has not started, and cancels the pending provider request); approvePurchase opens the Link approval_url in link_test mode, or calls simulate-approval in stub mode; and a new refreshPayment polls /payment/refresh.
- types.ts mirrors models.py exactly. Remove Funding and its PROPOSED events. Add the payment object, next_action, the resolution shape, and the field and label keys on results. Mark anything that is not in models.py as API-only in comments.
- Login page: calls /auth/demo-login and stores the token. Contracts page: add a small "Describe what to buy" box calling /drafts/compile if none exists, so the demo works without an MCP agent. Signing: replace the payment dialog with a clear review-and-confirm dialog that restates the cap, deadline, and merchant rules. Purchase page: show the payment state progression (Awaiting your Link approval, Rechecking checkout, Card released to agent, Paying, Completed, with order id and last4), the Approve in Link button with the approval_url, the Simulated provider approval button only in stub mode, and the two-consent explanation for escalations. Keep Replay and the progression animation working from real data. Show "Stripe Link: TEST MODE" or "Simulated provider" in the header. Product names always render as text.
- npm run lint and npm run build must pass. Keep Rohan's mock mode working.

## 17. What to report back when done

Report: the final file tree (top two levels); each milestone's result; the pytest summary; the npm lint and build results; the scripts/demo.py table from the live run; whether link_test was exercised for real (and if not, exactly what is missing); the MCP setup commands you verified; the GitHub repo URL (or the exact commands for me to run); every decision you made that is not in this file; every place a teammate's original behavior changed and why, in plain language I can forward to them; and remaining risks. Be exact about what is real, mocked, tested, or blocked. Do not claim something works unless you ran it.

## Appendix A: canonical models.py (byte-for-byte; the only copy lives at handshake/models.py)

The file content is everything between the two marker tags below. The tags themselves are not part of the file. It must be identical to backend/app/models.py as it exists now. Verify that before moving it.

<models_py>
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, get_args
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


# ============================================================
# Helpers
# ============================================================


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# Enums
# ============================================================


class ConstraintOperator(str, Enum):
    EQ = "eq"
    NEQ = "neq"

    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"

    IN = "in"
    NOT_IN = "not_in"

    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"

    BEFORE = "before"
    AFTER = "after"


class ConstraintSeverity(str, Enum):
    HARD = "hard"
    SOFT = "soft"
    ESCALATING = "escalating"


class ValueSource(str, Enum):
    USER = "user"
    INFERRED = "inferred"
    DEFAULT = "default"


class ContractStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    USED = "used"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ConstraintVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNVERIFIABLE = "unverifiable"


class PurchaseStatus(str, Enum):
    PENDING = "pending"
    VALIDATING = "validating"
    AUTHORIZED = "authorized"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    COMPLETED = "completed"
    FAILED = "failed"


class CredentialStatus(str, Enum):
    CREATED = "created"
    ACTIVE = "active"
    USED = "used"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ProductCondition(str, Enum):
    NEW = "new"
    USED = "used"
    REFURBISHED = "refurbished"
    OPEN_BOX = "open_box"


class SellerRequirement(str, Enum):
    ANY = "any"
    FIRST_PARTY = "first_party"
    VERIFIED = "verified"
    FIRST_PARTY_OR_VERIFIED = "first_party_or_verified"


class MerchantPolicyAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


# ============================================================
# Generic Constraint System
# ============================================================


ConstraintField = Literal[
    # Product identity
    "category",
    "brand",
    "model",
    "product_name",
    "condition",
    "color",
    "size",
    "quantity",

    # Electronics
    "screen_size_inches",
    "display_type",
    "refresh_rate_hz",
    "storage_gb",
    "memory_gb",

    # Clothing
    "material",
    "gender",
    "fit",

    # Food
    "food_size",
    "toppings",
    "dietary",

    # Tickets
    "event_name",
    "ticket_quantity",
    "seats_together",
    "section",

    # Merchant
    "merchant_name",
    "seller_of_record",

    # Terms
    "return_days",
    "subscription",
    "membership",
    "addons",

    # Delivery
    "delivery_date",

    # Pricing
    "item_price",
    "shipping",
    "fees",
    "tax",
    "total_price",
]

SUPPORTED_FIELDS = set(get_args(ConstraintField))


# Explicit types keep constraint values compatible with Structured Outputs.
ConstraintScalar = str | int | float | bool
ConstraintValue = ConstraintScalar | list[ConstraintScalar] | None


class Constraint(BaseModel):
    """
    Generic category-specific constraint.

    Example:
    {
        "field": "refresh_rate_hz",
        "operator": "gte",
        "value": 120,
        "severity": "hard",
        "source": "user"
    }
    """

    field: ConstraintField
    operator: ConstraintOperator
    value: ConstraintValue

    severity: ConstraintSeverity = ConstraintSeverity.HARD
    source: ValueSource = ValueSource.USER

    description: str | None = None

# ============================================================
# Spend
# ============================================================


class SpendPolicy(BaseModel):
    currency: str = Field(
        default="USD",
        min_length=3,
        max_length=3,
    )

    target: float | None = Field(
        default=None,
        ge=0,
    )

    hard_cap_all_in: float = Field(
        gt=0,
    )

    includes: list[
        Literal[
            "item",
            "tax",
            "shipping",
            "fees",
        ]
    ] = Field(
        default_factory=lambda: [
            "item",
            "tax",
            "shipping",
            "fees",
        ]
    )

    target_source: ValueSource = ValueSource.USER
    hard_cap_source: ValueSource = ValueSource.USER

    @model_validator(mode="after")
    def validate_spend(self):
        if (
            self.target is not None
            and self.target > self.hard_cap_all_in
        ):
            raise ValueError(
                "target cannot exceed hard_cap_all_in"
            )

        required = {
            "item",
            "tax",
            "shipping",
            "fees",
        }

        if not required.issubset(set(self.includes)):
            raise ValueError(
                "hard_cap_all_in must include item, tax, shipping, and fees"
            )

        return self


# ============================================================
# Delivery
# ============================================================


class DeliveryPolicy(BaseModel):
    deliver_by: datetime | None = None

    max_shipping: float | None = Field(
        default=None,
        ge=0,
    )

    require_verified_estimate: bool = False

    deliver_by_source: ValueSource = ValueSource.USER
    max_shipping_source: ValueSource = ValueSource.DEFAULT


# ============================================================
# Terms
# ============================================================


class TermsPolicy(BaseModel):
    no_subscription: bool = True
    no_membership: bool = True
    no_addons: bool = True

    min_return_days: int | None = Field(
        default=None,
        ge=0,
    )

    no_forced_account_creation: bool = False


# ============================================================
# Merchant Restrictions
# ============================================================


class MerchantPolicy(BaseModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    seller_requirement: SellerRequirement = (
        SellerRequirement.FIRST_PARTY_OR_VERIFIED
    )

    new_merchant: MerchantPolicyAction = (
        MerchantPolicyAction.ESCALATE
    )

    @model_validator(mode="after")
    def validate_merchants(self):
        overlap = set(self.allow) & set(self.deny)

        if overlap:
            raise ValueError(
                f"Merchants cannot appear in both allow and deny: {overlap}"
            )

        return self


# ============================================================
# Substitutions
# ============================================================


class SubstitutionPolicy(BaseModel):
    allowed: bool = False

    same_model_other_color: bool = False
    same_brand: bool = False

    max_price_delta: float | None = Field(
        default=None,
        ge=0,
    )


# ============================================================
# Data Sharing
# ============================================================


class DataSharingPolicy(BaseModel):
    allowed_fields: list[
        Literal[
            "name",
            "email",
            "phone",
            "shipping_address",
            "billing_address",
        ]
    ] = Field(default_factory=list)

    merchant_may_contact: bool = False


# ============================================================
# Payment Instrument
# ============================================================


class InstrumentPolicy(BaseModel):
    type: Literal[
        "virtual_single_use",
        "network_token",
        "stub",
    ] = "virtual_single_use"

    fixed_by_contract: bool = True


# ============================================================
# Contract Draft
# ============================================================


class ContractDraft(BaseModel):
    """
    Generated by the Handshake compiler.

    This object has NOT yet been signed by the user.
    """

    id: str = Field(
        default_factory=lambda: generate_id("draft")
    )

    hil_version: str = "0.2"

    goal: str = Field(min_length=1)

    category: str | None = None

    spend: SpendPolicy

    delivery: DeliveryPolicy | None = None

    terms: TermsPolicy = Field(
        default_factory=TermsPolicy
    )

    merchants: MerchantPolicy = Field(
        default_factory=MerchantPolicy
    )

    substitution: SubstitutionPolicy = Field(
        default_factory=SubstitutionPolicy
    )

    data_sharing: DataSharingPolicy = Field(
        default_factory=DataSharingPolicy
    )

    instrument: InstrumentPolicy = Field(
        default_factory=InstrumentPolicy
    )

    constraints: list[Constraint] = Field(
        default_factory=list
    )

    escalate_if: list[str] = Field(
        default_factory=lambda: [
            "any_unverifiable_hard"
        ]
    )

    selection_disclosure_required: bool = True

    single_use: bool = True
    revocable: bool = True

    expires_at: datetime | None = None

    created_at: datetime = Field(
        default_factory=utc_now
    )

    @model_validator(mode="after")
    def validate_expiration(self):
        if (
            self.expires_at is not None
            and self.expires_at <= self.created_at
        ):
            raise ValueError(
                "expires_at must be after created_at"
            )

        return self


# ============================================================
# Signed Contract
# ============================================================


class Contract(BaseModel):
    """
    Canonical user-authorized contract.
    """

    id: str = Field(
        default_factory=lambda: generate_id("contract")
    )

    hil_version: str = "0.2"

    status: ContractStatus = ContractStatus.ACTIVE

    goal: str
    category: str | None = None

    spend: SpendPolicy

    delivery: DeliveryPolicy | None = None
    terms: TermsPolicy
    merchants: MerchantPolicy
    substitution: SubstitutionPolicy
    data_sharing: DataSharingPolicy
    instrument: InstrumentPolicy

    constraints: list[Constraint] = Field(
        default_factory=list
    )

    escalate_if: list[str] = Field(
        default_factory=list
    )

    selection_disclosure_required: bool = True

    single_use: bool = True
    revocable: bool = True

    agent_key: str | None = None

    created_at: datetime
    signed_at: datetime

    expires_at: datetime | None = None

    contract_hash: str
    signature: str

    previous_contract_id: str | None = None

    @model_validator(mode="after")
    def validate_signed_contract(self):
        if self.signed_at < self.created_at:
            raise ValueError(
                "signed_at cannot be before created_at"
            )

        if (
            self.expires_at is not None
            and self.expires_at <= self.signed_at
        ):
            raise ValueError(
                "expires_at must be after signed_at"
            )

        return self


# ============================================================
# Merchant / Seller
# ============================================================


class MerchantIdentity(BaseModel):
    name: str

    domain: str | None = None

    merchant_id: str | None = None

    seller_of_record: str | None = None

    is_first_party: bool | None = None

    is_verified: bool | None = None


# ============================================================
# Line Items
# ============================================================


class LineItem(BaseModel):
    name: str

    category: str | None = None
    brand: str | None = None
    model: str | None = None

    gtin: str | None = None
    mpn: str | None = None

    condition: ProductCondition | str | None = None

    quantity: int = Field(
        default=1,
        ge=1,
    )

    unit_price: float = Field(
        ge=0,
    )

    attributes: dict[str, Any] = Field(
        default_factory=dict
    )

    @property
    def subtotal(self) -> float:
        return self.unit_price * self.quantity


# ============================================================
# Delivery Proposal
# ============================================================


class DeliveryProposal(BaseModel):
    promised_by: datetime | None = None

    carrier: str | None = None

    tracking_available: bool = False

    verified: bool = False

    evidence: str | None = None


# ============================================================
# Return Terms
# ============================================================


class ReturnTerms(BaseModel):
    returnable: bool | None = None

    return_window_days: int | None = Field(
        default=None,
        ge=0,
    )

    restocking_fee: float | None = Field(
        default=None,
        ge=0,
    )

    evidence: str | None = None


# ============================================================
# Recurring Billing
# ============================================================


class RecurringBilling(BaseModel):
    detected: bool = False

    interval: str | None = None

    amount: float | None = Field(
        default=None,
        ge=0,
    )

    description: str | None = None


# ============================================================
# Transaction Proposal
# ============================================================


class TransactionProposal(BaseModel):
    """
    Structured facts extracted independently from checkout.

    The shopping agent should not be trusted as the authoritative
    source for these values.
    """

    id: str = Field(
        default_factory=lambda: generate_id("proposal")
    )

    contract_id: str

    merchant: MerchantIdentity

    line_items: list[LineItem] = Field(
        min_length=1
    )

    item_subtotal: float = Field(
        ge=0,
    )

    tax: float = Field(
        default=0,
        ge=0,
    )

    shipping: float = Field(
        default=0,
        ge=0,
    )

    fees: float = Field(
        default=0,
        ge=0,
    )

    discounts: float = Field(
        default=0,
        ge=0,
    )

    total: float = Field(
        ge=0,
    )

    currency: str = Field(
        default="USD",
        min_length=3,
        max_length=3,
    )

    recurring_billing: RecurringBilling = Field(
        default_factory=RecurringBilling
    )

    addons_detected: bool = False
    membership_detected: bool = False

    delivery: DeliveryProposal | None = None

    return_terms: ReturnTerms | None = None

    extracted_attributes: dict[str, Any] = Field(
        default_factory=dict
    )

    source_url: str | None = None

    extracted_at: datetime = Field(
        default_factory=utc_now
    )

    extractor_ids: list[str] = Field(
        default_factory=list
    )

    extractors_agreed: bool = True

    evidence: dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_total(self):
        calculated = (
            self.item_subtotal
            + self.tax
            + self.shipping
            + self.fees
            - self.discounts
        )

        tolerance = 0.02

        if abs(calculated - self.total) > tolerance:
            raise ValueError(
                f"Transaction total mismatch. "
                f"Expected {calculated:.2f}, received {self.total:.2f}"
            )

        return self


# ============================================================
# Validation Results
# ============================================================


class ConstraintResult(BaseModel):
    constraint: str

    verdict: ConstraintVerdict

    expected: Any | None = None
    actual: Any | None = None

    reason: str

    severity: ConstraintSeverity = (
        ConstraintSeverity.HARD
    )

    evidence: dict[str, Any] | None = None


class ValidationDecision(BaseModel):
    id: str = Field(
        default_factory=lambda: generate_id("decision")
    )

    contract_id: str
    proposal_id: str

    verdict: ConstraintVerdict

    results: list[ConstraintResult]

    evaluated_at: datetime = Field(
        default_factory=utc_now
    )

    @model_validator(mode="after")
    def validate_overall_verdict(self):
        hard_results = [
            result
            for result in self.results
            if result.severity == ConstraintSeverity.HARD
        ]

        has_fail = any(
            result.verdict == ConstraintVerdict.FAIL
            for result in hard_results
        )

        has_unverifiable = any(
            result.verdict == ConstraintVerdict.UNVERIFIABLE
            for result in hard_results
        )

        expected: ConstraintVerdict

        if has_fail:
            expected = ConstraintVerdict.FAIL

        elif has_unverifiable:
            expected = ConstraintVerdict.UNVERIFIABLE

        else:
            expected = ConstraintVerdict.PASS

        if self.verdict != expected:
            raise ValueError(
                f"Decision verdict should be {expected.value}"
            )

        return self


# ============================================================
# Credential
# ============================================================


class Credential(BaseModel):
    """
    Represents authorization issued by Handshake.

    Do not expose raw payment credentials to the shopping agent.
    """

    id: str = Field(
        default_factory=lambda: generate_id("cred")
    )

    contract_id: str
    proposal_id: str

    merchant_name: str

    merchant_id: str | None = None

    max_amount: float = Field(
        gt=0,
    )

    currency: str = "USD"

    single_use: bool = True

    status: CredentialStatus = (
        CredentialStatus.CREATED
    )

    created_at: datetime = Field(
        default_factory=utc_now
    )

    expires_at: datetime

    provider_reference: str | None = None

    @model_validator(mode="after")
    def validate_credential(self):
        if self.expires_at <= self.created_at:
            raise ValueError(
                "credential expiration must be in the future"
            )

        return self


# ============================================================
# Purchase
# ============================================================


class Purchase(BaseModel):
    id: str = Field(
        default_factory=lambda: generate_id("purchase")
    )

    contract_id: str

    proposal_id: str | None = None
    decision_id: str | None = None
    credential_id: str | None = None

    status: PurchaseStatus = (
        PurchaseStatus.PENDING
    )

    merchant_name: str | None = None

    authorized_amount: float | None = Field(
        default=None,
        ge=0,
    )

    charged_amount: float | None = Field(
        default=None,
        ge=0,
    )

    currency: str = "USD"

    created_at: datetime = Field(
        default_factory=utc_now
    )

    completed_at: datetime | None = None

    error: str | None = None


# ============================================================
# Evidence Ledger
# ============================================================


class EvidenceEventType(str, Enum):
    CONTRACT_CREATED = "contract_created"
    CONTRACT_SIGNED = "contract_signed"
    CONTRACT_REVOKED = "contract_revoked"

    SHOPPING_STARTED = "shopping_started"

    PROPOSAL_CREATED = "proposal_created"

    VALIDATION_STARTED = "validation_started"
    VALIDATION_COMPLETED = "validation_completed"

    PURCHASE_BLOCKED = "purchase_blocked"
    PURCHASE_ESCALATED = "purchase_escalated"
    PURCHASE_AUTHORIZED = "purchase_authorized"

    CREDENTIAL_CREATED = "credential_created"
    CREDENTIAL_USED = "credential_used"

    PAYMENT_COMPLETED = "payment_completed"
    PAYMENT_MISMATCH = "payment_mismatch"


class EvidenceEvent(BaseModel):
    id: str = Field(
        default_factory=lambda: generate_id("event")
    )

    purchase_id: str | None = None
    contract_id: str

    event_type: EvidenceEventType

    timestamp: datetime = Field(
        default_factory=utc_now
    )

    message: str

    data: dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================
# Selection Report
# ============================================================


class CandidateProduct(BaseModel):
    name: str

    merchant: str

    price: float = Field(
        ge=0
    )

    currency: str = "USD"

    sponsored: bool = False
    affiliate: bool = False

    url: str | None = None

    attributes: dict[str, Any] = Field(
        default_factory=dict
    )


class SelectionReport(BaseModel):
    contract_id: str

    selected_candidate: CandidateProduct

    candidates: list[CandidateProduct]

    reasoning_summary: str

    created_at: datetime = Field(
        default_factory=utc_now
    )


# ============================================================
# MCP / API Request Models
# ============================================================


class CreateContractDraftRequest(BaseModel):
    intent: str = Field(
        min_length=1
    )


class SignContractRequest(BaseModel):
    draft_id: str

    agent_key: str | None = None

    signature: str | None = None


class PurchaseRequest(BaseModel):
    contract_id: str

    checkout_url: str

    selection_report: SelectionReport | None = None


class PurchaseStatusResponse(BaseModel):
    purchase_id: str

    status: PurchaseStatus

    decision: ValidationDecision | None = None


# ============================================================
# Compiler Output
# ============================================================


class CompilerOutput(BaseModel):
    """
    Exact object the contract-compiler LLM should return.
    """

    draft: ContractDraft

    assumptions: list[str] = Field(
        default_factory=list
    )

    clarifications_needed: list[str] = Field(
        default_factory=list
    )

    compiler_notes: list[str] = Field(
        default_factory=list
    )
</models_py>
