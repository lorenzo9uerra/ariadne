"""Billing and provider checks with mocked APIs."""

import copy
import sqlite3
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


def test_local_ceiling_can_change_without_resetting_old_holds(tmp_path, monkeypatch):
    monkeypatch.delenv("ARIADNE_SPENDING_LIMIT_USD", raising=False)
    path = tmp_path / "spend.sqlite3"
    first = Ledger(path, 1)
    first.reserve("previous", "agent", "model", "0.75")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE budget (cap INTEGER NOT NULL)")
        db.execute("INSERT INTO budget VALUES (1000000)")
    unlimited = Ledger(path, attempt_limit_usd=2)
    assert unlimited.totals()["remaining_usd"] is None
    unlimited.reserve("next", "agent", "model", "0.5")
    assert unlimited.totals()["held_usd"] == 1.25
    with pytest.raises(SpendingLimit):
        Ledger(path, 1).reserve("next", "agent", "model", "0.1")
    Ledger(path, 2).reserve("next", "agent", "model", "0.1")


def test_optional_local_ceiling_comes_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIADNE_SPENDING_LIMIT_USD", "1")
    ledger = Ledger(tmp_path / "spend.sqlite3")
    ledger.reserve("run", "agent", "model", "0.75")
    with pytest.raises(SpendingLimit):
        ledger.reserve("run", "agent", "model", "0.26")


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
    [{"limit": float("nan")}, {"limit_remaining": None}, {"limit_remaining": True}],
)
def test_key_rejects_unverifiable_allowance(patch):
    with pytest.raises(CostAccountingError):
        check_key(key_data() | patch)


def test_key_limit_is_provider_configuration():
    check_key(key_data())
    check_key(key_data() | {"limit": 100, "limit_reset": "daily"})
    check_key(key_data() | {"limit": None, "limit_remaining": None})
    check_key(key_data() | {"include_byok_in_limit": False})
    with pytest.raises(SpendingLimit):
        check_key(key_data() | {"limit_remaining": 0})
    with pytest.raises(SpendingLimit):
        check_key(key_data() | {"limit": 0})


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


@pytest.mark.parametrize("model", ["mistralai/mistral-large-4-0", "qwen/qwen3.8-flash"])
def test_selected_profile_prices_and_reservations(model):
    config = load_draft(model=model)
    prices = Prices.from_config(config)
    is_mistral = model.startswith("mistralai/")
    assert prices.input == Decimal("0.68" if is_mistral else "0.15")
    assert prices.output == Decimal("2.09" if is_mistral else "0.47")
    assert config["models"]["agent"] == "openrouter/" + model
    assert config["budgets"]["agent_max_output_tokens"] == (
        262144 if is_mistral else 131072
    )
    assert config["live"]["reasoning"] is True
    assert config["live"]["temperature"] == 0.6
    assert "limit_usd" not in config["spending"]
    assert load_draft()["models"]["agent"] == "openrouter/openai/gpt-4.1-mini"
    if not is_mistral:
        assert prices.reservation(131072) == Decimal("0.41160384")


def test_discount_change_stops_the_pinned_route():
    config = load_draft(model="mistralai/mistral-large-4-0")
    endpoint = {
        "tag": "mistral",
        "context_length": 524288,
        "max_completion_tokens": 262144,
        "supported_parameters": ["tools", "max_tokens", "reasoning"],
        "pricing": {
            "prompt": "0.00000068",
            "completion": "0.00000209",
            "input_cache_read": "0.00000007",
            "discount": 0.5,
        },
    }
    check_endpoint({"endpoints": [endpoint]}, config, Prices.from_config(config))
    endpoint["pricing"]["discount"] = 0
    with pytest.raises(CostAccountingError):
        check_endpoint({"endpoints": [endpoint]}, config, Prices.from_config(config))
