"""The spending ledger, reservations and billing checks, with mocked APIs."""

import copy
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    multiple,
    rule,
)

from benchmark.budgets import load_draft
from benchmark.costs import (
    AttemptSpendingLimit,
    CostAccountingError,
    Ledger,
    Prices,
    SpendingLimit,
    amount,
    billed_response,
    check_endpoint,
    check_key,
    microdollars,
    preflight,
    read_json,
)
from tests.support import TEST_MODEL

PROFILES = sorted(load_draft()["agents"])


# The ledger, against a plain model of what it should hold.


class LedgerModel(RuleBasedStateMachine):
    """Random reservations, charges and restarts, checked against plain sums.

    A request commits its charge once settled, nothing once it is an expected
    unbilled rejection, and otherwise its whole reservation.
    """

    requests = Bundle("requests")

    def __init__(self):
        super().__init__()
        self.folder = Path(tempfile.mkdtemp())
        self.rows: dict[str, dict] = {}

    def teardown(self):
        shutil.rmtree(self.folder)

    def committed(self, run=None):
        return sum(
            row["billed"]
            if row["billed"] is not None
            else 0
            if row["status"] == "expected_unbilled"
            else row["reserved"]
            for row in self.rows.values()
            if run is None or row["run"] == run
        )

    @initialize(
        limit=st.integers(1, 3_000_000),
        attempt_limit=st.none() | st.integers(1, 2_000_000),
    )
    def open(self, limit, attempt_limit):
        self.limit, self.attempt_limit = limit, attempt_limit
        self.reopen()

    @rule()
    def reopen(self):
        """A restart, which must lose nothing."""
        self.ledger = Ledger(
            self.folder / "spend.sqlite3",
            Decimal(self.limit) / 10**6,
            None if self.attempt_limit is None else Decimal(self.attempt_limit) / 10**6,
        )

    @rule(limit=st.integers(1, 3_000_000))
    def change_limit(self, limit):
        self.limit = limit
        self.reopen()

    @rule(
        target=requests,
        run=st.sampled_from(["a", "b"]),
        role=st.sampled_from(["agent", "classifier"]),  # One cap for both.
        micros=st.integers(1, 1_500_000),
        edge=st.sampled_from([None, "total", "attempt"]),
        offset=st.integers(-1, 1),
    )
    def reserve(self, run, role, micros, edge, offset):
        # Sometimes exactly what is left, or one microdollar either side of it.
        if edge == "total":
            micros = max(1, self.limit - self.committed() + offset)
        elif edge == "attempt" and self.attempt_limit is not None:
            micros = max(1, self.attempt_limit - self.committed(run) + offset)
        over_total = self.committed() + micros > self.limit
        over_attempt = (
            self.attempt_limit is not None
            and self.committed(run) + micros > self.attempt_limit
        )
        try:
            request = self.ledger.reserve(run, role, "model", Decimal(micros) / 10**6)
        except AttemptSpendingLimit:
            assert over_attempt and not over_total
            return multiple()
        except SpendingLimit:
            assert over_total
            return multiple()
        assert not over_total and not over_attempt
        self.rows[request] = {
            "run": run,
            "reserved": micros,
            "billed": None,
            "status": "reserved",
        }
        return request

    @rule(
        request=requests,
        micros=st.integers(0, 2_000_000),
        edge=st.booleans(),
        offset=st.integers(-1, 1),
    )
    def settle(self, request, micros, edge, offset):
        row = self.rows[request]
        if edge:  # A charge at its reservation, or one microdollar either side.
            micros = max(0, row["reserved"] + offset)
        settled = row["billed"] is not None
        if not settled:
            row["billed"], row["status"] = micros, "settled"
        try:
            self.ledger.settle(request, Decimal(micros) / 10**6, "generation")
        except CostAccountingError:
            # A second charge is refused, and an overcharge is kept, then reported.
            assert settled or micros > row["reserved"]
        else:
            assert not settled and micros <= row["reserved"]

    @rule(request=requests)
    def uncertain(self, request):
        row = self.rows[request]
        if row["billed"] is None and row["status"] != "expected_unbilled":
            row["status"] = "uncertain"
        self.ledger.uncertain(request)

    @rule(request=requests)
    def expect_unbilled(self, request):
        row = self.rows[request]
        if row["billed"] is None:
            row["status"] = "expected_unbilled"
        self.ledger.expect_unbilled(request, None)

    @invariant()
    def totals_match(self):
        if not hasattr(self, "ledger"):
            return
        for run in (None, "a", "b"):
            rows = [r for r in self.rows.values() if run is None or r["run"] == run]
            unbilled = [r for r in rows if r["billed"] is None]
            assert self.ledger.totals(run) == {
                "billed_usd": sum(r["billed"] or 0 for r in rows) / 1_000_000,
                "held_usd": sum(
                    r["reserved"]
                    for r in unbilled
                    if r["status"] != "expected_unbilled"
                )
                / 1_000_000,
                "expected_unbilled_requests": sum(
                    r["status"] == "expected_unbilled" for r in unbilled
                ),
                "remaining_usd": max(0, self.limit - self.committed()) / 1_000_000,
            }


TestLedgerModel = LedgerModel.TestCase


def test_concurrent_reservations_share_one_budget(tmp_path):
    ledger = Ledger(tmp_path / "spend.sqlite3", 1)

    def reserve(_):
        try:
            return ledger.reserve("run", "agent", "model", "0.6")
        except SpendingLimit:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, range(2)))
    assert sum(result is not None for result in results) == 1
    assert ledger.totals()["held_usd"] == 0.6


def test_the_local_ceiling_can_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIADNE_SPENDING_LIMIT_USD", "1")
    ledger = Ledger(tmp_path / "spend.sqlite3")
    ledger.reserve("run", "agent", "model", "0.75")
    with pytest.raises(SpendingLimit):
        ledger.reserve("run", "agent", "model", "0.26")


# Amounts and reservations.


@given(st.decimals(min_value=0, max_value=10**6, places=12))
def test_amounts_round_up_to_whole_microdollars(dollars):
    stored = microdollars(dollars)
    assert stored - 1 < dollars * 10**6 <= stored


@pytest.mark.parametrize(
    "value", [True, None, "invalid", "NaN", "Infinity", -1, float("nan")]
)
def test_an_invalid_amount_fails_closed(value):
    with pytest.raises(CostAccountingError):
        amount(value)


@given(
    st.sampled_from(PROFILES),
    st.floats(0, 1),
    st.floats(0, 1),
    st.floats(0, 1),
    st.floats(0, 1),
)
def test_a_reservation_covers_any_charge_the_route_allows(
    model, input_share, cached_share, written_share, output_share
):
    config = load_draft(model=model)
    prices = Prices.from_config(config)
    output_limit = config["budgets"]["agent_max_output_tokens"]
    prompt = int(input_share * prices.prompt_limit)
    cached = int(cached_share * prompt)
    written = int(written_share * prompt)
    output = int(output_share * output_limit)
    charge = (
        (prompt - cached) * prices.input
        + cached * prices.cached_input
        + written * prices.cache_write
        + output * prices.output
    ) / 10**6
    assert charge <= prices.reservation(output_limit)


# The pinned route, the key and the billing record.


def listing(model):
    """OpenRouter's listing for a profile's pinned endpoint, as reviewed."""
    config = load_draft(model=model)
    route, pricing = config["agent_route"], config["agent_route"]["pricing"]

    def per_token(key):
        return str(Decimal(pricing.get(key, 0)) / 1_000_000)

    return {
        "endpoints": [
            {
                "tag": route["provider"],
                "context_length": pricing["context_tokens"],
                "max_prompt_tokens": pricing.get("max_prompt_tokens"),
                "max_completion_tokens": config["budgets"]["agent_max_output_tokens"],
                "supported_parameters": [
                    "tools",
                    "reasoning",
                    route["output_token_parameter"],
                ],
                "pricing": {
                    "prompt": per_token("input_per_million"),
                    "completion": per_token("output_per_million"),
                    "input_cache_read": per_token("cached_input_per_million"),
                    "input_cache_write": per_token("cache_write_per_million"),
                    "discount": pricing.get("discount", 0),
                },
            }
        ],
    }


@pytest.mark.parametrize("model", PROFILES)
def test_every_agent_profile_is_complete_and_matches_its_listing(model):
    config = load_draft(model=model)
    route, prices = config["agent_route"], Prices.from_config(config)
    assert prices.input > 0 and prices.output > 0
    assert 0 < config["budgets"]["agent_max_output_tokens"] < prices.context_tokens
    assert route["reasoning_effort"] in ("low", "medium", "high", "xhigh", "max")
    assert 0 <= route["temperature"] <= 2
    assert route["provider"] and route["provider_name"] and route["token_encoding"]
    assert route["generation_model"].startswith(model.split("/")[0] + "/")
    assert "limit_usd" not in config["spending"]
    check_endpoint(listing(model), config, prices)


@pytest.mark.parametrize(
    "change",
    [
        ("tag", "other"),
        ("context_length", 2000000),
        ("max_prompt_tokens", 1000),
        ("max_completion_tokens", 100),
        ("supported_parameters", ["max_tokens"]),
        ("pricing", "prompt", "0.0000005"),
        ("pricing", "completion", "0.000002"),
        ("pricing", "input_cache_read", "0.0000002"),
        ("pricing", "input_cache_write", "0.000001"),
        ("pricing", "request", "0.01"),
        ("pricing", "discount", 0.25),
        ("pricing", "discount", 2),
    ],
)
def test_any_change_to_the_pinned_endpoint_stops_the_run(change):
    config = load_draft(model=TEST_MODEL)
    data = listing(TEST_MODEL)
    target = data["endpoints"][0]
    if change[0] == "pricing":
        target = target["pricing"]
        change = change[1:]
    target[change[0]] = change[1]
    with pytest.raises(CostAccountingError):
        check_endpoint(data, config, Prices.from_config(config))


KEY = {"limit": 10, "limit_reset": None, "limit_remaining": 10}


@pytest.mark.parametrize(
    "changes,outcome",
    [
        ({}, None),
        ({"limit": 100, "limit_reset": "daily"}, None),
        ({"limit": None, "limit_remaining": None}, None),
        ({"limit_remaining": 0}, SpendingLimit),
        ({"limit": 0}, SpendingLimit),
        ({"limit": float("nan")}, CostAccountingError),
        ({"limit_remaining": None}, CostAccountingError),
        ({"limit_remaining": True}, CostAccountingError),
    ],
)
def test_the_key_must_show_a_remaining_allowance(changes, outcome):
    if outcome is None:
        check_key(KEY | changes)
    else:
        with pytest.raises(outcome):
            check_key(KEY | changes)


def billing(**changes):
    usage = {"cost": "0.001", "is_byok": False} | changes.pop("usage", {})
    return {"id": "generation-test", "provider": "Azure", "usage": usage} | changes


@pytest.mark.parametrize(
    "response,charge",
    [
        (billing(), Decimal("0.001")),
        (
            billing(usage={"cost_details": {"upstream_inference_cost": "0.5"}}),
            Decimal("0.001"),
        ),
        (billing(provider="OpenAI"), None),
        (billing(id=None), None),
        (billing(usage={"cost": "NaN"}), None),
        (billing(usage={"is_byok": True}), None),
        ({"id": "generation-test", "provider": "Azure", "usage": {}}, None),
        ([billing(), billing()], None),
        (None, None),
    ],
)
def test_a_charge_is_read_only_from_one_verified_billing_record(response, charge):
    if charge is None:
        with pytest.raises(CostAccountingError):
            billed_response(response, "Azure")
    else:
        assert billed_response(response, "Azure") == (charge, "generation-test")


@pytest.mark.parametrize(
    "usage,charge",
    [
        (
            {
                "cost": "0.0001",
                "is_byok": True,
                "cost_details": {"upstream_inference_cost": "0.5"},
            },
            Decimal("0.5001"),
        ),
        # OpenRouter served the request from its own capacity instead of the key.
        ({"cost": "0.5", "is_byok": False}, None),
        ({"cost": "0", "is_byok": True}, None),
    ],
)
def test_a_byok_charge_adds_the_providers_charge_to_openrouters_fee(usage, charge):
    response = billing(usage=usage)
    if charge is None:
        with pytest.raises(CostAccountingError):
            billed_response(response, "Azure", byok=True)
    else:
        assert billed_response(response, "Azure", byok=True) == (
            charge,
            "generation-test",
        )


def test_preflight_checks_key_and_endpoint(monkeypatch):
    config = load_draft(model=TEST_MODEL)
    calls = []

    def read(path, key, timeout):
        calls.append(path)
        return KEY if path == "/key" else listing(TEST_MODEL)

    monkeypatch.setattr("benchmark.costs.read_json", read)
    assert preflight(config, "synthetic") == Prices.from_config(config)
    assert calls == ["/key", f"/models/{TEST_MODEL}/endpoints"]
    with pytest.raises(CostAccountingError):
        preflight(config, "")
    config = copy.deepcopy(config)
    del config["agent_route"]
    with pytest.raises(CostAccountingError):
        preflight(config, "synthetic")


@pytest.mark.parametrize("status,padding", [(200, 0), (302, 0), (200, 1_048_576)])
def test_preflight_transport_rejects_redirects_and_oversized_bodies(
    monkeypatch, status, padding
):
    def stream(method, url, *, headers, timeout, follow_redirects, trust_env):
        assert not follow_redirects and not trust_env
        return nullcontext(
            httpx.Response(
                status,
                headers={"Location": "https://example.invalid"},
                stream=httpx.ByteStream(b'{"data":{"valid":true}}' + b" " * padding),
                request=httpx.Request(method, url, headers=headers),
            )
        )

    monkeypatch.setattr(httpx, "stream", stream)
    if status == 200 and not padding:
        assert read_json("/key", "synthetic", 5) == {"valid": True}
    else:
        with pytest.raises(CostAccountingError, match="OpenRouter preflight failed"):
            read_json("/key", "synthetic", 5)
