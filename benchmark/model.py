"""The agent's model: OpenRouter completions, each request funded by a reservation
in the spending ledger before it is sent."""

import asyncio
import copy
import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
import tiktoken

from benchmark.audit import AuditTrail
from benchmark.budgets import AttemptClock
from benchmark.costs import (
    API_URL,
    CostAccountingError,
    Ledger,
    Prices,
    amount,
    billed_response,
    preflight,
    routing,
)


class ContextLimit(RuntimeError):
    """The next request would not fit the model's context window."""


class ModelAPIError(RuntimeError):
    """The model API failed in a way that ends the attempt."""


class RetryableAPIError(ModelAPIError):
    """A failure worth retrying: HTTP 408, 429 or 5xx."""


def retry_after(value: str | None) -> float | None:
    """The seconds a Retry-After header asks for, given as seconds or as a date.

    None when the header is missing or invalid; a date in the past means 0.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
        if seconds < 0:
            return None
    except ValueError:
        try:
            seconds = (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def pre_inference_rejection(response: httpx.Response) -> bool:
    """Whether an error is the provider's shared-pool rejection, sent before inference."""
    if response.status_code != 429 or len(response.content) > 16384:
        return False
    try:
        data = response.json()
        return (
            isinstance(data, dict)
            and not data.get("choices")
            and "usage" not in data
            and isinstance(data.get("error"), dict)
            and data["error"].get("code") in (429, "429")
            and isinstance(data["error"].get("metadata"), dict)
            and data["error"]["metadata"].get("limit_source")
            == "upstream_provider_shared_pool"
        )
    except ValueError:
        return False


def redact_credentials(value: object, api_key: str) -> str:
    """The value as text of at most 2048 characters, with any API keys removed."""
    text = str(value).replace(api_key, "[redacted]") if api_key else str(value)
    text = re.sub(r"\b(?:sk-or-v1-|sk-|tvly-)[\w-]+", "[redacted]", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", text)
    return text[:2048]


# The fields of an OpenRouter error's metadata worth keeping for diagnosis.
ERROR_METADATA = (
    "provider_name",
    "provider_code",
    "error_type",
    "raw",
    "limit_source",
    "reason",
    "remedy_hint",
)


def api_error(data: object, api_key: str) -> dict:
    """The error fields of a response body, without credentials."""
    error = data.get("error") if isinstance(data, dict) else None
    error = error if isinstance(error, dict) else {}
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    fields = {"code": error.get("code"), "message": error.get("message")}
    fields |= {key: metadata.get(key) for key in ERROR_METADATA}
    return {
        key: redact_credentials(value, api_key)
        for key, value in fields.items()
        if value is not None
    }


def error_details(response: httpx.Response, api_key: str) -> dict:
    """An error response's diagnostic fields and request IDs, without credentials."""
    try:
        data = response.json() if len(response.content) <= 16384 else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    details: dict = {"api_error": api_error(data, api_key)}
    identifiers = [response.headers.get("X-Generation-Id"), data.get("id")]
    identifiers = [
        v
        for v in identifiers
        if isinstance(v, str) and re.fullmatch(r"gen-[\w-]{1,240}", v)
    ]
    if identifiers and len(set(identifiers)) == 1:
        details["generation_id"] = identifiers[0]
    request_id = response.headers.get("X-Request-Id")
    if request_id:
        details["provider_request_id"] = redact_credentials(request_id, api_key)
    delay = retry_after(response.headers.get("Retry-After"))
    if delay is not None:
        details["retry_after_seconds"] = delay
    return details


def input_tokens(
    messages: list[dict],
    tools: list[dict],
    model: str,
    encoding_name: str | None = None,
) -> int:
    """Estimate a request's input tokens. The history is never trimmed to fit."""
    try:
        encoding = (
            tiktoken.get_encoding(encoding_name)
            if encoding_name is not None
            else tiktoken.encoding_for_model(model.rsplit("/", 1)[-1])
        )
    except (KeyError, ValueError):
        raise CostAccountingError("The model needs a reviewed tokenizer") from None
    text = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
    # The per-message overhead is a guess. The provider still rejects a request
    # that is too long, and the spending hold covers the whole context window.
    return len(encoding.encode(text, disallowed_special=())) + 32 * len(messages) + 128


def usage_fields(data: dict) -> dict:
    usage = data.get("usage", {})
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    values = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cached_tokens": cached,
    }
    if any(type(value) is not int or value < 0 for value in values.values()):
        raise CostAccountingError("Provider token usage is missing or invalid")
    if cached > values["prompt_tokens"]:
        raise CostAccountingError("Cached usage exceeds input usage")
    return values


class OpenRouterModel:
    def __init__(
        self,
        config: dict,
        api_key: str,
        ledger: Ledger,
        run_id: str,
        audit: AuditTrail,
        *,
        transport=None,
    ):
        self.config, self.api_key = copy.deepcopy(config), api_key
        self.ledger, self.run_id, self.audit = ledger, run_id, audit
        self.transport = transport
        self.prices = Prices.from_config(config)
        self.ready = False
        self.usage = dict.fromkeys(
            ("prompt_tokens", "completion_tokens", "cached_tokens"), 0
        )

    async def check_route(self) -> None:
        """Check the pinned route, its prices and the key's allowance before any request."""
        self.prices = await asyncio.to_thread(preflight, self.config, self.api_key)
        self.ready = True

    async def reconcile_error(
        self, client: httpx.AsyncClient, entry: dict, clock: AttemptClock
    ) -> None:
        generation_id = entry.get("generation_id")
        remaining = clock.remaining()
        if not generation_id or remaining <= 0:
            return
        entry["billing_status"] = "unconfirmed"
        timeout = min(remaining, self.config["requests"]["preflight_timeout_seconds"])
        try:
            response = await asyncio.wait_for(
                client.get(
                    API_URL + "/generation",
                    params={"id": generation_id},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=timeout,
                ),
                timeout,
            )
            if not response.is_success or len(response.content) > 16384:
                return
            data = response.json().get("data")
            if (
                not isinstance(data, dict)
                or data.get("id") != generation_id
                or data.get("model")
                != self.config["models"]["agent"].removeprefix("openrouter/")
                or data.get("is_byok") is not False
                or not isinstance(data.get("finish_reason"), str)
                or not data["finish_reason"]
            ):
                return
            charge = amount(data.get("total_cost"))
            if data.get("provider_name") != self.config["agent_route"][
                "provider_name"
            ] and not (data.get("provider_name") is None and charge == 0):
                return
        except (
            httpx.TransportError,
            TimeoutError,
            ValueError,
            AttributeError,
            CostAccountingError,
        ):
            return
        self.ledger.settle(entry["call_id"], charge, generation_id)
        entry.update(
            billing_status="confirmed",
            billed_usd=str(charge),
            billing_record={
                key: data[key]
                for key in (
                    "id",
                    "model",
                    "provider_name",
                    "is_byok",
                    "finish_reason",
                    "total_cost",
                )
                if key in data
            },
        )
        self.audit.publish(entry)

    async def generate(
        self, messages: list[dict], tools: list[dict], clock: AttemptClock
    ) -> dict:
        """One completion, with a spending reservation and retries per request."""
        if not self.ready:
            raise CostAccountingError("Model preflight must pass before generation")
        body, estimate, output = self._request(messages, tools)
        retries = self.config["budgets"]["model_retries"]
        async with httpx.AsyncClient(
            transport=self.transport, trust_env=False, follow_redirects=False
        ) as client:
            for attempt in range(retries + 1):
                remaining = clock.remaining()
                if remaining <= 0:
                    raise TimeoutError("Attempt deadline reached")
                request_id = self.ledger.reserve(
                    self.run_id,
                    "agent",
                    self.config["models"]["agent"],
                    self.prices.reservation(output),
                )
                entry = self.audit.record(
                    "model_request",
                    request_id,
                    status="pending",
                    retry=attempt,
                    estimated_input_tokens=estimate,
                    max_output_tokens=output,
                )
                try:
                    return await self._attempt(client, body, entry, clock, remaining)
                except (httpx.TransportError, TimeoutError) as error:
                    entry.update(
                        status="transport_error", error_type=type(error).__name__
                    )
                except RetryableAPIError:
                    pass
                except ModelAPIError:
                    raise
                except BaseException as error:
                    entry.update(status="failed", error_type=type(error).__name__)
                    raise
                finally:
                    # A request that may have been charged keeps its reservation,
                    # even when it failed or was cancelled.
                    self.ledger.uncertain(request_id)
                    self.audit.publish(entry)
                if attempt == retries:
                    raise ModelAPIError("Model API retries exhausted")
                await self._backoff(entry, attempt, clock)
        raise ModelAPIError("Model API did not return a completion")

    def _request(
        self, messages: list[dict], tools: list[dict]
    ) -> tuple[dict, int, int]:
        """The request body, its estimated input and the output that still fits."""
        estimate = input_tokens(
            messages,
            tools,
            self.config["models"]["agent"],
            self.config["agent_route"].get("token_encoding"),
        )
        output = min(
            self.config["budgets"]["agent_max_output_tokens"],
            self.prices.context_tokens - estimate,
        )
        if output <= 0 or estimate > self.prices.prompt_limit:
            raise ContextLimit("The next request exceeds the context window")
        body = {
            "model": self.config["models"]["agent"].removeprefix("openrouter/"),
            "messages": messages,
            "tools": tools,
            "temperature": self.config["agent_route"]["temperature"],
            "provider": routing(self.config),
            "reasoning": {"effort": self.config["agent_route"]["reasoning_effort"]},
            self.config["agent_route"]["output_token_parameter"]: output,
        }
        return body, estimate, output

    async def _attempt(
        self,
        client: httpx.AsyncClient,
        body: dict,
        entry: dict,
        clock: AttemptClock,
        remaining: float,
    ) -> dict:
        """Send one request; settle its charge and validate the completion."""
        request_id = entry["call_id"]
        response = await asyncio.wait_for(
            client.post(
                API_URL + "/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
                timeout=remaining,
            ),
            remaining,
        )
        if not response.is_success:
            entry.update(
                status="http_error",
                http_status=response.status_code,
                pre_inference_rejection=pre_inference_rejection(response),
                **error_details(response, self.api_key),
            )
            if entry["pre_inference_rejection"]:
                self.ledger.expect_unbilled(request_id, entry.get("generation_id"))
                entry["billing_status"] = "expected_zero_unconfirmed"
            self.audit.publish(entry)
            await self.reconcile_error(client, entry, clock)
            if (
                entry["pre_inference_rejection"]
                and entry.get("billing_status") != "confirmed"
            ):
                entry["billing_status"] = "expected_zero_unconfirmed"
            retryable = (
                response.status_code in (408, 429) or response.status_code >= 500
            )
            raise (RetryableAPIError if retryable else ModelAPIError)(
                "Model API request failed"
            )
        data = response.json()
        entry.update(status="received", response=data)
        charge, generation_id = billed_response(
            data, self.config["agent_route"]["provider_name"]
        )
        self.ledger.settle(request_id, charge, generation_id)
        usage = usage_fields(data)
        for name, value in usage.items():
            self.usage[name] += value
        entry.update(status="settled", billed_usd=str(charge))
        if (
            usage["prompt_tokens"] + usage["completion_tokens"]
            > self.prices.context_tokens
        ):
            raise CostAccountingError(
                "Reported usage exceeds the verified context window"
            )
        if len(data.get("choices", [])) != 1:
            raise ModelAPIError("Expected exactly one completion choice")
        message = data["choices"][0]["message"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ModelAPIError("Invalid completion message")
        return {"message": message, "usage": usage, "cost_usd": float(charge)}

    async def _backoff(self, entry: dict, attempt: int, clock: AttemptClock) -> None:
        """Exponential backoff, slower for rate limits, at least any Retry-After.

        The wait stops the attempt's clock while its allowance lasts.
        """
        timings = self.config["requests"]
        prefix = "rate_limit_retry" if entry.get("http_status") == 429 else "retry"
        delay = min(
            timings[f"{prefix}_initial_seconds"] * 2**attempt,
            timings[f"{prefix}_max_seconds"],
        )
        delay = max(delay, entry.get("retry_after_seconds", 0))
        entry["retry_wait_seconds"] = delay
        self.audit.publish(entry)
        clock.exclude(delay)
        if delay >= clock.remaining():
            raise TimeoutError("Attempt deadline reached during retry backoff")
        await asyncio.sleep(delay)
