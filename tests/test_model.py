"""Mocked OpenRouter requests and spending controls."""

import asyncio
import copy
import json
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from benchmark.audit import AuditTrail
from benchmark.budgets import AttemptClock, load_draft
from benchmark.costs import (
    CostAccountingError,
    Ledger,
    Prices,
    SpendingLimit,
    microdollars,
)
from benchmark.model import (
    ContextLimit,
    ModelAPIError,
    OpenRouterModel,
    error_details,
    input_tokens,
    pre_inference_rejection,
    retry_after,
)
from tests.support import TEST_MODEL, completion


@pytest.fixture
def model_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "benchmark.model.preflight", lambda config, key: Prices.from_config(config)
    )
    monkeypatch.setattr("benchmark.model.input_tokens", lambda *args: 128)
    counter = 0

    def create(handler, config=None, *, holds=4.5, attempt_limit=...):
        """attempt_limit defaults to `holds` requests' worth of reservations."""
        nonlocal counter
        counter += 1
        config = copy.deepcopy(config or load_draft(model=TEST_MODEL))
        if attempt_limit is ...:
            output = config["budgets"]["agent_max_output_tokens"]
            attempt_limit = holds * float(
                Prices.from_config(config).reservation(output)
            )
        config["requests"]["retry_initial_seconds"] = 0
        config["requests"]["retry_max_seconds"] = 0
        config["requests"]["rate_limit_retry_initial_seconds"] = 0
        config["requests"]["rate_limit_retry_max_seconds"] = 0
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


def generate(model, messages=None, *, deadline_seconds=5, wait_allowance=0):
    async def run():
        await model.check_route()
        return await model.generate(
            messages or [{"role": "user", "content": "Synthetic record"}],
            [],
            AttemptClock(deadline_seconds, wait_allowance),
        )

    return asyncio.run(run())


def test_generation_requires_preflight(model_factory):
    model = model_factory(lambda request: pytest.fail("An unverified request was sent"))
    with pytest.raises(CostAccountingError, match="preflight"):
        asyncio.run(model.generate([], [], AttemptClock(5)))
    assert model.ledger.totals()["held_usd"] == 0


@pytest.mark.parametrize(
    "defect", ["usage", "choices", "unspecified", "status", "malformed"]
)
def test_only_explicit_pre_inference_pool_rejection_qualifies(defect):
    data = {
        "error": {
            "code": 429,
            "metadata": {"limit_source": "upstream_provider_shared_pool"},
        }
    }
    status = 429
    if defect == "usage":
        data["usage"] = {"completion_tokens": 1}
    if defect == "choices":
        data["choices"] = [{"message": {"content": "Partial synthetic output"}}]
    if defect == "unspecified":
        data["error"]["metadata"] = {}
    if defect == "status":
        status = 503
    if defect == "malformed":
        data = []
    assert not pre_inference_rejection(httpx.Response(status, json=data))


@pytest.mark.parametrize(
    ("status", "header", "delays"),
    [
        (429, None, [30, 60, 120, 120, 120, 120, 120]),
        (429, "150", [150] * 7),
        (503, None, [3, 6, 12, 24, 48, 60, 60]),
    ],
)
def test_backoff_and_optional_attempt_cap(
    model_factory, monkeypatch, status, header, delays
):
    config = load_draft(model="qwen/qwen3.8-flash")
    assert config["spending"].get("attempt_limit_usd") is None
    waits = []
    calls = 0

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr("benchmark.model.asyncio.sleep", sleep)

    def reply(request):
        nonlocal calls
        calls += 1
        if calls <= 7:
            return httpx.Response(
                status, headers={"Retry-After": header} if header else {}
            )
        data = completion()
        data["provider"] = config["agent_route"]["provider_name"]
        return httpx.Response(200, json=data)

    model = model_factory(reply, config, attempt_limit=None)
    model.config["requests"].update(
        {key: value for key, value in config["requests"].items() if "retry" in key}
    )
    generate(model, deadline_seconds=900)
    assert calls == 8 and waits == delays
    assert model.ledger.totals()["held_usd"] == 2.881228
    assert model.ledger.totals()["billed_usd"] == 0.001


@pytest.mark.parametrize("status", [429, 503])
def test_retry_wait_within_the_allowance_stops_the_clock(
    model_factory, monkeypatch, status
):
    replies = iter([httpx.Response(status), httpx.Response(200, json=completion())])
    model = model_factory(lambda request: next(replies), attempt_limit=None)
    for prefix in ("rate_limit_retry", "retry"):
        model.config["requests"][f"{prefix}_initial_seconds"] = 30
        model.config["requests"][f"{prefix}_max_seconds"] = 120
    waits = []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr("benchmark.model.asyncio.sleep", sleep)
    clock = AttemptClock(20, wait_allowance=60)

    async def run():
        await model.check_route()
        return await model.generate([], [], clock)

    assert asyncio.run(run())["message"]
    assert waits == [30] and clock.excluded == 30
    assert 45 < clock.remaining() <= 50


def test_cancelled_request_keeps_its_hold(model_factory):
    async def reply(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json=completion())

    model = model_factory(reply)

    async def run():
        await model.check_route()
        task = asyncio.create_task(model.generate([], [], AttemptClock(30)))
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


def test_deadline_stops_before_reserving(model_factory):
    model = model_factory(lambda request: pytest.fail("Expired request was sent"))

    async def run():
        await model.check_route()
        await model.generate([], [], AttemptClock(-1))

    with pytest.raises(TimeoutError):
        asyncio.run(run())
    assert model.ledger.totals()["held_usd"] == 0


@pytest.mark.parametrize("model_id", sorted(load_draft()["agents"]))
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
        assert body["provider"]["only"] == [config["agent_route"]["provider"]]
        assert body["provider"]["allow_fallbacks"] is False
        assert body["reasoning"] == {
            "effort": config["agent_route"]["reasoning_effort"]
        }
        assert body["temperature"] == config["agent_route"]["temperature"]
        output = config["agent_route"]["output_token_parameter"]
        assert body[output] == config["budgets"]["agent_max_output_tokens"]
        other = {"max_tokens", "max_completion_tokens"} - {output}
        assert not other & set(body)
        data = completion()
        data["provider"] = config["agent_route"]["provider_name"]
        data["choices"][0]["message"] = message
        return httpx.Response(200, json=data)

    model = model_factory(reply, config)
    first = generate(model)
    generate(model, [{"role": "user", "content": "Synthetic record"}, first["message"]])
    assert requests[1]["messages"][1] == message
    assert model.ledger.totals()["billed_usd"] == 0.002


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
        data["provider"] = config["agent_route"]["provider_name"]
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
    config = load_draft(model=TEST_MODEL)
    config["budgets"]["model_retries"] = 0
    requests = []
    metadata = {
        "id": "gen-synthetic-error",
        "model": TEST_MODEL,
        "provider_name": config["agent_route"]["provider_name"],
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


def test_excluded_wait_also_moves_the_attempt_timeout():
    async def run():
        clock = AttemptClock(0.05, wait_allowance=1)
        async with asyncio.timeout(0.05) as limit:
            clock.timeout = limit
            clock.exclude(0.2)
            await asyncio.sleep(0.1)  # Past the original limit, within the moved one.
            assert clock.excluded == 0.2
            clock.exclude(5)
        return clock

    assert asyncio.run(run()).excluded == 1  # The allowance caps what is excluded.


# Every request is funded first, and only a verified charge releases its hold.

REPLIES = [
    "ok",
    "unbilled",
    "bad usage",
    "no choice",
    "transient",
    "rate limited",
    "pool rejection",
    "rejected",
    "timeout",
]


def provider_reply(kind):
    if kind == "timeout":
        raise httpx.ReadTimeout("Synthetic timeout")
    if kind in ("transient", "rate limited", "rejected"):
        return httpx.Response({"transient": 503, "rate limited": 429}.get(kind, 400))
    if kind == "pool rejection":
        error = {
            "code": 429,
            "metadata": {"limit_source": "upstream_provider_shared_pool"},
        }
        return httpx.Response(429, json={"error": error})
    data = completion()
    if kind == "unbilled":
        del data["usage"]["cost"]
    elif kind == "bad usage":
        data["usage"]["prompt_tokens"] = True
    elif kind == "no choice":
        data["choices"] = []
    return httpx.Response(200, json=data)


@settings(max_examples=150)
@given(
    st.lists(st.sampled_from(REPLIES), min_size=1, max_size=6),
    st.integers(0, 3),
    st.integers(1, 5),
)
def test_every_request_is_funded_first_and_only_a_verified_charge_releases_it(
    replies, retries, holds
):
    config = load_draft(model=TEST_MODEL)
    config["budgets"]["model_retries"] = retries
    for key in config["requests"]:
        if "retry" in key:
            config["requests"][key] = 0
    prices = Prices.from_config(config)
    reservation = prices.reservation(config["budgets"]["agent_max_output_tokens"])
    with tempfile.TemporaryDirectory() as folder:
        ledger = Ledger(
            Path(folder) / "spend.sqlite3", 10, holds * reservation + reservation / 2
        )
        sent = []

        def handler(request):
            with ledger.connect() as db:
                (rows,) = db.execute("SELECT COUNT(*) FROM requests").fetchone()
            assert rows == len(sent) + 1  # This request is already reserved.
            kind = replies[len(sent)] if len(sent) < len(replies) else "ok"
            sent.append(kind)
            return provider_reply(kind)

        model = OpenRouterModel(
            config,
            "synthetic-key",
            ledger,
            "run",
            AuditTrail("run", "sample"),
            transport=httpx.MockTransport(handler),
        )
        model.ready = True
        try:
            outcome = asyncio.run(
                model.generate(
                    [{"role": "user", "content": "Synthetic"}], [], AttemptClock(60)
                )
            )
        except (CostAccountingError, ModelAPIError, SpendingLimit) as error:
            outcome = error
        totals = ledger.totals()

    # What the protocol says, in microdollars.
    hold, limit = (
        microdollars(reservation),
        microdollars(holds * reservation + reservation / 2),
    )
    held = billed = unbilled = 0
    expected_sent, expected = [], ModelAPIError  # Retries exhausted, unless below.
    for attempt in range(retries + 1):
        if held + billed + hold > limit:
            expected = SpendingLimit
            break
        kind = replies[attempt] if attempt < len(replies) else "ok"
        expected_sent.append(kind)
        if kind in ("ok", "bad usage", "no choice"):
            billed += 1000  # Billing was verified, so the charge settles.
            expected = {"ok": dict, "bad usage": CostAccountingError}.get(
                kind, ModelAPIError
            )
            break
        if kind == "pool rejection":
            unbilled += 1  # Rejected before inference: recorded, not held, retried.
            continue
        held += hold  # It may have been charged, so the hold stays.
        if kind in ("unbilled", "rejected"):
            expected = CostAccountingError if kind == "unbilled" else ModelAPIError
            break
    assert sent == expected_sent
    assert isinstance(outcome, expected)
    assert totals["held_usd"] == held / 1_000_000
    assert totals["billed_usd"] == billed / 1_000_000
    assert totals["expected_unbilled_requests"] == unbilled


def test_a_retry_wait_that_does_not_fit_the_attempt_ends_it(model_factory):
    # A Retry-After past the deadline, or a backoff longer than the allowance.
    for header, allowance in (("60", 0), (None, 5)):
        headers = {"Retry-After": header} if header else {}
        model = model_factory(
            lambda request: httpx.Response(429, headers=headers), attempt_limit=None
        )
        model.config["requests"]["rate_limit_retry_initial_seconds"] = 30
        model.config["requests"]["rate_limit_retry_max_seconds"] = 120
        with pytest.raises(TimeoutError, match="backoff"):
            generate(model, deadline_seconds=20, wait_allowance=allowance)
        assert len(model.audit.items) == 1
        assert model.ledger.totals()["held_usd"] > 0


def test_the_token_estimate_needs_a_reviewed_tokenizer():
    text = [{"role": "user", "content": "Synthetic <|endoftext|> text"}]
    assert input_tokens(text, [], "openrouter/openai/gpt-4.1-mini") > 0
    assert input_tokens(text, [], "qwen/qwen3.8-flash", "o200k_base") > 0
    with pytest.raises(CostAccountingError):
        input_tokens([], [], "unknown/model")


# Diagnostics.


@given(st.integers(0, 10**6) | st.floats(0, 10**6))
def test_a_retry_after_delay_reads_back_as_seconds_or_as_a_date(seconds):
    assert retry_after(str(seconds)) == pytest.approx(seconds)
    date = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    assert retry_after(format_datetime(date, usegmt=True)) == pytest.approx(
        seconds, abs=2
    )


@pytest.mark.parametrize("value", ["-1", None, "invalid", "nan", "inf"])
def test_an_invalid_retry_after_is_ignored(value):
    assert retry_after(value) is None


SEPARATORS = st.text(" .,:;!?()[]{}<>'/=", max_size=20)
TOKEN = st.text(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=12,
    max_size=30,
)


@given(
    TOKEN,
    st.sampled_from(["sk-or-v1-", "sk-", "tvly-", "Bearer ", ""]),
    TOKEN,
    st.sampled_from(["message", "raw", "provider_name", "reason", "request id"]),
    SEPARATORS,
    SEPARATORS,
)
def test_error_diagnostics_never_contain_a_credential(
    key, prefix, other, place, before, after
):
    api_key = "sk-or-v1-" + key
    # The run's own key, or another credential-shaped value.
    secret = prefix + other if prefix else api_key
    text = before + secret + after
    metadata = {"provider_name": "Mistral", "ignored": "private"}
    error = {"code": 429, "message": "Rate limited", "metadata": metadata}
    headers = {"X-Request-Id": "request-123", "Retry-After": "12"}
    if place == "request id":
        headers["X-Request-Id"] = text
    elif place == "message":
        error["message"] = text
    else:
        metadata[place] = text
    response = httpx.Response(
        429, headers=headers, json={"id": "gen-synthetic-error", "error": error}
    )
    details = error_details(response, api_key)
    encoded = json.dumps(details, ensure_ascii=False)
    assert (other if prefix else key) not in encoded and "private" not in encoded
    # What diagnosis needs is kept.
    assert details["generation_id"] == "gen-synthetic-error"
    assert details["retry_after_seconds"] == 12 and "provider_request_id" in details
    response.headers["X-Generation-Id"] = "gen-conflicting"
    assert "generation_id" not in error_details(response, api_key)
