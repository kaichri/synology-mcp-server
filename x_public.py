"""Best-effort, anonymous public X reader. No login, internal APIs or browser code."""
import asyncio
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import threading
import time
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import weakref

X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "mobile.x.com"}
FETCH_HOSTS = X_HOSTS | {"t.co", "html.duckduckgo.com"}
MAX_BYTES = 2 * 1024 * 1024
USER = re.compile(r"[A-Za-z0-9_]{1,15}")
STATUS = re.compile(r"/(?:([A-Za-z0-9_]{1,15})|i/web)/status/([1-9][0-9]{0,19})(?:/(?:photo|video)/[1-9][0-9]*)?/?$")


class XError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def failure(code, message):
    return {"ok": False, "complete": False, "completeness": "unavailable", "error": {"code": code, "message": message}}


def checked_url(url, hosts=FETCH_HOSTS):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) <= 32 for c in url) or "\\" in url:
        raise XError("UNSUPPORTED_URL", "Invalid public URL.")
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or p.hostname not in hosts or p.username or p.password:
            raise ValueError()
        if p.port is not None and p.port != (443 if p.scheme == "https" else 80):
            raise ValueError()
    except ValueError:
        raise XError("UNSUPPORTED_URL", "Only allowed public HTTP(S) hosts and standard ports are supported.") from None
    return p


def normalize_url(url):
    p = checked_url(url, X_HOSTS)
    match = STATUS.fullmatch(p.path)
    if not match:
        raise XError("UNSUPPORTED_URL", "Expected an X or Twitter status URL.")
    username, post_id = match.groups()
    return "https://x.com/" + (username or "i/web") + "/status/" + post_id, post_id


def public_addresses(host, port):
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast
                            or ipaddress.ip_address(a).is_reserved for a in addresses):
        raise XError("UNSUPPORTED_URL", "Non-public DNS destination refused.")
    return addresses


class PublicHTTP:
    """DNS-pinned connections retain certificate checks and reject every unsafe redirect."""
    def __init__(self):
        self._lock = threading.Lock()
        self._next = 0.0
        self._slots = threading.BoundedSemaphore(2)

    def fetch(self, url):
        with self._slots:
            with self._lock:
                time.sleep(max(0, self._next - time.monotonic()))
                self._next = time.monotonic() + 0.5
            deadline = time.monotonic() + 25
            for _ in range(5):
                p = checked_url(url)
                port = p.port or (443 if p.scheme == "https" else 80)
                addresses = public_addresses(p.hostname, port)
                timeout = min(8, deadline - time.monotonic())
                if timeout <= 0:
                    raise XError("FETCH_FAILED", "Public request timed out.")
                if p.scheme == "https":
                    connection = http.client.HTTPSConnection(p.hostname, port, timeout=timeout, context=ssl.create_default_context())
                else:
                    connection = http.client.HTTPConnection(p.hostname, port, timeout=timeout)
                # Connect only to the IPs already checked, never resolve a second time.
                def _pinned_connect(address, conn_timeout, source_address=None):
                    last_error = None
                    for ip in addresses:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            return socket.create_connection(
                                (ip, port), min(conn_timeout, remaining), source_address)
                        except OSError as exc:
                            last_error = exc
                    raise last_error or TimeoutError("Public request timed out.")

                connection._create_connection = _pinned_connect
                try:
                    connection.request("GET", p.path + ("?" + p.query if p.query else ""), headers={
                        "User-Agent": "PublicMCPReader/1.0", "Accept": "text/html, application/json", "Accept-Encoding": "identity"})
                    response = connection.getresponse()
                    if response.status == 202 and p.hostname == "html.duckduckgo.com":
                        raise XError("BLOCKED", "Search discovery returned an anti-bot challenge; no bypass attempted.")
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.getheader("Location")
                        if not location:
                            raise XError("FETCH_FAILED", "Redirect has no destination.")
                        target = urljoin(url, location)
                        checked_url(target)
                        url = target
                        continue
                    if response.status == 429:
                        raise XError("RATE_LIMITED", "Public endpoint rate limited the request; try later.")
                    if response.status in {404, 410}:
                        raise XError("NOT_FOUND", "Post unavailable or deleted.")
                    if response.status in {401, 403}:
                        raise XError("BLOCKED", "Anonymous access refused; no login or bypass attempted.")
                    if response.status != 200:
                        raise XError("FETCH_FAILED", "Public endpoint returned an unsuccessful HTTP status.")
                    kind = response.getheader("Content-Type", "").split(";")[0].strip().lower()
                    if kind not in {"text/html", "application/json", "application/ld+json", "application/xhtml+xml"}:
                        raise XError("FETCH_FAILED", "Unsupported response content type.")
                    chunks, size = [], 0
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise XError("FETCH_FAILED", "Response timed out.")
                        if connection.sock:
                            connection.sock.settimeout(min(8, remaining))
                        chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise XError("FETCH_FAILED", "Response exceeds the 2 MiB limit.")
                        chunks.append(chunk)
                    return url, b"".join(chunks).decode("utf-8", errors="replace")
                finally:
                    connection.close()
            raise XError("FETCH_FAILED", "Too many redirects.")


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.links, self.documents, self.text = {}, [], [], []
        self._script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.meta[attrs.get("property", attrs.get("name", ""))] = attrs.get("content", "")
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "script" and (attrs.get("type") in {"application/ld+json", "application/json"}
                                or attrs.get("id") == "__NEXT_DATA__"):
            self._script = []

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        else:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            try:
                self.documents.append(json.loads("".join(self._script)))
            except (ValueError, RecursionError):
                pass
            self._script = None


def objects(value, depth=0):
    if depth > 30:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item, depth + 1)


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc).isoformat()


def safe_link(value):
    try:
        p = urlsplit(value)
        if p.scheme in {"http", "https"} and p.hostname and not p.username and not p.password:
            return value
    except (ValueError, TypeError):
        pass
    return None


def mapping(value):
    return value if isinstance(value, dict) else {}


def numeric_id(value):
    return str(value) if isinstance(value, (str, int)) and re.fullmatch(r"[1-9][0-9]{0,19}", str(value)) else None


def make_post(post_id, username, text, source, *, author_name=None, created=None,
              complete=False, reply=None, conversation=None, media=None, quote=None, urls=None):
    return {"post_id": post_id, "url": f"https://x.com/{username or 'i/web'}/status/{post_id}",
            "author_name": author_name, "username": username, "text": text,
            "published_at": timestamp(created), "quote_post": quote, "quote_status": "present" if quote else "unknown",
            "mentioned_accounts": list(dict.fromkeys(re.findall(r"(?<!\w)@([A-Za-z0-9_]{1,15})", text))),
            "hashtags": list(dict.fromkeys(re.findall(r"(?<!\w)#(\w+)", text))),
            "urls": list(dict.fromkeys(filter(None, [safe_link(u) for u in (urls or re.findall(r'https?://[^\s<>"\']+', text))]))),
            "media": media or [], "reply_to_post_id": str(reply) if reply else None,
            "conversation_id": str(conversation) if conversation else None,
            "complete": complete, "completeness": "complete" if complete else "partial", "text_complete": complete,
            "metadata_complete": False, "provider": "public_http", "source": source,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def parse_page(url, body):
    canonical, wanted = normalize_url(url)
    page = Page()
    page.feed(body)
    try:
        page.documents.append(json.loads(body))
    except (ValueError, RecursionError):
        pass
    posts = {}
    for doc in page.documents:
        for n, obj in enumerate(objects(doc)):
            if n > 20000:
                break
            legacy = mapping(obj.get("legacy", obj))
            post_id = numeric_id(obj.get("rest_id", legacy.get("id_str")))
            if post_id and isinstance(legacy.get("full_text"), str) and legacy["full_text"].strip():
                user_result = mapping(mapping(mapping(obj.get("core")).get("user_results")).get("result"))
                user = mapping(user_result.get("legacy")) or mapping(user_result.get("core")) or mapping(legacy.get("user"))
                username = user.get("screen_name")
                if isinstance(username, str) and USER.fullmatch(username):
                    note = mapping(mapping(mapping(obj.get("note_tweet")).get("note_tweet_results")).get("result")).get("text")
                    text = note if isinstance(note, str) and note.strip() else legacy["full_text"]
                    entities = mapping(legacy.get("entities"))
                    media = [{"type": m.get("type"), "url": safe_link(m.get("media_url_https")),
                              "alt_text": m.get("ext_alt_text"), "sizes": m.get("sizes"),
                              "video_variants": [{"url": safe_link(v.get("url")), "content_type": v.get("content_type"),
                                  "bitrate": v.get("bitrate")} for v in mapping(m.get("video_info")).get("variants", []) if isinstance(v, dict)]} for m in
                             (mapping(legacy.get("extended_entities")).get("media") or entities.get("media") or []) if isinstance(m, dict)]
                    quote_result = mapping(mapping(obj.get("quoted_status_result")).get("result"))
                    quote_id = numeric_id(legacy.get("quoted_status_id_str") or quote_result.get("rest_id"))
                    quote = {"post_id": str(quote_id), "url": f"https://x.com/i/web/status/{quote_id}"} if quote_id else None
                    posts[str(post_id)] = make_post(str(post_id), username, text, "embedded_json", author_name=user.get("name"),
                        created=legacy.get("created_at"), complete=bool(note) or not legacy.get("truncated", True),
                        reply=numeric_id(legacy.get("in_reply_to_status_id_str")), conversation=numeric_id(legacy.get("conversation_id_str")),
                        media=media, quote=quote, urls=[u.get("expanded_url", u.get("url", "")) for u in entities.get("urls", []) if isinstance(u, dict)])
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(t in ("SocialMediaPosting", "BlogPosting", "Article") for t in types):
                continue
            candidate = obj.get("url", obj.get("mainEntityOfPage"))
            if isinstance(candidate, dict):
                candidate = candidate.get("@id")
            try:
                normalized, post_id = normalize_url(candidate)
            except XError:
                continue
            text = obj.get("articleBody", obj.get("text"))
            if not isinstance(text, str) or not text.strip():
                continue
            author = obj.get("author") or {}
            author = author[0] if isinstance(author, list) and author else author
            author = author if isinstance(author, dict) else {}
            username = STATUS.fullmatch(urlsplit(normalized).path).group(1)
            image = obj.get("image")
            images = image if isinstance(image, list) else [image] if image else []
            media = [{"type": "image", "url": safe_link(i.get("url") if isinstance(i, dict) else i)} for i in images]
            posts.setdefault(post_id, make_post(post_id, username, text, "json_ld", author_name=author.get("name"),
                created=obj.get("datePublished"), complete=not text.rstrip().endswith(("…", "...")), media=media))
    if wanted not in posts:
        text = page.meta.get("og:description", page.meta.get("twitter:description", ""))
        if text.strip() and not any(s in text.lower() for s in ("join twitter", "join x today", "log in to x", "sign in to x", "from breaking news", "what's happening", "posts are protected", "tweets are protected")):
            username = STATUS.fullmatch(urlsplit(canonical).path).group(1)
            try:
                meta_url, meta_id = normalize_url(page.meta.get("og:url", ""))
                if meta_id == wanted:
                    username = STATUS.fullmatch(urlsplit(meta_url).path).group(1) or username
            except XError:
                pass
            author_name = None
            title = re.fullmatch(r"(.+?)\s+\(@([A-Za-z0-9_]{1,15})\) on (?:X|Twitter)", page.meta.get("og:title", ""))
            if title and username and title[2].lower() == username.lower():
                author_name = title[1]
            media = []
            image = safe_link(page.meta.get("og:image:secure_url", page.meta.get("og:image")))
            if image and urlsplit(image).hostname == "pbs.twimg.com":
                media.append({"type": "image_preview", "url": image, "content_type": page.meta.get("og:image:type"),
                              "attribution": "page_preview_not_verified_post_attachment"})
            posts[wanted] = make_post(wanted, username, text, "meta", complete=False,
                author_name=author_name, created=page.meta.get("article:published_time"), media=media)
    if wanted not in posts:
        visible = " ".join(page.text).lower()
        if any(s in visible for s in ("these posts are protected", "this account is private", "these tweets are protected")):
            raise XError("PRIVATE_OR_PROTECTED", "Public content is protected; no login attempted.")
        if any(s in visible for s in ("post was deleted", "tweet was deleted", "page doesn't exist")):
            raise XError("NOT_FOUND", "Post unavailable or deleted.")
        if any(s in visible for s in ("captcha", "verify you are human", "access denied", "sign in to x", "log in to x")):
            raise XError("BLOCKED", "Public content is blocked or requires login.")
        raise XError("PARTIAL_CONTENT", "No readable public post text; page may require JavaScript.")
    for post in posts.values():
        if post["quote_post"] and post["quote_post"]["post_id"] in posts:
            quote = posts[post["quote_post"]["post_id"]]
            post["quote_post"] = {k: v for k, v in quote.items() if k != "quote_post"}
    links = []
    for link in page.links:
        try:
            links.append(normalize_url(urljoin(url, link))[0])
        except XError:
            pass
    return {"post": posts[wanted], "related_posts": list(posts.values()), "status_links": list(dict.fromkeys(links))[:100]}


def _close_sqlite(db):
    try:
        db.close()
    except sqlite3.Error:
        pass


class Cache:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self._db = None
        self._finalizer = None

    def _connect(self):
        if self._db is not None:
            return self._db
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if os.name != "nt":
            os.chmod(path, 0o600)
        db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS public_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_public_cache_expires ON public_cache(expires)")
        if os.name != "nt":
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)
        self._db = db
        self._finalizer = weakref.finalize(self, _close_sqlite, db)
        return db

    def get(self, key):
        with self.lock:
            db = self._connect()
            row = db.execute("SELECT value FROM public_cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, key, value, ttl):
        with self.lock:
            db = self._connect()
            now = time.time()
            with db:
                db.execute("DELETE FROM public_cache WHERE expires<=?", (now,))
                db.execute("INSERT OR REPLACE INTO public_cache VALUES (?,?,?)", (key, json.dumps(value), now + ttl))
                # Bound persistent storage even when a client supplies many distinct searches.
                db.execute("DELETE FROM public_cache WHERE key IN (SELECT key FROM public_cache ORDER BY expires DESC LIMIT -1 OFFSET 2000)")

    def close(self):
        with self.lock:
            if self._finalizer is not None:
                self._finalizer()
                self._finalizer = None
            self._db = None


class XPublicProvider(Protocol):
    async def read_post(self, url: str, refresh: bool = False) -> dict: ...
    async def read_thread(self, url: str, max_posts: int = 20, include_replies: bool = False, refresh: bool = False) -> dict: ...
    async def search(self, query: str, limit: int = 20, since: str | None = None,
                     until: str | None = None, from_user: str | None = None, refresh: bool = False) -> dict: ...


class FreePublicProvider:
    def __init__(self, fetcher=None, cache=None):
        self.http = fetcher or PublicHTTP()
        self.cache = cache if cache is not None else (Cache(os.environ.get("MCP_X_CACHE_PATH", "data/x-public.sqlite3"))
            if os.environ.get("MCP_X_CACHE_ENABLED", "true").lower() != "false" else None)

    async def _document(self, url, refresh=False):
        p = checked_url(url, X_HOSTS | {"t.co"})
        if p.hostname == "t.co":
            url, body = await asyncio.to_thread(self.http.fetch, url)
            canonical, post_id = normalize_url(url)
        else:
            canonical, post_id = normalize_url(url)
            body = None
        key = "post:" + post_id
        cached = await asyncio.to_thread(self.cache.get, key) if self.cache and not refresh else None
        if cached:
            cached["post"]["cached"] = True
            return cached
        if body is None:
            final, body = await asyncio.to_thread(self.http.fetch, canonical)
            _, final_id = normalize_url(final)
            if final_id != post_id:
                raise XError("FETCH_FAILED", "Redirect changed the requested post identity.")
        doc = parse_page(canonical, body)
        doc["post"]["cached"] = False
        if self.cache:
            await asyncio.to_thread(self.cache.put, key, doc, 30 * 86400 if doc["post"]["text_complete"] else 3600)
        return doc

    async def read_post(self, url, refresh=False):
        doc = await self._document(url, refresh)
        post = doc["post"]
        return {"ok": True, "post": post, "warnings": [] if post["text_complete"] else ["PARTIAL_CONTENT"]}

    async def _discover(self, query):
        _, body = await asyncio.to_thread(self.http.fetch, "https://html.duckduckgo.com/html/?" + urlencode({"q": query}))
        page = Page()
        page.feed(body)
        visible = " ".join(page.text).lower()
        if "captcha" in visible or "bots use duckduckgo" in visible or "anomaly" in visible:
            raise XError("BLOCKED", "Search discovery is blocked; no challenge bypass attempted.")
        urls = {}
        for link in page.links:
            parsed = urlsplit(urljoin("https://html.duckduckgo.com", link))
            if parsed.hostname in {"duckduckgo.com", "html.duckduckgo.com"}:
                link = parse_qs(parsed.query).get("uddg", [link])[0]
            try:
                canonical, post_id = normalize_url(link)
                urls[post_id] = canonical
            except XError:
                pass
        return list(urls.values())[:100]

    async def search(self, query, limit=20, since=None, until=None, from_user=None, refresh=False):
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or not 1 <= limit <= 50:
            raise XError("INVALID_ARGUMENT", "Use a non-empty query of at most 500 characters and limit 1..50.")
        if from_user is not None and not USER.fullmatch(from_user):
            raise XError("INVALID_ARGUMENT", "Invalid public username.")
        try:
            if any(d and (not isinstance(d, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)) for d in (since, until)):
                raise ValueError()
            dates = [date.fromisoformat(d) if d else None for d in (since, until)]
        except ValueError:
            raise XError("INVALID_ARGUMENT", "Dates must be YYYY-MM-DD.") from None
        if dates[0] and dates[1] and dates[0] >= dates[1]:
            raise XError("INVALID_ARGUMENT", "since must precede until (exclusive).")
        key = "search:" + json.dumps([query, limit, since, until, from_user])
        cached = await asyncio.to_thread(self.cache.get, key) if self.cache and not refresh else None
        if cached:
            cached["cached"] = True
            return cached
        candidates = {}
        for host in ("x.com", "twitter.com"):
            q = f"{query} site:{host}/" + (f"{from_user}/status" if from_user else "")
            if since:
                q += " after:" + since
            if until:
                q += " before:" + until
            for url in await self._discover(q):
                candidates[normalize_url(url)[1]] = url
        results, skipped = [], {}
        for url in list(candidates.values())[:min(100, limit * 2)]:
            try:
                post = (await self.read_post(url, refresh))["post"]
                if from_user and (post["username"] or "").lower() != from_user.lower():
                    continue
                published = date.fromisoformat(post["published_at"][:10]) if post["published_at"] else None
                if any(dates) and (published is None or (dates[0] and published < dates[0]) or (dates[1] and published >= dates[1])):
                    skipped["DATE_UNVERIFIABLE_OR_OUTSIDE_RANGE"] = skipped.get("DATE_UNVERIFIABLE_OR_OUTSIDE_RANGE", 0) + 1
                    continue
                results.append(post)
                if len(results) >= limit:
                    break
            except XError as exc:
                skipped[exc.code] = skipped.get(exc.code, 0) + 1
        result = {"ok": True, "posts": results, "cached": False, "provider": "public_http",
                  "fetched_at": datetime.now(timezone.utc).isoformat(), "completeness": "partial", "posts_found": len(results), "discovered_count": len(candidates),
                  "skipped": skipped, "complete": False, "limit_reached": len(results) >= limit,
                  "warnings": ["Search engines index only a subset of public posts; results are not exhaustive."]}
        if self.cache:
            await asyncio.to_thread(self.cache.put, key, result, 300)
        return result

    async def read_thread(self, url, max_posts=20, include_replies=False, refresh=False):
        if not 1 <= max_posts <= 50:
            raise XError("INVALID_ARGUMENT", "max_posts must be between 1 and 50.")
        root = await self._document(url, refresh)
        seed = root["post"]
        candidates = {p["post_id"]: p for p in root["related_posts"]}
        links = list(root["status_links"])
        if seed["reply_to_post_id"]:
            links.insert(0, "https://x.com/i/web/status/" + seed["reply_to_post_id"])
        warnings = ["Public discovery cannot prove a thread is exhaustive."]
        if seed["username"]:
            try:
                links += await self._discover(f'"{seed["conversation_id"] or seed["post_id"]}" site:x.com/{seed["username"]}/status')
            except XError as exc:
                warnings.append(exc.code)
        seen = {seed["post_id"]}
        attempts = 0
        while links and attempts < max_posts:
            target = links.pop(0)
            target_id = normalize_url(target)[1]
            if target_id in seen:
                continue
            seen.add(target_id)
            attempts += 1
            try:
                doc = await self._document(target, refresh)
                candidates.update({p["post_id"]: p for p in doc["related_posts"]})
                links.extend(doc["status_links"][:max_posts])
                parent = doc["post"]["reply_to_post_id"]
                if parent and parent not in seen:
                    links.append("https://x.com/i/web/status/" + parent)
            except XError as exc:
                warnings.append(exc.code)
        selected = {seed["post_id"]: seed}
        for _ in range(len(candidates)):
            changed = False
            for post_id, post in candidates.items():
                if post_id in selected:
                    continue
                same_author = bool(seed["username"] and post["username"] and seed["username"].lower() == post["username"].lower())
                related = bool(seed["conversation_id"] and post["conversation_id"] == seed["conversation_id"])
                related = related or post["reply_to_post_id"] in selected or any(p["reply_to_post_id"] == post_id for p in selected.values())
                if related and (include_replies or same_author):
                    selected[post_id] = post
                    changed = True
            if not changed:
                break
        known_dates = all(p["published_at"] for p in selected.values())
        posts = sorted(selected.values(), key=lambda p: (p["published_at"], int(p["post_id"])) if known_dates else int(p["post_id"]))
        termination = "max_posts_reached" if len(posts) >= max_posts else "provider_limit" if links else "thread_end_unknown"
        if len(posts) < max_posts:
            for code, reason in (("RATE_LIMITED", "rate_limited"), ("BLOCKED", "blocked"), ("FETCH_FAILED", "fetch_failed")):
                if code in warnings:
                    termination = reason
                    break
        return {"ok": True, "posts": posts[:max_posts], "posts_found": min(len(posts), max_posts), "complete": False,
                "completeness": "partial", "termination_reason": termination,
                "limit_reached": len(posts) >= max_posts or bool(links), "fetch_attempts": attempts,
                "stop_reason": "fetch_budget" if links else "public_candidates_exhausted",
                "ordering": "published_at" if known_dates else "post_id_chronological", "warnings": list(dict.fromkeys(warnings))}


_provider = None


def get_provider() -> XPublicProvider:
    global _provider
    if _provider is None:
        _provider = FreePublicProvider()
    return _provider


async def call_provider(method, **kwargs):
    try:
        async with asyncio.timeout(90):
            return await getattr(get_provider(), method)(**kwargs)
    except XError as exc:
        return failure(exc.code, exc.message)
    except Exception:
        # No transport errors, raw pages, cookies, file paths or stack traces cross MCP.
        return failure("FETCH_FAILED", "Public provider could not complete the request.")
