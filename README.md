# Synology MCP Server

[![M8ven Verified](https://m8ven.ai/badge/mcp/kaichri-synology-mcp-server-12jumx?variant=verified)](https://m8ven.ai/mcp/kaichri-synology-mcp-server-12jumx?s=readme)

Personal MCP server designed for deployment on Synology NAS. The current setup is tested primarily with ChatGPT, while the MCP interface itself remains client-agnostic. Other MCP-compatible clients can connect using the supported transport and authentication methods; compatibility with untested clients is not guaranteed.

Project identifier and repository name: `synology-mcp-server`.
Display name: **Synology MCP Server**. MCP server name: **Synology MCP**.
Repository: [kaichri/synology-mcp-server](https://github.com/kaichri/synology-mcp-server).

A Python MCP server for Exa web search and page retrieval, finance news, current time, and YouTube metadata, transcripts, and comments. Two separate HTTP listeners share the same tool registry, schemas, and business logic.

The server also provides anonymous, read-only discovery and reading of public X/Twitter posts. See [Public X tools](#public-x-tools) for coverage limits and examples.

| Access | Endpoint | Authentication |
| --- | --- | --- |
| Trusted LAN | `http://<NAS_LAN_IP>:8000/mcp` | None |
| ChatGPT / Internet | `https://mcp.example.com/mcp` | OAuth 2.1, Authorization Code + PKCE S256 |
| Existing Internet clients | The same public endpoint | `Authorization: Bearer <MCP_AUTH_TOKEN>` remains supported |

Authentication depends exclusively on the listener. `Host`, client IP, `X-Forwarded-For`, `X-Real-IP`, and `X-Forwarded-Host` never bypass external authentication. Host and origin checks also protect the MCP transport but do not select authentication. Proxy headers are not used to generate public URLs.

## Setup and venv

Use Python 3.13. A virtual environment isolates local dependencies and is never committed.

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Linux / macOS:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

`requirements.txt` is sufficient for runtime use; `requirements-dev.txt` adds pytest. Run `deactivate` to leave the environment. Recreate it and install dependencies on another computer. Direct dependencies are pinned to tested versions; pip resolves transitive dependencies.

## Configure OAuth

**Keep** your existing `.env` and `MCP_AUTH_TOKEN`. Keep MCP/OAuth deployment settings in `.env`; Exa settings now use the separate ignored `config.env` described below. All addresses below are placeholders: replace the public origin and `<NAS_LAN_IP>` with your actual deployment values in the ignored local `.env` only.

```dotenv
MCP_PUBLIC_BASE_URL=https://mcp.example.com
MCP_LAN_BIND_IP=<NAS_LAN_IP>
MCP_OAUTH_USERNAME=owner
MCP_ALLOW_LEGACY_TOKEN=true
MCP_ACCESS_TOKEN_SECONDS=3600
MCP_REFRESH_TOKEN_SECONDS=63072000
MCP_AUTH_CODE_SECONDS=120
```

Choose a long, private login password. Generate its Argon2id hash interactively:

```bash
python oauth_admin.py hash-password
```

Save the output as the only line in `secrets/oauth-password-hash.txt`. Alternatively, create and check the file directly:

```bash
python oauth_admin.py hash-password --output secrets/oauth-password-hash.txt
python oauth_admin.py verify-password --hash-file secrets/oauth-password-hash.txt
```

Both commands prompt for the password interactively. `--output` writes exactly one hash followed by a newline, without displaying it or overwriting an existing file. `verify-password` reports only success or failure. Without `--hash-file`, it uses the configured hash secret or `secrets/oauth-password-hash.txt`.

The file and directory are excluded from Git and the Docker build context. Docker Compose mounts the file as a secret at `/run/secrets/oauth_password_hash`. Keep plaintext passwords and real tokens out of project files and shell commands. Configuration accepts only a complete Argon2id hash with strong, bounded parameters; extra file contents and weak hashes prevent startup. The generator uses 64 MiB of memory, three iterations, four parallel lanes, a 16-byte salt, and a 32-byte hash.

After transferring the file to the NAS, set permissions for the container user from the project directory over SSH:

```bash
sudo chown 10001:10001 secrets/oauth-password-hash.txt
sudo chmod 600 secrets/oauth-password-hash.txt
```

Additional DSM ACLs must allow UID 10001 to read the file. File-backed Compose secrets may inherit bind-mount permissions; do not assume that `uid`, `gid`, or `mode` is applied automatically. After startup, verify readability without displaying the hash:

```bash
docker compose exec mcp-server python -c "from pathlib import Path; from oauth_config import validate_password_hash; validate_password_hash(Path('/run/secrets/oauth_password_hash').read_text().strip()); print('Secret is readable and valid')"
docker compose exec mcp-server python oauth_admin.py verify-password
```

There is one private user. The username and password hash come from configuration or secrets; no additional user management is needed. Missing OAuth configuration prevents startup so that the external listener cannot accidentally run without protection.

| Variable | Meaning / default |
| --- | --- |
| `MCP_PUBLIC_BASE_URL` | Public HTTPS origin; required for direct Python startup |
| `MCP_OAUTH_USERNAME` | Private login username; required |
| `MCP_OAUTH_PASSWORD_HASH_FILE` | Argon2id hash file; configured by Compose |
| `MCP_OAUTH_PASSWORD_HASH` | Alternative to the hash file for direct startup |
| `MCP_AUTH_TOKEN` | Existing static bearer token; retain it for existing clients |
| `MCP_ALLOW_LEGACY_TOKEN` | `true` (default) or `false`; disables legacy access without deleting the token |
| `MCP_AUTH_TOKEN_FILE` | Alternative secret file for direct startup |
| `MCP_OAUTH_USERNAME_FILE` | Alternative secret file for the username |
| `MCP_OAUTH_DATABASE` | Docker: `/data/oauth.sqlite3`; local: `data/oauth.sqlite3` |
| `MCP_LAN_BIND_IP` | Compose bind IP for port 8000; set your NAS LAN IP (generic default `192.168.1.50`) |
| `MCP_LAN_HOST` | Allowed LAN host for direct startup; set your NAS LAN IP (generic default `192.168.1.50`) |
| `MCP_ACCESS_TOKEN_SECONDS` | Access-token lifetime; default 3600 seconds / 1 hour |
| `MCP_REFRESH_TOKEN_SECONDS` | Absolute refresh-family lifetime; default 63072000 seconds / 730 days |
| `MCP_AUTH_CODE_SECONDS` | One-time code lifetime; default 120 seconds |

`*_FILE` takes precedence over the corresponding direct secret value. The static token has no automatic expiration; revoke it by changing or removing configuration and restarting. An empty token or `MCP_ALLOW_LEGACY_TOKEN=false` disables this access path. After migration, prefer OAuth and set the legacy switch to `false`. It affects only `/mcp` on the external listener, never login, token, or discovery endpoints. Port 8000 remains unauthenticated independently.

Direct Python startup does **not** automatically load `.env`. PowerShell example after creating the secret file:

```powershell
$env:MCP_PUBLIC_BASE_URL = "https://mcp.example.com"
$env:MCP_OAUTH_USERNAME = "owner"
$env:MCP_OAUTH_PASSWORD_HASH_FILE = "$PWD\secrets\oauth-password-hash.txt"
$env:MCP_LAN_HOST = "<NAS_LAN_IP>"
python server.py
```

Also provide `MCP_AUTH_TOKEN` in the process environment for existing Internet clients. Direct Python operation binds both ports to `0.0.0.0`; the firewall must restrict port 8001 to the local reverse proxy. Use the Docker deployment below for the NAS.

## Docker

```bash
docker compose config --quiet
docker compose up -d --build
```

`docker compose config --quiet` validates without displaying environment values. The normal detailed configuration output can disclose secrets.

Port mappings:

- `<NAS_LAN_IP>:8000:8000`: unauthenticated LAN access on the NAS LAN address.
- `127.0.0.1:8001:8001`: authenticated backend port on NAS loopback for the reverse proxy.

Both listeners bind to `0.0.0.0` inside the container. Do not use host networking, which would remove the port-mapping restrictions. The container runs without root privileges as UID/GID 10001. Ensure that existing `/data` volumes are writable by this user.

The named `oauth-data` volume stores SQLite, the RSA signing key, client metadata, authorization codes, refresh-token hashes, token families, and revocations. Normal restarts and rebuilds preserve these records. `docker compose down -v` deletes the volume and all OAuth connections. Protect backups of the volume and login secret; back up a live SQLite database with its backup API or after a clean shutdown. Signing keys and databases are sensitive runtime data and must not be committed.

The Docker healthcheck checks LAN `/healthz` (database available) and external `/mcp` (401 with a bearer challenge) every 30 seconds. It uses no token or login and prints no sensitive data. `/healthz` exists only on port 8000; it does not bypass `/mcp` authentication. The healthcheck marks the container healthy or unhealthy; `restart: unless-stopped` alone does not restart a merely unhealthy container.

```bash
docker inspect --format '{{.State.Health.Status}}' synology-mcp
docker compose exec mcp-server python healthcheck.py
```

## Synology Reverse Proxy

DSM 7: **Control Panel > Login Portal > Advanced > Reverse Proxy**. Configure your public hostname; `mcp.example.com` is an example:

| Field | Source / external | Destination / internal |
| --- | --- | --- |
| Protocol | HTTPS | HTTP |
| Hostname | `mcp.example.com` | `127.0.0.1` |
| Port | `443` | `8001` |
| Path / forwarding | Entire origin `/` | Preserve paths unchanged |

DSM versions without a separate path field route by hostname and port; do not restrict forwarding to `/mcp`. `/oauth/*` and `/.well-known/*` must also reach port 8001. Remove or update conflicting rules. Assign a valid TLS certificate for your public hostname; use HSTS if the domain is exclusively HTTPS.

The public rule must **no longer target port 8000**. LAN clients continue using `http://<NAS_LAN_IP>:8000/mcp`. OAuth does not require a second public domain.

Forward `Authorization`, `Accept`, `Content-Type`, `Mcp-Session-Id`, and `MCP-Protocol-Version` unchanged. Preserve the public `Host`; the backend also accepts `127.0.0.1:8001` / `localhost:8001`. Do not rewrite Location headers. Synology WebSocket presets are unnecessary for Streamable HTTP / SSE. Use sufficient HTTP/SSE read timeouts and disable SSE response buffering if available. Do not log Authorization headers, OAuth query strings, or form bodies in reverse-proxy / DSM logs; authorization codes appear in the browser callback. Uvicorn access logs are therefore disabled.

## Firewall

- Internet: expose only TCP 443 to the NAS.
- Do not forward router ports 8000 or 8001.
- Do not create a public reverse-proxy rule targeting unauthenticated port 8000.
- LAN: allow port 8000 only from trusted LAN subnets; restrict guest and VPN networks.
- Port 8001: NAS loopback / reverse proxy only; never expose it directly to LAN or Internet clients.

Check port bindings and DSM/router rules after deployment from a separate network. Docker port publication may interact with firewall rules differently from regular host processes; retain loopback and LAN-IP bindings.

Check the installed Docker Engine version on the NAS:

```bash
docker version --format '{{.Server.Version}}'
docker port synology-mcp
```

Port 8000 must bind to your NAS LAN IP and port 8001 to `127.0.0.1`, never `0.0.0.0` or `[::]`. Container Manager can change port settings when recreating a container; check after GUI changes. With Docker Engine **before 28.0.0**, other machines on the same Layer 2 network can reach localhost-published ports in some configurations. This is a documented [Docker limitation](https://docs.docker.com/engine/network/port-publishing/), independent of OAuth. Test port 8001 from a second LAN computer; if reachable, use a supported Engine version or additional effective network/firewall rules. The port still requires authentication.

## OAuth endpoints and security behavior

| Endpoint | Purpose |
| --- | --- |
| `GET /.well-known/oauth-protected-resource` | Protected Resource Metadata |
| `GET /.well-known/oauth-protected-resource/mcp` | Equivalent path-specific metadata |
| `GET /.well-known/oauth-authorization-server` | Authorization Server Metadata |
| `GET /oauth/authorize` | Validation, login, and explicit consent |
| `POST /oauth/authorize` | CSRF-protected login and consent |
| `POST /oauth/token` | Authorization code / refresh token |
| `POST /oauth/revoke` | Revoke the token family for this client |

Example issuer: `https://mcp.example.com`. The resource and JWT audience consistently use **`https://mcp.example.com/mcp`**, the concrete MCP endpoint. Metadata URLs come exclusively from `MCP_PUBLIC_BASE_URL`, never request or proxy headers.

Authlib 1.8 processes authorization-code and refresh grants and PKCE. PyJWT/cryptography sign access tokens with RS256 and verify signature, issuer, audience, and expiration; database checks also enforce issuance, revocation, and stored permissions. The persistent RSA key is generated on first startup. Opaque refresh tokens and codes are stored only as SHA-256 hashes. Authorization codes are short-lived and single-use; SQLite serializes concurrent token transactions.

The consent page offers a checked-by-default checkbox, "Verbindung ohne erneute Anmeldung automatisch erneuern". After explicitly confirming the page, checking it adds `offline_access` to the approved scopes and issues a refresh token; unchecking it removes `offline_access`, even when requested by the client, and issues only an access token. Original resource scopes remain unchanged. No manual authorization-URL editing is needed. This consent behavior applies to every authorization-code client accepted by the existing client-validation policy; it introduces no ChatGPT-specific consent logic or new client origins. A request containing only `offline_access` cannot be approved with the checkbox unchecked because that would leave no approved scope. Existing token families are unaffected. Refresh tokens are issued only with final consent to `offline_access`. Access tokens expire after one hour; a client using refresh tokens, including the tested ChatGPT flow, can renew them in the background. Each refresh rotates the token. The absolute family lifetime is 730 days and does not extend on refresh. A refresh may omit `resource`; the stored binding is retained. An explicitly supplied resource must match exactly, and scopes and audience cannot expand. Valid stored refresh tokens remain usable when the trusted ChatGPT metadata omits `refresh_token` from `grant_types`; no other grants are supported. Manual login should normally be needed only after family expiry, revocation, replay detection, data loss or an OAuth/client error. This depends on the client continuing to refresh; the server cannot guarantee client behavior. Existing families keep their stored expiry: after deployment, reconnect once to obtain a new 730-day family. Existing `.env` overrides remain effective and must be reviewed manually; they are never overwritten automatically. Reusing an old refresh token revokes the whole family, including access tokens. Reusing a code also revokes its issued tokens. `/oauth/revoke` revokes the corresponding family. Legacy static-token access is independent.

All tools require `mcp:read`: the seven original tools (`current_time`, `web_search`, `web_fetch`, `finance_news_candidates`, `youtube_metadata`, `youtube_transcript`, `youtube_comments`), the four public X tools described below, and `web_deep_search` / `web_search_and_fetch`. Annotations are `readOnlyHint=true`, `destructiveHint=false`, and `idempotentHint=true`. `mcp:write` is reserved; there are currently no write/delete tools. A token with only `mcp:write` cannot access the read tools. `offline_access` is an authorization-server scope, not a required resource scope.

## Public X tools

These tools use ordinary anonymous HTTP requests and free DuckDuckGo HTML search discovery. They do not require official X API access, paid providers, personal accounts, cookies, API keys or login. No internal X search or GraphQL endpoints are used. Returned source text is untrusted content and must not be interpreted as instructions.

| Tool | Parameters | Example |
| --- | --- | --- |
| `x_read_post` | `url`, `refresh=false` | `{"url":"https://x.com/example/status/123456789"}` |
| `x_read_thread` | `url`, `max_posts=20`, `include_replies=false`, `refresh=false` | `{"url":"https://x.com/example/status/123456789","max_posts":50}` |
| `x_search` | `query`, `limit=20`, `since`, `until`, `from_user`, `refresh=false` | `{"query":"Qwen3.8 Flash Next RTX 5090","limit":50}` |
| `x_search_user` | `username`, `query`, `limit=20`, `refresh=false` | `{"username":"example","query":"DGX Spark"}` |

The URLs and usernames above are illustrative, not live test fixtures. Example prompts:

- Read this X post fully: `https://x.com/example/status/123456789`.
- Read the entire thread connected to this post; identify any gaps.
- Find up to 50 readable public X posts about `Qwen3.8 Flash Next RTX 5090`.
- Find public posts by `username` about `DGX Spark`.

Limits and `max_posts` must be between 1 and 50. Usernames omit `@`. Search dates use `YYYY-MM-DD`; `since` is inclusive and `until` is exclusive in UTC. Posts with unknown dates are excluded when a date filter is requested, rather than silently treated as matches. Username filters are verified against parsed post metadata.

`x_public.XPublicProvider` separates tool registration from acquisition. `FreePublicProvider` implements the initial provider; `get_provider()` is the single composition point for a future optional provider or fallback. Existing tools, business functions and authentication are independent of this module. No new dependencies are required.

The reader strips tracking parameters and normalizes X/Twitter status URLs by post ID. It also supports `i/web/status` links, mobile hostnames, photo/video suffixes and public `t.co` redirects when they resolve to a supported status URL. It parses publicly delivered JSON-LD, embedded JSON post records, long-form note text and HTML description metadata. It never executes page scripts or downloads media files. Media links, quote metadata and conversation/reply IDs are returned only when present in public source data; unavailable fields are null or empty.

`complete`, `completeness` and `text_complete` describe the available **post text**, not proof that every metadata field exists. Meta-tag previews always report `complete=false` and `partial`. `metadata_complete` remains false because public pages do not prove that all media or relationships have been disclosed. A missing quote reports `quote_status=unknown`; page images are marked as previews rather than verified attachments. Results include provider, source, canonical URL and UTC fetch time. A readable partial post is returned with a `PARTIAL_CONTENT` warning; a page with no readable text returns a structured error.

Threads combine embedded records, public status links, explicit reply relationships and bounded web discovery. They retain connected same-author posts by default; `include_replies=true` permits connected other-author replies. Unrelated same-author posts are excluded. Posts are deduplicated by ID and sorted by timestamp when all timestamps are known, otherwise by chronological numeric post ID. Every thread reports `complete=false`: this provider cannot prove exhaustive coverage. The result includes fetch attempts, a stop reason, warnings and limit information. At most `max_posts` additional pages are fetched.

Search discovers status URLs with `site:x.com` and `site:twitter.com`, deduplicates them and reads each candidate through the same reader. Search snippets are never returned as verified post bodies. Up to twice the requested result limit (at most 100 candidates) are read. Unreadable candidates are skipped with error counts. Search always reports `complete=false`; indexed posts, ranking and requested keywords do not guarantee exhaustive or exact X-search semantics.

### Cache and network limits

Posts and search results are stored in a separate SQLite cache. This does not modify the OAuth database. Default local path: `data/x-public.sqlite3`; Docker uses `/data/x-public.sqlite3` in the existing persistent data volume. Git ignores the cache and SQLite sidecars.

```dotenv
MCP_X_CACHE_ENABLED=true
# Optional for local Python execution; Docker Compose uses /data/x-public.sqlite3.
MCP_X_CACHE_PATH=data/x-public.sqlite3
```

Set `MCP_X_CACHE_ENABLED=false` before starting the server to disable caching. Pass `refresh=true` to bypass the cache and replace a successful cached result. Complete post text is retained for 30 days, previews for one hour and searches for five minutes. Old post cache entries may remain readable until expiry even if a post is later deleted or protected; use refresh when current availability matters. The cache is bounded to 2,000 entries and contains public content, not authentication data.

Outbound fetches are restricted to explicit public X/Twitter, short-link and discovery hostnames. DNS answers must all be globally routable; connections are pinned to a validated address with hostname certificate verification. Every redirect is validated again. Ambient HTTP proxies, account cookies and credentials are not used. Requests have socket timeouts, a 25-second per-fetch deadline, at most five request hops, a 2 MiB response limit, two concurrent fetches and at least 0.5 seconds between fetch starts. Tool calls have a 90-second overall deadline; timed-out requests return a safe structured failure. DNS resolution and already-running worker cleanup depend on the OS; an overall timeout does not forcibly terminate a worker thread.

Failures use `{"ok":false,"error":{"code":"...","message":"..."}}`, with `UNSUPPORTED_URL`, `NOT_FOUND`, `PRIVATE_OR_PROTECTED`, `BLOCKED`, `RATE_LIMITED`, `PARTIAL_CONTENT`, `FETCH_FAILED` or `INVALID_ARGUMENT`. Transport exceptions and raw stack traces are never exposed to MCP clients.

### Known limitations and tests

Anonymous X pages often require JavaScript or a login; such posts can be unreadable even when visible in a logged-in browser. Captchas, protected posts and access restrictions are reported rather than bypassed. Search engines can also rate-limit or block anonymous discovery. HTML changes may require parser maintenance. No browser fallback is currently installed: Playwright is optional in the requested design, and this version avoids browser execution, downloads and added runtime dependencies. A future browser provider must retain sandboxing and the same outbound destination policy.

Run the full offline suite with `python -m pytest -q`. `tests/test_x_public.py` covers synthetic HTML/JSON fixtures, URL parsing, public DNS and redirect restrictions, response bounds, error handling, discovery, author/date filters, cache/refresh and thread relationships. Authentication tests check identical LAN/external tool registries and OAuth metadata on all eleven tools. The seven original schema snapshots remain unchanged. No live X access is required by the normal test suite.

Implementation validation on 2026-10-02: **172 tests passed**, including the original 106 and 66 new offline checks. Actual MCP tool calls were exercised on both listeners using a mocked public provider; live X availability and search-engine coverage were not verified by this result. No NAS deployment, commit or push was performed for this integration.

Subsequent live validation on 2026-10-02: **176 offline tests passed** after four live-derived regressions were added. Anonymous post reads returned partial SEO previews; full threads could not be resolved, and all tested searches hit an HTTP-202 challenge. This provider is **not ready for production full-post/thread/search use**. See the [detailed live report](docs/x-live-smoke-report.md), including cache behavior, tested sources and the distinction between partial results and working features. Thread results now include `posts_found` and `termination_reason`; errors explicitly report unavailable completeness. No commit, push or NAS deployment was performed.

Alternative free sources were subsequently evaluated without changing runtime code: Bing HTML/RSS, Google, Brave, Yahoo, Mojeek, official oEmbed/syndication/embed HTML and five Nitter preflights. No viable full reader/discovery/thread combination was confirmed. See the [provider matrix and Playwright assessment](docs/x-free-provider-evaluation.md). No browser dependency, account access or challenge bypass was introduced.

A subsequent [isolated Playwright PoC](docs/x-playwright-poc-report.md) found a 704-character long post, all six numbered thread posts and quote context in normal visible Chromium. Both tested headless variants failed, and anonymous public search yielded no results. This experiment uses a separate ignored environment and is not integrated into the MCP provider or production dependencies. The report includes resource measurements and why the current NAS integration is not recommended.

The later [Exa-first WebReader architecture and PoC](docs/web-browser-architecture.md) compares Exa, public HTTP and one shared generic/X BrowserReader. Headless reads work for the tested general pages, but Exa already provides useful content there. At that PoC stage, the Remote schema correction (`urls` for fetch and `objective` for search) was isolated and 201 offline tests passed. The subsequent production adapters below now correct that mapping, support Direct API and add two Web tools; the original Web signatures and runtime dependencies remain unchanged.

External tool descriptors declare `securitySchemes=[{"type":"oauth2","scopes":["mcp:read"]}]`; LAN descriptors declare `[{"type":"noauth"}]`. Both also contain `_meta["securitySchemes"]`. Installed Python SDK 2.2.0 has no matching decorator parameter and discards unknown fields in its `Tool` model. Its public context-middleware API returns `tools/list` as a wire dictionary, allowing these extensions without SDK patches or tool-schema changes. The listener marker comes from the server-side ASGI scope, never headers. [OpenAI documents the declaration and compatibility field](https://developers.openai.com/plugins/reference).

Login uses Argon2id, secure HttpOnly/SameSite cookies, CSRF binding to a five-minute single-use request, and explicit consent. There are no persistent browser logins. Global persistent rate limits allow 10 login attempts per minute, 60 authorization-page requests, and 120 token and revocation requests each. A failed login requires restarting OAuth. Limits do not rely on client-IP headers.

Missing or invalid tokens receive external HTTP 401 with:

```http
WWW-Authenticate: Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource", scope="mcp:read"
```

Invalid tokens add `error="invalid_token"`. Valid OAuth tokens without `mcp:read` receive 403 with `error="insufficient_scope"`. A valid configured static token can access all existing tools. Tokens in query strings are rejected and are never forwarded to Exa or YouTube.

For rejected identifiable `tools/call` requests, the same HTTP 401/403 response also includes an MCP tool error (`isError=true`) with `_meta["mcp/www_authenticate"]`, including resource-metadata URL, scope, error, and description. The HTTP challenge remains authoritative for transport clients; no anonymous tool executes and no second login flow starts. JSON request reading is bounded; malformed or large requests retain the HTTP authentication error. This follows [OpenAI tool-auth documentation](https://developers.openai.com/plugins/build/auth); verify actual ChatGPT UI behavior during the NAS connection test.

Login, OAuth error, and discovery responses include `no-store`, `no-cache`, `no-referrer`, `nosniff`, `X-Frame-Options: DENY`, restrictive CSP, and disabled camera/microphone/geolocation permissions. COOP is not added to avoid interfering with popup/redirect communication.

SQLite uses WAL and a 5000 ms busy timeout. Code redemption, refresh rotation, and replay revocation remain atomic. Cleanup runs on new authorization requests; there are no new background services. Database, WAL, and SHM files have Linux mode 0600. There are no foreign-key relationships. New RSA keys have 3072 bits; persistent existing keys require at least 2048 bits. No JWKS endpoint is needed because the same resource server validates its issued tokens locally.

Existing mcp_dart protocol-header compatibility remains for LAN and legacy Internet token clients. OAuth clients use normal MCP protocol negotiation. The listeners have separate MCP sessions.

## Connect ChatGPT

Select your public MCP URL, represented here as `https://mcp.example.com/mcp`, with OAuth in ChatGPT's MCP/app dialog. Prefer CIMD; no manually entered client secret is needed. The server advertises `client_id_metadata_document_supported=true` and `token_endpoint_auth_methods_supported=["none"]`. It reads the current ChatGPT metadata document and chooses the supported intersection `none`; the singular legacy preference `private_key_jwt` does not prevent this compatible choice.

This private server permits CIMD URLs only at `https://chatgpt.com/oauth/client.json` or `https://chatgpt.com/oauth/<callback_id>/client.json`. Allowed redirects are `https://chatgpt.com/connector_platform_oauth_redirect` and `https://chatgpt.com/connector/oauth/<callback_id>`, additionally matched **exactly** against published client metadata. No other hosts/paths, wildcards, credentials, fragments, or query strings are allowed. HTTPS fetching has a timeout, a 64-KiB size limit, exact JSON content-type validation, `trust_env=False`, and no redirect following. The server provides no DCR, OIDC, or `private_key_jwt` endpoints. ChatGPT also supports DCR/static clients, but the selected CIMD configuration does not require them.

Expected flow:

1. ChatGPT requests `/mcp` without a token and receives 401 with discovery information.
2. ChatGPT reads Protected Resource Metadata and Authorization Server Metadata.
3. ChatGPT identifies itself through CIMD, currently `https://chatgpt.com/oauth/client.json`.
4. ChatGPT starts Authorization Code + PKCE S256 with `state` and resource `/mcp`.
5. Enter the username/password in the browser and allow access.
6. The server redirects to the exact callback with a one-time code, unchanged `state`, and `iss`.
7. ChatGPT exchanges the code/verifier for an access token and, with `offline_access`, a refresh token.
8. ChatGPT calls `/mcp` with its bearer token and renews access using the refresh token.

Published issuer metadata currently enables the stable callback `https://chatgpt.com/connector_platform_oauth_redirect`. The actual client metadata document is authoritative. LAN clients and existing static Internet token clients remain usable in parallel.

## curl tests

The server uses **Streamable HTTP with sessions and SSE**. A plain GET to `/mcp` without an MCP session is not a complete functional test and may return a transport error. Use POST `initialize` instead. These examples use Bash; on Windows use `curl.exe` and appropriate shell quoting. Replace `<NAS_LAN_IP>` and the example public hostname before running requests.

LAN without authentication: expect HTTP 200 and an MCP initialize response (SSE):

```bash
curl -i --max-time 10 'http://<NAS_LAN_IP>:8000/mcp' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl-test","version":"1"}}}'
```

Internet without authentication: expect HTTP 401 and `WWW-Authenticate`:

```bash
curl -i https://mcp.example.com/mcp
```

Discovery: expect HTTP 200 and public HTTPS URLs only:

```bash
curl -i https://mcp.example.com/.well-known/oauth-protected-resource
curl -i https://mcp.example.com/.well-known/oauth-authorization-server
```

Internet with an OAuth access token **or an existing static token**: expect HTTP 200 and an initialize response:

```bash
curl -i --max-time 10 https://mcp.example.com/mcp \
  -H 'Authorization: Bearer <TOKEN>' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl-test","version":"1"}}}'
```

For subsequent MCP calls, send the returned `Mcp-Session-Id` and `MCP-Protocol-Version: 2025-11-25`, and first send `notifications/initialized`. Tests automate this flow, including `tools/list` and `tools/call`.

## Tests and revocation

```bash
python -m pytest -q
```

Tests run both ASGI apps with their actual MCP lifecycle and cover the full login/token flow, legacy clients, manipulated headers, metadata, PKCE, single-use codes, JWT claims, scopes, refresh rotation/expiration, revocation, CSRF, persistence, and CIMD validation. A schema baseline from the previous server protects existing tool names and input/output schemas. Real network smoke tests check both HTTP listeners independently.

Hardening tests add listener-specific security metadata under concurrent requests, MCP authentication challenges, the legacy switch, complete password hashes and admin verification, startup failure before listeners open, healthcheck, SQLite key/rate-limit persistence, and official callback formats.

Administratively revoke all OAuth families:

```bash
docker compose exec mcp-server python oauth_admin.py revoke-all
```

This does not change the static `MCP_AUTH_TOKEN`. Disable it with `MCP_ALLOW_LEGACY_TOKEN=false` and recreate the container; for permanent revocation also change or remove the token.

## Final setup checklist

### One-time setup

1. Install Python dependencies, run `python oauth_admin.py hash-password`, and save only its hash to `secrets/oauth-password-hash.txt`. Alternatively use `hash-password --output secrets/oauth-password-hash.txt`. Check it with `verify-password`.
2. Transfer the file to the NAS and set the permissions for UID 10001 described above. Keep and extend your existing ignored `.env`; replace the example values with your own deployment settings:

```dotenv
MCP_PUBLIC_BASE_URL=https://mcp.example.com
MCP_LAN_BIND_IP=<NAS_LAN_IP>
MCP_OAUTH_USERNAME=owner
MCP_ALLOW_LEGACY_TOKEN=true
```

Initially retain the existing `MCP_AUTH_TOKEN`. Then run from the NAS project directory:

```bash
docker compose config --quiet
docker compose up -d --build
```

3. Route the entire public HTTPS origin on port 443 to `http://127.0.0.1:8001` through Synology Reverse Proxy.
4. Check port mappings, secret readability, healthcheck, and firewall. LAN remains at `http://<NAS_LAN_IP>:8000/mcp` without authentication.

### After deployment

```bash
curl -i --max-time 10 'http://<NAS_LAN_IP>:8000/mcp'
curl -i https://mcp.example.com/mcp
curl -sS https://mcp.example.com/.well-known/oauth-protected-resource
curl -sS https://mcp.example.com/.well-known/oauth-protected-resource/mcp
curl -sS https://mcp.example.com/.well-known/oauth-authorization-server
```

A plain LAN GET must not return an authentication challenge but may return an MCP session/Accept transport error. For a positive functional test, use the `initialize` POST above and expect HTTP 200. An external GET without a token must return 401. All discovery documents must advertise public HTTPS URLs and resource `/mcp`. Port 8001 must be inaccessible from another LAN computer. Then complete a real ChatGPT connection, login, and tool call; disable the legacy switch afterwards if desired.

## LAN

MCP URL: `http://<NAS_LAN_IP>:8000/mcp`
Authentication: **None**. Trusted local networks only.

## ChatGPT / Internet

MCP URL: `https://mcp.example.com/mcp`
Authentication: **OAuth 2.1**, plus the existing static bearer token for legacy clients.
Public Base URL: `https://mcp.example.com`
Protected Resource Metadata: `https://mcp.example.com/.well-known/oauth-protected-resource`
OAuth Metadata: `https://mcp.example.com/.well-known/oauth-authorization-server`

## Exa web search and contents

The production Web tools use separate Direct API and Remote MCP adapters.
`web_search(query, num_results=5)` and `web_fetch(url)` keep their string return
contracts. `web_deep_search(query)` adds explicit research with grounded output;
`web_search_and_fetch(query, num_results=10, fetch_top=5)` returns structured JSON
and prefers a single batch Contents request for the selected ranked URLs.

Exa settings use the same ignored `config.env` locally and in Docker Compose.
Copy the example only when `config.env` does not already exist:

```powershell
if (-not (Test-Path config.env)) { Copy-Item config.env.example config.env }
```

Enter your real key manually in `config.env`:

```dotenv
EXA_API_KEY=
EXA_PROVIDER=auto
EXA_QUOTA_COOLDOWN_SECONDS=3600
EXA_RATE_LIMIT_COOLDOWN_SECONDS=60
```

Never paste the key into chat or commit it. Both `config.env` and `.env` are
gitignored and excluded from the Docker build context. Keep existing MCP/OAuth
deployment settings in `.env` and existing password-hash secret files unchanged.
The non-Exa settings in the example are references, not replacements for them.

For local tests or smoke commands, load only the Exa settings into the current
PowerShell process, then start Python from that same shell:

```powershell
. .\tools\load-config-env.ps1
.\.venv\Scripts\python.exe -m pytest -q
```

Use simple `NAME=value` lines (optional matching outer quotes), without variable
interpolation or inline comments on Exa values. The loader never prints values,
changes persistent environment variables, or loads other MCP settings. Python
inherits the Exa variables from this shell; the server itself does not implicitly
load an env file. No `python-dotenv` dependency is required.

On Synology, place the same `config.env` next to `docker-compose.yml`, enter the
key there and use `docker compose up -d` or the existing deployment procedure.
Compose loads `./config.env` through the service's `env_file`; Exa settings are
not duplicated in its `environment` block. After changing values, recreate the
container with Compose rather than only restarting it. Configuration changes
do not deploy changed Python code; that still requires the existing build/deploy.

| Mode | Behavior |
| --- | --- |
| `auto` | Direct when a key exists; otherwise Remote MCP. Eligible Direct failures fall back to Remote MCP. |
| `direct` | Direct only; a missing key or failed request returns a classified error. |
| `remote_mcp` | Remote MCP only; the Direct key is not sent to it. |

Normal Direct search uses `POST /search`, `type=auto`. Deep explicitly uses the
same endpoint with `type=deep` and a text synthesis schema. The verified Remote
MCP currently has no Deep mode: Deep returns a clear error if Direct is absent,
unavailable or its circuit is open. It never silently substitutes normal search.

Direct Search accepts an optional `objective`; Remote Search requires it. The
adapter supplies source-ranking and evidence-selection instructions distinct
from the query. Contents sends `urls=[url]` for single reads and a URL array for
batches. The API supports up to 100 URLs; the combined tool fetches at most five.

Search is capped at 20 results, individual Contents text at 20,000 characters,
combined search/fetch text at 50,000 characters. Calls are bounded to 25 seconds,
Deep to 65 seconds, and the combined workflow to 90 seconds. Error classifications
include authentication, quota, rate limit, timeout, availability, invalid request
and unknown failure. In `auto`, eligible failures use Remote; invalid requests
and content-policy rejections do not. Quota and rate failures open separate
configurable cooldowns shared by Direct Search and Contents in each process.
They do not predict monthly credit reset dates.

Remote MCP is a limited technical fallback, **not guaranteed unlimited or free**.
If it also fails, tools report a structured error. An optional `HTTPReader`
interface is prepared, but no experimental local reader or SearXNG service is
enabled. Page crawl errors are handled independently from account quota errors.
Provider metadata includes timestamps and unknown completeness rather than a
claim of full extraction. Returned `costDollars` are retained as estimates with
`billing_exact=false`, not as account balance or exact billing usage.

**DO_NOT_INTEGRATE_BROWSER** for general Web tools: the prior PoC found adequate
Exa contents, about 1 GiB browser RAM, and no demonstrated general-content benefit.
X remains a separate possible future browser integration. No production browser
dependency was added. See [Exa implementation and verification](docs/exa-provider-verification.md)
and the historical [browser PoC report](docs/web-browser-architecture.md).

## Specifications and deployment limits

Sources checked on October 1, 2026:

- [MCP Authorization Specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [MCP Client Registration / CIMD](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration)
- [OpenAI MCP OAuth / CIMD and ChatGPT](https://developers.openai.com/plugins/build/auth)
- [Authlib Authorization Server](https://docs.authlib.org/en/latest/oauth2/authorization_server.html)

Local automated tests do not replace checking NAS router, firewall, and DSM configuration. Change the reverse-proxy target to 8001 and rebuild the container on the NAS. Then run LAN and Internet requests from their respective networks and complete the actual browser login in ChatGPT. Repository changes alone do not update a running NAS container. Keep all real deployment settings in ignored local files; committed documentation and test addresses are generic examples.
