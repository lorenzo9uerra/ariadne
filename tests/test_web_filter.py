"""Classifier adapters with mocked HTTP: no provider requests and no spending."""

import asyncio
import html
import json
import zlib

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmark import classifiers, web, web_filter
from benchmark.audit import AuditTrail
from benchmark.budgets import load_draft
from benchmark.costs import Ledger
from tests.support import GOOD, chat_reply, endpoint_data

CONTEXT = {"challenge_id": "demo-01", "answer_type": "flag"}
CANDIDATE = {
    "text": "Generic library documentation.",
    "url": "https://docs.example.org/",
}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def sleep(seconds):
        return None

    monkeypatch.setattr(classifiers.asyncio, "sleep", sleep)
    monkeypatch.setattr(classifiers, "read_json", lambda *args: endpoint_data())


def classifier(name="mistral-small", **budget_overrides):
    config = load_draft()
    config["budgets"] = config["budgets"] | budget_overrides
    return classifiers.load_classifiers(config, [name])[0]


def message():
    return classifiers.render_user_message("response", "web_fetch", CONTEXT, CANDIDATE)


def run(candidate, handler, tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3", "10")
    requests = []

    async def recorded(request):
        requests.append(json.loads(request.content))
        result = handler(len(requests))
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(recorded)) as client:
            return await candidate.classify(
                client,
                message(),
                "test-key",
                ledger,
                "run-1",
                "classifier",
                "DeepInfra",
            )

    return asyncio.run(go()), requests, ledger


@given(st.text(), st.one_of(st.none(), st.text()), st.text())
def test_the_candidate_reaches_the_classifier_intact_inside_its_wrapper(
    text, url, note
):
    candidate = {"text": text, "url": url}
    rendered = classifiers.render_user_message(
        "response", "web_fetch", CONTEXT | {"note": note}, candidate
    )
    # Neither the page nor the context can open or close a wrapper.
    assert rendered.count("<candidate>") == rendered.count("</candidate>") == 1
    assert rendered.count("<review_record>") == 1
    inner = rendered.split("<candidate>\n", 1)[1].rsplit("\n</candidate>", 1)[0]
    assert json.loads(html.unescape(inner)) == candidate
    assert rendered.endswith(classifiers.REMINDER)


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "{}",
        '{"verdict":"allow","reason":"ok","extra":true}',
        '{"verdict":"allow","reason":""}',
        '{"verdict":"allow","verdict":"forbidden","reason":"ok"}',
        '{"verdict":"maybe","reason":"ok"}',
        json.dumps({"verdict": "forbidden", "reason": "r" * 241}),
    ],
)
def test_a_classifier_verdict_must_be_exact(raw):
    with pytest.raises(ValueError):
        classifiers.parse_verdict(raw)
    reply = {"reason": "r" * 240, "verdict": "forbidden"}
    assert classifiers.parse_verdict(json.dumps(reply)) == reply


def test_candidate_fields_must_match_stage_and_tool():
    with pytest.raises(ValueError):
        classifiers.render_user_message("request", "web_search", CONTEXT, {"url": "x"})


def test_chat_success_settles_the_billed_charge(tmp_path):
    result, requests, ledger = run(classifier(), lambda n: chat_reply(GOOD), tmp_path)
    assert (result.status, result.verdict) == ("ok", "allow")
    body = requests[0]
    assert body["temperature"] == 0 and body["seed"] == 20261001
    assert body["provider"]["only"] == ["deepinfra/fp8"]
    assert body["provider"]["allow_fallbacks"] is False
    assert body["response_format"]["json_schema"]["strict"] is True
    assert ledger.totals()["billed_usd"] == pytest.approx(0.000012)


def test_reasoning_is_disabled_where_configured(tmp_path):
    _, requests, _ = run(
        classifier("deepseek-flash"), lambda n: chat_reply(GOOD), tmp_path
    )
    assert requests[0]["reasoning"] == {"enabled": False}


@pytest.mark.parametrize(
    "replies,status,sent",
    [
        ([chat_reply("not json")], "invalid", 1),
        ([chat_reply(GOOD, finish="length")], "invalid", 1),
        ([httpx.Response(503), chat_reply(GOOD)], "ok", 2),
        ([httpx.Response(429, headers={"Retry-After": "60"})], "provider_error", 1),
        ([httpx.Response(401)], "provider_error", 1),
    ],
    ids=["invalid", "truncated", "transient", "retry-after-too-late", "auth"],
)
def test_only_a_transient_error_is_retried_and_only_once(
    tmp_path, replies, status, sent
):
    result, requests, _ = run(classifier(), lambda n: replies[n - 1], tmp_path)
    assert result.status == status and len(requests) == sent


def test_deadline_keeps_the_hold(tmp_path):
    async def slow(n):
        await asyncio.get_running_loop().create_future()  # never answers

    candidate = classifier(filter_timeout_seconds=0.05)
    result, _, ledger = run(candidate, slow, tmp_path)
    assert result.status == "timeout"
    assert ledger.totals()["held_usd"] > 0


def test_oversized_request_is_rejected_before_reserving(tmp_path):
    result, requests, ledger = run(
        classifier(filter_max_input_tokens=50), lambda n: chat_reply(GOOD), tmp_path
    )
    assert result.status == "oversized" and not requests
    assert ledger.totals()["held_usd"] == 0


# The selected classifier behind the web tools: real session, tools and adapter,
# with the provider mocked.


class Backend:
    def __init__(self, text="Generic library documentation."):
        self.text, self.gets = text, []

    async def search(self, query, max_results):
        return []

    async def resolve(self, host):
        return ["1.1.1.1"]

    async def get(self, url, addresses, max_bytes):
        from benchmark.web import HTTPReply

        self.gets.append(url)
        return HTTPReply(200, self.text.encode())


def live_fetch(
    tmp_path, handler, context=CONTEXT, text="Generic library documentation."
):
    from benchmark.policy import Session, execute_benchmark_tools
    from tests.support import call, web_tools

    config = load_draft()
    ledger = Ledger(tmp_path / "ledger.sqlite3", "10")
    bodies = []

    def recorded(request):
        bodies.append(json.loads(request.content))
        return handler(len(bodies))

    classifier = web_filter.filter_classifier(
        config, ledger, "test-key", "run-1", httpx.MockTransport(recorded)
    )
    session = Session(config, classifier, context, AuditTrail("run-1", "sample-1"))
    backend = Backend(text)
    proposal = call("c1", "web_fetch", {"url": CANDIDATE["url"]})

    async def go():
        await classifier.check_route(1)
        return await execute_benchmark_tools(
            [proposal], web_tools(session, backend), session
        )

    try:
        result = asyncio.run(go())
    except Exception as error:  # A stopped attempt surfaces as termination.
        result = error
    return result, session, bodies, ledger, backend


def test_model_classifier_is_the_configured_classifier(tmp_path):
    # Changing it needs a new comparison (docs/classifier_selection.md).
    assert load_draft()["models"]["web_filter"] == "deepseek-flash"
    # Contamination labels come from a different model (protocol section 13.2).
    assert load_draft()["autoreview"]["labelling_model"] != "deepseek-flash"
    ledger = Ledger(tmp_path / "ledger.sqlite3", "10")
    classifier = web_filter.filter_classifier(load_draft(), ledger, "k", "r")
    assert classifier.model.settings["model"] == "deepseek/deepseek-v4.1-flash"


def test_live_classifier_requires_preflight(tmp_path):
    from benchmark.costs import CostAccountingError

    classifier = web_filter.filter_classifier(
        load_draft(), Ledger(tmp_path / "ledger", 10), "test-key", "run"
    )
    with pytest.raises(CostAccountingError, match="preflight"):
        asyncio.run(classifier({"reviewer_context": CONTEXT}, 512, 675840))


@pytest.mark.parametrize(
    "change", ["price", "context", "parameters", "provider", "route"]
)
def test_changed_live_classifier_metadata_blocks_route(tmp_path, monkeypatch, change):
    from benchmark.costs import CostAccountingError

    data = endpoint_data()
    endpoint = data["endpoints"][0]
    if change == "price":
        endpoint["pricing"]["prompt"] = "0.1"
    elif change == "context":
        endpoint["context_length"] = 1
    elif change == "parameters":
        endpoint["supported_parameters"] = []
    elif change == "provider":
        endpoint["provider_name"] = None
    else:
        endpoint["tag"] = "other"
    monkeypatch.setattr(classifiers, "read_json", lambda *args: data)
    classifier = web_filter.filter_classifier(
        load_draft(), Ledger(tmp_path / "ledger", 10), "test-key", "run"
    )
    with pytest.raises(CostAccountingError):
        asyncio.run(classifier.check_route(1))
    assert classifier.billing_provider is None


@pytest.mark.parametrize("change", ["cost", "provider", "byok", "usage", "oversized"])
def test_unverified_live_classifier_response_never_reaches_backend(tmp_path, change):
    data = json.loads(chat_reply(GOOD).content)
    if change == "cost":
        data["usage"].pop("cost")
    elif change == "provider":
        data["provider"] = "unexpected"
    elif change == "byok":
        data["usage"]["is_byok"] = True
    elif change == "usage":
        data["usage"]["prompt_tokens"] = True
    else:
        data["usage"]["prompt_tokens"] = 32769
    result, session, _, ledger, backend = live_fetch(
        tmp_path, lambda n: httpx.Response(200, json=data)
    )
    assert isinstance(result, Exception) and not backend.gets
    assert session.failures
    if change in ("cost", "provider", "byok"):
        assert ledger.totals()["held_usd"] > 0


def test_live_review_sends_the_compared_request_and_delivers_allowed_text(tmp_path):
    result, session, bodies, ledger, _ = live_fetch(
        tmp_path, lambda n: chat_reply(GOOD)
    )
    assert "Generic library documentation." in result[0]["text"]
    sent = [body["messages"][1]["content"] for body in bodies]
    assert sent == [
        classifiers.render_user_message(
            "request",
            "web_fetch",
            CONTEXT,
            {"url": CANDIDATE["url"], "cached_snippet": None},
        ),
        classifiers.render_user_message(
            "response",
            "web_fetch",
            CONTEXT,
            {"text": "Generic library documentation.", "url": CANDIDATE["url"]},
        ),
    ]
    assert bodies[0]["provider"]["order"] == ["deepinfra/fp8"]
    assert session.counters.filter_tokens == 2 * 920
    assert ledger.totals()["billed_usd"] > 0
    decision = next(i for i in session.audit.items if i["stage"] == "response")
    assert decision["filter_details"]["classifier"] == "deepseek-flash"


def test_live_forbidden_response_is_withheld(tmp_path):
    from benchmark.web_filter import WITHHELD

    forbidden = json.dumps({"reason": "A writeup.", "verdict": "forbidden"})
    replies = {1: chat_reply(GOOD), 2: chat_reply(forbidden)}
    result, *_ = live_fetch(tmp_path, lambda n: replies[n], text="A writeup.")
    assert result[0]["text"] == WITHHELD


def test_live_provider_failure_stops_before_retrieval(tmp_path):
    result, session, _, _, backend = live_fetch(tmp_path, lambda n: httpx.Response(401))
    assert "termination" in str(result) and not backend.gets
    assert session.failures == {"c1": "infrastructure_error"}
    request = next(i for i in session.audit.items if i["stage"] == "request")
    assert request["verdict"] == "error" and "provider_error" in request["error_detail"]


def test_live_oversized_context_is_never_sent(tmp_path):
    context = CONTEXT | {"task_summary": "word " * 5000}
    result, session, bodies, _ledger, backend = live_fetch(
        tmp_path, lambda n: chat_reply(GOOD), context
    )
    assert "termination" in str(result) and not bodies and not backend.gets
    request = next(i for i in session.audit.items if i["stage"] == "request")
    assert request["error_type"] == "OversizedRequest"


# The live backend, with the network mocked.


def backend(tmp_path, handler, limits=None):
    from benchmark.web import NetworkBackend

    config = load_draft()["web"] | (limits or {})
    ledger = Ledger(tmp_path / "ledger.sqlite3", "10")
    seen = []

    def recorded(request):
        seen.append(request)
        return handler(request)

    live = NetworkBackend(
        "tvly-test", ledger, "run-1", config, httpx.MockTransport(recorded)
    )
    return live, seen, ledger


def test_tavily_search_is_basic_and_billed_by_credit(tmp_path):
    reply = {
        "results": [{"title": "T", "url": "https://a.org/", "content": "C"}],
        "usage": {"credits": 1},
    }
    live, seen, ledger = backend(tmp_path, lambda r: httpx.Response(200, json=reply))
    hits = asyncio.run(live.search("sha-256 padding", 5))
    assert [(h.title, h.url, h.snippet) for h in hits] == [("T", "https://a.org/", "C")]
    body = json.loads(seen[0].content)
    assert body["search_depth"] == "basic" and body["max_results"] == 5
    assert not body["include_raw_content"] and not body["include_answer"]
    assert seen[0].headers["authorization"] == "Bearer tvly-test"
    assert ledger.totals()["billed_usd"] == 0.008
    assert ledger.totals()["held_usd"] == 0


def test_tavily_failure_keeps_the_hold_and_raises(tmp_path):
    live, _, ledger = backend(tmp_path, lambda r: httpx.Response(432))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(live.search("q", 5))
    assert ledger.totals()["held_usd"] > 0


def test_fetch_connects_to_the_validated_address_with_the_real_hostname(tmp_path):
    live, seen, _ = backend(
        tmp_path,
        lambda r: httpx.Response(
            302,
            headers={"location": "/next", "content-type": "text/html"},
            stream=httpx.ByteStream(b""),
        ),
    )
    reply = asyncio.run(
        live.get("https://docs.example.org/a?b=1", ("93.184.215.14",), 100)
    )
    request = seen[0]
    assert request.url.host == "93.184.215.14" and request.url.query == b"b=1"
    assert request.headers["host"] == "docs.example.org"
    assert request.extensions["sni_hostname"] == "docs.example.org"
    assert (reply.status, reply.location) == (302, "/next")  # Not followed here.


@pytest.mark.parametrize("credits", [None, True, 2, -1])
def test_unverified_search_billing_keeps_hold_and_rejects_results(tmp_path, credits):
    from benchmark.costs import CostAccountingError

    live, _, ledger = backend(
        tmp_path,
        lambda r: httpx.Response(
            200, json={"results": [], "usage": {"credits": credits}}
        ),
    )
    with pytest.raises(CostAccountingError):
        asyncio.run(live.search("general documentation", 5))
    assert ledger.totals()["held_usd"] == pytest.approx(0.008)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@given(
    st.binary(max_size=3000),
    st.sampled_from(["identity", "gzip", "deflate"]),
    st.integers(1, 2000),
    st.lists(st.integers(1, 500), max_size=8),
    st.booleans(),
)
def test_a_body_is_read_whole_or_known_to_be_incomplete(
    body, encoding, max_bytes, sizes, cut
):
    cut = cut and encoding != "identity"  # Only a compressed stream can end early.
    encoded = body
    if encoding != "identity":
        wbits = zlib.MAX_WBITS | (16 if encoding == "gzip" else 0)
        encoder = zlib.compressobj(wbits=wbits)
        encoded = encoder.compress(body) + encoder.flush()
        if cut:
            encoded = encoded[:-1]
    chunks, rest = [], encoded
    for size in sizes:
        chunks.append(rest[:size])
        rest = rest[size:]
    chunks.append(rest)
    response = httpx.Response(
        200, headers={"content-encoding": encoding}, stream=Chunks(chunks)
    )
    try:
        read = asyncio.run(web.read_body(response, max_bytes))
    except ValueError:
        assert encoding != "identity"  # Too large or damaged before decoding.
        return
    # What comes back is always the start of the body, and a result within the
    # limit is the whole body. Anything longer is rejected by fetch_text.
    assert read == body[: len(read)] and len(read) <= max_bytes + 1
    assert len(read) == max_bytes + 1 or (read == body and not cut)


def test_an_unsupported_http_encoding_is_rejected(tmp_path):
    live, _, _ = backend(
        tmp_path,
        lambda r: httpx.Response(
            200, headers={"content-encoding": "br"}, stream=httpx.ByteStream(b"opaque")
        ),
    )
    with pytest.raises(ValueError, match="encoding"):
        asyncio.run(live.get("https://docs.example.org/", ("1.1.1.1",), 100))
