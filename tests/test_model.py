"""Mocked OpenRouter requests and spending controls."""

import asyncio
import copy
import json
import time

import httpx
import pytest

from benchmark.audit import AuditTrail
from benchmark.budgets import load_draft
from benchmark.costs import CostAccountingError, Ledger, Prices, SpendingLimit
from benchmark.model import ContextLimit, ModelAPIError, OpenRouterModel, input_tokens
from tests.support import completion


@pytest.fixture
def model_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "benchmark.model.preflight", lambda config, key: Prices.from_config(config)
    )
    monkeypatch.setattr("benchmark.model.input_tokens", lambda *args: 128)
    counter = 0

    def create(handler, config=None):
        nonlocal counter
        counter += 1
        config = copy.deepcopy(config or load_draft())
        config["live"]["retry_initial_seconds"] = 0
        config["live"]["retry_max_seconds"] = 0
        ledger = Ledger(tmp_path / f"spending-{counter}.sqlite3", 10, 2)
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


def generate(model, messages=None):
    async def run():
        await model.check_route()
        return await model.generate(
            messages or [{"role": "user", "content": "Synthetic record"}],
            [],
            time.monotonic() + 5,
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
