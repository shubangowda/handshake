# Integration plan

This follows the milestones in HANDSHAKE_BUILD.md section 14. It records what the inventory found, the conflicts it found beyond those already decided in section 3, and how each one will be handled.

## Inventory (milestone 1)

| Piece | Path | Found |
|---|---|---|
| Shuban: backend | `backend/` | `app/{models,db,intent_diff,services,main,__init__}.py`, `tests/{conftest,test_intent_diff,test_api}.py`, `requirements.txt`, `README.md`, and a `.venv`. 181 tests. |
| Ajay: MCP, compiler, Link | `_incoming/handshake_mcp/` | `mcp.md`, `models.py`, `llm.py`, `prompts.py`, `tools.py`, `contract.py` (empty), `stripe.py`, `test_stripe.py`. **There is no `venv/`, `__pycache__/` or `.env` in this copy**, so no secret file is present. |
| Ajay: handoff | `_incoming/CLAUDE_CODE_HANDOFF.md` | Read in full. Folded into the build spec. |
| Rohan: frontend | `_incoming/handshake-frontend/` | Next 16.3.6, React 19.2.8, Tailwind v4, shadcn on Base UI. There is no `node_modules`, so `npm install` is needed before the Next docs in `node_modules/next/dist/docs/` can be read. |
| Sri: merchant | `_incoming/HackGt13/` | `mock-amazon.html`, `assets/` (6 files), `generate_fixtures.py`, `fixtures/*.json` (8 files), `test_fixtures.py`. |
| Product overview | `_incoming/handshake_overview.md` | Used for the README wording. |

**models.py integrity.** The SHA-256 of Appendix A is `63778dcdb0c369e02f8113573d383156dde892967895578c1338776f007dac02`. `backend/app/models.py` matches it exactly. Ajay's copy differs only by one leading `\n`; with that stripped, it matches.

**Environment.** Python 3.12.0, Node 22.13.1 and npm 10.9.2 are installed, and git 2.39.5 is available. Neither `link-cli` nor `gh` is installed. On npm, `@stripe/link-cli` is at 0.23.0, and it will be run through `npx` pinned to that version. `OPENAI_API_KEY` is not set, so the live compiler check is blocked until the owner adds a key to `.env`. There is no Link login on this machine, so `link_test` can't be run for real; the steps will be documented instead.

## Conflicts beyond section 3, and their resolutions

None of these changes the architecture. Each one is also recorded in `docs/DECISIONS.md`.

1. **When the single-use contract is consumed.** Section 9.4 says to mark it used at completion. The backend consumes it atomically at authorization, and that is the double-spend guard its tests rely on. If consumption waited for completion, two purchases could both be authorized against one single-use contract while waiting for Link approval.
   **Resolution:** keep the atomic claim at authorization, as a reservation. At completion, "mark used" is then already true, and the step is idempotent. If the payment ends in a state that guarantees no money moved (`denied`, `expired`, `checkout_changed`, or the user declining before payment), the reservation is released back to `active` with a conditional update and an evidence event. `unknown` and `paying` never release it.

2. **What the models.py `Credential` record now means.** The backend creates a `Credential` at authorization, and existing tests assert its summary fields. The card itself now comes from Link, or from the stub provider.
   **Resolution:** the `Credential` row stays as Handshake's authorization grant. It covers the exact amount, is single use, and its `provider_reference` points at the payment row. It never holds card data. The card lives only in memory, for the moment it is released or submitted.

3. **Event lists in existing tests.** A few tests assert the exact evidence list for an authorized purchase. The payment flow adds a `payment_requested` event after `credential_created`.
   **Resolution:** extend those expected lists by appending the new event. Nothing is removed, and no engine assertion changes.

4. **`POST /purchases/{id}/complete` becomes internal (403 over HTTP).** Its existing tests move to the service layer with the same assertions, as section 6.1 allows.

5. **The frontend's "Pending" group assumed that the contract stays active while a purchase waits for approval.** Because of resolution 1, the contract is `used` (reserved) at authorization.
   **Resolution:** the frontend derives Pending from the purchase's payment state (`awaiting_approval`), not from the contract status.

6. **The `agent_key` default.** Existing signing accepts an optional `agent_key`, but the frontend never sends one. A contract with no `agent_key` would be unusable by any agent under section 8.
   **Resolution:** when signing, if no `agent_key` is given, bind the contract to the configured `HANDSHAKE_AGENT_ID`.

7. **Sri's HTML renders line items with `innerHTML`.** The spec requires product names to be rendered as text.
   **Resolution:** escape every merchant-provided string before it reaches `innerHTML`, keeping his markup and design.

8. **Ajay's venv is absent**, so there is no installed MCP SDK version to pin against.
   **Resolution:** pin the newest `mcp` release that installs and passes the tests.

9. **Clicking through the frontend.** This session has no interactive browser.
   **Resolution:** drive the real frontend with a headless browser (Playwright) for the valid, blocked and escalated flows, and report exactly what was checked.

## Milestone order

1. Inventory and this plan.
2. `.gitignore` and the four "imported as delivered" commits.
3. Restructure into the `handshake/` package, add `pyproject.toml` and `config.py`, keep the 181 tests green, and add the models.py integrity test.
4. Auth and ownership.
5. The compiler, lint, and the draft routes.
6. The mock merchant and extractor, and switching the tests to the static extractor.
7. Payments: stub mode first, then the `link_test` adapter once the real CLI has been inspected.
8. The MCP server, `.mcp.json`, and a real client check.
9. Frontend integration.
10. Scenarios: the pytest end-to-end module, `scripts/demo.py`, and a live run with `dev.py`.
11. Docs, the localhost scan, DEPLOY.md, and the final full run.
12. Secret scan, then push commands (gh is not installed).
