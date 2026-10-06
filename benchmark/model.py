"""OpenRouter completions with a reservation for every physical API request."""

import asyncio
import copy
import json
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
import tiktoken

from benchmark.audit import AuditTrail
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
    pass


class ModelAPIError(RuntimeError):
    pass


def retry_after(value: str | None) -> float | None:
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


def error_details(response: httpx.Response, api_key: str) -> dict:
    """Keep diagnostic fields and correlation IDs, without credentials."""

    def clean(value):
        text = str(value).replace(api_key, "[redacted]") if api_key else str(value)
        text = re.sub(r"\b(?:sk-or-v1-|sk-|tvly-)[\w-]+", "[redacted]", text)
        text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", text)
        return text[:2048]

    try:
        data = response.json() if len(response.content) <= 16384 else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    error = data.get("error", {})
    error = error if isinstance(error, dict) else {}
    metadata = error.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    details: dict = {
        "api_error": {
            k: clean(v)
            for k, v in {
                "code": error.get("code"),
                "message": error.get("message"),
                **{
                    k: metadata.get(k)
                    for k in (
                        "provider_name",
                        "provider_code",
                        "error_type",
                        "raw",
                        "limit_source",
                        "reason",
                        "remedy_hint",
                    )
                },
            }.items()
            if v is not None
        }
    }
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
        details["provider_request_id"] = clean(request_id)
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
    """Estimate this request, without truncating history or imposing a run token cap."""
    try:
        encoding = (
            tiktoken.get_encoding(encoding_name)
            if encoding_name is not None
            else tiktoken.encoding_for_model(model.rsplit("/", 1)[-1])
        )
    except (KeyError, ValueError):
        raise CostAccountingError("The model needs a reviewed tokenizer") from None
    text = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
    # Tool/message framing is an estimate. Provider context validation remains
    # authoritative; the spending hold uses the whole verified context window.
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
        self.prices = await asyncio.to_thread(preflight, self.config, self.api_key)
        self.ready = True

    async def reconcile_error(
        self, client: httpx.AsyncClient, entry: dict, deadline: float
    ) -> None:
        generation_id = entry.get("generation_id")
        remaining = deadline - time.monotonic()
        if not generation_id or remaining <= 0:
            return
        entry["billing_status"] = "unconfirmed"
        timeout = min(remaining, self.config["live"]["preflight_timeout_seconds"])
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
            if data.get("provider_name") != self.config["live"][
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
        self, messages: list[dict], tools: list[dict], deadline: float
    ) -> dict:
        if not self.ready:
            raise CostAccountingError("Model preflight must pass before generation")
        estimate = input_tokens(
            messages,
            tools,
            self.config["models"]["agent"],
            self.config["live"].get("token_encoding"),
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
            "temperature": self.config["live"]["temperature"],
            "provider": routing(self.config),
            self.config["live"]["output_token_parameter"]: output,
        }
        if "reasoning" in self.config["live"]:
            body["reasoning"] = {"enabled": self.config["live"]["reasoning"]}
        retries = self.config["budgets"]["model_retries"]
        async with httpx.AsyncClient(
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            for attempt in range(retries + 1):
                remaining = deadline - time.monotonic()
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
                retryable = False
                try:
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
                        retryable = (
                            response.status_code in (408, 429)
                            or response.status_code >= 500
                        )
                        entry.update(
                            status="http_error",
                            http_status=response.status_code,
                            **error_details(response, self.api_key),
                        )
                        self.audit.publish(entry)
                        await self.reconcile_error(client, entry, deadline)
                        raise ModelAPIError("Model API request failed")
                    data = response.json()
                    entry.update(status="received", response=data)
                    charge, generation_id = billed_response(
                        data, self.config["live"]["provider_name"]
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
                    if (
                        not isinstance(message, dict)
                        or message.get("role") != "assistant"
                    ):
                        raise ModelAPIError("Invalid completion message")
                    return {
                        "message": message,
                        "usage": usage,
                        "cost_usd": float(charge),
                    }
                except (httpx.TransportError, TimeoutError) as error:
                    retryable = True
                    entry.update(
                        status="transport_error", error_type=type(error).__name__
                    )
                except ModelAPIError:
                    if not retryable:
                        raise
                except BaseException as error:
                    entry.update(status="failed", error_type=type(error).__name__)
                    raise
                finally:
                    # Settled requests are unchanged; all uncertain charges retain
                    # their holds, including cancellations and invalid billing.
                    self.ledger.uncertain(request_id)
                    self.audit.publish(entry)
                if attempt == retries:
                    raise ModelAPIError("Model API retries exhausted")
                delay = min(
                    self.config["live"]["retry_initial_seconds"] * 2**attempt,
                    self.config["live"]["retry_max_seconds"],
                )
                delay = max(delay, entry.get("retry_after_seconds", 0))
                entry["retry_wait_seconds"] = delay
                self.audit.publish(entry)
                if delay >= deadline - time.monotonic():
                    raise TimeoutError("Attempt deadline reached during retry backoff")
                await asyncio.sleep(delay)
        raise ModelAPIError("Model API did not return a completion")
