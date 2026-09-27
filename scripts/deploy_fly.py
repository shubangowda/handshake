"""
deploy_fly.py: deploy the whole Handshake stack to Fly.io, with one command.

    python scripts/deploy_fly.py --google-client-id <id>.apps.googleusercontent.com
    python scripts/deploy_fly.py --dry-run --google-client-id <id>   # print the plan, touch nothing
    python scripts/deploy_fly.py --only web                           # redeploy one app
    python scripts/deploy_fly.py --allow-demo-login                   # first smoke test, before a Google client exists

What it deploys
---------------
Four Fly apps, all named from one prefix (default `handshake-sg`), so a
second copy of the stack is just a different --prefix:

    <prefix>-store   the mock "Amazon.com" store   (handshake/merchant.py)
    <prefix>-api     the backend + SQLite volume   (handshake/api.py)
    <prefix>-mcp     the hosted MCP server          (handshake/mcp_server.py)
    <prefix>         the website                    (frontend/)

Their configs are deploy/fly/<app>.toml. Those files hold the values for the
default prefix; this script never edits them. It passes the real app name
with --app and every prefix-derived URL with --env / --build-arg instead, so
the files stay a readable, reviewable description of each app.

Steps, in order (each one skipped when already done, so re-running is safe):
1. `fly auth whoami`: stop early with a clear message if not logged in.
2. Create any missing app (all of them before any deploy, so a taken name
   fails before anything is half-deployed).
3. Create the API's `handshake_data` volume if it is missing.
4. Generate and set the API's secrets if missing (see "Secrets" below).
5. Deploy store -> api -> mcp -> web. That order means every app's
   dependencies are already up when it starts: the API reads checkouts from
   the store, the MCP server calls the API, and the website calls both.
6. Smoke-check each /health (the website: /) and print the four URLs.

Secrets
-------
HANDSHAKE_SIGNING_SECRET, HANDSHAKE_SESSION_SECRET (must differ), and
HANDSHAKE_CARD_ENCRYPTION_KEY are generated here with `secrets`, piped to
`fly secrets import` on stdin, and then forgotten: they are never printed,
logged, put in argv (where `ps` could see them), or written to disk.

They are generated ONLY for names the app does not already have. Re-running
the script must not rotate them, because rotating the session secret logs
everyone out and disconnects every agent, rotating the signing secret breaks
the signatures on existing contracts, and rotating the card key makes every
stored (funded, unused) card undecryptable. --rotate does it anyway, on
purpose.

OPENAI_API_KEY is optional and never asked for: the owner sets it with
`fly secrets import` (docs/DEPLOY.md, "Fly.io"). Without it the compiler uses
its offline demo fixture.

Why remote builds (`fly deploy --remote-only`)
----------------------------------------------
Fly's builder runs on the same CPU architecture (amd64) as the machines, so
nothing is emulated, and the image goes straight into Fly's registry from
inside Fly's network. A local build on an Apple Silicon laptop would have to
cross-compile under emulation and then upload ~700 MB over a home
connection. It also means deploying needs only `fly`, not Docker.

This script uses only the standard library (like the rest of scripts/).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
FLY_CONFIG_DIR = REPO / "deploy" / "fly"
BACKEND_DOCKERFILE = REPO / "deploy" / "backend.Dockerfile"
FRONTEND_DIR = REPO / "frontend"

DEFAULT_PREFIX = "handshake-sg"
DEFAULT_REGION = "iad"
DEFAULT_ORG = "personal"

# Store first, website last: see step 5 in the module docstring.
DEPLOY_ORDER = ("store", "api", "mcp", "web")

# Must match [mounts] source in deploy/fly/api.toml.
API_VOLUME = "handshake_data"
API_VOLUME_GB = 1

# The API's generated secrets. Anything else (OPENAI_API_KEY) the owner sets.
SIGNING = "HANDSHAKE_SIGNING_SECRET"
SESSION = "HANDSHAKE_SESSION_SECRET"
CARD_KEY = "HANDSHAKE_CARD_ENCRYPTION_KEY"
API_SECRETS = (SIGNING, SESSION, CARD_KEY)

# Fly app names become DNS labels (<name>.fly.dev). Leave room for "-store".
PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,40}[a-z0-9]$")

HEALTH_TIMEOUT_SECONDS = 180  # a first boot on a fresh app can take a minute or two


class DeployError(Exception):
    """A step failed; the message says what to do about it."""


# ============================================================
# The four apps
# ============================================================


@dataclass(frozen=True)
class FlyApp:
    """One Fly app: its name, its fly.toml, and how its image is built."""

    key: str  # api / mcp / web / store (what --only takes)
    name: str  # the Fly app name, from the prefix
    config: Path  # deploy/fly/<key>.toml
    context: Path  # build context = `fly deploy`'s working directory
    dockerfile: Path
    health_path: str  # what the smoke check GETs

    @property
    def url(self) -> str:
        """The app's public https address on fly.dev."""
        return public_url(self.name)


def public_url(app_name: str) -> str:
    """https://<app>.fly.dev: every app gets this name and a TLS certificate from Fly."""
    return f"https://{app_name}.fly.dev"


def build_apps(prefix: str) -> dict[str, FlyApp]:
    """The four apps for a prefix. The website gets the bare prefix, since it is the address people visit."""
    backend = dict(context=REPO, dockerfile=BACKEND_DOCKERFILE)
    return {
        "store": FlyApp("store", f"{prefix}-store", FLY_CONFIG_DIR / "store.toml", health_path="/health", **backend),
        "api": FlyApp("api", f"{prefix}-api", FLY_CONFIG_DIR / "api.toml", health_path="/health", **backend),
        "mcp": FlyApp("mcp", f"{prefix}-mcp", FLY_CONFIG_DIR / "mcp.toml", health_path="/health", **backend),
        "web": FlyApp("web", prefix, FLY_CONFIG_DIR / "web.toml", context=FRONTEND_DIR,
                      dockerfile=FRONTEND_DIR / "Dockerfile", health_path="/"),
    }


def runtime_env(apps: dict[str, FlyApp], key: str, google_client_id: str | None, allow_demo_login: bool) -> dict[str, str]:
    """
    The prefix-derived [env] values for one app, passed as `fly deploy --env`.

    These override the defaults written in the app's fly.toml. Everything
    else in [env] (modes, paths) is the same for every prefix and stays in
    the file. The URLs have to agree across apps (the API's CORS list must
    name the website, the store must know its own address, and so on), which
    is why they are computed here in one place rather than typed per file.
    """
    api, mcp, web, store = apps["api"].url, apps["mcp"].url, apps["web"].url, apps["store"].url
    if key == "api":
        env = {
            "HANDSHAKE_API_URL": api,
            "HANDSHAKE_FRONTEND_URL": web,
            "HANDSHAKE_MERCHANT_URL": store,
            "HANDSHAKE_MCP_PUBLIC_URL": mcp,
            "HANDSHAKE_CORS_ORIGINS": f"{web},{store}",
            "HANDSHAKE_ALLOWED_MERCHANT_ORIGINS": store,
            "HANDSHAKE_MERCHANT_IDENTITIES": f"{store}=Amazon.com",
        }
        if google_client_id:
            env["HANDSHAKE_GOOGLE_CLIENT_ID"] = google_client_id
        if allow_demo_login:
            # "Type any email to log in": lets anyone act as anyone, so it is
            # off in prod unless asked for (a private first smoke test).
            env["HANDSHAKE_ALLOW_DEMO_LOGIN"] = "true"
        return env
    if key == "mcp":
        return {"HANDSHAKE_API_URL": api, "HANDSHAKE_MCP_PUBLIC_URL": mcp, "HANDSHAKE_FRONTEND_URL": web}
    if key == "store":
        return {"HANDSHAKE_MERCHANT_URL": store, "HANDSHAKE_FRONTEND_URL": web}
    return {}


def build_args(apps: dict[str, FlyApp], key: str) -> dict[str, str]:
    """The website's NEXT_PUBLIC_* values: compiled into the bundle, so they are build args, not env."""
    if key != "web":
        return {}
    return {
        "NEXT_PUBLIC_API_URL": apps["api"].url,
        # The MCP endpoint itself (what a user pastes into their MCP client).
        "NEXT_PUBLIC_MCP_URL": f"{apps['mcp'].url}/mcp",
        "NEXT_PUBLIC_USE_MOCKS": "false",
    }


# ============================================================
# Running fly
# ============================================================


def find_fly() -> str:
    """The flyctl binary: on PATH as fly or flyctl, or where the installer puts it (~/.fly/bin)."""
    for name in ("fly", "flyctl"):
        found = shutil.which(name)
        if found:
            return found
    installed = Path.home() / ".fly" / "bin" / "fly"
    if installed.is_file():
        return str(installed)
    raise DeployError("flyctl is not installed. See https://fly.io/docs/flyctl/install/ and run `fly auth login`.")


class Fly:
    """
    Runs fly commands, or (dry run) only prints them.

    Every command is printed before it runs, so the log is a replayable
    record. Secret VALUES never appear in a command line: they only ever
    travel on stdin (see set_secrets), and the printed stand-in says so.
    """

    def __init__(self, binary: str, dry_run: bool) -> None:
        """binary is the flyctl path; dry_run prints instead of running."""
        self.binary = binary
        self.dry_run = dry_run

    def show(self, args: list[str], stdin_note: str | None = None) -> None:
        """Print a command the way a person would type it."""
        line = "fly " + " ".join(shlex.quote(arg) for arg in args)
        if stdin_note:
            line += f"   <<< {stdin_note}"
        print(f"  $ {line}", flush=True)

    def run(self, args: list[str], *, stdin: str | None = None, stdin_note: str | None = None) -> None:
        """Run a command that changes something. Dry run: print it only. Fails loudly."""
        self.show(args, stdin_note)
        if self.dry_run:
            return
        result = subprocess.run([self.binary, *args], input=stdin, text=True)
        if result.returncode != 0:
            raise DeployError(f"`fly {args[0]} {args[1] if len(args) > 1 else ''}` failed (exit {result.returncode}); see its output above.")

    def query(self, args: list[str]) -> Any:
        """Run a read-only command with --json and return the parsed output (or None if it failed)."""
        result = subprocess.run([self.binary, *args, "--json"], capture_output=True, text=True)
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout or "null")
        except ValueError:
            return None


def names_in(listing: Any) -> set[str]:
    """The `name` of every item in a fly --json listing (flyctl is inconsistent about Name vs name)."""
    found: set[str] = set()
    for item in listing or []:
        if isinstance(item, dict):
            value = item.get("Name") or item.get("name")
            if value:
                found.add(str(value))
    return found


# ============================================================
# Steps
# ============================================================


def check_login(fly: Fly) -> None:
    """Step 1: fail fast (before creating anything) when flyctl is not logged in."""
    print("\n== Fly login")
    if fly.dry_run:
        fly.show(["auth", "whoami"])
        return
    result = subprocess.run([fly.binary, "auth", "whoami"], capture_output=True, text=True)
    if result.returncode != 0:
        raise DeployError("flyctl is not logged in. Run `fly auth login`, then run this script again.")
    print(f"  logged in as {result.stdout.strip()}")


def ensure_apps(fly: Fly, apps: list[FlyApp], org: str) -> None:
    """Step 2: create every selected app that does not exist yet."""
    print("\n== Apps")
    existing = set() if fly.dry_run else names_in(fly.query(["apps", "list"]))
    for app in apps:
        if app.name in existing:
            print(f"  {app.name}: exists")
            continue
        if fly.dry_run:
            print(f"  {app.name}: create if missing")
        fly.run(["apps", "create", app.name, "--org", org])


def ensure_volume(fly: Fly, api: FlyApp, region: str) -> None:
    """Step 3: the API's persistent volume (SQLite database and each user's Link login)."""
    print("\n== API volume")
    if not fly.dry_run and API_VOLUME in names_in(fly.query(["volumes", "list", "--app", api.name])):
        print(f"  {API_VOLUME}: exists")
        return
    if fly.dry_run:
        print(f"  {API_VOLUME}: create if missing ({API_VOLUME_GB} GB, {region})")
    fly.run(["volumes", "create", API_VOLUME, "--app", api.name, "--region", region, "--size", str(API_VOLUME_GB), "--yes"])


def generate_secret(name: str) -> str:
    """A fresh random value in the format config.py expects for this secret."""
    if name == CARD_KEY:
        # AES-256-GCM key: exactly 32 random bytes, base64 (config.validate checks the length).
        return base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    # HMAC keys: 48 random bytes (384 bits), URL-safe so they need no quoting.
    return secrets.token_urlsafe(48)


def set_secrets(fly: Fly, api: FlyApp, rotate: bool) -> None:
    """
    Step 4: generate the API's missing secrets (or all of them with --rotate)
    and hand them to Fly on stdin.

    `--stage` stores them without restarting anything; the deploy in step 5
    picks them up. That avoids an extra restart, and on a brand-new app
    (no machines yet) it is the only thing that works.
    """
    print("\n== API secrets")
    if fly.dry_run:
        present: set[str] = set()
    else:
        listing = fly.query(["secrets", "list", "--app", api.name])
        if listing is None:
            raise DeployError(f"Could not list the secrets of {api.name}; is the app created and are you a member of its org?")
        present = names_in(listing)

    wanted = list(API_SECRETS) if rotate else [name for name in API_SECRETS if name not in present]
    for name in API_SECRETS:
        if name in present and name not in wanted:
            print(f"  {name}: already set, kept (pass --rotate to replace)")
    if not wanted:
        return
    if rotate:
        print("  --rotate: replacing every generated secret. Everyone is logged out, agents must reconnect,")
        print("  existing contract signatures stop verifying, and stored (unused) funded cards cannot be decrypted.")
    if fly.dry_run and not rotate:
        print("  (a real run generates only the names the app does not have yet)")

    values = {name: generate_secret(name) for name in wanted}
    # Signing and session keys protect different things and must differ
    # (config.validate refuses equal ones). Two fresh 384-bit values colliding
    # is impossible in practice, but the check costs nothing.
    if SIGNING in values and SESSION in values and values[SIGNING] == values[SESSION]:
        raise DeployError("Generated signing and session secrets collided; run again.")
    payload = "".join(f"{name}={value}\n" for name, value in values.items())
    note = "'" + " ".join(f"{name}=<generated, not shown>" for name in values) + "' (on stdin)"
    fly.run(["secrets", "import", "--app", api.name, "--stage"], stdin=payload, stdin_note=note)
    # Drop our only references to the values as soon as Fly has them.
    del payload, values


def deploy(fly: Fly, app: FlyApp, apps: dict[str, FlyApp], region: str, google_client_id: str | None, allow_demo_login: bool) -> None:
    """
    Step 5: `fly deploy` one app from its fly.toml.

    - The working directory (first argument) is the build context: the repo
      root for the Python images (filtered by /.dockerignore), frontend/ for
      the website (filtered by frontend/.dockerignore).
    - --dockerfile is an absolute path, so it means the same thing no matter
      how flyctl resolves relative paths.
    - --ha=false: exactly one machine. The API has one SQLite volume, and the
      store keeps carts in memory, so a second machine would be wrong, not
      just wasteful; the MCP server and website are small enough not to need one.
    """
    print(f"\n== Deploy {app.key}: {app.name}")
    args = [
        "deploy", str(app.context),
        "--config", str(app.config),
        "--app", app.name,
        "--dockerfile", str(app.dockerfile),
        "--remote-only",
        "--ha=false",
        "--primary-region", region,
    ]
    for name, value in runtime_env(apps, app.key, google_client_id, allow_demo_login).items():
        args += ["--env", f"{name}={value}"]
    for name, value in build_args(apps, app.key).items():
        args += ["--build-arg", f"{name}={value}"]
    fly.run(args)


def _tls_context() -> "ssl.SSLContext":
    """
    Verify HTTPS with certifi's CA bundle when it's installed (it is, in the repo
    venv). python.org's macOS Python ships without system CA certificates, so the
    default context fails every check with CERTIFICATE_VERIFY_FAILED even though
    the site is fine. Verification is never turned off.
    """
    import ssl

    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def smoke_check(app: FlyApp) -> str:
    """Step 6: GET the app's health path over the public https URL until it answers 200 (or time runs out)."""
    url = app.url + app.health_path
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "handshake-deploy-smoke-check"})
            with urllib.request.urlopen(request, timeout=10, context=_tls_context()) as response:
                body = response.read(4096).decode("utf-8", "replace")
                if response.status == 200:
                    return summarize_health(app, body)
                last = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = type(exc).__name__
        time.sleep(5)
    raise DeployError(f"{url} did not answer 200 within {HEALTH_TIMEOUT_SECONDS}s (last: {last}). Check `fly logs --app {app.name}`.")


def summarize_health(app: FlyApp, body: str) -> str:
    """A one-line summary of a health answer (the API's says which payment and compiler modes are live)."""
    if app.key != "api":
        return "ok"
    try:
        health = json.loads(body)
    except ValueError:
        return "ok"
    return f"ok, {health.get('payment_label', '?')}, compiler {health.get('compiler_mode', '?')}"


# ============================================================
# Entry point
# ============================================================


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Command-line flags (see the module docstring)."""
    parser = argparse.ArgumentParser(description="Deploy Handshake (store, api, mcp, web) to Fly.io.")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help=f"app name prefix (default {DEFAULT_PREFIX}); the website is <prefix>, the others <prefix>-api etc.")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"Fly region for every app and the volume (default {DEFAULT_REGION})")
    parser.add_argument("--org", default=DEFAULT_ORG, help=f"Fly organization that owns new apps (default {DEFAULT_ORG})")
    parser.add_argument("--google-client-id", help="the Google OAuth client id for 'Sign in with Google' (public; the api needs this or --allow-demo-login)")
    parser.add_argument("--allow-demo-login", action="store_true", help="turn on 'type any email' demo login in prod (HANDSHAKE_ALLOW_DEMO_LOGIN=true); anyone can then act as anyone")
    parser.add_argument("--only", action="append", choices=DEPLOY_ORDER, help="deploy only this app (repeatable); default: all four")
    parser.add_argument("--rotate", action="store_true", help="replace the API's generated secrets even if set (logs everyone out; see docs/DEPLOY.md)")
    parser.add_argument("--dry-run", action="store_true", help="print the fly commands without running any (secrets are never shown)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the deploy (or print the plan). Returns the process exit code."""
    args = parse_args(argv)
    if not PREFIX_PATTERN.match(args.prefix):
        print(f"--prefix {args.prefix!r}: use lowercase letters, digits and dashes (3-42 characters, starting with a letter).", file=sys.stderr)
        return 2
    selected = [key for key in DEPLOY_ORDER if not args.only or key in args.only]
    if "api" in selected and not (args.google_client_id or args.allow_demo_login):
        # The API refuses to start in prod without a way to log in, so fail
        # here rather than after a five-minute build.
        print("Deploying the api needs --google-client-id (or --allow-demo-login for a private smoke test).", file=sys.stderr)
        return 2
    if "api" in selected and args.allow_demo_login:
        print("warning: --allow-demo-login lets anyone log in as any email address. Redeploy without it once Google sign-in works.", file=sys.stderr)
    if args.google_client_id and not args.google_client_id.endswith(".apps.googleusercontent.com"):
        print("warning: that does not look like a Google OAuth client id (<id>.apps.googleusercontent.com).", file=sys.stderr)

    apps = build_apps(args.prefix)
    chosen = [apps[key] for key in selected]
    try:
        fly = Fly(find_fly() if not args.dry_run else (shutil.which("fly") or "fly"), args.dry_run)
        if args.dry_run:
            print("DRY RUN: nothing below is executed, and no fly command is run.")
        print(f"Deploying {', '.join(app.name for app in chosen)} to {args.region} (org {args.org}).")

        check_login(fly)
        ensure_apps(fly, chosen, args.org)
        if "api" in selected:
            ensure_volume(fly, apps["api"], args.region)
            set_secrets(fly, apps["api"], args.rotate)
        for app in chosen:
            deploy(fly, app, apps, args.region, args.google_client_id, args.allow_demo_login)

        print("\n== Smoke check")
        for app in chosen:
            if args.dry_run:
                print(f"  GET {app.url}{app.health_path}  (expect 200)")
                continue
            print(f"  {app.key:<5} {app.url}{app.health_path}: {smoke_check(app)}", flush=True)
    except DeployError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1

    print("\nHandshake on Fly.io:")
    print(f"  website  {apps['web'].url}")
    print(f"  api      {apps['api'].url}")
    print(f"  mcp      {apps['mcp'].url}/mcp")
    print(f"  store    {apps['store'].url}")
    if "api" in selected:
        print(f"\nGoogle sign-in: the OAuth client must list {apps['web'].url} as an authorized JavaScript origin.")
        print(f"Optional live compiler: fly secrets import --app {apps['api'].name}, paste OPENAI_API_KEY=<key>, Ctrl-D (docs/DEPLOY.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
