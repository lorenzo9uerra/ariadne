"""Persistent request reservations and OpenRouter billing checks for live runs."""

import json
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
    """The shared development allowance cannot fund another request."""


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
    """SQLite transactions prevent concurrent processes from overspending locally.

    Reserved and uncertain requests consume their entire reservation until
    settled. Crashes therefore retain the hold. Fault attribution never refunds
    actual API spending; benchmark exclusions are separate reporting decisions.
    """

    def __init__(
        self, path: Path, limit_usd: object, attempt_limit_usd: object | None = None
    ):
        self.path = path
        self.limit = microdollars(limit_usd)
        # A safety net per run_id (one attempt), checked with the shared cap.
        self.attempt_limit = (
            microdollars(attempt_limit_usd) if attempt_limit_usd is not None else None
        )
        if self.limit <= 0:
            raise CostAccountingError("The development spending cap must be positive")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS budget (cap INTEGER NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests ("
                "id TEXT PRIMARY KEY, run_id TEXT NOT NULL, role TEXT NOT NULL, "
                "model TEXT NOT NULL, reserved INTEGER NOT NULL, billed INTEGER, "
                "status TEXT NOT NULL, generation_id TEXT, "
                "created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT cap FROM budget").fetchone()
            if row is None:
                db.execute("INSERT INTO budget VALUES (?)", (self.limit,))
            elif row[0] != self.limit:
                raise CostAccountingError("The ledger's spending cap cannot be changed")

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
                "SELECT COALESCE(SUM(COALESCE(billed, reserved)), 0) FROM requests"
            ).fetchone()[0]
            if committed + reserved > self.limit:
                raise SpendingLimit("Development spending allowance exhausted")
            if self.attempt_limit is not None:
                spent = db.execute(
                    "SELECT COALESCE(SUM(COALESCE(billed, reserved)), 0) "
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
                "UPDATE requests SET status='uncertain' WHERE id=? AND billed IS NULL",
                (request_id,),
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
                "SELECT COALESCE(SUM(COALESCE(billed, reserved)), 0) FROM requests "
                "WHERE role=?",
                (role,),
            ).fetchone()[0]
        return Decimal(committed) / 1_000_000

    def totals(self, run_id: str | None = None) -> dict:
        query = (
            "SELECT COALESCE(SUM(billed), 0), "
            "COALESCE(SUM(CASE WHEN billed IS NULL THEN reserved ELSE 0 END), 0) "
            "FROM requests"
        )
        with self.connect() as db:
            db.execute("BEGIN")
            billed, held = db.execute(
                query + (" WHERE run_id=?" if run_id is not None else ""),
                (run_id,) if run_id is not None else (),
            ).fetchone()
            committed = db.execute(
                "SELECT COALESCE(SUM(COALESCE(billed, reserved)), 0) FROM requests"
            ).fetchone()[0]
        return {
            "billed_usd": billed / 1_000_000,
            "held_usd": held / 1_000_000,
            "remaining_usd": max(0, self.limit - committed) / 1_000_000,
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

    @classmethod
    def from_config(cls, config: dict) -> "Prices":
        pricing = config["live"]["pricing"]
        context = pricing["context_tokens"]
        if type(context) is not int or context <= 0:
            raise CostAccountingError("A positive context-token bound is required")
        return cls(
            amount(pricing["input_per_million"]),
            amount(pricing["output_per_million"]),
            amount(pricing["cached_input_per_million"]),
            context,
        )

    def reservation(self, output_tokens: int) -> Decimal:
        # Reserve a whole context window, not a tokenizer estimate: the server
        # rejects longer input. Pricing every input token at the higher of the
        # regular and cached rates keeps the hold an upper bound.
        return (
            self.context_tokens * max(self.input, self.cached_input)
            + output_tokens * self.output
        ) / MICRODOLLARS


def check_key(data: dict, limit_usd: object) -> None:
    cap = amount(limit_usd)
    if (
        data.get("limit") is None
        or amount(data["limit"]) <= 0
        or amount(data["limit"]) > cap
        or data.get("limit_reset") is not None
    ):
        raise CostAccountingError(
            "Use a dedicated OpenRouter key with a non-resetting cap at most "
            f"${cap}; BYOK inference is unsupported"
        )
    if data.get("limit_remaining") is None or amount(data["limit_remaining"]) <= 0:
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
    if (
        amount(pricing["prompt"]) * MICRODOLLARS != prices.input
        or amount(pricing["completion"]) * MICRODOLLARS != prices.output
        or amount(pricing["input_cache_read"]) * MICRODOLLARS != prices.cached_input
        or amount(pricing.get("request", 0)) != 0
        or endpoint["context_length"] != prices.context_tokens
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
    check_key(read_json("/key", api_key, timeout), config["spending"]["limit_usd"])
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
