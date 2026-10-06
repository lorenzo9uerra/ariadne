"""Billing and provider checks with mocked APIs."""

import copy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from benchmark.budgets import load_draft
from benchmark.costs import (
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
    routing,
)


def key_data():
    return {
        "limit": 10,
        "limit_reset": None,
        "limit_remaining": 10,
        "include_byok_in_limit": True,
    }


def endpoint_data():
    return {
        "endpoints": [
            {
                "tag": "azure",
                "context_length": 1047576,
                "max_completion_tokens": 32768,
                "supported_parameters": ["tools", "max_completion_tokens"],
                "pricing": {
                    "prompt": "0.0000004",
                    "completion": "0.0000016",
                    "input_cache_read": "0.0000001",
                },
            }
        ],
    }


def billing_event(cost="0.001", provider="Azure"):
    response = {
        "id": "generation-test",
        "provider": provider,
        "usage": {"cost": cost, "is_byok": False},
    }
    return response


@pytest.mark.parametrize(
    "value", [True, None, "invalid", "NaN", "Infinity", -1, float("nan")]
)
def test_invalid_amounts_fail_closed(value):
    with pytest.raises(CostAccountingError):
        amount(value)


def test_storage_rounds_up():
    assert microdollars("0.0000001") == 1
    assert microdollars("0.0000011") == 2


def test_ledger_survives_restarts_and_settles_actual_charge(tmp_path):
    path = tmp_path / "spend.sqlite3"
    first = Ledger(path, 10)
    request = first.reserve("run", "agent", "model", 2)
    restarted = Ledger(path, 10)
    assert restarted.totals() == {"billed_usd": 0, "held_usd": 2, "remaining_usd": 8}
    restarted.settle(request, "0.125", "generation")
    assert first.totals() == {
        "billed_usd": 0.125,
        "held_usd": 0,
        "remaining_usd": 9.875,
    }
    with pytest.raises(CostAccountingError, match="already settled"):
        restarted.settle(request, 0, "generation")


def test_uncertain_charge_keeps_full_hold(tmp_path):
    ledger = Ledger(tmp_path / "spend.sqlite3", 1)
    request = ledger.reserve("failed", "agent", "model", "0.75")
    ledger.uncertain(request)
    with pytest.raises(SpendingLimit):
        ledger.reserve("replacement", "agent", "model", "0.26")
    assert ledger.totals("failed")["held_usd"] == 0.75


def test_reviewed_unbilled_rejection_releases_hold_without_generation_id(tmp_path):
    ledger = Ledger(tmp_path / "spend.sqlite3", 1)
    request = ledger.reserve("rejected", "agent", "model", "0.75")
    ledger.uncertain(request)
    ledger.settle(request, 0, None)
    assert ledger.totals() == {"billed_usd": 0, "held_usd": 0, "remaining_usd": 1}


def test_cap_cannot_be_changed_on_restart(tmp_path):
    path = tmp_path / "spend.sqlite3"
    Ledger(path, 10)
    with pytest.raises(CostAccountingError, match="cannot be changed"):
        Ledger(path, 20)


def test_overrun_is_preserved_and_prevents_further_spending(tmp_path):
    ledger = Ledger(tmp_path / "spend.sqlite3", 1)
    request = ledger.reserve("run", "agent", "model", "0.5")
    with pytest.raises(CostAccountingError, match="exceeded"):
        ledger.settle(request, "1.25", "generation")
    assert ledger.totals()["billed_usd"] == 1.25
    with pytest.raises(SpendingLimit):
        ledger.reserve("run", "agent", "model", "0.01")


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


def test_agent_and_monitor_share_cap_without_refunding_faults(tmp_path):
    ledger = Ledger(tmp_path / "spend.sqlite3", 1)
    request = ledger.reserve("implementation-fault", "agent", "model", "0.8")
    ledger.settle(request, "0.8", "generation")
    with pytest.raises(SpendingLimit):
        ledger.reserve("replacement", "monitor", "model", "0.3")
    assert ledger.totals("implementation-fault")["billed_usd"] == 0.8


def test_per_run_totals_report_shared_remaining_allowance(tmp_path):
    ledger = Ledger(tmp_path / "spend.sqlite3", 1)
    first = ledger.reserve("first", "agent", "model", "0.6")
    ledger.settle(first, "0.4", "generation")
    ledger.reserve("second", "agent", "model", "0.2")
    assert ledger.totals("second") == {
        "billed_usd": 0,
        "held_usd": 0.2,
        "remaining_usd": 0.4,
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"limit": None},
        {"limit": 11},
        {"limit": 0},
        {"limit_reset": "daily"},
        {"limit": float("nan")},
    ],
)
def test_key_must_have_nonresetting_development_cap(patch):
    with pytest.raises(CostAccountingError):
        check_key(key_data() | patch, 10)


def test_key_exhaustion_and_valid_cap():
    check_key(key_data(), 10)
    check_key(key_data() | {"include_byok_in_limit": False}, 10)
    with pytest.raises(SpendingLimit):
        check_key(key_data() | {"limit_remaining": 0}, 10)


def test_full_context_reservation_and_verified_rates():
    config = load_draft()
    prices = Prices.from_config(config)
    assert prices.reservation(16384) == Decimal("0.4452448")
    check_endpoint(endpoint_data(), config, prices)


@pytest.mark.parametrize(
    "field,value",
    [
        ("context_length", 2000000),
        ("max_completion_tokens", 100),
        ("supported_parameters", ["max_tokens"]),
        ("tag", "other"),
    ],
)
def test_endpoint_limits_and_provider_changes_stop_run(field, value):
    config = load_draft()
    data = endpoint_data()
    data["endpoints"][0][field] = value
    with pytest.raises(CostAccountingError):
        check_endpoint(data, config, Prices.from_config(config))


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", "0.0000005"),
        ("completion", "0.000002"),
        ("input_cache_read", "0.0000002"),
        ("request", "0.01"),
    ],
)
def test_endpoint_price_changes_stop_run(field, value):
    config = load_draft()
    data = endpoint_data()
    data["endpoints"][0]["pricing"][field] = value
    with pytest.raises(CostAccountingError):
        check_endpoint(data, config, Prices.from_config(config))


def test_billing_uses_raw_response_and_pinned_provider():
    assert billed_response(billing_event(), "Azure") == (
        Decimal("0.001"),
        "generation-test",
    )
    with pytest.raises(CostAccountingError):
        billed_response(billing_event(provider="OpenAI"), "Azure")


@pytest.mark.parametrize(
    "failure", ["missing", "duplicate", "no_cost", "byok", "invalid_cost"]
)
def test_unverifiable_billing_stops_run(failure):
    event = billing_event()
    response = event
    events = event
    if failure == "missing":
        events = None
    elif failure == "duplicate":
        events = [event, event]
    elif failure == "no_cost":
        response["usage"] = {}
    elif failure == "byok":
        response["usage"] = {
            "cost": 0.001,
            "is_byok": True,
            "cost_details": {"upstream_inference_cost": 0.5},
        }
    else:
        response["usage"] = {"cost": "NaN", "is_byok": False}
    with pytest.raises(CostAccountingError):
        billed_response(events, "Azure")


def test_prepaid_billing_can_include_upstream_cost_details():
    event = billing_event()
    event["usage"] = {
        "cost": "0.000232",
        "is_byok": False,
        "cost_details": {"upstream_inference_cost": "0.000232"},
    }
    assert billed_response(event, "Azure")[0] == Decimal("0.000232")


def test_routing_is_pinned_without_provider_fallback():
    config = load_draft()
    assert routing(config)["only"] == [config["live"]["provider"]]
    assert routing(config)["allow_fallbacks"] is False


def test_preflight_checks_key_and_endpoint(monkeypatch):
    config = load_draft()
    calls = []

    def read(path, key, timeout):
        calls.append(path)
        return key_data() if path == "/key" else endpoint_data()

    monkeypatch.setattr("benchmark.costs.read_json", read)
    assert preflight(config, "synthetic") == Prices.from_config(config)
    assert calls == ["/key", "/models/openai/gpt-4.1-mini/endpoints"]
    with pytest.raises(CostAccountingError):
        preflight(config, "")
    config = copy.deepcopy(config)
    config["enable_live"] = False
    with pytest.raises(CostAccountingError):
        preflight(config, "synthetic")


def test_attempt_ceiling_applies_per_run(tmp_path):
    from benchmark.costs import AttemptSpendingLimit

    ledger = Ledger(tmp_path / "ledger.sqlite3", "10", "1")
    ledger.settle(ledger.reserve("a", "agent", "m", "0.6"), "0.6", None)
    with pytest.raises(AttemptSpendingLimit):
        ledger.reserve("a", "monitor", "m", "0.5")
    ledger.reserve("b", "agent", "m", "0.9")  # Another attempt is unaffected.
    assert issubclass(AttemptSpendingLimit, SpendingLimit)
