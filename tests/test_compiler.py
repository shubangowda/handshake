"""
test_compiler.py: the contract compiler, its lint, and the draft routes (section 5 and 6.3).

The OpenAI client is always a fake here (no network, no key). Each failure
mode must come back as a structured CompileError, never a half-made draft.
Lint rules are checked one by one. The API tests use the offline fixture
compiler, which is what runs when no OPENAI_API_KEY is configured.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from handshake import compiler, db
from handshake.compiler import CompileError, compile_intent, compile_with_openai, lint_draft
from handshake.config import override_settings
from handshake.models import CompilerOutput, ContractDraft
from conftest import draft_payload

NOW = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)  # a Saturday, noon in New York


# ------------------------------------------------------------
# A fake OpenAI client
# ------------------------------------------------------------


class FakeResponses:
    """Stands in for client.responses; records calls and does whatever `behavior` says."""

    def __init__(self, behavior: Any) -> None:
        """behavior: a response object to return, an exception to raise, or an async callable."""
        self.behavior = behavior
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        """Mimic AsyncOpenAI().responses.parse."""
        self.calls.append(kwargs)
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        if callable(self.behavior):
            return await self.behavior()
        return self.behavior


def fake_client(behavior: Any) -> SimpleNamespace:
    """An object with a .responses attribute, like AsyncOpenAI."""
    return SimpleNamespace(responses=FakeResponses(behavior))


def good_output() -> CompilerOutput:
    """What a well-behaved model returns: a valid draft with an LLM-chosen id and created_at."""
    draft = ContractDraft.model_validate({**draft_payload(), "id": "draft_chosen_by_llm", "created_at": "2020-01-01T00:00:00Z"})
    return CompilerOutput(draft=draft, assumptions=["size is US men's"], clarifications_needed=["Which color?"])


def run(coro: Any) -> Any:
    """Run a coroutine to completion (tests are synchronous)."""
    return asyncio.run(coro)


# ============================================================
# OpenAI compiler: success and every failure mode
# ============================================================


def test_success_and_server_overrides_id_and_created_at() -> None:
    """The server replaces the LLM's id and created_at, then re-validates through ContractDraft."""
    override_settings(openai_api_key="sk-test-not-real", compiler="openai")
    client = fake_client(SimpleNamespace(output_parsed=good_output(), output=[]))
    result = run(compile_intent("running shoes size 10 under $135", now=NOW, client=client))

    assert result.source == "openai"
    assert result.output.draft.id != "draft_chosen_by_llm"
    assert result.output.draft.id.startswith("draft_")
    assert result.output.draft.created_at == NOW
    assert result.output.clarifications_needed == ["Which color?"]


def test_request_carries_trusted_context_and_no_merchant_content() -> None:
    """The model gets COMPILER_PROMPT, today's date in the user's timezone, and the fenced intent."""
    override_settings(openai_api_key="sk-test-not-real", compiler="openai", compiler_model="gpt-test", compiler_service_tier=None)
    client = fake_client(SimpleNamespace(output_parsed=good_output(), output=[]))
    run(compile_intent("shoes by Friday", now=NOW, client=client))

    call = client.responses.calls[0]
    assert call["model"] == "gpt-test"
    assert "service_tier" not in call  # opt-in only
    assert call["text_format"] is CompilerOutput
    system, user = call["input"]
    from handshake.prompts import COMPILER_PROMPT

    assert system["content"] == COMPILER_PROMPT
    assert "2026-09-26T12:00:00-04:00" in user["content"]  # noon in New York
    assert "America/New_York" in user["content"]
    assert "<<<USER_REQUEST\nshoes by Friday\nUSER_REQUEST>>>" in user["content"]


def test_service_tier_is_passed_only_when_configured() -> None:
    """HANDSHAKE_COMPILER_SERVICE_TIER=flex is forwarded; unset means not sent."""
    override_settings(openai_api_key="sk-test-not-real", compiler="openai", compiler_service_tier="flex")
    client = fake_client(SimpleNamespace(output_parsed=good_output(), output=[]))
    run(compile_intent("shoes", now=NOW, client=client))
    assert client.responses.calls[0]["service_tier"] == "flex"


def test_refusal_is_a_structured_error() -> None:
    """A model refusal is reported, not turned into an empty draft."""
    refusal = SimpleNamespace(output_parsed=None, output=[SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="I can't help with that.")])])
    with pytest.raises(CompileError) as caught:
        run(compile_with_openai("buy something", NOW, fake_client(refusal)))
    assert caught.value.code == "refusal"


def test_missing_parsed_output_is_a_structured_error() -> None:
    """No parsed output means no draft."""
    with pytest.raises(CompileError) as caught:
        run(compile_with_openai("buy something", NOW, fake_client(SimpleNamespace(output_parsed=None, output=[]))))
    assert caught.value.code == "no_parsed_output"


def test_timeout_from_sdk_is_a_structured_error() -> None:
    """The SDK's timeout exception becomes code=timeout."""
    import httpx
    import openai

    error = openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    with pytest.raises(CompileError) as caught:
        run(compile_with_openai("buy something", NOW, fake_client(error)))
    assert caught.value.code == "timeout"


def test_hung_provider_hits_the_backstop_timeout() -> None:
    """Even if the SDK never returns, the compile gives up at the configured timeout (plus 1s)."""
    override_settings(compiler_timeout_seconds=0.05)

    async def hang() -> Any:
        """Never answers in time."""
        await asyncio.sleep(10)

    with pytest.raises(CompileError) as caught:
        run(compile_with_openai("buy something", NOW, fake_client(hang)))
    assert caught.value.code == "timeout"


def test_validation_error_is_a_structured_error() -> None:
    """Output that doesn't match the schema is invalid_output."""
    try:
        CompilerOutput.model_validate({})
    except ValidationError as exc:
        error = exc
    with pytest.raises(CompileError) as caught:
        run(compile_with_openai("buy something", NOW, fake_client(error)))
    assert caught.value.code == "invalid_output"


def test_provider_error_never_echoes_secrets() -> None:
    """A provider failure reports its type and status, not the request or the key."""
    boom = RuntimeError("upstream exploded with key sk-test-not-real")
    with pytest.raises(CompileError) as caught:
        run(compile_with_openai("buy something", NOW, fake_client(boom)))
    assert caught.value.code == "provider_error"
    assert "sk-" not in caught.value.message and "sk-" not in str(caught.value.details)


def test_missing_key_is_a_structured_error() -> None:
    """Calling the live compiler without a key says so explicitly."""
    override_settings(openai_api_key=None, compiler="openai")
    with pytest.raises(CompileError) as caught:
        run(compile_with_openai("buy something", NOW))
    assert caught.value.code == "missing_api_key"


def test_client_is_not_created_at_import() -> None:
    """Importing the app builds no OpenAI client and doesn't even import the SDK (no key needed to start)."""
    import os
    import subprocess
    import sys

    code = (
        "import sys; import handshake.api, handshake.compiler as c; "
        "assert c._client_cache == {}; assert 'openai' not in sys.modules; print('ok')"
    )
    env = {**os.environ, "OPENAI_API_KEY": ""}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert result.stdout.strip() == "ok", result.stderr


# ============================================================
# Fixture compiler
# ============================================================


def test_fixture_compiler_returns_the_demo_contract() -> None:
    """With no key, the demo intent compiles to the exact section 12.1 contract, labeled as fixture."""
    override_settings(openai_api_key=None, compiler="openai")  # no key -> fixture fallback
    result = run(compile_intent(compiler.DEMO_INTENT, now=NOW))
    draft = result.output.draft

    assert result.source == "fixture"
    assert compiler.FIXTURE_NOTE in result.output.compiler_notes
    assert draft.goal == "Buy Nike Pegasus 41 running shoes"
    assert draft.category == "running_shoes"
    assert (draft.spend.currency, draft.spend.target, draft.spend.hard_cap_all_in) == ("USD", 120, 135)
    assert draft.delivery.max_shipping == 10
    # 3 days from Saturday Sep 26 in New York = end of Tuesday Sep 29, New York time.
    assert draft.delivery.deliver_by == datetime(2026, 9, 30, 3, 59, 59, tzinfo=timezone.utc)
    assert draft.terms.min_return_days is None
    assert draft.merchants.allow == ["Amazon.com"]
    assert [(c.field, c.operator.value, c.value) for c in draft.constraints] == [
        ("size", "eq", "10"),
        ("condition", "in", ["new"]),
        ("product_name", "contains", "Pegasus 41"),
        ("quantity", "eq", 1),
    ]
    assert all(c.severity.value == "hard" and c.source.value == "user" for c in draft.constraints)


def test_fixture_compiler_refuses_other_intents() -> None:
    """Anything but the demo request gets a structured 'not configured' error, not a guess."""
    override_settings(compiler="fixture")
    with pytest.raises(CompileError) as caught:
        run(compile_intent("a 65 inch OLED TV", now=NOW))
    assert caught.value.code == "live_compiler_not_configured"
    assert caught.value.details["clarifications_needed"]


def test_empty_intent_is_rejected() -> None:
    """An empty request compiles to nothing."""
    with pytest.raises(CompileError) as caught:
        run(compile_intent("   ", now=NOW))
    assert caught.value.code == "empty_intent"


# ============================================================
# Lint, one rule at a time
# ============================================================


def draft_with(**changes: Any) -> ContractDraft:
    """The fixture draft with top-level fields replaced (deep keys via dicts)."""
    data = {**draft_payload(), **changes}
    return ContractDraft.model_validate(data)


def lint_errors(draft: ContractDraft) -> list[str]:
    """The blocking errors lint reports at NOW."""
    return lint_draft(draft, NOW).errors


def test_clean_draft_has_no_lint_errors() -> None:
    """The fixture draft lints clean."""
    assert lint_errors(draft_with()) == []


def test_lint_deadline_in_the_past() -> None:
    """A delivery deadline that already passed blocks signing."""
    errors = lint_errors(draft_with(delivery={"deliver_by": "2026-09-01T00:00:00Z"}))
    assert any("deadline is already in the past" in e for e in errors)


def test_lint_expiry_in_the_past() -> None:
    """An expires_at before now blocks signing."""
    errors = lint_errors(draft_with(created_at="2026-09-01T00:00:00Z", expires_at="2026-09-02T00:00:00Z"))
    assert any("expiry time is already in the past" in e for e in errors)


def test_lint_naive_datetime_gets_user_timezone_and_a_note() -> None:
    """A deadline with no timezone is read in the user's timezone, and the note says so."""
    report = lint_draft(draft_with(delivery={"deliver_by": "2026-10-10T23:59:59"}), NOW)
    assert report.errors == []
    assert report.draft.delivery.deliver_by.utcoffset() == timedelta(hours=-4)  # New York in October
    assert any("interpreted in America/New_York" in n for n in report.notes)


def test_lint_unsupported_currency() -> None:
    """Only currencies the payment rail supports are allowed."""
    errors = lint_errors(draft_with(spend={"currency": "EUR", "hard_cap_all_in": 135}))
    assert any("EUR is not supported" in e for e in errors)
    errors = lint_errors(draft_with(spend={"currency": "US1", "hard_cap_all_in": 135}))
    assert any("not a three-letter currency code" in e for e in errors)


@pytest.mark.parametrize(
    "constraint, fragment",
    [
        ({"field": "refresh_rate_hz", "operator": "lt", "value": [1, 2]}, "needs a single number"),
        ({"field": "refresh_rate_hz", "operator": "gte", "value": "fast"}, "needs a single number"),
        ({"field": "condition", "operator": "in", "value": "new"}, "should list the allowed values"),
        ({"field": "delivery_date", "operator": "before", "value": "soon"}, "needs a date"),
        ({"field": "color", "operator": "eq", "value": None}, "has no value"),
    ],
)
def test_lint_operator_value_mismatch(constraint: dict[str, Any], fragment: str) -> None:
    """Operators that can't work with their value would be UNVERIFIABLE forever; lint catches them first."""
    assert any(fragment in e for e in lint_errors(draft_with(constraints=[constraint])))


@pytest.mark.parametrize(
    "constraints",
    [
        [{"field": "size", "operator": "eq", "value": 10}, {"field": "size", "operator": "eq", "value": 11}],
        [{"field": "size", "operator": "eq", "value": "10"}, {"field": "size", "operator": "neq", "value": 10}],
        [{"field": "color", "operator": "eq", "value": "black"}, {"field": "color", "operator": "not_in", "value": ["black"]}],
        [{"field": "color", "operator": "eq", "value": "red"}, {"field": "color", "operator": "in", "value": ["black", "white"]}],
    ],
)
def test_lint_contradictory_constraints(constraints: list[dict[str, Any]]) -> None:
    """Rules that can never all be true block signing."""
    assert lint_errors(draft_with(constraints=constraints)) != []


def test_lint_merchant_allowed_and_denied_case_insensitively() -> None:
    """models.py only catches exact-case overlaps; lint catches 'Mock Nike' vs 'mock nike'."""
    errors = lint_errors(draft_with(merchants={"allow": ["Mock Nike"], "deny": ["mock nike"]}))
    assert any("both allowed and denied" in e for e in errors)


def test_lint_required_merchant_that_is_denied() -> None:
    """A merchant_name eq rule for a denied merchant is a contradiction."""
    errors = lint_errors(draft_with(merchants={"deny": ["Shady Shoes"]}, constraints=[{"field": "merchant_name", "operator": "eq", "value": "shady shoes"}]))
    assert any("also on the deny list" in e for e in errors)


def test_lint_blank_goal() -> None:
    """A goal of only whitespace is not a goal."""
    assert any("no goal" in e for e in lint_errors(draft_with(goal="   ")))


def test_merge_lint_replaces_old_entries() -> None:
    """Re-running lint after a fix removes the old 'lint:' entries but keeps the LLM's own questions."""
    meta = {"clarifications_needed": ["Which color?", "lint: old problem"], "compiler_notes": ["lint-note: old note", "keep"]}
    report = lint_draft(draft_with(), NOW)
    merged = compiler.merge_lint(meta, report)
    assert merged["clarifications_needed"] == ["Which color?"]
    assert merged["compiler_notes"] == ["keep"]


# ============================================================
# Draft routes (fixture compiler, through the real API)
# ============================================================


def compile_demo(client: TestClient) -> dict[str, Any]:
    """Compile the demo intent through the API (the fixture compiler runs: no key in tests)."""
    response = client.post("/drafts/compile", json={"intent": compiler.DEMO_INTENT})
    assert response.status_code == 201, response.json()
    return response.json()


def test_compile_persists_a_draft_record(client: TestClient) -> None:
    """POST /drafts/compile stores the draft and returns Rohan's DraftRecord plus review_url."""
    record = compile_demo(client)
    assert record["status"] == "draft"
    assert record["compiler_source"] == "fixture"
    assert record["review_url"].endswith(f"/contracts/{record['id']}")
    assert record["blocking_issues"] == []
    assert record["previous_contract_id"] is None
    assert client.get(f"/drafts/{record['id']}").json()["goal"] == record["goal"]
    assert [d["id"] for d in client.get("/drafts").json()] == [record["id"]]
    with db.SessionLocal() as session:
        assert db.get_draft_row(session, record["id"]).owner == "demo@handshake.dev"


def test_agent_can_compile_but_not_sign(client: TestClient, agent_client: TestClient) -> None:
    """The agent may draft; only the user may sign what it drafted."""
    record = compile_demo(agent_client)
    assert agent_client.post(f"/contracts/{record['id']}/sign").status_code == 403
    assert client.post(f"/contracts/{record['id']}/sign").status_code == 200


def test_compile_failure_creates_nothing(client: TestClient) -> None:
    """A failed compile returns a structured error and stores no draft."""
    response = client.post("/drafts/compile", json={"intent": "a 65 inch OLED TV"})
    assert response.status_code == 422
    assert response.json()["error"] == "live_compiler_not_configured"
    assert response.json()["details"]["draft_created"] is False
    assert client.get("/drafts").json() == []


def test_patch_marks_edits_as_user_and_relints(client: TestClient) -> None:
    """Edited values become source=user; a bad edit shows up as a blocking issue."""
    record = compile_demo(client)
    patched = client.patch(f"/drafts/{record['id']}", json={"hard_cap_all_in": 140, "target": 110}).json()
    assert patched["spend"]["hard_cap_all_in"] == 140
    assert patched["spend"]["hard_cap_source"] == "user"
    assert patched["spend"]["target_source"] == "user"

    bad = client.patch(f"/drafts/{record['id']}", json={"deliver_by": "2020-01-01T00:00:00Z"}).json()
    assert any("deadline is already in the past" in issue for issue in bad["blocking_issues"])
    assert any(c.startswith("lint: ") for c in bad["clarifications_needed"])


def test_draft_with_blocking_issues_cannot_be_signed(client: TestClient) -> None:
    """Sign returns 409 draft_has_blocking_issues until lint is clean."""
    record = compile_demo(client)
    client.patch(f"/drafts/{record['id']}", json={"deliver_by": "2020-01-01T00:00:00Z"})
    response = client.post(f"/contracts/{record['id']}/sign")
    assert response.status_code == 409
    assert response.json()["error"] == "draft_has_blocking_issues"

    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    fixed = client.patch(f"/drafts/{record['id']}", json={"deliver_by": future}).json()
    assert fixed["blocking_issues"] == []
    assert client.post(f"/contracts/{record['id']}/sign").status_code == 200


def test_patch_rejects_unknown_fields_and_signed_drafts(client: TestClient) -> None:
    """Only DraftPatch fields can change, and never on a draft that is already signed."""
    record = compile_demo(client)
    assert client.patch(f"/drafts/{record['id']}", json={"spend": {}}).status_code == 422
    client.post(f"/contracts/{record['id']}/sign")
    assert client.patch(f"/drafts/{record['id']}", json={"goal": "x"}).status_code == 409


def test_agent_cannot_patch_or_amend(client: TestClient, agent_client: TestClient) -> None:
    """Editing the rules is a user action."""
    record = compile_demo(client)
    assert agent_client.patch(f"/drafts/{record['id']}", json={"hard_cap_all_in": 1000}).status_code == 403
    contract_id = client.post(f"/contracts/{record['id']}/sign").json()["id"]
    assert agent_client.post(f"/contracts/{contract_id}/amend").status_code == 403


def test_amend_then_sign_replaces_the_old_version_atomically(client: TestClient) -> None:
    """Amending creates a draft pointing back; signing it revokes the old contract and links the new one."""
    old_id = client.post(f"/contracts/{compile_demo(client)['id']}/sign").json()["id"]

    amended = client.post(f"/contracts/{old_id}/amend").json()
    assert amended["status"] == "draft" and amended["previous_contract_id"] == old_id
    assert client.get(f"/contracts/{old_id}").json()["status"] == "active"  # still in force until signed

    new_contract = client.post(f"/contracts/{amended['id']}/sign").json()
    assert new_contract["previous_contract_id"] == old_id
    assert client.get(f"/contracts/{old_id}").json()["status"] == "revoked"
    with db.SessionLocal() as session:
        last = db.list_evidence_for_contract(session, old_id)[-1].data
    assert last["event_type"] == "contract_revoked"
    assert last["data"]["kind"] == "contract_amended"
    assert last["data"]["replaced_by"] == new_contract["id"]


def test_raw_draft_posting_is_also_linted(client: TestClient) -> None:
    """POST /contracts drafts go through the same lint as compiled ones."""
    response = client.post("/contracts", json={**draft_payload(), "delivery": {"deliver_by": "2020-01-01T00:00:00Z"}})
    assert response.status_code == 201
    assert response.json()["blocking_issues"]
