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
    calls = []
    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "abc123", "title": "Video"}), stderr="")
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server, "_youtube_cooldowns", {server.YOUTUBE_DIRECT: (0.0, 2)})
    result = json.loads(asyncio.run(server.youtube_metadata("https://youtu.be/abc123")))
    assert result["id"] == "abc123" and result["title"] == "Video"
    assert calls[0][-2:] == ["--", "https://www.youtube.com/watch?v=abc123"]
    assert server.YOUTUBE_DIRECT not in server._youtube_cooldowns


@pytest.mark.parametrize("bad_url", [
    "--batch-file=/etc/passwd",
    "-a=/run/secrets/oauth_password_hash",
    "file:///etc/passwd",
    "http://127.0.0.1:8000/mcp",
    "https://example.org/watch?v=abc123",
    "https://www.youtube.com/watch?v=abc/def",
    "https://youtu.be/abc/../../etc/passwd",
])
def test_youtube_rejects_argument_injection_and_non_youtube_urls(monkeypatch, bad_url):
    def unexpected(*a, **kw):
        pytest.fail("yt-dlp must never run for invalid YouTube URLs")
    monkeypatch.setattr(server.subprocess, "run", unexpected)
    assert "error" in asyncio.run(server.youtube_metadata(bad_url)).lower()
    assert "error" in asyncio.run(server.youtube_comments(bad_url)).lower()
    assert "error" in asyncio.run(server.youtube_transcript(bad_url)).lower()


def test_youtube_vtt_without_hours_and_sub_url_validation():
    segments = server._parse_vtt_captions("WEBVTT\n\n01:05.500 --> 01:08.000\nHello world\n")
    assert segments == [(65.5, "Hello world")]
    for unsafe in ("file:///etc/passwd", "http://www.youtube.com/api/timedtext",
                   "https://127.0.0.1/api/timedtext", "https://evil.example/sub.vtt"):
        with pytest.raises(ValueError):
            server._fetch_sub_url(unsafe)
