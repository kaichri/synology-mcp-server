"""Offline public-reader tests. Fixtures are synthetic; no X network or account access."""
import asyncio
import copy
import json
from pathlib import Path
import socket
from urllib.parse import quote

import pytest
import x_public as x
import server
from test_oauth import apps, config, password_hash, tokens, tools_list, rpc_response

URL = "https://x.com/example/status/123456789"
FIXTURES = Path(__file__).parent / "fixtures"
BODY = (FIXTURES / "x_public_post.json").read_text()


@pytest.mark.parametrize("url", [URL, URL + "?s=20#fragment", "http://twitter.com/example/status/123456789",
    "https://mobile.twitter.com/example/status/123456789/photo/1", "https://x.com/i/web/status/123456789"])
def test_normalization(url):
    normalized, post_id = x.normalize_url(url)
    assert normalized.startswith("https://x.com/") and "?" not in normalized and "#" not in normalized
    assert post_id == "123456789"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/status/1", "http://192.168.1.1/status/1",
    "http://[::1]/status/1", "https://x.com.evil.example/a/status/1", "https://x.com@evil.example/a/status/1",
    "https://user:secret@x.com/example/status/1", "https://x.com:8443/example/status/1",
    "https://x.com/example", "https://x.com/example/status/0", "https://x.com/evil/status/1/../../a",
    "https://x.com\\@evil.example/a/status/1", "https://x.com/example/status/1\n"])
def test_unsupported_url(url):
    with pytest.raises(x.XError) as exc:
        x.normalize_url(url)
    assert exc.value.code == "UNSUPPORTED_URL"


def test_embedded_post_fields():
    post = x.parse_page(URL, BODY)["post"]
    assert post["username"] == "example" and post["author_name"] == "Example Author"
    assert post["text_complete"] and post["conversation_id"] == "123456789"
    assert post["published_at"] == "2026-01-01T10:00:00+00:00"
    assert post["mentioned_accounts"] == ["reader"] and post["hashtags"] == ["DGX"]
    assert post["media"][0]["alt_text"] == "Synthetic test image"
    assert post["urls"] == ["https://example.org/article"]


def test_meta_preview_is_partial():
    doc = x.parse_page(URL, (FIXTURES / "x_public_meta.html").read_text())
    assert not doc["post"]["text_complete"] and doc["post"]["completeness"] == "partial"
    assert doc["status_links"] == ["https://x.com/example/status/123456790"]


def test_live_seo_metadata_remains_partial_but_preserves_public_fields():
    post = x.parse_page(URL, (FIXTURES / "x_public_live_meta.html").read_text())["post"]
    assert post["author_name"] == "Example Author" and post["username"] == "example"
    assert post["published_at"] == "2025-02-02T17:33:41+00:00"
    assert post["media"][0]["type"] == "image_preview"
    assert post["media"][0]["attribution"] == "page_preview_not_verified_post_attachment"
    assert post["quote_status"] == "unknown"
    assert not post["complete"] and post["completeness"] == "partial"
    assert post["hashtags"] == ["Example"] and post["mentioned_accounts"] == ["reader"]


def test_live_seo_identity_is_checked():
    body = (FIXTURES / "x_public_live_meta.html").read_text().replace('/example/status/123456789', '/other/status/999')
    post = x.parse_page(URL, body)["post"]
    assert post["post_id"] == "123456789" and post["username"] == "example"


def test_live_seo_can_correct_a_renamed_account():
    body = (FIXTURES / "x_public_live_meta.html").read_text().replace('/example/status/123456789', '/renamed/status/123456789').replace('(@example)', '(@renamed)')
    post = x.parse_page(URL, body)["post"]
    assert post["username"] == "renamed" and post["url"] == "https://x.com/renamed/status/123456789"


def test_json_ld_and_longform_quote():
    body = '<script type="application/ld+json">' + json.dumps({"@type": "SocialMediaPosting",
        "url": URL, "articleBody": "Complete public structured text", "author": {"name": "Example"},
        "datePublished": "2026-01-02T00:00:00Z"}) + '</script>'
    assert x.parse_page(URL, body)["post"]["text"] == "Complete public structured text"
    fixture = json.loads(BODY)
    tweet = fixture["data"]["tweet"]
    tweet["note_tweet"] = {"note_tweet_results": {"result": {"text": "Long public article text"}}}
    tweet["legacy"]["truncated"] = True
    tweet["legacy"]["quoted_status_id_str"] = "123456788"
    post = x.parse_page(URL, json.dumps(fixture))["post"]
    assert post["text_complete"] and post["text"] == "Long public article text"
    assert post["quote_post"]["post_id"] == "123456788"


@pytest.mark.parametrize("body,code", [("<p>These posts are protected</p>", "PRIVATE_OR_PROTECTED"),
    ("<p>Post was deleted</p>", "NOT_FOUND"), ("<p>Verify you are human captcha</p>", "BLOCKED"),
    ("<html><script>loadApplication()</script></html>", "PARTIAL_CONTENT")])
def test_unreadable_pages(body, code):
    with pytest.raises(x.XError) as exc:
        x.parse_page(URL, body)
    assert exc.value.code == code


class FakeHTTP:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url):
        self.calls.append(url)
        result = self.pages[url] if url in self.pages else self.pages.get("search")
        if isinstance(result, Exception):
            raise result
        return url, result


def test_cache_refresh_and_expiry(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(x.time, "time", lambda: now[0])
    cache = x.Cache(str(tmp_path / "cache.sqlite3"))
    cache.put("test", {"text": "public"}, 10)
    assert cache.get("test") == {"text": "public"}
    assert cache._connect().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    now[0] += 11
    assert cache.get("test") is None
    http = FakeHTTP({URL: BODY})
    provider = x.FreePublicProvider(http, cache)
    first = asyncio.run(provider.read_post(URL))
    second = asyncio.run(provider.read_post(URL + "?tracking=1"))
    assert first["post"]["cached"] is False and second["post"]["cached"] is True
    asyncio.run(provider.read_post(URL, refresh=True))
    assert len(http.calls) == 2
    cache.close()


def test_disabled_cache(monkeypatch):
    monkeypatch.setenv("MCP_X_CACHE_ENABLED", "false")
    http = FakeHTTP({URL: BODY})
    provider = x.FreePublicProvider(http)
    asyncio.run(provider.read_post(URL)); asyncio.run(provider.read_post(URL))
    assert provider.cache is None and len(http.calls) == 2


def test_discovery_deduplicates_and_filters(tmp_path):
    other_url = "https://x.com/other/status/123456790"
    other = json.loads(BODY)
    other["data"]["tweet"]["rest_id"] = "123456790"
    other["data"]["tweet"]["core"]["user_results"]["result"]["legacy"]["screen_name"] = "other"
    discovery = f'<a href="https://html.duckduckgo.com/l/?uddg={quote(URL, safe="")}">result</a>'
    discovery += f'<a href="https://twitter.com/example/status/123456789">duplicate</a><a href="{other_url}">other</a>'
    discovery += '<a href="http://127.0.0.1/a/status/1">unsafe</a>'
    http = FakeHTTP({URL: BODY, other_url: json.dumps(other), "search": discovery})
    provider = x.FreePublicProvider(http, x.Cache(str(tmp_path / "cache.sqlite3")))
    result = asyncio.run(provider.search("DGX", from_user="example", since="2026-01-01", until="2026-01-02"))
    assert [p["post_id"] for p in result["posts"]] == ["123456789"]
    assert result["discovered_count"] == 2 and result["complete"] is False
    assert sum(url == URL for url in http.calls) == 1
    cached = asyncio.run(provider.search("DGX", from_user="example", since="2026-01-01", until="2026-01-02"))
    assert cached["cached"]


def test_thread_connected_authors_sorted_and_deduplicated():
    root = json.loads(BODY)["data"]["tweet"]
    following = copy.deepcopy(root)
    following["rest_id"] = "123456790"
    following["legacy"].update(in_reply_to_status_id_str="123456789", created_at="2026-01-01T11:00:00Z")
    reply = copy.deepcopy(following)
    reply["rest_id"] = "123456791"
    reply["core"]["user_results"]["result"]["legacy"]["screen_name"] = "other"
    unrelated = copy.deepcopy(following)
    unrelated["rest_id"] = "123456792"
    unrelated["legacy"].update(conversation_id_str="123456792", in_reply_to_status_id_str=None)
    http = FakeHTTP({URL: json.dumps([reply, following, unrelated, root, root]), "search": "<html></html>"})
    provider = x.FreePublicProvider(http, cache=False)
    result = asyncio.run(provider.read_thread(URL))
    assert [p["post_id"] for p in result["posts"]] == ["123456789", "123456790"]
    assert result["posts_found"] == 2 and result["termination_reason"] == "thread_end_unknown"
    result = asyncio.run(provider.read_thread(URL, include_replies=True, max_posts=2))
    assert result["limit_reached"] and not result["complete"]
    assert result["termination_reason"] == "max_posts_reached"
    result = asyncio.run(provider.read_thread(URL, include_replies=True))
    assert [p["post_id"] for p in result["posts"]] == ["123456789", "123456790", "123456791"]


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1", "ff02::1"])
def test_dns_ssrf_rejected(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (address, 443))])
    with pytest.raises(x.XError):
        x.public_addresses("x.com", 443)


class Response:
    def __init__(self, status=200, headers=None, body=b"<html></html>"):
        self.status, self.headers, self.body = status, headers or {"Content-Type": "text/html"}, body
    def getheader(self, name, default=None):
        return self.headers.get(name, default)
    def read1(self, n):
        chunk, self.body = self.body[:n], self.body[n:]
        return chunk


def fake_connections(monkeypatch, response):
    monkeypatch.setattr(x, "public_addresses", lambda *a: ["93.184.216.34"])
    class Connection:
        sock = None
        def __init__(self, *a, **k): pass
        def request(self, *a, **k): pass
        def getresponse(self): return response
        def close(self): pass
    monkeypatch.setattr(x.http.client, "HTTPSConnection", Connection)


@pytest.mark.parametrize("status,code", [(429, "RATE_LIMITED"), (404, "NOT_FOUND"), (410, "NOT_FOUND"),
    (401, "BLOCKED"), (403, "BLOCKED"), (500, "FETCH_FAILED")])
def test_transport_http_errors(monkeypatch, status, code):
    fake_connections(monkeypatch, Response(status))
    with pytest.raises(x.XError) as exc:
        x.PublicHTTP().fetch(URL)
    assert exc.value.code == code


@pytest.mark.parametrize("target", ["http://127.0.0.1/", "http://192.168.1.1/", "https://evil.example/", "file:///etc/passwd"])
def test_redirect_ssrf_rejected(monkeypatch, target):
    fake_connections(monkeypatch, Response(302, {"Location": target}))
    with pytest.raises(x.XError) as exc:
        x.PublicHTTP().fetch(URL)
    assert exc.value.code == "UNSUPPORTED_URL"


def test_response_size_limit(monkeypatch):
    fake_connections(monkeypatch, Response(body=b"x" * (x.MAX_BYTES + 1)))
    with pytest.raises(x.XError) as exc:
        x.PublicHTTP().fetch(URL)
    assert exc.value.code == "FETCH_FAILED"


def test_live_discovery_202_challenge_is_blocked(monkeypatch):
    fake_connections(monkeypatch, Response(202))
    with pytest.raises(x.XError) as exc:
        x.PublicHTTP().fetch("https://html.duckduckgo.com/html/?q=Qwen")
    assert exc.value.code == "BLOCKED"


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 51}, {"since": "yesterday"}, {"since": "20260101"},
    {"since": "2026-01-03", "until": "2026-01-01"}, {"from_user": "../../evil"}])
def test_invalid_search_arguments(kwargs):
    provider = x.FreePublicProvider(cache=False)
    with pytest.raises(x.XError):
        asyncio.run(provider.search("topic", **kwargs))


def test_tool_provider_boundary_sanitizes_errors(monkeypatch):
    class Provider:
        async def read_post(self, **kwargs):
            raise RuntimeError("secret local path or header must never escape")
        async def search(self, **kwargs):
            return {"ok": True, "username": kwargs.get("from_user")}
    monkeypatch.setattr(x, "_provider", Provider())
    result = asyncio.run(server.x_read_post(URL))
    assert result == x.failure("FETCH_FAILED", "Public provider could not complete the request.")
    assert asyncio.run(server.x_search_user("example", "topic"))["username"] == "example"


def test_mixed_public_private_dns_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(x.XError):
        x.public_addresses("x.com", 443)


def test_dns_pinning_preserves_tls_hostname(monkeypatch):
    observed = {}
    monkeypatch.setattr(x, "public_addresses", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout, source: observed.update(target=addr))
    class Connection:
        sock = None
        def __init__(self, host, port, **kwargs):
            observed.update(host=host, port=port, tls=kwargs.get("context"))
        def request(self, method, path, headers):
            self._create_connection(("x.com", 443), 8)
            observed["headers"] = headers
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(x.http.client, "HTTPSConnection", Connection)
    x.PublicHTTP().fetch(URL)
    assert observed["host"] == "x.com" and observed["target"] == ("93.184.216.34", 443)
    assert observed["tls"].check_hostname
    assert "Cookie" not in observed["headers"] and "Authorization" not in observed["headers"]


def test_short_link_and_changed_identity():
    class Redirect:
        def fetch(self, url):
            return URL, BODY
    provider = x.FreePublicProvider(Redirect(), cache=False)
    assert asyncio.run(provider.read_post("https://t.co/public-example"))["post"]["post_id"] == "123456789"
    with pytest.raises(x.XError):
        asyncio.run(provider.read_post("https://x.com/example/status/999"))


def test_malformed_embedded_records_do_not_hide_valid_post():
    body = json.dumps([{"legacy": None}, {"@type": ["Thing", "WebPage"]}, json.loads(BODY)])
    assert x.parse_page(URL, body)["post"]["post_id"] == "123456789"


def test_empty_embedded_text_is_not_a_readable_post():
    fixture = json.loads(BODY)
    fixture["data"]["tweet"]["legacy"]["full_text"] = " "
    with pytest.raises(x.XError) as exc:
        x.parse_page(URL, json.dumps(fixture))
    assert exc.value.code == "PARTIAL_CONTENT"


def test_search_skips_unreadable_and_unknown_dates():
    unreadable = "https://x.com/example/status/123456790"
    discovery = f'<a href="{URL}">one</a><a href="{unreadable}">two</a>'
    provider = x.FreePublicProvider(FakeHTTP({URL: BODY, unreadable: x.XError("BLOCKED", "blocked"),
                                            "search": discovery}), cache=False)
    result = asyncio.run(provider.search("topic"))
    assert len(result["posts"]) == 1 and result["skipped"] == {"BLOCKED": 1}
    provider = x.FreePublicProvider(FakeHTTP({URL: '<meta property="og:description" content="preview">',
                                            unreadable: BODY, "search": f'<a href="{URL}">one</a>'}), cache=False)
    result = asyncio.run(provider.search("topic", since="2026-01-01"))
    assert result["posts"] == [] and result["skipped"]["DATE_UNVERIFIABLE_OR_OUTSIDE_RANGE"] == 1


def test_search_block_page():
    provider = x.FreePublicProvider(FakeHTTP({"search": "<p>Unfortunately, bots use DuckDuckGo too</p>"}), cache=False)
    with pytest.raises(x.XError) as exc:
        asyncio.run(provider.search("topic"))
    assert exc.value.code == "BLOCKED"


@pytest.mark.parametrize("limit", [0, 51])
def test_thread_limit_validation(limit):
    with pytest.raises(x.XError):
        asyncio.run(x.FreePublicProvider(cache=False).read_thread(URL, max_posts=limit))


def test_timeout_and_expected_errors_are_structured(monkeypatch):
    class Provider:
        async def read_post(self, **kwargs):
            raise TimeoutError("untrusted transport details")
        async def read_thread(self, **kwargs):
            raise x.XError("RATE_LIMITED", "Try later.")
    monkeypatch.setattr(x, "_provider", Provider())
    assert asyncio.run(server.x_read_post(URL))["error"]["code"] == "FETCH_FAILED"
    assert asyncio.run(server.x_read_thread(URL))["error"]["code"] == "RATE_LIMITED"


def test_new_tools_execute_through_both_mcp_listeners(apps, monkeypatch):
    calls = []
    class Provider:
        async def read_post(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "post": {"text": "offline-public-post"}}
        async def read_thread(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "posts": [], "complete": False}
        async def search(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "posts": [], "complete": False}
    monkeypatch.setattr(x, "_provider", Provider())
    lan, public, _ = apps
    token = tokens(public)["access_token"]
    for client, bearer in ((lan, None), (public, token)):
        _, headers = tools_list(client, bearer)
        for name, arguments in [("x_read_post", {"url": URL}), ("x_read_thread", {"url": URL}),
                                ("x_search", {"query": "topic"}), ("x_search_user", {"username": "example", "query": "topic"})]:
            response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 31,
                "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            result = rpc_response(response)
            assert response.status_code == 200
            assert not result["result"].get("isError")
            assert json.loads(result["result"]["content"][0]["text"])["ok"] is True
    assert len(calls) == 8
    denied = public.post("/mcp", headers={"Content-Type": "application/json"}, json={
        "jsonrpc": "2.0", "id": 32, "method": "tools/call", "params": {"name": "x_read_post", "arguments": {"url": URL}}})
    assert denied.status_code == 401 and len(calls) == 8


def test_ipv6_failure_falls_back_to_ipv4(monkeypatch):
    attempted = []
    monkeypatch.setattr(x, "public_addresses", lambda host, port: ["2001:db8::1", "93.184.216.34"])
    def fake_connect(addr, timeout, source):
        attempted.append(addr)
        if addr[0] == "2001:db8::1":
            raise OSError("Network is unreachable")
        return "ok-socket"
    monkeypatch.setattr(socket, "create_connection", fake_connect)
    class Connection:
        sock = None
        def __init__(self, host, port, **kwargs): pass
        def request(self, method, path, headers):
            assert self._create_connection(("x.com", 443), 8) == "ok-socket"
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(x.http.client, "HTTPSConnection", Connection)
    x.PublicHTTP().fetch(URL)
    assert attempted == [("2001:db8::1", 443), ("93.184.216.34", 443)]
