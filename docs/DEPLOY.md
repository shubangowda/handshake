# Deploying to the real domain

Everything runs on localhost today. Moving to the domain means changing **environment variables only**, never code: `tests/test_integrity.py` fails if a host or port is hardcoded outside `handshake/config.py`. The example below uses `handshake.example` for the domain.

## 1. URLs

| Variable | Example |
|---|---|
| `HANDSHAKE_ENV` | `prod` (refuses to start with dev secrets or a wildcard CORS setting) |
| `HANDSHAKE_API_URL` | `https://api.handshake.example` |
| `HANDSHAKE_FRONTEND_URL` | `https://app.handshake.example` |
| `HANDSHAKE_MERCHANT_URL` | `https://store.handshake.example` (the mock store, if it is still used) |
| `HANDSHAKE_CORS_ORIGINS` | `https://app.handshake.example,https://store.handshake.example` (never `*`) |
| `HANDSHAKE_ALLOWED_MERCHANT_ORIGINS` | the exact merchant origins the extractor may fetch |
| `HANDSHAKE_MERCHANT_IDENTITIES` | `https://store.handshake.example=Amazon.com` (which merchant each origin is) |
| `frontend/.env.production` | `NEXT_PUBLIC_API_URL=https://api.handshake.example` and `NEXT_PUBLIC_USE_MOCKS=false` |

## 2. HTTPS

Terminate TLS in front of every service (a load balancer, Caddy, or nginx). The backend, frontend, merchant, and MCP-over-HTTP all speak plain HTTP behind the proxy. The device-login link and the review links use `HANDSHAKE_FRONTEND_URL`, so it must be the public `https://` address.

## 3. Build and start

```bash
pip install -e .                                   # one venv
uvicorn handshake.api:app --host 0.0.0.0 --port 8000 --workers 2
uvicorn handshake.merchant:app --host 0.0.0.0 --port 3001   # only if the mock store is deployed
cd frontend && npm ci && npm run build && npm run start -- --port 3000
```

## 4. MCP over HTTP

```bash
HANDSHAKE_MCP_HTTP_TOKEN=<long random value> handshake-mcp --transport http
```

It listens on `HANDSHAKE_MCP_HTTP_HOST`:`HANDSHAKE_MCP_HTTP_PORT`. Put it behind TLS. Remote MCP clients send `Authorization: Bearer <HANDSHAKE_MCP_HTTP_TOKEN>`, and each agent then connects to a user's account through the device-login link.

## 5. Rotate every secret

Generate fresh values for `HANDSHAKE_SIGNING_SECRET`, `HANDSHAKE_SESSION_SECRET` (the two must differ), `HANDSHAKE_AGENT_TOKEN` (or leave it unset and use only the device login), and `HANDSHAKE_MCP_HTTP_TOKEN`. The dev values in anyone's `.env` must not reach production.

Rotating the session secret invalidates every login token and every agent token issued through the device flow. That is also the way to revoke agents today.

## 6. Database

Set `DATABASE_URL=postgresql+psycopg://user:pass@host/handshake` and install a driver (`pip install psycopg[binary]`). Tables are created on startup; there are no migrations yet, so plan for Alembic before the schema changes again. The atomic conditional updates (the double-spend guard, payment claims, and escalation claims) work the same way on Postgres.

## 7. Payments stay in test mode

`HANDSHAKE_PAYMENT_MODE` accepts only `stub` and `link_test`. Real charges are **not** possible in this codebase: every Link spend request is created with `--test`, and there is no live mode to switch on. Going live would be a separate, deliberate decision with its own review. It would need a new, audited code path, not a config change.

For `link_test`, log the server's Link CLI in once with `python -m handshake.payments login`. One Link wallet is shared by every user of that server, which is fine for a demo but not for real users.

## 8. Before real users

- **Replace demo auth.** `POST /auth/demo-login` hands a token to anyone who types an email address. Replace it with passkeys or OAuth, and sign contracts with a user-held key instead of the server HMAC.
- Add a revocation list for agent tokens, rate limiting on `/auth/*` and `/oauth/*`, and an externally anchored evidence ledger.
- Keep CORS and the merchant allowlist exact.
