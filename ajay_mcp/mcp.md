# Ajay — MCP, Contract Compiler & Integration

## Mission
Own the agent-facing side of Handshake and make the full demo work end-to-end.

Handshake's core rule: **the shopping agent proposes; Handshake decides.** The agent must never receive or control payment credentials.

## Stack
- Python 3.11+
- FastAPI
- Pydantic
- MCP Python SDK
- LLM provider for contract compilation
- `httpx` for backend calls

## You Own
1. MCP server exposed to shopping agents.
2. Natural-language intent → HIL contract draft.
3. Contract schema/Pydantic models shared with the backend.
4. Agent shopping workflow and purchase requests.
5. End-to-end integration across frontend, backend, and mock merchant.

## MCP Tools

### `list_contracts`
Returns contracts available to the connected agent.

### `create_contract_draft`
Input:
```json
{
  "intent": "Buy running shoes size 10 around $120 delivered by Monday"
}
```

Output: a draft HIL contract. **Do not sign it automatically.**

### `get_contract`
Input:
```json
{"contract_id": "c_123"}
```

### `request_purchase`
Input:
```json
{
  "contract_id": "c_123",
  "checkout_url": "http://localhost:3001/checkout/abc"
}
```

This should trigger Handshake's independent transaction extraction/validation rather than trusting agent-provided claims about the cart.

### `get_purchase_status`
Returns `PENDING`, `AUTHORIZED`, `BLOCKED`, or `ESCALATED`.

## Contract Compiler

The compiler receives:
- User shopping request
- Explicit user preferences/defaults
- Prior signed-contract defaults if your demo supports them

It must **not** receive merchant pages or other untrusted web content.

Produce a fixed contract schema containing at minimum:
```json
{
  "goal": "running shoes",
  "spend": {
    "target": 120,
    "hard_cap_all_in": 135,
    "currency": "USD"
  },
  "product": {
    "category": "running_shoes",
    "condition": ["new"],
    "size": "10"
  },
  "delivery": {
    "by": "..."
  },
  "terms": {
    "no_subscription": true,
    "no_addons": true
  },
  "single_use": true,
  "revocable": true
}
```

Track which fields were inferred so the UI can highlight them.

## Compiler Flow
```text
User intent
    ↓
LLM structured extraction
    ↓
Pydantic validation
    ↓
Deterministic lint
    ↓
Draft stored by backend
    ↓
Rohan's review screen
    ↓
User signs
```

Lint rules should include:
- hard cap >= target
- deadline is in the future
- cap represents all-in spend
- required fields exist

## Integration Contract

Coordinate these shared models early:
- `Contract`
- `TransactionProposal`
- `ConstraintResult`
- `ValidationDecision`

Do not let each teammate invent incompatible JSON.

## Suggested Python Structure
```text
agent/
├── main.py
├── mcp_server.py
├── compiler.py
├── schemas.py
├── handshake_client.py
└── prompts/
    └── compile_contract.txt
```

## Demo Goal
The final demo should show:
1. User tells an agent what to buy.
2. Agent creates a Handshake draft.
3. User reviews/edits/signs it.
4. Agent finds a product.
5. Agent requests purchase.
6. Handshake independently validates checkout.
7. Invalid transaction is blocked OR uncertain transaction escalates.
8. Valid transaction gets a stubbed/scoped credential.
9. Purchase succeeds.

## Priority
**P0**
- Shared schemas
- Contract compiler
- MCP server
- End-to-end integration

**P1**
- Better compiler ambiguity handling
- Contract revocation
- Agent status polling

**P2**
- Native commerce protocol adapters
- More sophisticated standing contracts

## Definition of Done
A third-party MCP-capable agent can create/read a Handshake contract and request a purchase without ever having direct authority to approve its own transaction or see a payment credential.
