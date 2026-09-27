# syntax=docker/dockerfile:1
#
# backend.Dockerfile: the three Python images (api, mcp, store) from one file.
#
# Why one file with three targets
# -------------------------------
# The API, the MCP server, and the mock store are all the same `handshake`
# package with a different entry point. Sharing one `base` stage means the
# dependency layer is built (and cached, and pushed) once. Each fly.toml
# picks its image with `build-target`:
#
#     base  (python 3.12-slim, `handshake` installed, non-root user `app`)
#       |-- store  uvicorn handshake.merchant:app  (port 3001)
#       |-- mcp    handshake-mcp --transport http   (port from HANDSHAKE_MCP_HTTP_PORT)
#       '-- api    + Node 22 + the pinned Stripe Link CLI, uvicorn handshake.api:app (port 8000)
#
# The build context is the repo root, filtered by /.dockerignore (an
# allowlist: no .env, database, Link logins, tests, or frontend get in).
#
# Build locally (what scripts/deploy_fly.py's remote builder does on Fly):
#     docker build -f deploy/backend.Dockerfile --target api -t handshake-api .

ARG PYTHON_IMAGE=python:3.12-slim-trixie
# The same Debian release as the Python image, so the node binary we copy
# out of it links against the same glibc and libstdc++.
ARG NODE_IMAGE=node:22-trixie-slim

# ============================================================
# base: the handshake package and its pinned dependencies
# ============================================================
FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # config.py loads <repo>/.env when it exists. No .env is ever copied in
    # (see .dockerignore), and this makes sure one could not be loaded even if
    # it were: on Fly, settings come only from [env] and `fly secrets`.
    HANDSHAKE_SKIP_DOTENV=1

# A fixed, unprivileged user. No process in these images runs as root (the
# api entrypoint starts as root only long enough to hand /data to this user).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first, in their own layer, so editing handshake/*.py does not
# reinstall FastAPI, OpenAI, MCP and friends. A placeholder package is enough
# for setuptools to resolve and install every pinned dependency.
COPY pyproject.toml README.md ./
RUN mkdir handshake && touch handshake/__init__.py \
 && pip install --editable . \
 && rm -rf handshake

# Why an editable install instead of a plain `pip install .`:
# config.py derives REPO_ROOT from its own location, and merchant.py serves
# REPO_ROOT/merchant_static. A regular install would put the package in
# site-packages, where merchant_static does not exist. Editable keeps the
# package importable from /app/handshake, so REPO_ROOT is /app, exactly like
# a developer checkout, with no code change. (No dev extras: pytest is not
# installed.)
COPY handshake ./handshake
COPY merchant_static ./merchant_static

# Everything under /app stays owned by root and read-only to `app`: the
# processes write only to /tmp, the user's home, and (api) the /data volume.

# ============================================================
# store: the mock "Amazon.com" merchant
# ============================================================
FROM base AS store
USER app
EXPOSE 3001
# --proxy-headers with any forwarder: only Fly's edge proxy can reach the
# machine's port, and it sets X-Forwarded-For/-Proto, so the app sees the
# real scheme (https) and client address.
CMD ["uvicorn", "handshake.merchant:app", "--host", "0.0.0.0", "--port", "3001", "--proxy-headers", "--forwarded-allow-ips", "*"]

# ============================================================
# mcp: the MCP server over streamable HTTP (a thin client of the API)
# ============================================================
FROM base AS mcp
USER app
# The listen address and port come from HANDSHAKE_MCP_HTTP_HOST and
# HANDSHAKE_MCP_HTTP_PORT (set in deploy/fly/mcp.toml), read by config.py.
EXPOSE 8765
CMD ["handshake-mcp", "--transport", "http"]

# ============================================================
# api: the backend, plus Node 22 and the pinned Stripe Link CLI
# ============================================================
FROM ${NODE_IMAGE} AS node

FROM base AS api

# Neither npm nor the Link CLI (update-notifier) may phone the registry to
# look for updates, at build time or on every Link call at run time.
ENV NO_UPDATE_NOTIFIER=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

# Node comes from the official image (a copy of its binary and npm) rather
# than an apt repository: it is the exact upstream build, and there is no
# extra package source to trust at build time.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# The Link CLI is PRE-INSTALLED at the pinned, verified version instead of
# fetched by `npx` at run time: nothing is downloaded while a user waits on
# "Connect Stripe Link", the version cannot drift, and the machine does not
# need npm's registry to take a payment. Its package.json declares the
# binary as `link-cli`. The version check fails the build if it doesn't run.
ARG LINK_CLI_VERSION=0.23.0
RUN npm install --global "@stripe/link-cli@${LINK_CLI_VERSION}" \
 && rm -rf /root/.npm \
 && link-cli --version

# payments.py runs exactly this binary (HANDSHAKE_LINK_CLI), never npx.
ENV HANDSHAKE_LINK_CLI=/usr/local/bin/link-cli

# Fly mounts a volume root-owned. The entrypoint gives /data to `app` and
# then drops to `app` for good (see api-entrypoint.sh), so this stage stays
# USER root on purpose: nothing but that small script runs as root.
COPY deploy/api-entrypoint.sh /usr/local/bin/api-entrypoint.sh
RUN chmod 0755 /usr/local/bin/api-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/api-entrypoint.sh"]
# One worker on purpose: SQLite has one writer, and in-progress Link logins
# are child processes tracked in this process's memory (payments._logins).
CMD ["uvicorn", "handshake.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
