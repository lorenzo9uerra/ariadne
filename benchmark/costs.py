"""Persistent request reservations and OpenRouter billing checks for live runs."""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import uuid4

API_URL = "https://openrouter.ai/api/v1"
MICRODOLLARS = Decimal(1_000_000)


class SpendingLimit(RuntimeError):
    """The configured spending allowance cannot fund another request."""


class AttemptSpendingLimit(SpendingLimit):
    """One attempt's safety ceiling cannot fund another request."""


class CostAccountingError(RuntimeError):
    """Billing or routing cannot be verified; stop before executing agent tools."""


def amount(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise CostAccountingError("Expected a numeric dollar amount")
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        raise CostAccountingError("Invalid dollar amount") from None
    if not result.is_finite() or result < 0:
        raise CostAccountingError("Dollar amounts must be finite and nonnegative")
    return result


def microdollars(value: object) -> int:
    # Round up at the storage boundary so precision never creates extra credit.
    return int((amount(value) * MICRODOLLARS).to_integral_value(ROUND_CEILING))


class Ledger:
    """Track charges and reservations, enforcing any configured local ceilings.

    Reserved and ambiguous requests consume their reservation until settled.
    Explicit pre-inference rejections are expected unbilled, without a hold.
    Crashes retain the hold. Fault attribution never refunds
    actual API spending; benchmark exclusions are separate reporting decisions.
    """

    def __init__(
        self,
        path: Path,
        limit_usd: object = None,
        attempt_limit_usd: object | None = None,
    ):
        self.path = path
        if limit_usd is None:
            limit_usd = os.environ.get("ARIADNE_SPENDING_LIMIT_USD")
        self.limit = microdollars(limit_usd) if limit_usd is not None else None
        # One run_id represents one attempt.
        self.attempt_limit = (
            microdollars(attempt_limit_usd) if attempt_limit_usd is not None else None
        )
        if self.limit is not None and self.limit <= 0:
            raise CostAccountingError("The local spending ceiling must be positive")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests ("
                "id TEXT PRIMARY KEY, run_id TEXT NOT NULL, role TEXT NOT NULL, "
                "model TEXT NOT NULL, reserved INTEGER NOT NULL, billed INTEGER, "
                "status TEXT NOT NULL, generation_id TEXT, "
                "created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, run_id: str, role: str, model: str, dollars: object) -> str:
        reserved = microdollars(dollars)
        if reserved <= 0:
            raise CostAccountingError("A request reservation must be positive")
        request_id = uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            committed = db.execute(
                "SELECT COALESCE(SUM(COALESCE(billed, CASE WHEN status="
                "'expected_unbilled' THEN 0 ELSE reserved END)), 0) FROM requests"
            ).fetchone()[0]
            if self.limit is not None and committed + reserved > self.limit:
                raise SpendingLimit("Local spending allowance exhausted")
            if self.attempt_limit is not None:
                spent = db.execute(
                    "SELECT COALESCE(SUM(COALESCE(billed, CASE WHEN status="
                    "'expected_unbilled' THEN 0 ELSE reserved END)), 0) "
                    "FROM requests WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                if spent + reserved > self.attempt_limit:
                    raise AttemptSpendingLimit("Attempt spending ceiling reached")
            db.execute(
                "INSERT INTO requests (id, run_id, role, model, reserved, status) "
                "VALUES (?, ?, ?, ?, ?, 'reserved')",
                (request_id, run_id, role, model, reserved),
            )
        return request_id

    def uncertain(self, request_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE requests SET status='uncertain' WHERE id=? AND billed IS NULL "
                "AND status != 'expected_unbilled'",
                (request_id,),
            )

    def expect_unbilled(self, request_id: str, generation_id: str | None) -> None:
        """Stop holding a reviewed rejection without claiming a confirmed charge."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT billed FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise CostAccountingError("Unknown reservation")
            if row[0] is not None:
                return
            db.execute(
                "UPDATE requests SET status='expected_unbilled', generation_id=? WHERE id=?",
                (generation_id, request_id),
            )

    def settle(
        self, request_id: str, dollars: object, generation_id: str | None
    ) -> None:
        billed = microdollars(dollars)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT reserved, billed FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None or row[1] is not None:
                raise CostAccountingError("Unknown or already settled reservation")
            db.execute(
                "UPDATE requests SET billed=?, status='settled', generation_id=? "
                "WHERE id=?",
                (billed, generation_id, request_id),
            )
        # Preserve an unexpected real charge before reporting the defect.
        if billed > row[0]:
            raise CostAccountingError("Reported charge exceeded its reservation")

    def committed_for_role(self, role: str) -> Decimal:
        """Billed plus held dollars for one role, for ceilings within the cap."""
        with self.connect() as db:
            committed = db.execute(
                "SELECT COALESCE(SUM(COALESCE(billed, CASE WHEN status="
                "'expected_unbilled' THEN 0 ELSE reserved END)), 0) FROM requests "
                "WHERE role=?",
                (role,),
            ).fetchone()[0]
        return Decimal(committed) / 1_000_000

    def totals(self, run_id: str | None = None) -> dict:
        query = (
            "SELECT COALESCE(SUM(billed), 0), "
            "COALESCE(SUM(CASE WHEN billed IS NULL AND status != 'expected_unbilled' "
            "THEN reserved ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status='expected_unbilled' AND billed IS NULL "
            "THEN 1 ELSE 0 END), 0) "
            "FROM requests"
        )
        with self.connect() as db:
            db.execute("BEGIN")
            billed, held, expected_unbilled = db.execute(
                query + (" WHERE run_id=?" if run_id is not None else ""),
                (run_id,) if run_id is not None else (),
            ).fetchone()
            committed = db.execute(
                "SELECT COALESCE(SUM(COALESCE(billed, CASE WHEN status="
                "'expected_unbilled' THEN 0 ELSE reserved END)), 0) FROM requests"
            ).fetchone()[0]
        return {
            "billed_usd": billed / 1_000_000,
            "held_usd": held / 1_000_000,
            "expected_unbilled_requests": expected_unbilled,
            "remaining_usd": (
                max(0, self.limit - committed) / 1_000_000
                if self.limit is not None
                else None
            ),
        }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CostAccountingError("OpenRouter preflight must not redirect")


def read_json(path: str, api_key: str | None, timeout: int) -> dict:
    """Read only fixed OpenRouter control endpoints, without proxies or redirects."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = Request(API_URL + path, headers=headers)
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("Response size")
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
            raise ValueError("Response schema")
        return result["data"]
    except Exception:
        # Credentials and response bodies must not enter a diagnostic.
        raise CostAccountingError("OpenRouter preflight failed") from None


@dataclass(frozen=True)
class Prices:
    input: Decimal
    output: Decimal
    cached_input: Decimal
    context_tokens: int
    cache_write: Decimal = Decimal(0)
    max_prompt_tokens: int | None = None

    @property
    def prompt_limit(self) -> int:
        return self.max_prompt_tokens or self.context_tokens

    @classmethod
    def from_config(cls, config: dict) -> "Prices":
        pricing = config["live"]["pricing"]
        context = pricing["context_tokens"]
        if type(context) is not int or context <= 0:
            raise CostAccountingError("A positive context-token bound is required")
        prompt_limit = pricing.get("max_prompt_tokens")
        if prompt_limit is not None and (
            type(prompt_limit) is not int or not 0 < prompt_limit <= context
        ):
            raise CostAccountingError("Invalid prompt-token limit")
        return cls(
            amount(pricing["input_per_million"]),
            amount(pricing["output_per_million"]),
            amount(pricing["cached_input_per_million"]),
            context,
            amount(pricing.get("cache_write_per_million", 0)),
            prompt_limit,
        )

    def reservation(self, output_tokens: int) -> Decimal:
        # Reserve a whole context window, not a tokenizer estimate: the server
        # rejects longer input. Pricing every input token at the higher of the
        # regular and cached rates, plus any cache-write charge, covers both costs.
        return (
            self.context_tokens
            * (max(self.input, self.cached_input) + self.cache_write)
            + output_tokens * self.output
        ) / MICRODOLLARS


def check_key(data: dict) -> None:
    if "limit" not in data:
        raise CostAccountingError("OpenRouter key allowance is missing")
    if data["limit"] is not None:
        if amount(data["limit"]) <= 0:
            raise SpendingLimit("OpenRouter key allowance exhausted")
        if data.get("limit_remaining") is None:
            raise CostAccountingError("OpenRouter key remaining allowance is missing")
        if amount(data["limit_remaining"]) <= 0:
            raise SpendingLimit("OpenRouter key allowance exhausted")


def check_endpoint(data: dict, config: dict, prices: Prices) -> None:
    live = config["live"]
    endpoints = [
        endpoint
        for endpoint in data["endpoints"]
        if endpoint["tag"] == live["provider"]
    ]
    if len(endpoints) != 1:
        raise CostAccountingError("The pinned OpenRouter endpoint is unavailable")
    endpoint = endpoints[0]
    pricing = endpoint["pricing"]
    discount = amount(pricing.get("discount", 0))
    if discount > 1:
        raise CostAccountingError("Invalid endpoint discount")
    if (
        amount(pricing["prompt"]) * MICRODOLLARS != prices.input
        or amount(pricing["completion"]) * MICRODOLLARS != prices.output
        or amount(pricing.get("input_cache_read") or 0) * MICRODOLLARS
        != prices.cached_input
        or amount(pricing.get("input_cache_write") or 0) * MICRODOLLARS
        != prices.cache_write
        or discount != amount(live["pricing"].get("discount", 0))
        or amount(pricing.get("request", 0)) != 0
        or endpoint["context_length"] != prices.context_tokens
        or endpoint.get("max_prompt_tokens") != prices.max_prompt_tokens
        or ("reasoning" in live and "reasoning" not in endpoint["supported_parameters"])
        or endpoint["max_completion_tokens"]
        < config["budgets"]["agent_max_output_tokens"]
        or "tools" not in endpoint["supported_parameters"]
        or live["output_token_parameter"] not in ("max_tokens", "max_completion_tokens")
        or live["output_token_parameter"] not in endpoint["supported_parameters"]
    ):
        raise CostAccountingError(
            "Pinned model pricing or limits changed; review config"
        )


def routing(config: dict) -> dict:
    return {
        "only": [config["live"]["provider"]],
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {
            "prompt": float(Prices.from_config(config).input),
            "completion": float(Prices.from_config(config).output),
        },
    }


def preflight(config: dict, api_key: str) -> Prices:
    """Check configured pricing, routing and the provider-side key allowance."""
    name = config["models"]["agent"]
    if not config["enable_live"] or not name.startswith("openrouter/"):
        raise CostAccountingError("The configured OpenRouter route is disabled")
    if not api_key:
        raise CostAccountingError("OPENROUTER_API_KEY is required")
    prices = Prices.from_config(config)
    timeout = config["live"]["preflight_timeout_seconds"]
    check_key(read_json("/key", api_key, timeout))
    model_id = name.removeprefix("openrouter/")
    check_endpoint(
        read_json(f"/models/{model_id}/endpoints", None, timeout), config, prices
    )
    return prices


def billed_response(response: object, provider_name: str) -> tuple[Decimal, str]:
    """Read provider billing from the trusted raw HTTP response."""
    if not isinstance(response, dict):
        raise CostAccountingError("Expected one raw model response")
    usage = response.get("usage")
    generation_id = response.get("id")
    if (
        response.get("provider") != provider_name
        or not isinstance(generation_id, str)
        or not generation_id
        or not isinstance(usage, dict)
        or "cost" not in usage
    ):
        raise CostAccountingError("OpenRouter billing or provider identity is missing")
    if usage.get("is_byok") is not False:
        raise CostAccountingError("BYOK billing is unsupported")
    return amount(usage["cost"]), generation_id
