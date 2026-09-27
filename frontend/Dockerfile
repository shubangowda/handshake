# syntax=docker/dockerfile:1
#
# frontend/Dockerfile: the Handshake web app (Next.js) as a small production image.
#
# Three stages:
#   deps    npm ci from the lockfile (cached until package*.json changes)
#   build   next build with output "standalone" (see next.config.ts)
#   run     only .next/standalone + static assets, as the non-root `node` user
#
# NEXT_PUBLIC_* values are BUILD-TIME in Next.js: they are inlined into the
# JavaScript sent to browsers, so changing one means rebuilding the image.
# On Fly they come from [build.args] in deploy/fly/web.toml (and
# scripts/deploy_fly.py passes the prefix-derived values as --build-arg).
# None of them is a secret: every browser can read them.
#
# Build context is frontend/ (filtered by frontend/.dockerignore: no .env*,
# no host node_modules or .next).
#
#     docker build -t handshake-web \
#       --build-arg NEXT_PUBLIC_API_URL=https://handshake-sg-api.fly.dev \
#       --build-arg NEXT_PUBLIC_MCP_URL=https://handshake-sg-mcp.fly.dev \
#       frontend

ARG NODE_IMAGE=node:22-trixie-slim

# ============================================================
# deps: exact dependency tree from package-lock.json
# ============================================================
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
ENV NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false
COPY package.json package-lock.json ./
RUN npm ci

# ============================================================
# build: next build (standalone output)
# ============================================================
FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 NPM_CONFIG_UPDATE_NOTIFIER=false

ARG NEXT_PUBLIC_API_URL=""
ARG NEXT_PUBLIC_MCP_URL=""
# Default to the real backend: a production image must never quietly serve
# the in-memory mock data.
ARG NEXT_PUBLIC_USE_MOCKS=false
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_MCP_URL=${NEXT_PUBLIC_MCP_URL} \
    NEXT_PUBLIC_USE_MOCKS=${NEXT_PUBLIC_USE_MOCKS}

# Fail the build, not the first page view, when the backend URL is missing:
# without it every API call in the bundle would go nowhere.
RUN if [ "$NEXT_PUBLIC_USE_MOCKS" = "false" ] && [ -z "$NEXT_PUBLIC_API_URL" ]; then \
      echo "NEXT_PUBLIC_API_URL build arg is required when NEXT_PUBLIC_USE_MOCKS=false" >&2; exit 1; \
    fi

COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ============================================================
# run: the standalone server only
# ============================================================
FROM ${NODE_IMAGE} AS run
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    # server.js reads these: listen on every interface (Fly's proxy connects
    # over the machine's private network), on the port web.toml expects.
    HOSTNAME=0.0.0.0 \
    PORT=3000

# standalone/ holds server.js and the traced node_modules. The static assets
# and public/ are not traced, so they are copied next to it (Next.js serves
# them from there). Owned by root, read-only to `node`; only .next/cache
# (Next's runtime image/data cache) must be writable.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
RUN mkdir -p .next/cache && chown node:node .next/cache

# `node` (uid 1000) is the unprivileged user the official image provides.
USER node
EXPOSE 3000
CMD ["node", "server.js"]
