"""Mocked OpenRouter requests and spending controls."""

import asyncio
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from benchmark.audit import AuditTrail
from benchmark.budgets import load_draft
from benchmark.costs import CostAccountingError, Ledger, Prices, SpendingLimit
from benchmark.model import (
    ContextLimit,
    ModelAPIError,
    OpenRouterModel,
    error_details,
    input_tokens,
    retry_after,
)
from tests.support import completion


@pytest.fixture
def model_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "benchmark.model.preflight", lambda config, key: Prices.from_config(config)
    )
    monkeypatch.setattr("benchmark.model.input_tokens", lambda *args: 128)
    counter = 0

    def create(handler, config=None, *, attempt_limit=2):
        nonlocal counter
        counter += 1
        config = copy.deepcopy(config or load_draft())
        config["live"]["retry_initial_seconds"] = 0
        config["live"]["retry_max_seconds"] = 0
        ledger = Ledger(tmp_path / f"spending-{counter}.sqlite3", 10, attempt_limit)
        audit = AuditTrail(
            "synthetic-run", "synthetic-sample", tmp_path / f"audit-{counter}.jsonl"
        )
        return OpenRouterModel(
            config,
            "synthetic-key",
            ledger,
            "synthetic-run",
            audit,
            transport=httpx.MockTransport(handler),
        )

    return create


def generate(model, messages=None, *, deadline_seconds=5):
    async def run():
        await model.check_route()
        return await model.generate(
            messages or [{"role": "user", "content": "Synthetic record"}],
            [],
            time.monotonic() + deadline_seconds,
        )

    return asyncio.run(run())


def test_generation_requires_preflight(model_factory):
    model = model_factory(lambda request: pytest.fail("An unverified request was sent"))
    with pytest.raises(CostAccountingError, match="preflight"):
        asyncio.run(model.generate([], [], time.monotonic() + 5))
    assert model.ledger.totals()["held_usd"] == 0


def test_request_is_reserved_and_routed_before_response(model_factory):
    model = None

    def reply(request):
        assert model is not None
        assert model.ledger.totals()["held_usd"] > 0
        body = json.loads(request.content)
        assert body["provider"]["only"] == ["azure"]
        assert body["provider"]["allow_fallbacks"] is False
        assert body["max_completion_tokens"] == 16384
        assert "max_tokens" not in body
        assert body["temperature"] == 0
        return httpx.Response(200, json=completion())

    model = model_factory(reply)
    result = generate(model)
    assert result["cost_usd"] == 0.001
    assert result["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cached_tokens": 0,
    }
    assert model.ledger.totals()["billed_usd"] == 0.001
    assert model.ledger.totals()["held_usd"] == 0
    assert model.audit.items[-1]["status"] == "settled"


@pytest.mark.parametrize(
    "defect", ["billing", "provider", "byok", "tokens", "cached", "choices"]
)
def test_invalid_billing_or_response_is_not_delivered(model_factory, defect):
    data = completion()
    if defect == "billing":
        del data["usage"]["cost"]
    elif defect == "provider":
        data["provider"] = "Unexpected provider"
    elif defect == "byok":
        data["usage"]["is_byok"] = True
    elif defect == "tokens":
        data["usage"]["prompt_tokens"] = True
    elif defect == "cached":
        data["usage"]["prompt_tokens_details"] = {"cached_tokens": 101}
    else:
        data["choices"] = []
    model = model_factory(lambda request: httpx.Response(200, json=data))
    with pytest.raises((CostAccountingError, ModelAPIError)):
        generate(model)
    totals = model.ledger.totals()
    assert (
        totals["held_usd"] > 0
        if defect in ("billing", "provider", "byok")
        else totals["billed_usd"] > 0
    )


@pytest.mark.parametrize("failure", ["timeout", "status"])
def test_each_retry_has_a_hold_and_keeps_uncertain_charge(model_factory, failure):
    count = 0

    def reply(request):
        nonlocal count
        count += 1
        if count == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("Synthetic timeout")
            return httpx.Response(503)
        return httpx.Response(200, json=completion())

    model = model_factory(reply)
    generate(model)
    assert count == 2
    assert model.ledger.totals()["held_usd"] > 0
    assert model.ledger.totals()["billed_usd"] == 0.001
    assert len(model.audit.items) == 2


def test_nontransient_error_is_not_retried(model_factory):
    model = model_factory(lambda request: httpx.Response(400))
    with pytest.raises(ModelAPIError):
        generate(model)
    assert len(model.audit.items) == 1
    assert model.ledger.totals()["held_usd"] > 0


def test_spending_cap_prevents_another_request(model_factory):
    count = 0

    def reply(request):
        nonlocal count
        count += 1
        return httpx.Response(503)

    model = model_factory(reply)
    with pytest.raises(SpendingLimit):
        generate(model)
    assert count == 4
    assert model.ledger.totals()["held_usd"] < 2


def test_cancelled_request_keeps_its_hold(model_factory):
    async def reply(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json=completion())

    model = model_factory(reply)

    async def run():
        await model.check_route()
        task = asyncio.create_task(model.generate([], [], time.monotonic() + 30))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert model.ledger.totals()["held_usd"] > 0
    assert model.audit.items[-1]["error_type"] == "CancelledError"


def test_context_limit_does_not_send_or_truncate_a_request(model_factory, monkeypatch):
    model = model_factory(lambda request: pytest.fail("Oversized context was sent"))
    monkeypatch.setattr(
        "benchmark.model.input_tokens", lambda *args: model.prices.context_tokens
    )
    with pytest.raises(ContextLimit):
        generate(model)
    assert model.ledger.totals()["held_usd"] == 0


def test_usage_above_old_run_token_limit_is_recorded(model_factory):
    model = model_factory(
        lambda request: httpx.Response(200, json=completion(prompt_tokens=70000))
    )
    generate(model)
    assert model.usage["prompt_tokens"] == 70000


def test_deadline_stops_before_reserving(model_factory):
    model = model_factory(lambda request: pytest.fail("Expired request was sent"))

    async def run():
        await model.check_route()
        await model.generate([], [], time.monotonic() - 1)

    with pytest.raises(TimeoutError):
        asyncio.run(run())
    assert model.ledger.totals()["held_usd"] == 0


def test_token_estimate_accepts_literal_special_token_text():
    assert (
        input_tokens(
            [{"role": "user", "content": "Synthetic <|endoftext|> text"}],
            [],
            "openrouter/openai/gpt-4.1-mini",
        )
        > 0
    )


@pytest.mark.parametrize(
    "model_id",
    ["mistralai/mistral-large-4-0", "qwen/qwen3.8-flash", "z-ai/glm-5.3"],
)
def test_reasoning_profile_request_and_billing(model_factory, model_id):
    config = load_draft(model=model_id)
    message = {
        "role": "assistant",
        "content": None,
        "reasoning_details": [
            {"type": "reasoning.text", "text": "Synthetic reasoning."}
        ],
    }
    requests = []

    def reply(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == model_id
        assert body["provider"]["only"] == [config["live"]["provider"]]
        assert body["reasoning"] == {"enabled": True}
        assert body["temperature"] == 0.6
        assert body["max_tokens"] == config["budgets"]["agent_max_output_tokens"]
        assert "max_completion_tokens" not in body
        data = completion()
        data["provider"] = config["live"]["provider_name"]
        data["choices"][0]["message"] = message
        return httpx.Response(200, json=data)

    model = model_factory(reply, config)
    first = generate(model)
    generate(model, [{"role": "user", "content": "Synthetic record"}, first["message"]])
    assert requests[1]["messages"][1] == message
    assert model.ledger.totals()["billed_usd"] == 0.002


def test_unknown_model_requires_explicit_token_estimate():
    assert (
        input_tokens(
            [{"role": "user", "content": "Synthetic record"}],
            [],
            "qwen/qwen3.8-flash",
            "o200k_base",
        )
        > 0
    )
    with pytest.raises(CostAccountingError):
        input_tokens([], [], "unknown/model")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("12", 12),
        ("0", 0),
        ("-1", None),
        (None, None),
        ("invalid", None),
        ("nan", None),
        ("inf", None),
    ],
)
def test_retry_after_header(value, expected):
    assert retry_after(value) == expected


def test_retry_after_accepts_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert retry_after(format_datetime(future, usegmt=True)) == pytest.approx(60, abs=2)


def test_error_diagnostics_redact_credentials_and_keep_correlation_ids():
    response = httpx.Response(
        429,
        headers={"X-Request-Id": "request-123", "Retry-After": "12"},
        json={
            "id": "gen-synthetic-error",
            "error": {
                "code": 429,
                "message": "Rejected synthetic-key and Bearer other-secret",
                "metadata": {
                    "provider_name": "Azure",
                    "raw": "sk-or-v1-another-key",
                    "limit_source": "openrouter_in_flight_budget",
                    "ignored": "private",
                },
            },
        },
    )
    details = error_details(response, "synthetic-key")
    encoded = json.dumps(details)
    assert all(
        secret not in encoded
        for secret in (
            "synthetic-key",
            "other-secret",
            "sk-or-v1-another-key",
            "private",
        )
    )
    assert details["generation_id"] == "gen-synthetic-error"
    assert details["provider_request_id"] == "request-123"
    assert details["retry_after_seconds"] == 12
    assert details["api_error"]["provider_name"] == "Azure"
    assert details["api_error"]["limit_source"] == "openrouter_in_flight_budget"
    response.headers["X-Generation-Id"] = "gen-conflicting"
    assert "generation_id" not in error_details(response, "synthetic-key")


def test_confirmed_error_charges_release_holds_before_retry(model_factory, monkeypatch):
    config = load_draft(model="mistralai/mistral-large-4-0")
    config["budgets"]["model_retries"] = 4
    calls = 0
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("benchmark.model.asyncio.sleep", sleep)

    def reply(request):
        nonlocal calls
        if request.method == "GET":
            assert request.url.path == "/api/v1/generation"
            assert request.url.params["id"] == f"gen-synthetic-{calls}"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": f"gen-synthetic-{calls}",
                        "model": "mistralai/mistral-large-4-0",
                        "provider_name": None,
                        "is_byok": False,
                        "finish_reason": "error",
                        "total_cost": 0,
                    }
                },
            )
        calls += 1
        if calls <= 4:
            return httpx.Response(
                429,
                headers={"Retry-After": "12"},
                json={
                    "id": f"gen-synthetic-{calls}",
                    "error": {"code": 429, "message": "Rate limited"},
                },
            )
        data = completion()
        data["provider"] = config["live"]["provider_name"]
        return httpx.Response(200, json=data)

    model = model_factory(reply, config, attempt_limit=3)
    generate(model, deadline_seconds=60)
    assert calls == 5
    assert delays == [12] * 4
    assert model.ledger.totals()["held_usd"] == 0
    assert model.ledger.totals()["billed_usd"] == 0.001
    assert all(
        entry["billing_status"] == "confirmed" for entry in model.audit.items[:4]
    )


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "id",
        "model",
        "provider",
        "byok",
        "unfinished",
        "cost",
        "missing",
        "timeout",
        "malformed",
    ],
)
def test_error_lookup_requires_confirmed_matching_billing(model_factory, defect):
    config = load_draft()
    config["budgets"]["model_retries"] = 0
    requests = []
    metadata = {
        "id": "gen-synthetic-error",
        "model": "openai/gpt-4.1-mini",
        "provider_name": config["live"]["provider_name"],
        "is_byok": False,
        "finish_reason": "error",
        "total_cost": 0.007,
    }
    field_changes = {
        "id": ("id", "gen-other"),
        "model": ("model", "another/model"),
        "provider": ("provider_name", "Unexpected provider"),
        "byok": ("is_byok", True),
        "unfinished": ("finish_reason", None),
        "cost": ("total_cost", None),
    }
    if defect in field_changes:
        field, value = field_changes[defect]
        metadata[field] = value

    def reply(request):
        requests.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                503,
                headers={"X-Generation-Id": "gen-synthetic-error"},
                json={"error": {"message": "Provider unavailable"}},
            )
        if defect == "missing":
            return httpx.Response(404)
        if defect == "timeout":
            raise httpx.ReadTimeout("Synthetic lookup timeout")
        return httpx.Response(
            200, json=[] if defect == "malformed" else {"data": metadata}
        )

    model = model_factory(reply, config)
    with pytest.raises(ModelAPIError):
        generate(model)
    assert requests == ["POST", "GET"]
    totals = model.ledger.totals()
    entry = model.audit.items[-1]
    assert entry["generation_id"] == "gen-synthetic-error"
    assert entry["api_error"]["message"] == "Provider unavailable"
    if defect is None:
        assert totals["billed_usd"] == 0.007
        assert totals["held_usd"] == 0
        assert entry["billing_status"] == "confirmed"
    else:
        assert totals["billed_usd"] == 0
        assert totals["held_usd"] > 0
        assert entry["billing_status"] == "unconfirmed"


def test_retry_after_cannot_extend_attempt_deadline(model_factory):
    model = model_factory(
        lambda request: httpx.Response(429, headers={"Retry-After": "60"})
    )
    with pytest.raises(TimeoutError, match="backoff"):
        generate(model)
    assert len(model.audit.items) == 1
    assert model.audit.items[0]["retry_wait_seconds"] == 60
    assert model.ledger.totals()["held_usd"] > 0
