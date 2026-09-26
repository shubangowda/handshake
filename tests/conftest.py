"""
conftest.py: shared pytest fixtures and sample data.

pytest automatically loads this file before running the tests in this
folder. It provides:
  - draft_payload() / proposal_payload(): the team's demo contract (running
    shoes, $135 all-in cap, Mock Nike) and a checkout that satisfies it
    (119.99 + 8.40 tax = 128.39).
  - make_contract() / make_proposal(): the same data as Pydantic models, for
    pure engine tests that never touch the database.
  - test_db / client: a fresh temporary SQLite database per test and a
    FastAPI TestClient wired to it, for end-to-end API tests.
"""

from __future__ import annotations

import copy
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

# Make `import handshake...` work even without `pip install -e .`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Test-only environment, set BEFORE any handshake module loads its settings:
#   - never read a developer's personal .env (results must not depend on it)
#   - fixed, distinct HMAC secrets (and it silences the dev-secret warning)
os.environ["HANDSHAKE_SKIP_DOTENV"] = "1"
os.environ.setdefault("HANDSHAKE_SIGNING_SECRET", "test-secret")
os.environ.setdefault("HANDSHAKE_SESSION_SECRET", "test-session-secret")
# The shopping agent's static token, and the user it acts for.
os.environ["HANDSHAKE_AGENT_TOKEN"] = "test-agent-token"
os.environ["HANDSHAKE_AGENT_OWNER"] = "demo@handshake.dev"
os.environ["HANDSHAKE_AGENT_ID"] = "agent_demo"

from handshake import db  # noqa: E402  (must come after the environment setup)
from handshake.models import Contract, ContractDraft, TransactionProposal  # noqa: E402

# The logged-in test user. The agent (above) acts for this same user, so the
# agent can see the user's records; OTHER_USER is a stranger.
TEST_USER = "demo@handshake.dev"
OTHER_USER = "stranger@example.com"
AGENT_TOKEN = "test-agent-token"

# Every engine test evaluates at this exact moment, so results never depend
# on when the tests happen to run.
EVAL_TIME = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

# A function that edits a raw proposal dict in place (used to build variants).
Mutator = Callable[[dict[str, Any]], Any]


def draft_payload() -> dict[str, Any]:
    """The team's demo contract draft as raw JSON: running shoes, $135 all-in, Mock Nike."""
    return {
        "id": "draft_demo_shoes",
        "goal": "buy running shoes",
        "category": "shoes",
        "spend": {"currency": "USD", "target": 120.00, "hard_cap_all_in": 135.00},
        "delivery": {"deliver_by": "2026-10-10T23:59:59Z", "max_shipping": 10.00},
        "terms": {"no_subscription": True, "no_membership": True, "no_addons": True},
        "merchants": {
            "allow": ["Mock Nike"],
            "deny": [],
            "seller_requirement": "first_party_or_verified",
            "new_merchant": "escalate",
        },
        "constraints": [
            {"field": "size", "operator": "eq", "value": 10, "severity": "hard"},
            {"field": "condition", "operator": "in", "value": ["new"], "severity": "hard"},
        ],
        "created_at": "2026-09-26T10:00:00Z",
    }


def api_draft_payload() -> dict[str, Any]:
    """
    The demo draft for API tests, with a ROLLING deadline 14 days from now.

    Signing re-runs lint, and lint refuses a deadline in the past. With the
    fixed 2026-10-10 date the API tests would start failing on 2026-10-11.
    The engine tests keep the fixed dates (they evaluate at a fixed EVAL_TIME).
    """
    payload = draft_payload()
    deadline = datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=14)
    payload["delivery"] = {**payload["delivery"], "deliver_by": deadline.isoformat()}
    return payload


def proposal_payload(contract_id: str = "contract_demo") -> dict[str, Any]:
    """A raw checkout proposal that satisfies every rule of the demo contract (total 128.39)."""
    return {
        "id": "proposal_demo_pass",
        "contract_id": contract_id,
        "merchant": {
            "name": "Mock Nike",
            "domain": "mocknike.example",
            "merchant_id": "m_nike",
            "is_first_party": True,
            "is_verified": True,
        },
        "line_items": [
            {
                "name": "Pegasus 41 Running Shoe",
                "category": "shoes",
                "brand": "Nike",
                "condition": "new",
                "quantity": 1,
                "unit_price": 119.99,
                # Size arrives as the STRING "10"; the contract says integer 10.
                "attributes": {"size": "10", "color": "black"},
            }
        ],
        "item_subtotal": 119.99,
        "tax": 8.40,
        "shipping": 0.00,
        "fees": 0.00,
        "total": 128.39,
        "currency": "USD",
        # The three terms flags are sent EXPLICITLY, as the extractor must.
        "recurring_billing": {"detected": False},
        "addons_detected": False,
        "membership_detected": False,
        "delivery": {"promised_by": "2026-10-08T18:00:00Z", "carrier": "UPS", "verified": True},
        "return_terms": {"returnable": True, "return_window_days": 30},
        "extractor_ids": ["dom_extractor"],
    }


def make_contract(**overrides: Any) -> Contract:
    """A signed Contract model for pure engine tests. Hash and signature are placeholders."""
    draft = ContractDraft.model_validate(draft_payload())
    data = draft.model_dump(exclude={"id"})
    data.update(
        id="contract_demo",
        signed_at=datetime(2026, 9, 26, 10, 5, tzinfo=timezone.utc),
        contract_hash="test-hash",
        signature="test-signature",
    )
    data.update(overrides)
    return Contract.model_validate(data)


def make_proposal(mutate: Mutator | None = None) -> tuple[TransactionProposal, dict[str, Any]]:
    """Parse the passing proposal (optionally edited by `mutate` first). Returns (model, raw dict)."""
    raw = proposal_payload()
    if mutate is not None:
        mutate(raw)
    return TransactionProposal.model_validate(raw), raw


@pytest.fixture
def contract() -> Contract:
    """The demo contract as a model."""
    return make_contract()


@pytest.fixture
def passing_proposal() -> TransactionProposal:
    """The passing proposal as a model."""
    return make_proposal()[0]


@pytest.fixture
def draft_json() -> dict[str, Any]:
    """A fresh copy of the draft JSON that a test may modify freely."""
    return copy.deepcopy(api_draft_payload())


@pytest.fixture
def test_db(tmp_path: Path) -> Iterator[Any]:
    """Point the app at a brand-new SQLite file for this test only; never touches the dev database."""
    db.configure(f"sqlite:///{tmp_path / 'test.db'}")
    db.create_tables()
    yield db
    db.engine.dispose()


def bearer(token: str) -> dict[str, str]:
    """The Authorization header for a token."""
    return {"Authorization": f"Bearer {token}"}


def user_headers(email: str = TEST_USER) -> dict[str, str]:
    """Authorization header carrying a fresh demo login token for `email`."""
    from handshake.auth import issue_user_token

    token, _ = issue_user_token(email)
    return bearer(token)


# The extractor API tests use (section 7.6). POST /purchases no longer takes
# a proposal from the caller, so tests choose what "the checkout" contains by
# loading this StaticExtractor, which the api_app fixture installs through a
# FastAPI dependency override.
from handshake.extractor import StaticExtractor  # noqa: E402

STATIC_EXTRACTOR = StaticExtractor()


@pytest.fixture
def api_app(test_db: Any) -> Any:
    """A fresh FastAPI app built from the current settings, reading checkouts through STATIC_EXTRACTOR."""
    from handshake.api import create_app
    from handshake.extractor import get_extractor

    STATIC_EXTRACTOR.proposal = None
    STATIC_EXTRACTOR.calls.clear()
    app = create_app()
    app.dependency_overrides[get_extractor] = lambda: STATIC_EXTRACTOR
    return app


@pytest.fixture
def client(api_app: Any) -> Iterator[Any]:
    """A TestClient logged in as TEST_USER (every existing API test runs as the user)."""
    from fastapi.testclient import TestClient

    with TestClient(api_app, headers=user_headers()) as test_client:
        yield test_client


@pytest.fixture
def agent_client(api_app: Any) -> Iterator[Any]:
    """A TestClient authenticated as the shopping agent (acts for TEST_USER)."""
    from fastapi.testclient import TestClient

    with TestClient(api_app, headers=bearer(AGENT_TOKEN)) as test_client:
        yield test_client


@pytest.fixture
def stranger_client(api_app: Any) -> Iterator[Any]:
    """A TestClient logged in as a different user, who must not see TEST_USER's records."""
    from fastapi.testclient import TestClient

    with TestClient(api_app, headers=user_headers(OTHER_USER)) as test_client:
        yield test_client


@pytest.fixture
def anon_client(api_app: Any) -> Iterator[Any]:
    """A TestClient with no credentials at all."""
    from fastapi.testclient import TestClient

    with TestClient(api_app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    """Reload settings from the (test) environment before each test and after it, so overrides never leak."""
    from handshake import config

    config.reset_settings()
    yield
    config.reset_settings()
