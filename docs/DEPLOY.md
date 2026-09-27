# Deploying Handshake

Everything runs on localhost in development. Moving to a real host means changing **environment variables only**, never code: `tests/test_integrity.py` fails if a host or port is hardcoded outside `handshake/config.py`.

There are two ways to deploy:

- **[Fly.io](#flyio)**: one command, `python scripts/deploy_fly.py`. This is how the public demo runs.
- **Your own servers**: sections 1 to 8 below, with `handshake.example` standing in for the domain.

## Fly.io

`scripts/deploy_fly.py` deploys the stack as four Fly apps, all named from one prefix (default `handshake-sg`):

| App | Name | What it runs | Machine |
|---|---|---|---|
| Store | `<prefix>-store` | The mock "Amazon.com" store (`handshake/merchant.py`) | shared-cpu-1x, 256 MB, always on (carts live in memory) |
| API | `<prefix>-api` | The backend (`handshake/api.py`), Node 22, and the pinned Stripe Link CLI (`@stripe/link-cli@0.23.0`, pre-installed) | shared-cpu-1x, 512 MB, always on, 1 GB volume `handshake_data` at `/data` |
| MCP | `<prefix>-mcp` | The hosted MCP server (`handshake-mcp --transport http`). Agents connect to `https://<prefix>-mcp.fly.dev/mcp` and sign in per user through OAuth. | shared-cpu-1x, 256 MB, stops when idle |
| Web | `<prefix>` | The website (Next.js standalone server) | shared-cpu-1x, 512 MB, stops when idle |

The API is a single machine that never stops. SQLite on its volume has one writer, and a user's "Connect Stripe Link" login runs as a child process that the API tracks in memory. That's also why a deploy restarts the API with a few seconds of downtime, and why any Link login in progress at that moment has to be started again.

The files involved:

| File | What it is |
|---|---|
| `deploy/fly/{api,mcp,web,store}.toml` | One Fly config per app. The URLs in them are for the default prefix; the script overrides them with `--env` or `--build-arg`, so you never edit these files for a different prefix. |
| `deploy/backend.Dockerfile` | One Dockerfile for the three Python images, with a build target per app (`store`, `mcp`, `api`). Python 3.12-slim, non-root user `app`, no dev extras. |
| `deploy/api-entrypoint.sh` | Hands the root-owned Fly volume to `app` and then drops root for good. |
| `frontend/Dockerfile` | The website image: `next build` with `output: "standalone"`, served by `node server.js` as the non-root `node` user. |
| `.dockerignore`, `frontend/.dockerignore` | Keep `.env*`, databases, Link logins, `node_modules`, and `.next` out of every image. The root file is an allowlist. |

### Prerequisites

1. Install `flyctl` ([fly.io/docs/flyctl/install](https://fly.io/docs/flyctl/install/)) and run `fly auth login`.
2. Create a Google OAuth client ID (Google Cloud Console, then APIs & Services, then Credentials, then "OAuth client ID", type "Web application"). Add `https://<prefix>.fly.dev` under **Authorized JavaScript origins**. The client ID is public; there is no client secret to handle.
3. Python 3.11 or newer to run the script (standard library only). You don't need Docker locally, because the images build on Fly's remote builders.

### Deploy

```bash
python scripts/deploy_fly.py --google-client-id <id>.apps.googleusercontent.com
```

The script is safe to re-run. It:

1. checks `fly auth whoami`
2. creates any missing apps
3. creates the API volume if it's missing
4. generates the API secrets the app doesn't already have
5. deploys store, then API, then MCP, then web (`fly deploy --remote-only`, one machine each)
6. checks each `/health` (the website's `/`) and prints the four URLs

| Flag | Default | Meaning |
|---|---|---|
| `--prefix` | `handshake-sg` | App names: `<prefix>`, `<prefix>-api`, `<prefix>-mcp`, `<prefix>-store` |
| `--region` | `iad` | Region for every app and the volume |
| `--org` | `personal` | Fly organization for new apps |
| `--google-client-id` | none | Required when deploying the API, unless `--allow-demo-login` is given |
| `--allow-demo-login` | off | Turns on "type any email" login in prod, for a private first smoke test before the Google client exists. Anyone can then act as anyone, so redeploy without it afterward. |
| `--only api\|mcp\|web\|store` | all four | Deploy only these apps (repeatable) |
| `--rotate` | off | Replace the generated secrets even if they're already set (see below) |
| `--dry-run` | off | Print every `fly` command without running any. Secret values are never shown. |

The API runs with `HANDSHAKE_ENV=prod`, `HANDSHAKE_PAYMENT_MODE=link_optional`, and `HANDSHAKE_CREDENTIAL_MODE=agent_visible`. In `link_optional` mode, funding is simulated for everyone except users who connect their own Link account, and those users get real Link **test-mode** cards. The MCP and store apps don't set `HANDSHAKE_ENV=prod`, because they hold none of the API's secrets. The store runs with `HANDSHAKE_ENV=dev` on purpose: that enables its hidden demo panel and the "change the cart" endpoint used by the `checkout_changed` demo. Never set `HANDSHAKE_AGENT_TOKEN` on any app, and note that prod refuses to start with it.

The website's `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_MCP_URL`, and `NEXT_PUBLIC_USE_MOCKS=false` are compiled into the JavaScript at build time, so they're build args (`[build.args]` in `web.toml`), not runtime env. Changing one means redeploying `web`.

### Secrets

The script generates `HANDSHAKE_SIGNING_SECRET`, `HANDSHAKE_SESSION_SECRET`, and `HANDSHAKE_CARD_ENCRYPTION_KEY` (32 random bytes, base64) for the API. It sends them to `fly secrets import` on stdin and never prints them or writes them to disk. It only generates names the app doesn't have yet, so re-running it rotates nothing. `fly secrets list --app <prefix>-api` shows which names are set, but never their values.

**OpenAI key (optional).** Without a key, the contract compiler uses its offline demo fixture. To use the live compiler, run this and paste the key on stdin, so it stays out of your shell history:

```bash
fly secrets import --app handshake-sg-api
OPENAI_API_KEY=<paste your key>
<Ctrl-D>
```

This restarts the API. To go back to the fixture, run `fly secrets unset OPENAI_API_KEY --app handshake-sg-api`.

**Rolling secrets.** Each secret invalidates something different when it changes:

| Secret | What rotating it breaks |
|---|---|
| `HANDSHAKE_SESSION_SECRET` | Every login and every agent token: everyone signs in again, and agents reconnect. This is the emergency "revoke everything" switch. |
| `HANDSHAKE_SIGNING_SECRET` | Signatures on existing contracts stop verifying. |
| `HANDSHAKE_CARD_ENCRYPTION_KEY` | Any funded, unused card stored on a contract can't be decrypted anymore, so those contracts have to be funded again. |

To roll one secret without ever seeing the value:

```bash
python3 -c 'import secrets; print("HANDSHAKE_SESSION_SECRET=" + secrets.token_urlsafe(48))' \
  | fly secrets import --app handshake-sg-api
```

To roll all three, run `python scripts/deploy_fly.py --only api --rotate --google-client-id <id>`. To revoke a single agent, use the Agents page instead; it needs no secret change.

### Logs and status

```bash
fly logs --app handshake-sg-api          # live logs (also -mcp, -store, and handshake-sg for the website)
fly status --app handshake-sg-api        # machines, health checks, release
fly ssh console --app handshake-sg-api   # a shell on the API machine (the database is /data/handshake.db)
fly releases --app handshake-sg-api      # deploy history; roll back with fly deploy --image <previous image>
```

### Cost

This is roughly what each app costs, at Fly's list prices for `iad` when this was written. Check [fly.io/docs/about/pricing](https://fly.io/docs/about/pricing/) for current prices.

| App | Approximate cost |
|---|---|
| API (always on, 512 MB) | about $3.20 a month |
| Store (always on, 256 MB) | about $2 a month |
| MCP and web | Stop when idle, so a few cents to a few dollars a month depending on traffic |
| Volume | 1 GB at about $0.15 a month |
| IPv4 | The shared IPv4 address Fly assigns by default is free. Don't allocate a dedicated one unless you need it (about $2 a month each). |

Expect roughly $6 to $10 a month in total. To pause everything, run `fly scale count 0 --app <name>` for each app; bring it back with `fly scale count 1`. Deleting the API app (`fly apps destroy`) also deletes its volume and every contract on it.

## 1. URLs

| Variable | Example |
|---|---|
| `HANDSHAKE_ENV` | `prod`. Refuses to start with dev secrets, a wildcard CORS setting, a static `HANDSHAKE_AGENT_TOKEN`, or no way to log in. |
| `HANDSHAKE_API_URL` | `https://api.handshake.example` (also the OAuth issuer) |
| `HANDSHAKE_FRONTEND_URL` | `https://app.handshake.example` |
| `HANDSHAKE_MERCHANT_URL` | `https://store.handshake.example` (the mock store, if it is still used) |
| `HANDSHAKE_MCP_PUBLIC_URL` | `https://mcp.handshake.example` (the hosted MCP server; the API checks OAuth `resource` values against it) |
| `HANDSHAKE_CORS_ORIGINS` | `https://app.handshake.example,https://store.handshake.example` (never `*`) |
| `HANDSHAKE_ALLOWED_MERCHANT_ORIGINS` | The exact merchant origins the extractor may fetch |
| `HANDSHAKE_MERCHANT_IDENTITIES` | `https://store.handshake.example=Amazon.com` (which merchant each origin is) |
| `HANDSHAKE_GOOGLE_CLIENT_ID` | The Google OAuth client ID for "Sign in with Google" (public) |
| `HANDSHAKE_ALLOW_DEMO_LOGIN` | Unset in prod (demo login is off). Set it to `true` only for a private demo. |
| Frontend build | `NEXT_PUBLIC_API_URL=https://api.handshake.example`, `NEXT_PUBLIC_MCP_URL=https://mcp.handshake.example/mcp`, `NEXT_PUBLIC_USE_MOCKS=false`. These are build-time values: set them in the environment of `npm run build` or in `frontend/.env.production`. |

## 2. HTTPS

Terminate TLS in front of every service (a load balancer, Caddy, or nginx). The backend, frontend, merchant, and MCP server all speak plain HTTP behind the proxy. The login links and the review links use `HANDSHAKE_FRONTEND_URL`, so it must be the public `https://` address.

## 3. Build and start

```bash
pip install -e .                                   # one venv (editable, so merchant_static/ is found next to the package)
uvicorn handshake.api:app --host 0.0.0.0 --port 8000 --proxy-headers   # ONE worker: in-progress Link logins live in process memory
uvicorn handshake.merchant:app --host 0.0.0.0 --port 3001   # only if the mock store is deployed
cd frontend && npm ci && npm run build && PORT=3000 HOSTNAME=0.0.0.0 node .next/standalone/server.js
```

With `output: "standalone"`, copy `frontend/public` and `frontend/.next/static` next to `server.js` first (`frontend/Dockerfile` shows how), or use `npm run start`, which works but prints a warning.

## 4. MCP over HTTP

**Hosted, multi-user.** Set `HANDSHAKE_MCP_PUBLIC_URL` and run:

```bash
HANDSHAKE_MCP_PUBLIC_URL=https://mcp.handshake.example HANDSHAKE_MCP_HTTP_HOST=0.0.0.0 handshake-mcp --transport http
```

Each MCP client signs its user in through OAuth against the API, and every request carries that user's own token. The server answers `/mcp`, `/health`, and the OAuth discovery documents. It accepts only its public host name and loopback (DNS-rebinding protection).

**Single user, local.** Without a public URL, `HANDSHAKE_MCP_HTTP_TOKEN=<long random value> handshake-mcp --transport http` serves one user behind a static bearer token.

## 5. Rotate every secret

Generate fresh values for `HANDSHAKE_SIGNING_SECRET`, `HANDSHAKE_SESSION_SECRET` (the two must differ), and `HANDSHAKE_CARD_ENCRYPTION_KEY`, plus `HANDSHAKE_MCP_HTTP_TOKEN` if you use the single-user MCP mode. Leave `HANDSHAKE_AGENT_TOKEN` unset: prod refuses it. The dev values in anyone's `.env` must not reach production.

Rotating the session secret invalidates every login and every agent token. Rotating the card key strands any funded card that's still stored on a contract. See the table under [Fly.io > Secrets](#secrets).

## 6. Database

Set `DATABASE_URL=postgresql+psycopg://user:pass@host/handshake` and install a driver (`pip install psycopg[binary]`). Tables are created on startup. There are no migrations yet, so plan for Alembic before the schema changes again. The atomic conditional updates (the double-spend guard, payment claims, and escalation claims) work the same way on Postgres.

The Fly deployment keeps SQLite on the API's volume, which is fine for one machine.

## 7. Payments stay in test mode

`HANDSHAKE_PAYMENT_MODE` accepts only `stub`, `link_test`, and `link_optional`. Real charges are **not** possible in this codebase: every Link spend request is created with `--test`, and there is no live mode to switch on. Going live would be a separate, deliberate decision with its own review. It would need a new, audited code path, not a config change.

Each user connects **their own** Link account from the website. The server keeps a private Link CLI home per user under `HANDSHAKE_LINK_HOME_ROOT`, which is `/data/link-homes` on Fly. The CLI version is pinned: `HANDSHAKE_LINK_CLI` is either `npx --yes @stripe/link-cli@0.23.0` or, in the Fly image, the pre-installed `/usr/local/bin/link-cli`.

## 8. Before real users

- Contracts are still signed with the server's HMAC key. Sign them with a user-held key (passkeys) before real money is ever involved.
- Add rate limiting on `/auth/*` and `/oauth/*`, and an externally anchored evidence ledger.
- Keep CORS and the merchant allowlist exact.
