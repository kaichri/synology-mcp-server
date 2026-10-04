"""Deterministic finance-news quality regression tests; no network or Exa credits."""
import asyncio
from datetime import datetime, timezone
import json
import pytest
import server

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)

@pytest.fixture
def news(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(server, "datetime", Clock)
    calls = {"search": [], "fetch": []}
    state = {"pages": [], "body": '<title>NVIDIA update</title><meta name="description" content="earnings report"><meta name="date" content="2026-10-04T11:00:00Z">', "type": "text/html"}
    async def search(query, count):
        calls["search"].append((query, count))
        return json.dumps(state["pages"])
    def fetch(url):
        calls["fetch"].append(url)
        return state["body"], 200, state["type"]
    monkeypatch.setattr(server, "web_search", search)
    monkeypatch.setattr(server, "_direct_http_fetch", fetch)
    def run(interests=None, **kwargs):
        return json.loads(asyncio.run(server.finance_news_candidates(
            json.dumps(interests if interests is not None else [interest()]), **kwargs)))
    return state, calls, run


def interest(**changes):
    return dict({"id": "nv", "name": "NVIDIA", "category": "finance", "priority": "high", "topics": ["earnings"]}, **changes)


def article(**changes):
    return dict({"url": "https://example.org/news", "title": "NVIDIA earnings", "snippet": "Useful search metadata", "publishedDate": "2026-10-04T11:00:00Z"}, **changes)


@pytest.mark.parametrize("content_type,body", [
    ("application/pdf", "%PDF-1.7 binary content"),
    ("application/pdf", "Unknown binary data"),
    ("application/octet-stream", "Binary data"),
    ("image/png", "PNG binary"),
    ("text/html", "%PDF-1.7 disguised as HTML"),
    ("text/plain", "  %PDF-1.7 disguised as text"),
    ("text/html", "binary\x00content"),
])
def test_binary_fetch_preserves_search_metadata(news, content_type, body):
    state, calls, run = news
    state.update(pages=[article(publishedDate="")], type=content_type, body=body)
    result = run()
    assert len(calls["fetch"]) == 1
    assert result["fetch_failed"] == 1 and result["fetched"] == 0
    assert result["candidates"][0]["title"] == "NVIDIA earnings"
    assert result["candidates"][0]["snippet"] == "Useful search metadata"
    assert result["undated_kept"] == 1


@pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", "application/xhtml+xml", "text/plain"])
def test_text_fetch_still_enriches(news, content_type):
    state, calls, run = news
    state.update(pages=[article(snippet="", publishedDate="")], type=content_type)
    result = run()
    item = result["candidates"][0]
    assert result["fetched"] == 1
    assert item["snippet"] == "earnings report"
    assert item["published"] == "2026-10-04T11:00:00Z"


def test_newest_first_in_same_priority_and_topic_group(news):
    state, _, run = news
    state["pages"] = [article(url="https://example.org/old", publishedDate="2026-10-03T12:00:00Z"),
                      article(url="https://example.org/new", publishedDate="2026-10-04T11:00:00Z")]
    assert [x["url"] for x in run()["candidates"]] == ["https://example.org/new", "https://example.org/old"]


def test_priority_still_precedes_freshness(news):
    state, _, run = news
    # Each interest sees both URLs; make URLs distinct per query to avoid first-interest dedupe semantics.
    original = server.web_search
    async def search(query, count):
        await original(query, count)
        high = '"NVIDIA"' in query
        return json.dumps([article(url="https://example.org/high" if high else "https://example.org/medium",
                                  publishedDate="2026-10-03T12:00:00Z" if high else "2026-10-04T11:00:00Z")])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server, "web_search", search)
        result = run([interest(), interest(id="btc", name="Bitcoin", priority="medium")])
    assert [x["priority"] for x in result["candidates"]] == ["high", "medium"]


def test_topic_group_still_precedes_freshness(news):
    state, _, run = news
    state["pages"] = [article(url="https://example.org/unmatched", title="Unrelated", publishedDate="2026-10-04T11:30:00Z"),
                      article(url="https://example.org/matched", publishedDate="2026-10-03T12:00:00Z")]
    assert run()["candidates"][0]["url"].endswith("/matched")


@pytest.mark.parametrize("published,keep", [
    ("2026-10-04T06:00:01Z", True), ("2026-10-04T05:59:59Z", False),
    ("2026-10-04T06:00:00Z", True),
    ("2026-10-04T08:00:01+02:00", True), ("2026-10-04T07:59:59+02:00", False),
    ("2026-10-04T06:00:00.123456Z", True),
])
def test_timestamp_lookback_preserves_time_and_offset(news, published, keep):
    state, _, run = news
    state["pages"] = [article(publishedDate=published)]
    result = run(lookback_hours=6)
    assert bool(result["candidates"]) == keep
    if keep:
        assert result["candidates"][0]["published"] == published


@pytest.mark.parametrize("published,lookback,keep", [
    ("2026-10-04", 6, True), ("2026-10-03", 6, False),
    ("2026-10-03", 36, True), ("2026-10-02", 36, False),
])
def test_date_only_day_must_overlap_window(news, published, lookback, keep):
    # A calendar day is an uncertain interval, not an exact midnight publication.
    state, _, run = news
    state["pages"] = [article(publishedDate=published)]
    result = run(lookback_hours=lookback)
    assert bool(result["candidates"]) == keep
    if keep:
        assert result["candidates"][0]["published"] == published


@pytest.mark.parametrize("topics,matched", [
    ([" earnings ", 123, None, {}, ["nested"], ""], ["earnings"]),
    (123, []), (None, []), ({"earnings": 1}, []), ("earnings", []),
])
def test_invalid_topics_ignored_in_search_and_enrichment(news, topics, matched):
    state, calls, run = news
    state["pages"] = [article(snippet="")]
    item = run([interest(topics=topics)])["candidates"][0]
    assert item["matched_topics"] == matched
    assert len(calls["fetch"]) == 1
    assert '123' not in calls["search"][0][0] and 'nested' not in calls["search"][0][0]


def test_finance_filters_empty_results_dedupe_and_limit(news):
    state, calls, run = news
    assert run([interest(category="science"), interest(news_alert="off")])["candidate_count"] == 0
    assert not calls["search"]
    assert run()["candidate_count"] == 0
    state["pages"] = [article(), article()] + [article(url=f"https://example.org/{i}") for i in range(12)]
    result = run(max_candidates=2)
    assert result["candidate_count"] == 2 and result["urls_found"] == 13
    assert len({x["url"] for x in result["candidates"]}) == 2


def test_undated_rank_after_dated_without_excluding_them(news):
    state, _, run = news
    state.update(pages=[article(url="https://example.org/undated", publishedDate=""), article()], type="application/pdf", body="%PDF-1.7")
    result = run()
    assert result["candidates"][0]["url"] == "https://example.org/news"
    assert result["undated_kept"] == 1
