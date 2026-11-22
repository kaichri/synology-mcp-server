"""Bounded Exa adapters. No browser, implicit deep search, or raw upstream errors.

Verified against Exa OpenAPI and live MCP list_tools on 2026-10-02.
The HTTPReader/SearchProvider protocols are extension points, not installed services.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
import re
import threading
import time
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlsplit
import weakref

import httpx

_http_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = weakref.WeakKeyDictionary()


def _shared_http_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _http_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        _http_clients[loop] = client
    return client


async def close_shared_clients() -> None:
    loop = asyncio.get_running_loop()
    client = _http_clients.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()

MAX_RESULTS = 20
MAX_FETCH = 5
MAX_TEXT = 20000
MAX_TOTAL_TEXT = 50000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_SECONDS = 25
DEEP_SECONDS = 65
PIPELINE_SECONDS = 90
OBJECTIVE = (
    "Find evidence to answer the user's query. Rank original sources, official documentation "
    "and directly relevant reporting first; exclude duplicate pages and promotional noise. "
    "Extract the facts, dates and supporting source URLs needed to assess the answer."
)
QUOTA_TAGS = {"NO_MORE_CREDITS", "API_KEY_BUDGET_EXCEEDED", "TEAM_BUDGET_EXCEEDED"}
INVALID_TAGS = {"INVALID_REQUEST_BODY", "INVALID_REQUEST", "INVALID_NUM_RESULTS", "NUM_RESULTS_EXCEEDED",
                "INVALID_JSON_SCHEMA", "SUBPAGES_LIMIT_EXCEEDED", "PROHIBITED_CONTENT", "CONTENT_FILTER_ERROR"}
RETRYABLE = {"EXA_AUTH_ERROR", "EXA_QUOTA_EXHAUSTED", "EXA_RATE_LIMITED", "EXA_TIMEOUT", "EXA_UNAVAILABLE"}


class ExaError(Exception):
    """Only fixed, non-secret classification is retained; no response bodies or requests."""
    def __init__(self, code: str, *, status: int | None = None, reason: str | None = None):
        self.code = code
        self.status = status
        self.reason = reason
        self.attempts = []
        super().__init__(code)

    def as_dict(self):
        value = {"code": self.code, "http_status": self.status,
                 "reason": self.reason, "configuration_error": self.code == "EXA_AUTH_ERROR"}
        if self.attempts:
            value["attempts"] = self.attempts
        return value


def classify(status: int, body: object) -> ExaError:
    tag = body.get("tag") if isinstance(body, dict) else None
    if not isinstance(tag, str):
        tag = None
    if tag in QUOTA_TAGS or status == 402:
        code = "EXA_QUOTA_EXHAUSTED"
    elif tag in INVALID_TAGS or status in (400, 404, 409, 422):
        code = "EXA_INVALID_REQUEST"
    elif tag == "INVALID_API_KEY" or status in (401, 403):
        code = "EXA_AUTH_ERROR"
    elif tag == "RATE_LIMIT_EXCEEDED" or status == 429:
        code = "EXA_RATE_LIMITED"
    elif status in (408, 504):
        code = "EXA_TIMEOUT"
    elif tag == "SERVICE_OVERLOADED" or 500 <= status <= 599:
        code = "EXA_UNAVAILABLE"
    else:
        code = "EXA_UNKNOWN_ERROR"
    # Never retain an arbitrary upstream tag/error string (it may echo credentials).
    return ExaError(code, status=status)


def exception_error(error: Exception) -> ExaError:
    if isinstance(error, ExaError):
        return error
    if isinstance(error, BaseExceptionGroup):
        errors = [exception_error(e) for e in error.exceptions if isinstance(e, Exception)]
        return next((e for e in errors if e.code != "EXA_UNKNOWN_ERROR"), ExaError("EXA_UNKNOWN_ERROR"))
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return ExaError("EXA_TIMEOUT")
    if isinstance(error, httpx.HTTPStatusError):
        try:
            body = error.response.json()
        except (ValueError, UnicodeError):
            body = None
        return classify(error.response.status_code, body)
    if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError, OSError)):
        return ExaError("EXA_UNAVAILABLE")
    # MCP wraps tool failures as text. Inspect only known codes, never return that text.
    message = str(error).upper()
    for tag in sorted(QUOTA_TAGS | INVALID_TAGS | {"INVALID_API_KEY", "RATE_LIMIT_EXCEEDED", "SERVICE_OVERLOADED"}):
        if re.search(r"\b" + tag + r"\b", message):
            return classify(0, {"tag": tag})
    status = re.search(r"\bHTTP(?: STATUS)?[: =]*(\d{3})\b", message)
    if status:
        return classify(int(status.group(1)), None)
    return ExaError("EXA_UNKNOWN_ERROR")


def redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, dict):
        return {redact(k, secret): redact(v, secret) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class Config:
    key: str = field(default="", repr=False)
    mode: str = "auto"
    quota_cooldown: int = 3600
    rate_cooldown: int = 60

    @classmethod
    def from_env(cls):
        mode = os.getenv("EXA_PROVIDER", "auto").strip().lower()
        if mode not in {"auto", "direct", "remote_mcp"}:
            raise ExaError("EXA_INVALID_REQUEST", reason="invalid_provider_configuration")
        def seconds(name, default):
            try:
                n = int(os.getenv(name, str(default)))
                if not 1 <= n <= 2592000:
                    raise ValueError()
                return n
            except ValueError:
                raise ExaError("EXA_INVALID_REQUEST", reason="invalid_cooldown_configuration") from None
        key = os.getenv("EXA_API_KEY", "").strip()
        if any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise ExaError("EXA_AUTH_ERROR", reason="invalid_key_configuration")
        return cls(key, mode, seconds("EXA_QUOTA_COOLDOWN_SECONDS", 3600),
                   seconds("EXA_RATE_LIMIT_COOLDOWN_SECONDS", 60))


@dataclass(frozen=True)
class SearchRequest:
    query: str
    num_results: int = 5
    objective: str = OBJECTIVE

    def __post_init__(self):
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 4096:
            raise ExaError("EXA_INVALID_REQUEST", reason="invalid_query")
        if not self.objective.strip() or len(self.objective) > 4096:
            raise ExaError("EXA_INVALID_REQUEST", reason="invalid_objective")
        object.__setattr__(self, "num_results", max(1, min(self.num_results, MAX_RESULTS)))


def checked_urls(urls):
    if not isinstance(urls, list) or not 1 <= len(urls) <= 100:
        raise ExaError("EXA_INVALID_REQUEST", reason="invalid_url_count")
    for url in urls:
        try:
            p = urlsplit(url)
            host = (p.hostname or "").lower()
            if (not isinstance(url, str) or len(url) > 2048 or p.scheme not in {"http", "https"}
                    or p.username is not None or p.password is not None or not host
                    or p.port not in (None, 80 if p.scheme == "http" else 443)
                    or host == "localhost" or host.endswith((".localhost", ".local"))):
                raise ValueError()
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                if "." not in host:
                    raise ValueError()
            else:
                if not ip.is_global or ip.is_multicast or ip.is_reserved:
                    raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ExaError("EXA_INVALID_REQUEST", reason="invalid_public_url") from None
    return list(dict.fromkeys(urls))


def now():
    return datetime.now(timezone.utc).isoformat()


def usage(data):
    # Exa documents costDollars as an estimate, NOT an exact billing meter.
    cost = data.get("costDollars")
    def numeric_cost(value, depth=0):
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            return value if 0 <= value < float("inf") else None
        if isinstance(value, dict) and depth < 3:
            return {k: numeric_cost(v, depth + 1) for k, v in value.items()
                    if k in {"total", "search", "neural", "keyword", "summary", "contents", "text", "highlights"}}
        return None
    cost = numeric_cost(cost)
    return {"estimated_cost_dollars": cost, "billing_exact": False} if isinstance(cost, dict) else None


def page(item, provider, request_url=None):
    text = item.get("text")
    if not isinstance(text, str):
        highlights = item.get("highlights", [])
        text = "\n".join(h for h in highlights if isinstance(h, str)) if isinstance(highlights, list) else ""
    def string(name, limit):
        value = item.get(name)
        return value[:limit] if isinstance(value, str) else None
    return {"provider": provider, "url": string("url", 2048) or request_url, "requested_url": request_url,
            "title": string("title", 500), "text": text[:MAX_TEXT], "author": string("author", 500),
            "published_at": string("publishedDate", 100), "fetched_at": now(), "complete": None,
            "completeness": "unknown", "truncated": len(text) >= MAX_TEXT, "usage": None}


def envelope(provider, results, data=None, raw=None):
    data = data or {}
    request_id = data.get("requestId")
    return {"provider": provider, "results": results, "fetched_at": now(),
            "usage": usage(data), "request_id": request_id[:200] if isinstance(request_id, str) else None, "raw_text": raw,
            "fallbacks": []}


class SearchProvider(Protocol):
    async def search(self, request: SearchRequest, *, deep: bool = False) -> dict: ...
    async def contents(self, urls: list[str]) -> dict: ...


class HTTPReader(Protocol):
    """Optional future reader must pin public DNS, validate redirects and bound I/O.

    The browser PoC's HTTP extractor is experimental; no local reader is enabled
    by default. Its implementation must not import Playwright into production.
    """
    async def contents(self, urls: list[str]) -> dict: ...


class DirectExaApiProvider:
    name = "exa_direct"

    def __init__(self, key: str, *, transport=None):
        self._key = key
        self._transport = transport

    async def _post(self, endpoint, payload, timeout=REQUEST_SECONDS):
        if not self._key:
            raise ExaError("EXA_AUTH_ERROR", reason="api_key_missing")

        async def send(client):
            async with client.stream("POST", "https://api.exa.ai/" + endpoint,
                                     headers={"x-api-key": self._key}, json=payload, timeout=timeout) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ExaError("EXA_UNAVAILABLE", reason="response_size_limit")
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    data = None
                if response.status_code != 200:
                    raise classify(response.status_code, data)
                if isinstance(data, dict) and data.get("error"):
                    raise classify(response.status_code, data)
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise ExaError("EXA_UNAVAILABLE", reason="malformed_response")
                return redact(data, self._key)

        try:
            if self._transport is not None:
                async with httpx.AsyncClient(transport=self._transport, trust_env=False,
                                             follow_redirects=False, timeout=timeout) as client:
                    return await send(client)
            return await send(_shared_http_client())
        except Exception as error:
            raise exception_error(error) from None

    async def search(self, request, *, deep=False):
        payload = {"query": request.query, "objective": request.objective,
                   "numResults": request.num_results, "type": "deep" if deep else "auto",
                   "contents": {"highlights": {"maxCharacters": 1500}}}
        if deep:
            payload["outputSchema"] = {"type": "text", "description": "Concise evidence-based answer with source citations."}
        data = await self._post("search", payload, DEEP_SECONDS if deep else REQUEST_SECONDS)
        result = envelope(self.name, [page(i, self.name) for i in data["results"][:request.num_results]
                                      if isinstance(i, dict)], data)
        if deep:
            output = data.get("output")
            if isinstance(output, dict):
                content = output.get("content")
                result["output"] = {"content": content[:MAX_TOTAL_TEXT] if isinstance(content, str) else None,
                                    "grounding": []}
                result["output_truncated"] = isinstance(content, str) and len(content) > MAX_TOTAL_TEXT
                grounding = output.get("grounding", [])
                for evidence in grounding[:20] if isinstance(grounding, list) else []:
                    if not isinstance(evidence, dict):
                        continue
                    citations = evidence.get("citations", [])
                    result["output"]["grounding"].append({
                        "field": str(evidence.get("field", ""))[:200],
                        "citations": [{k: v[:2048] for k, v in c.items() if k in {"title", "url"} and isinstance(v, str)}
                                      for c in (citations[:20] if isinstance(citations, list) else []) if isinstance(c, dict)]})
            else:
                result["output"] = None
        return result

    async def contents(self, urls):
        urls = checked_urls(urls)
        data = await self._post("contents", {"urls": urls, "text": {"maxCharacters": MAX_TEXT}})
        items = [i for i in data["results"] if isinstance(i, dict)]
        raw_statuses = data.get("statuses", [])
        statuses = {s.get("id"): s for s in (raw_statuses if isinstance(raw_statuses, list) else [])
                    if isinstance(s, dict) and isinstance(s.get("id"), str)}
        results = []
        for url in urls:
            status = statuses.get(url, {})
            item = next((i for i in items if i.get("id") == url or i.get("url") == url), None)
            if item is None and len(urls) == len(items) == 1:
                item = items[0]  # A single canonicalized URL is unambiguous.
            if status.get("status") == "error" or item is None:
                status_error = status.get("error")
                error_tag = status_error.get("tag") if isinstance(status_error, dict) else None
                if not isinstance(error_tag, str):
                    error_tag = None
                code = "EXA_TIMEOUT" if error_tag in {"CRAWL_TIMEOUT", "CRAWL_LIVECRAWL_TIMEOUT"} else "EXA_UNAVAILABLE"
                if error_tag in {"UNSUPPORTED_URL", "CRAWL_NOT_FOUND", "CRAWL_HTTP_403"}:
                    code = "EXA_INVALID_REQUEST"
                failed = page({}, self.name, url)
                failed["error"] = ExaError(code, reason="page_content_unavailable").as_dict()
                results.append(failed)
            else:
                results.append(page(item, self.name, url))
        return envelope(self.name, results, data)


def parse_remote(raw):
    """Parse the observed MCP Title/URL text, or structured JSON; keep source order."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        data = data.get("results")
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)], True
    entries = []
    current = None
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(?:#{1,3}\s*)?Title:\s*(.*)", line, re.I)
        if not match and index + 1 < len(lines) and re.match(r"^URL:\s*https?://", lines[index + 1], re.I):
            # Observed Fetch format: '# Page title' immediately followed by URL.
            match = re.match(r"^#{1,3}\s+(.+)$", line)
        if match:
            if current and current.get("url"):
                entries.append(current)
            current = {"title": match.group(1), "text": ""}
        elif current is not None:
            match = re.match(r"^URL:\s*(\S+)", line, re.I)
            if match:
                current["url"] = match.group(1)
            else:
                published = re.match(r"^Published:\s*(.+)$", line, re.I)
                if published and not current.get("publishedDate"):
                    current["publishedDate"] = published.group(1).strip()
                else:
                    current["text"] += line + "\n"
    if current and current.get("url"):
        entries.append(current)
    return entries, bool(entries)


class RemoteExaMcpProvider:
    name = "exa_remote_mcp"

    def __init__(self, call: Callable[[str, dict], Awaitable[str]], secret=""):
        self.call = call
        self._secret = secret

    async def _call(self, tool, args):
        try:
            raw = await asyncio.wait_for(self.call(tool, args), REQUEST_SECONDS)
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise ExaError("EXA_UNAVAILABLE", reason="malformed_or_oversized_remote_response")
            return redact(raw, self._secret)
        except Exception as error:
            raise exception_error(error) from None

    async def search(self, request, *, deep=False):
        if deep:
            raise ExaError("EXA_UNAVAILABLE", reason="remote_deep_not_supported")
        raw = await self._call("web_search_exa", {"query": request.query,
                               "numResults": request.num_results, "objective": request.objective})
        items, parsed = parse_remote(raw)
        result = envelope(self.name, [page(i, self.name) for i in items[:request.num_results]], raw=raw)
        result["metadata_parsed"] = parsed
        return result

    async def contents(self, urls):
        urls = checked_urls(urls)
        raw = await self._call("web_fetch_exa", {"urls": urls, "maxCharacters": MAX_TEXT})
        items, parsed = parse_remote(raw)
        results = []
        for url in urls:
            item = next((i for i in items if i.get("url") == url or i.get("id") == url), None)
            if item is None and len(urls) == 1:
                item = items[0] if len(items) == 1 else {"url": url, "text": raw}
            value = page(item or {}, self.name, url)
            if item is None:
                value["error"] = ExaError("EXA_UNAVAILABLE", reason="unmapped_remote_batch_result").as_dict()
            results.append(value)
        return envelope(self.name, results, raw=raw)


class CircuitBreaker:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._until = 0
        self._code = None
        self._lock = threading.Lock()

    def check(self):
        with self._lock:
            if self.clock() < self._until:
                raise ExaError(self._code, reason="direct_circuit_open")

    def record(self, error, config):
        if error.reason == "direct_circuit_open":
            return  # Reading an open circuit must not restart its cooldown.
        cooldown = {"EXA_QUOTA_EXHAUSTED": config.quota_cooldown,
                    "EXA_RATE_LIMITED": config.rate_cooldown}.get(error.code)
        if cooldown:
            with self._lock:
                until = self.clock() + cooldown
                if until >= self._until:
                    self._until, self._code = until, error.code


_circuits = {}
_circuit_lock = threading.Lock()


def shared_circuit(config):
    identity = (hashlib.sha256(config.key.encode()).hexdigest(), config.quota_cooldown, config.rate_cooldown)
    with _circuit_lock:
        if identity not in _circuits:
            # Configuration/key changes should not retain an unlimited credential cache.
            if len(_circuits) >= 16:
                _circuits.clear()
            _circuits[identity] = CircuitBreaker()
        return _circuits[identity]


class ExaRouter:
    def __init__(self, config, remote, *, direct=None, circuit=None, http_reader=None):
        self.config = config
        self.direct = direct or DirectExaApiProvider(config.key)
        self.remote = remote
        self.circuit = circuit or shared_circuit(config)
        self.http_reader = http_reader

    async def _run(self, method, argument, **kwargs):
        fallbacks = []
        if self.config.mode != "remote_mcp" and (self.config.key or self.config.mode == "direct"):
            try:
                self.circuit.check()
                result = await asyncio.wait_for(getattr(self.direct, method)(argument, **kwargs),
                                                DEEP_SECONDS if kwargs.get("deep") else REQUEST_SECONDS)
            except Exception as failure:
                error = exception_error(failure)
                self.circuit.record(error, self.config)
                if self.config.mode == "direct" or error.code not in RETRYABLE or kwargs.get("deep"):
                    raise error from None
                fallbacks.append({"provider": "exa_direct", **error.as_dict()})
            else:
                # URL errors are not account errors. Remote failures must never
                # be reclassified as Direct failures or retried a second time.
                if method == "contents" and self.config.mode == "auto":
                    failed = [p["requested_url"] for p in result["results"]
                              if p.get("error", {}).get("code") in RETRYABLE]
                    if failed:
                        events = [{"provider": "exa_direct", "reason": "partial_contents_failure",
                                   "failed_urls": failed}]
                        try:
                            replacement = await self._remote_fetch(failed, events)
                        except ExaError as error:
                            if len(failed) == len(result["results"]):
                                raise
                            result["fallbacks"] = events + [{"provider": "exa_remote_mcp", **error.as_dict()}]
                        else:
                            mapping = {p["requested_url"]: p for p in replacement["results"]}
                            result["results"] = [mapping.get(p["requested_url"], p) for p in result["results"]]
                            result["fallbacks"] = replacement["fallbacks"]
                            result["fallback_usage"] = replacement["usage"]
                return result
        if method == "contents":
            return await self._remote_fetch(argument, fallbacks)
        try:
            result = await self.remote.search(argument, **kwargs)
        except ExaError as error:
            error.attempts = fallbacks + [{"provider": "exa_remote_mcp", **error.as_dict()}]
            raise
        result["fallbacks"] = fallbacks
        return result

    async def _remote_fetch(self, urls, fallbacks):
        try:
            result = await self.remote.contents(urls)
        except ExaError as error:
            if self.http_reader is None or error.code not in RETRYABLE:
                error.attempts = fallbacks + [{"provider": "exa_remote_mcp", **error.as_dict()}]
                raise
            fallbacks.append({"provider": "exa_remote_mcp", **error.as_dict()})
            result = await asyncio.wait_for(self.http_reader.contents(urls), REQUEST_SECONDS)
        else:
            failed = [p["requested_url"] for p in result["results"]
                      if p.get("error", {}).get("code") in RETRYABLE]
            if failed and self.http_reader is not None:
                replacement = await asyncio.wait_for(self.http_reader.contents(failed), REQUEST_SECONDS)
                mapping = {p["requested_url"]: p for p in replacement["results"]}
                result["results"] = [mapping.get(p["requested_url"], p) for p in result["results"]]
                fallbacks.append({"provider": "exa_remote_mcp", "reason": "partial_contents_failure"})
        result["fallbacks"] = fallbacks
        return result

    async def search(self, request, *, deep=False):
        return await self._run("search", request, deep=deep)

    async def contents(self, urls):
        return await self._run("contents", checked_urls(urls))

    async def search_and_fetch(self, query, num_results=10, fetch_top=5):
        async def pipeline():
            search = await self.search(SearchRequest(query, num_results))
            urls = []
            for p in search["results"]:
                url = p.get("url")
                if url and url not in urls:
                    try:
                        checked_urls([url])
                    except ExaError:
                        continue
                    urls.append(url)
            urls = urls[:max(0, min(fetch_top, MAX_FETCH, num_results))]
            fetched = await self.contents(urls) if urls else envelope("none", [])
            budget = MAX_TOTAL_TEXT
            for p in search["results"]:
                text = p["text"]
                p["text"] = text[:min(1000, budget)]
                p["truncated"] |= len(text) > len(p["text"])
                budget -= len(p["text"])
            for index, p in enumerate(fetched["results"]):
                text = p["text"]
                allowance = budget // (len(fetched["results"]) - index)
                p["text"] = text[:allowance]
                p["truncated"] |= len(text) > allowance
                budget -= len(p["text"])
            # Don't duplicate opaque remote strings alongside structured bounded text.
            search.pop("raw_text", None)
            fetched.pop("raw_text", None)
            return redact({"query": query, "search": search, "contents": fetched,
                    "limits": {"num_results": MAX_RESULTS, "fetch_top": MAX_FETCH,
                               "total_text_characters": MAX_TOTAL_TEXT, "timeout_seconds": PIPELINE_SECONDS}}, self.config.key)
        try:
            return await asyncio.wait_for(pipeline(), PIPELINE_SECONDS)
        except TimeoutError:
            raise ExaError("EXA_TIMEOUT", reason="pipeline_timeout") from None


def render(result):
    if result.get("raw_text") is not None:
        return result["raw_text"]
    # Keep readable Title/URL/Published text blocks.
    return "\n\n".join("Title: " + str(p.get("title") or "") + "\nURL: " + str(p.get("url") or "")
                        + "\nPublished: " + str(p.get("published_at") or "") + "\n" + p["text"]
                        for p in result["results"] if not p.get("error"))
