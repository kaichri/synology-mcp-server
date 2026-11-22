"""Offline regression checks for the unchanged existing business logic."""
import asyncio
from datetime import datetime
import json
from types import SimpleNamespace

import pytest
import server
from exa_provider import OBJECTIVE, MAX_TEXT


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=abc123", "https://youtu.be/abc123",
                                  "https://www.youtube.com/shorts/abc123", "https://youtube.com/live/abc123"])
def test_youtube_video_id(url):
    assert server.get_video_id(url) == "abc123"


def test_time_and_transcript_helpers():
    assert datetime.fromisoformat(server.current_time()).tzinfo is not None
    assert server.clean_caption("I I think,   you know this works.") == "I think, this works."
    assert server.format_timestamp(3661) == "1:01:01"
    assert server._format_transcript_blocks([(0.0, "First sentence."), (5.0, "Next sentence.")]) == "[0:00] First sentence.\n[0:05] Next sentence."


def test_web_search_and_fetch_contract(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setenv("EXA_PROVIDER", "remote_mcp")
    calls = []
    async def fake(tool, args):
        calls.append((tool, args))
        return "unchanged-text-output"
    monkeypatch.setattr(server, "call_exa", fake)
    assert asyncio.run(server.web_search("query", 99)) == "unchanged-text-output"
    assert asyncio.run(server.web_fetch("https://example.org")) == "unchanged-text-output"
    assert calls == [("web_search_exa", {"query": "query", "numResults": 20, "objective": OBJECTIVE}),
                     ("web_fetch_exa", {"urls": ["https://example.org"], "maxCharacters": MAX_TEXT})]


def test_youtube_metadata_contract(monkeypatch):
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=json.dumps({"id": "abc123", "title": "Video"}), stderr=""))
    result = json.loads(asyncio.run(server.youtube_metadata("https://youtu.be/abc123")))
    assert result["id"] == "abc123" and result["title"] == "Video"


def test_news_parsing_and_invalid_input():
    parsed = server._parse_exa_results("Title: News\nURL: https://example.org/news\nPublished: 2026-09-30\nSnippet: Market update")
    assert parsed[0]["url"] == "https://example.org/news"
    assert parsed[0]["publishedDate"] == "2026-09-30"
    result = json.loads(asyncio.run(server.finance_news_candidates("not-json")))
    assert result["candidates"] == [] and "error" in result
