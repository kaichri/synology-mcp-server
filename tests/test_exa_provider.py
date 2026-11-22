"""Provider contracts and failure routing, without live requests or real keys."""
import asyncio
import json

import httpx
import pytest

import exa_provider as exa
import server

KEY = "test-only-exa-key-not-a-secret"
URL = "https://example.org/a"
URL2 = "https://example.org/b"


def run(awaitable):
    return asyncio.run(awaitable)


def direct_with(handler):
    return exa.DirectExaApiProvider(KEY, transport=httpx.MockTransport(handler))


class FakeProvider:
    def __init__(self, error=None, results=None):
        self.error = error
        self.results = results or [{"url": URL, "title": "Source", "text": "Evidence"}]
        self.calls = []

    async def search(self, request, *, deep=False):
        self.calls.append(("search", request, deep))
        if self.error:
            raise self.error
        return exa.envelope("fake", [exa.page(p, "fake") for p in self.results])

    async def contents(self, urls):
        self.calls.append(("contents", urls))
        if self.error:
            raise self.error
        return exa.envelope("fake", [exa.page({"url": u, "text": "Contents"}, "fake", u) for u in urls])


def router(direct, remote=None, *, mode="auto", key=KEY, clock=None, http_reader=None):
    config = exa.Config(key, mode, 100, 10)
    breaker = exa.CircuitBreaker(clock) if clock else exa.CircuitBreaker()
    return exa.ExaRouter(config, remote or FakeProvider(), direct=direct,
                         circuit=breaker, http_reader=http_reader)


def test_direct_search_schema_and_metadata():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [{"url": URL, "title": "Primary", "author": "Writer",
                                                     "publishedDate": "2026-10-02", "highlights": ["Fact"]}],
                                         "requestId": "request-1", "costDollars": {"total": 0.005}})
    result = run(direct_with(handler).search(exa.SearchRequest("A rich query", 99)))
    request = calls[0]
    payload = json.loads(request.content)
    assert request.method == "POST" and str(request.url) == "https://api.exa.ai/search"
    assert request.headers["x-api-key"] == KEY
    assert payload == {"query": "A rich query", "objective": exa.OBJECTIVE,
                       "numResults": 20, "type": "auto", "contents": {"highlights": {"maxCharacters": 1500}}}
    assert payload["objective"] != payload["query"]
    assert "original sources" in payload["objective"]
    assert result["provider"] == "exa_direct" and result["request_id"] == "request-1"
    assert result["results"][0]["published_at"] == "2026-10-02"
    assert result["results"][0]["author"] == "Writer"
    assert result["results"][0]["complete"] is None
    assert result["usage"] == {"estimated_cost_dollars": {"total": 0.005}, "billing_exact": False}


@pytest.mark.parametrize("urls", [[URL], [URL, URL2]])
def test_direct_contents_uses_urls_and_maps_reordered_results(urls):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [{"id": u, "url": u, "text": "Content for " + u}
                                                     for u in reversed(urls)]})
    result = run(direct_with(handler).contents(urls))
    assert str(requests[0].url) == "https://api.exa.ai/contents"
    assert json.loads(requests[0].content) == {"urls": urls, "text": {"maxCharacters": 20000}}
    assert [p["requested_url"] for p in result["results"]] == urls
    assert [p["text"] for p in result["results"]] == ["Content for " + u for u in urls]
    assert result["usage"] is None


def test_canonicalized_single_contents_and_bounded_text():
    provider = direct_with(lambda r: httpx.Response(200, json={"results": [{"url": URL2, "text": "x" * 21000}]}))
    result = run(provider.contents([URL]))["results"][0]
    assert result["url"] == URL2 and result["requested_url"] == URL
    assert len(result["text"]) == 20000 and result["truncated"]
    assert result["complete"] is None


def test_deep_is_explicit_and_preserves_grounded_output():
    calls = []
    output = {"content": "Answer", "grounding": [{"field": "answer", "citations": [{"url": URL}]}]}
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [{"url": URL}], "output": output})
    result = run(direct_with(handler).search(exa.SearchRequest("Compare approaches"), deep=True))
    assert calls[0]["type"] == "deep" and calls[0]["numResults"] == 5
    assert calls[0]["outputSchema"]["type"] == "text"
    assert result["output"] == output


@pytest.mark.parametrize("status,tag,code", [
    (401, "INVALID_API_KEY", "EXA_AUTH_ERROR"), (403, "FEATURE_DISABLED", "EXA_AUTH_ERROR"),
    (402, "NO_MORE_CREDITS", "EXA_QUOTA_EXHAUSTED"),
    (403, "API_KEY_BUDGET_EXCEEDED", "EXA_QUOTA_EXHAUSTED"),
    (429, "TEAM_BUDGET_EXCEEDED", "EXA_QUOTA_EXHAUSTED"),
    (429, "RATE_LIMIT_EXCEEDED", "EXA_RATE_LIMITED"), (500, None, "EXA_UNAVAILABLE"),
    (503, "SERVICE_OVERLOADED", "EXA_UNAVAILABLE"), (504, None, "EXA_TIMEOUT"),
    (408, None, "EXA_TIMEOUT"), (400, "INVALID_REQUEST_BODY", "EXA_INVALID_REQUEST"),
    (422, None, "EXA_INVALID_REQUEST"), (403, "PROHIBITED_CONTENT", "EXA_INVALID_REQUEST"),
    (403, "CONTENT_FILTER_ERROR", "EXA_INVALID_REQUEST"), (418, "UNRECOGNIZED", "EXA_UNKNOWN_ERROR"),
])
def test_documented_classification_and_no_secret_errors(status, tag, code, caplog):
    provider = direct_with(lambda r: httpx.Response(status, json={"tag": tag, "error": KEY}))
    with pytest.raises(exa.ExaError) as caught:
        run(provider.search(exa.SearchRequest("query")))
    assert caught.value.code == code
    assert KEY not in str(caught.value) + repr(caught.value) + json.dumps(caught.value.as_dict()) + caplog.text


@pytest.mark.parametrize("error,code", [(httpx.ReadTimeout(KEY), "EXA_TIMEOUT"),
                                       (httpx.ConnectError(KEY), "EXA_UNAVAILABLE"),
                                       (httpx.RemoteProtocolError(KEY), "EXA_UNAVAILABLE")])
def test_network_errors_are_safe(error, code):
    def handler(request):
        raise error
    with pytest.raises(exa.ExaError) as caught:
        run(direct_with(handler).search(exa.SearchRequest("query")))
    assert caught.value.code == code and KEY not in str(caught.value)


@pytest.mark.parametrize("code", sorted(exa.RETRYABLE))
def test_direct_to_remote_fallback_search_and_fetch(code):
    direct = FakeProvider(exa.ExaError(code))
    remote = FakeProvider()
    service = router(direct, remote)
    result = run(service.search(exa.SearchRequest("query")))
    assert result["fallbacks"][0]["code"] == code
    assert result["fallbacks"][0]["configuration_error"] == (code == "EXA_AUTH_ERROR")
    fetched = run(service.contents([URL]))
    assert fetched["results"][0]["text"] == "Contents"
    assert [c[0] for c in remote.calls] == ["search", "contents"]


@pytest.mark.parametrize("code", ["EXA_INVALID_REQUEST", "EXA_UNKNOWN_ERROR"])
def test_invalid_and_unknown_requests_do_not_fall_back(code):
    remote = FakeProvider()
    with pytest.raises(exa.ExaError, match=code):
        run(router(FakeProvider(exa.ExaError(code)), remote).search(exa.SearchRequest("query")))
    assert remote.calls == []


def test_direct_mode_never_uses_remote_or_http():
    remote, reader = FakeProvider(), FakeProvider()
    with pytest.raises(exa.ExaError, match="EXA_QUOTA_EXHAUSTED"):
        run(router(FakeProvider(exa.ExaError("EXA_QUOTA_EXHAUSTED")), remote,
                   mode="direct", http_reader=reader).contents([URL]))
    assert remote.calls == reader.calls == []


@pytest.mark.parametrize("mode,key,uses_direct", [("auto", "", False), ("auto", KEY, True),
                                                 ("remote_mcp", KEY, False)])
def test_provider_selection(mode, key, uses_direct):
    direct, remote = FakeProvider(), FakeProvider()
    run(router(direct, remote, mode=mode, key=key).search(exa.SearchRequest("query")))
    assert bool(direct.calls) == uses_direct
    assert bool(remote.calls) != uses_direct


def test_missing_key_direct_mode_and_repr():
    config = exa.Config(KEY)
    assert KEY not in repr(config)
    remote = FakeProvider()
    with pytest.raises(exa.ExaError) as caught:
        run(router(exa.DirectExaApiProvider(""), remote, mode="direct", key="").search(exa.SearchRequest("query")))
    assert caught.value.code == "EXA_AUTH_ERROR" and caught.value.reason == "api_key_missing"
    assert not remote.calls


@pytest.mark.parametrize("code,cooldown", [("EXA_QUOTA_EXHAUSTED", 100), ("EXA_RATE_LIMITED", 10)])
def test_breaker_shared_search_fetch_and_expires(code, cooldown):
    timestamp = [1000]
    direct, remote = FakeProvider(exa.ExaError(code)), FakeProvider()
    service = router(direct, remote, clock=lambda: timestamp[0])
    run(service.search(exa.SearchRequest("query")))
    direct.error = None
    result = run(service.contents([URL]))
    assert len(direct.calls) == 1 and result["fallbacks"][0]["reason"] == "direct_circuit_open"
    timestamp[0] += cooldown - 1
    run(service.search(exa.SearchRequest("query")))
    assert len(direct.calls) == 1
    timestamp[0] += 1
    result = run(service.search(exa.SearchRequest("query")))
    assert len(direct.calls) == 2 and result["fallbacks"] == []


def test_shared_breaker_survives_router_recreation_and_key_change():
    config = exa.Config("isolated-key-for-breaker", quota_cooldown=12)
    first = exa.shared_circuit(config)
    first.record(exa.ExaError("EXA_QUOTA_EXHAUSTED"), config)
    assert exa.shared_circuit(config) is first
    assert exa.shared_circuit(exa.Config("different-key-for-breaker", quota_cooldown=12)) is not first


def test_contents_partial_failure_retries_only_failed_urls():
    provider = direct_with(lambda r: httpx.Response(200, json={
        "results": [{"id": URL, "url": URL, "text": "Success"}],
        "statuses": [{"id": URL, "status": "success"},
                     {"id": URL2, "status": "error", "error": {"tag": "CRAWL_TIMEOUT", "httpStatusCode": 504}}]}))
    remote = FakeProvider()
    service = router(provider, remote)
    result = run(service.contents([URL, URL2]))
    assert result["results"][0]["text"] == "Success"
    assert result["results"][1]["text"] == "Contents"
    assert remote.calls == [("contents", [URL2])]
    service.circuit.check()  # Target URL errors never open the account breaker.


def test_remote_partial_failure_is_not_recorded_as_direct_quota_or_retried():
    provider = direct_with(lambda r: httpx.Response(200, json={"results": [], "statuses": []}))
    remote = FakeProvider(exa.ExaError("EXA_QUOTA_EXHAUSTED"))
    service = router(provider, remote)
    with pytest.raises(exa.ExaError, match="EXA_QUOTA_EXHAUSTED"):
        run(service.contents([URL]))
    assert remote.calls == [("contents", [URL])]
    service.circuit.check()


def test_target_403_is_not_auth_or_quota_and_not_retried():
    provider = direct_with(lambda r: httpx.Response(200, json={"results": [], "statuses": [
        {"id": URL, "status": "error", "error": {"tag": "CRAWL_HTTP_403", "httpStatusCode": 403}}]}))
    remote = FakeProvider()
    service = router(provider, remote)
    result = run(service.contents([URL]))
    assert result["results"][0]["error"]["code"] == "EXA_INVALID_REQUEST"
    assert remote.calls == []
    service.circuit.check()


def test_fetch_http_reader_extension_is_optional_and_last():
    direct = FakeProvider(exa.ExaError("EXA_QUOTA_EXHAUSTED"))
    remote = FakeProvider(exa.ExaError("EXA_UNAVAILABLE"))
    reader = FakeProvider()
    result = run(router(direct, remote, http_reader=reader).contents([URL]))
    assert result["results"][0]["text"] == "Contents"
    assert [f["provider"] for f in result["fallbacks"]] == ["exa_direct", "exa_remote_mcp"]
    assert reader.calls == [("contents", [URL])]


def test_both_providers_fail_safely_and_no_implicit_third_search():
    with pytest.raises(exa.ExaError, match="EXA_UNAVAILABLE"):
        run(router(FakeProvider(exa.ExaError("EXA_TIMEOUT")), FakeProvider(exa.ExaError("EXA_UNAVAILABLE")))
            .search(exa.SearchRequest("query")))


def test_remote_actual_contract_and_batch_markdown_order():
    calls = []
    async def call(tool, args):
        calls.append((tool, args))
        if tool == "web_search_exa":
            return "Title: First\nURL: " + URL + "\nText: Evidence"
        return f"# Second\nURL: {URL2}\n\nSecond content\n\n# First\nURL: {URL}\n\nFirst content"
    provider = exa.RemoteExaMcpProvider(call)
    searched = run(provider.search(exa.SearchRequest("query", 3)))
    fetched = run(provider.contents([URL, URL2]))
    assert calls == [("web_search_exa", {"query": "query", "numResults": 3, "objective": exa.OBJECTIVE}),
                     ("web_fetch_exa", {"urls": [URL, URL2], "maxCharacters": 20000})]
    assert searched["results"][0]["url"] == URL
    assert fetched["results"][0]["text"].strip() == "First content"
    assert fetched["results"][1]["text"].strip() == "Second content"
    assert fetched["usage"] is None


def test_remote_unmapped_batch_does_not_attribute_other_pages_content():
    async def call(tool, args):
        return "Opaque content without source URLs"
    result = run(exa.RemoteExaMcpProvider(call).contents([URL, URL2]))
    assert all(p["text"] == "" and p["error"]["code"] == "EXA_UNAVAILABLE" for p in result["results"])


def test_no_fake_deep_fallback():
    async def call(tool, args):
        pytest.fail("Remote MCP has no verified Deep Search mode")
    with pytest.raises(exa.ExaError) as caught:
        run(router(FakeProvider(), exa.RemoteExaMcpProvider(call), key="").search(exa.SearchRequest("query"), deep=True))
    assert caught.value.reason == "remote_deep_not_supported"
    remote = FakeProvider()
    with pytest.raises(exa.ExaError, match="EXA_QUOTA_EXHAUSTED"):
        run(router(FakeProvider(exa.ExaError("EXA_QUOTA_EXHAUSTED")), remote).search(exa.SearchRequest("query"), deep=True))
    assert remote.calls == []


def test_search_and_fetch_one_batch_in_rank_order_and_total_limit():
    calls = []
    urls = [f"https://example.org/{i}" for i in range(20)]
    def handler(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        selected = urls if request.url.path == "/search" else list(reversed(payload["urls"]))
        return httpx.Response(200, json={"results": [{"id": u, "url": u, "text": "x" * 10000} for u in selected]})
    result = run(router(direct_with(handler)).search_and_fetch("query", 100, 100))
    assert len(calls) == 2 and calls[1][1]["urls"] == urls[:5]
    assert len(result["search"]["results"]) == 20 and len(result["contents"]["results"]) == 5
    assert [p["requested_url"] for p in result["contents"]["results"]] == urls[:5]
    assert sum(len(p["text"]) for group in ["search", "contents"] for p in result[group]["results"]) <= 50000
    assert all(p["text"] for p in result["contents"]["results"])
    assert "raw_text" not in result["search"] and "raw_text" not in result["contents"]


def test_search_and_fetch_zero_fetch_and_empty_results():
    direct = FakeProvider()
    result = run(router(direct).search_and_fetch("query", 10, 0))
    assert result["contents"]["results"] == [] and len(direct.calls) == 1


@pytest.mark.parametrize("url", ["file:///secret", "http://127.0.0.1", "https://localhost/a",
                                 "https://192.168.1.2", "https://user:password@example.org", "https://example.org:999"])
def test_invalid_url_never_reaches_provider(url):
    direct = FakeProvider()
    with pytest.raises(exa.ExaError, match="EXA_INVALID_REQUEST"):
        run(router(direct).contents([url]))
    assert not direct.calls


@pytest.mark.parametrize("query", ["", " ", "x" * 4097])
def test_invalid_query(query):
    with pytest.raises(exa.ExaError, match="EXA_INVALID_REQUEST"):
        exa.SearchRequest(query)


def test_configuration_and_secrets(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", KEY)
    monkeypatch.setenv("EXA_PROVIDER", "auto")
    assert exa.Config.from_env().key == KEY
    monkeypatch.setenv("EXA_PROVIDER", "invalid")
    with pytest.raises(exa.ExaError, match="EXA_INVALID_REQUEST"):
        exa.Config.from_env()
    monkeypatch.setenv("EXA_PROVIDER", "auto")
    monkeypatch.setenv("EXA_RATE_LIMIT_COOLDOWN_SECONDS", "not-a-number")
    with pytest.raises(exa.ExaError, match="EXA_INVALID_REQUEST"):
        exa.Config.from_env()


def test_success_echoed_key_is_redacted_and_remote_receives_no_key():
    direct = direct_with(lambda r: httpx.Response(200, json={"results": [{"url": URL, "title": KEY, "text": KEY}],
                                                           "requestId": KEY}))
    assert KEY not in json.dumps(run(direct.search(exa.SearchRequest("query"))))
    async def call(tool, args):
        assert KEY not in json.dumps(args)
        return KEY
    result = run(exa.RemoteExaMcpProvider(call, KEY).search(exa.SearchRequest("query")))
    assert KEY not in json.dumps(result)


def test_mcp_exception_group_and_raw_errors_are_safe():
    async def call(tool, args):
        raise ExceptionGroup("failed " + KEY, [httpx.ReadTimeout(KEY)])
    with pytest.raises(exa.ExaError) as caught:
        run(exa.RemoteExaMcpProvider(call).search(exa.SearchRequest("query")))
    assert caught.value.code == "EXA_TIMEOUT" and KEY not in str(caught.value)


def test_malformed_and_oversized_response():
    for response in [httpx.Response(200, text="not-json " + KEY),
                     httpx.Response(200, json={"results": None}),
                     httpx.Response(200, content=b"x" * (exa.MAX_RESPONSE_BYTES + 1))]:
        with pytest.raises(exa.ExaError, match="EXA_UNAVAILABLE"):
            run(direct_with(lambda r: response).search(exa.SearchRequest("query")))


def test_outer_direct_timeout_falls_back(monkeypatch):
    class Slow(FakeProvider):
        async def search(self, request, *, deep=False):
            await asyncio.sleep(1)
    monkeypatch.setattr(exa, "REQUEST_SECONDS", 0.01)
    result = run(router(Slow()).search(exa.SearchRequest("query")))
    assert result["fallbacks"][0]["code"] == "EXA_TIMEOUT"


def test_pipeline_timeout(monkeypatch):
    class Slow(FakeProvider):
        async def search(self, request, *, deep=False):
            await asyncio.sleep(1)
    monkeypatch.setattr(exa, "PIPELINE_SECONDS", 0.01)
    with pytest.raises(exa.ExaError) as caught:
        run(router(Slow()).search_and_fetch("query"))
    assert caught.value.reason == "pipeline_timeout"


def test_registered_tools_return_structured_errors(monkeypatch):
    monkeypatch.setenv("EXA_PROVIDER", "direct")
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    for coroutine in [server.web_search("query"), server.web_fetch(URL),
                      server.web_deep_search("query"), server.web_search_and_fetch("query")]:
        assert json.loads(run(coroutine))["error"]["code"] == "EXA_AUTH_ERROR"


def test_quota_tag_even_with_http_200_opens_breaker():
    provider = direct_with(lambda r: httpx.Response(200, json={"error": KEY, "tag": "NO_MORE_CREDITS"}))
    service = router(provider)
    result = run(service.search(exa.SearchRequest("query")))
    assert result["fallbacks"][0]["code"] == "EXA_QUOTA_EXHAUSTED"
    with pytest.raises(exa.ExaError, match="EXA_QUOTA_EXHAUSTED"):
        service.circuit.check()


def test_double_failure_has_safe_provider_attempts():
    with pytest.raises(exa.ExaError) as caught:
        run(router(FakeProvider(exa.ExaError("EXA_AUTH_ERROR")), FakeProvider(exa.ExaError("EXA_UNAVAILABLE")))
            .search(exa.SearchRequest("query")))
    attempts = caught.value.as_dict()["attempts"]
    assert [a["provider"] for a in attempts] == ["exa_direct", "exa_remote_mcp"]
    assert attempts[0]["configuration_error"] is True


def test_search_and_fetch_redacts_echoed_key_from_query():
    result = run(router(FakeProvider()).search_and_fetch(KEY, fetch_top=0))
    assert KEY not in json.dumps(result)


def test_deep_output_bound():
    provider = direct_with(lambda r: httpx.Response(200, json={"results": [], "output": {
        "content": "x" * 51000, "grounding": []}}))
    result = run(provider.search(exa.SearchRequest("query"), deep=True))
    assert len(result["output"]["content"]) == 50000 and result["output_truncated"]


def test_parse_remote_extracts_published_date():
    entries, parsed = exa.parse_remote(f"Title: Release\nURL: {URL}\nPublished: 2026-10-05\nSummary text")
    assert parsed and entries[0]["publishedDate"] == "2026-10-05"
    assert "Published:" not in entries[0]["text"]


def test_partial_direct_contents_preserves_successes_when_remote_fails():
    provider = direct_with(lambda r: httpx.Response(200, json={
        "results": [{"id": URL, "url": URL, "text": "Success"}],
        "statuses": [{"id": URL, "status": "success"},
                     {"id": URL2, "status": "error", "error": {"tag": "CRAWL_TIMEOUT"}}]}))
    remote = FakeProvider(exa.ExaError("EXA_UNAVAILABLE"))
    result = run(router(provider, remote).contents([URL, URL2]))
    assert result["results"][0]["text"] == "Success"
    assert result["results"][1]["error"]["code"] == "EXA_TIMEOUT"
    assert [f["provider"] for f in result["fallbacks"]] == ["exa_direct", "exa_remote_mcp"]
