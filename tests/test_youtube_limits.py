import asyncio
import logging
from urllib.error import HTTPError

import httpx
import pytest

import server


@pytest.fixture(autouse=True)
def youtube_state(monkeypatch):
    monkeypatch.setattr(server, "_youtube_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_youtube_next_job", 0)
    monkeypatch.setattr(server, "_youtube_cooldowns", {})
    monkeypatch.setattr(server, "_transcript_cache", {})
    monkeypatch.setattr(server, "YOUTUBE_JOB_INTERVAL", 0.01)
    monkeypatch.setattr(server, "YOUTUBE_QUEUE_TIMEOUT", 0.08)


def test_concurrent_transcripts_share_cache_by_video_and_languages(monkeypatch, caplog):
    calls = []

    async def remote(url, tool, args):
        calls.append(args)
        await asyncio.sleep(0.01)
        return "Transcript text"

    monkeypatch.setattr(server, "call_mcp", remote)

    async def run():
        first = server.youtube_transcript("https://youtu.be/abc123")
        second = server.youtube_transcript("https://www.youtube.com/watch?v=abc123")
        assert await asyncio.gather(first, second) == ["Transcript text"] * 2
        assert len(calls) == 1
        await server.youtube_transcript("https://youtu.be/abc123", "en,de")
        assert len(calls) == 2

    with caplog.at_level(logging.INFO, logger="uvicorn.error.youtube"):
        asyncio.run(run())
    messages = [record.getMessage() for record in caplog.records
                if record.name == "uvicorn.error.youtube"]
    assert len(messages) == 3
    assert all("tool=youtube_transcript" in text and "queue_ms=" in text and "fetch_ms=" in text
               for text in messages)
    cache_hits = [text for text in messages if "outcome=cached" in text]
    assert len(cache_hits) == 1 and "fetch_ms=0 cache=hit" in cache_hits[0]
    assert all("abc123" not in text and "Transcript text" not in text for text in messages)


def test_remote_429_honors_header_and_stops_fallbacks(monkeypatch, caplog):
    calls = []

    async def remote(*args):
        calls.append(args)
        request = httpx.Request("POST", server.TRANSCRIPT_MCP_URL)
        response = httpx.Response(429, headers={"Retry-After": "120"}, request=request)
        raise ExceptionGroup("MCP exception", [
            httpx.HTTPStatusError("Too Many Requests", request=request, response=response)
        ])

    def unexpected(*args, **kwargs):
        pytest.fail("YouTube fallback ran after a remote rate limit")

    monkeypatch.setattr(server, "call_mcp", remote)
    monkeypatch.setattr(server, "YouTubeTranscriptApi", unexpected)
    with caplog.at_level(logging.WARNING):
        first = asyncio.run(server.youtube_transcript("https://youtu.be/abc123"))
        second = asyncio.run(server.youtube_transcript("https://youtu.be/other"))
    assert "Retry in 120 seconds" in first
    assert "paused" in second and len(calls) == 1
    assert "stage=remote_transcript route=youtube-transcript.ai status=429" in caplog.text
    assert "abc123" not in caplog.text


def test_direct_limit_stops_ytdlp_and_pauses_metadata(monkeypatch):
    async def remote(*args):
        return "# no captions available"

    def limited(*args, **kwargs):
        raise server.YouTubeRateLimited()

    def unexpected(*args, **kwargs):
        pytest.fail("yt-dlp started during direct rate limit")

    monkeypatch.setattr(server, "call_mcp", remote)
    monkeypatch.setattr(server, "YouTubeTranscriptApi", limited)
    monkeypatch.setattr(server, "_transcript_via_ytdlp", unexpected)
    monkeypatch.setattr(server, "subprocess", type("Stub", (), {"run": unexpected}))
    url = "https://youtu.be/abc123"
    assert "Retry in 300 seconds" in asyncio.run(server.youtube_transcript(url))
    assert "paused for www.youtube.com" in asyncio.run(server.youtube_metadata(url))


def test_subtitle_429_is_not_retried(monkeypatch):
    calls = []

    def limited(*args, **kwargs):
        calls.append(args)
        raise HTTPError("https://www.youtube.com/api/timedtext", 429, "limited",
                        {"Retry-After": "75"}, None)

    monkeypatch.setattr("urllib.request.urlopen", limited)
    with pytest.raises(server.YouTubeRateLimited) as exc:
        server._fetch_sub_url("https://www.youtube.com/api/timedtext")
    assert exc.value.retry_after == "75"
    assert len(calls) == 1


def test_queue_wait_is_limited_and_cancelled_caller_does_not_free_worker(monkeypatch):
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        async def work():
            started.set()
            await finish.wait()
            return "done", False

        async def unexpected():
            pytest.fail("A second job started before the first finished")

        caller = asyncio.create_task(server._run_youtube_job("youtube_metadata", work, server.YOUTUBE_DIRECT))
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert "queue busy" in await server._run_youtube_job(
            "youtube_comments", unexpected, server.YOUTUBE_DIRECT)
        finish.set()
        async with asyncio.timeout(1):
            async with server._youtube_lock:
                pass

    asyncio.run(run())


def test_cooldown_escalates_without_retry_after(monkeypatch):
    now = 1000
    monkeypatch.setattr(server.time, "monotonic", lambda: now)
    for delay in (300, 900, 1800, 1800):
        assert f"Retry in {delay} seconds" in server._record_youtube_limit(
            server.YOUTUBE_DIRECT, "test", server.YouTubeRateLimited())
        now += delay + 1
