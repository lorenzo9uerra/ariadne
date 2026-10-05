"""OpenRouter completions with a reservation for every physical API request."""

import asyncio
import copy
import json
import time

import httpx
import tiktoken

from benchmark.audit import AuditTrail
from benchmark.costs import (
    API_URL,
    CostAccountingError,
    Ledger,
    Prices,
    billed_response,
    preflight,
    routing,
)


class ContextLimit(RuntimeError):
    pass


class ModelAPIError(RuntimeError):
    pass


def input_tokens(messages: list[dict], tools: list[dict], model: str) -> int:
    """Estimate this request, without truncating history or imposing a run token cap."""
    try:
        encoding = tiktoken.encoding_for_model(model.rsplit("/", 1)[-1])
    except KeyError:
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

    async def generate(
        self, messages: list[dict], tools: list[dict], deadline: float
    ) -> dict:
        if not self.ready:
            raise CostAccountingError("Model preflight must pass before generation")
        estimate = input_tokens(messages, tools, self.config["models"]["agent"])
        output = min(
            self.config["budgets"]["agent_max_output_tokens"],
            self.prices.context_tokens - estimate,
        )
        if output <= 0:
            raise ContextLimit("The next request exceeds the context window")
        body = {
            "model": self.config["models"]["agent"].removeprefix("openrouter/"),
            "messages": messages,
            "tools": tools,
            "temperature": self.config["live"]["temperature"],
            "provider": routing(self.config),
            self.config["live"]["output_token_parameter"]: output,
        }
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
                            status="http_error", http_status=response.status_code
                        )
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
                except (httpx.TransportError, TimeoutError):
                    retryable = True
                    entry.update(status="transport_error")
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
                if delay >= deadline - time.monotonic():
                    raise TimeoutError("Attempt deadline reached during retry backoff")
                await asyncio.sleep(delay)
        raise ModelAPIError("Model API did not return a completion")
