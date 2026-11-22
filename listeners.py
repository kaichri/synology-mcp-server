"""Two separate MCP applications sharing one tool registry and business logic."""
import asyncio
from contextlib import contextmanager
import logging
import json
import re
import secrets
import signal
import sqlite3
from urllib.parse import urlsplit

import jwt
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from oauth_config import OAuthConfig
from oauth_server import OAuthService


class ToolSecurityMetadata:
    """SDK context middleware returns wire dictionaries, including tool extensions."""
    async def __call__(self, ctx, call_next):
        result = await call_next(ctx)
        if ctx.method == "tools/list" and isinstance(result, dict) and "tools" in result:
            external = ctx.request is not None and ctx.request.scope.get("mcp_external_oauth") is not None
            schemes = [{"type": "oauth2", "scopes": ["mcp:read"]}] if external else [{"type": "noauth"}]
            return {**result, "tools": [
                {**tool, "securitySchemes": schemes,
                 "_meta": {**tool.get("_meta", {}), "securitySchemes": schemes}}
                for tool in result["tools"]]}
        return result


def auth_challenge(config, error=None, description=None):
    value = ('Bearer resource_metadata="' + config.public_base_url
             + '/.well-known/oauth-protected-resource", scope="mcp:read"')
    if error:
        value += f', error="{error}"'
    if description:
        value += f', error_description="{description}"'
    return value


PROTOCOL_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clamp_protocol_headers(headers):
    clamped = []
    for key, value in headers:
        if key == b"mcp-protocol-version":
            version = value.decode("latin-1").strip()
            if PROTOCOL_VERSION_RE.fullmatch(version) and version > "2025-11-25":
                value = b"2025-11-25"
        clamped.append((key, value))
    return clamped


class ProtocolCompatibility:
    """Preserve the existing mcp_dart protocol-header compatibility on LAN only."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["headers"] = _clamp_protocol_headers(scope["headers"])
        await self.app(scope, receive, send)


class ExternalAuthorization:
    def __init__(self, app, oauth):
        self.app, self.oauth = app, oauth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            return await self.app(scope, receive, send)
        auth_headers = [value.decode("latin-1") for key, value in scope["headers"] if key == b"authorization"]
        valid, insufficient, error = False, False, None
        legacy_used = False
        if len(auth_headers) == 1:
            parts = auth_headers[0].split()
            if len(parts) == 2 and parts[0].lower() == "bearer" and len(parts[1]) <= 8192:
                token = parts[1]
                # Explicit legacy compatibility, only when a nonempty secret is configured.
                legacy = self.oauth.config.legacy_token
                if self.oauth.config.allow_legacy_token and legacy and secrets.compare_digest(token.encode(), legacy.encode()):
                    valid = True
                    legacy_used = True
                else:
                    try:
                        claims = await run_in_threadpool(self.oauth.verify_access, token)
                        insufficient = "mcp:read" not in claims["scope"].split()
                        valid = not insufficient
                    except (jwt.InvalidTokenError, ValueError, TypeError):
                        error = "invalid_token"
            else:
                error = "invalid_token"
        elif auth_headers:
            error = "invalid_token"
        if not valid:
            if insufficient:
                error = "insufficient_scope"
            challenge = auth_challenge(self.oauth.config, error)
            body = {"error": error or "unauthorized"}
            # Keep the transport 401/403. Attach the SDK's tool-error result only
            # for identifiable tools/call requests; never execute an unauthenticated RPC.
            message = await self.rejected_tool_call(scope, receive)
            if message is not None:
                description = "mcp:read scope is required" if insufficient else "Authenticate to access the private MCP server"
                tool_challenge = auth_challenge(self.oauth.config, error or "invalid_token", description)
                result = CallToolResult(content=[TextContent(type="text", text=description)],
                                        is_error=True, meta={"mcp/www_authenticate": [tool_challenge]})
                body = {"jsonrpc": "2.0", "id": message["id"],
                        "result": result.model_dump(by_alias=True, exclude_none=True)}
            response = JSONResponse(body,
                                    status_code=403 if insufficient else 401,
                                    headers={"WWW-Authenticate": challenge, "Cache-Control": "no-store"})
            return await response(scope, receive, send)
        scope = dict(scope)
        # Not a header: cannot be selected by the client or the reverse proxy.
        scope["mcp_external_oauth"] = self.oauth
        if legacy_used:
            # Existing mcp_dart clients also use the legacy token over the Internet.
            scope["headers"] = _clamp_protocol_headers(scope["headers"])
        await self.app(scope, receive, send)

    async def rejected_tool_call(self, scope, receive):
        headers = dict(scope["headers"])
        content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if scope["method"] != "POST" or content_type != b"application/json":
            return None
        try:
            async with asyncio.timeout(2):
                body = bytearray()
                while True:
                    event = await receive()
                    if event["type"] != "http.request":
                        return None
                    body.extend(event.get("body", b""))
                    if len(body) > 16384:
                        return None
                    if not event.get("more_body", False):
                        break
                message = json.loads(body)
                if (isinstance(message, dict) and message.get("jsonrpc") == "2.0"
                        and message.get("method") == "tools/call" and type(message.get("id")) in (str, int)):
                    return message
        except (TimeoutError, ValueError, UnicodeError):
            pass
        return None


def create_apps(mcp, config):
    oauth = OAuthService(config)
    if not any(isinstance(item, ToolSecurityMetadata) for item in mcp.middleware):
        mcp.middleware.append(ToolSecurityMetadata())
    public = urlsplit(config.public_base_url)
    public_host = public.netloc
    public_hosts = list(dict.fromkeys([
        public_host,
        public.hostname,
        f"{public.hostname}:443",
        "localhost:8001",
        "127.0.0.1:8001",
        "mcp-server:8001",
    ]))
    lan_security = TransportSecuritySettings(
        allowed_hosts=[config.lan_host, config.lan_host + ":8000", "localhost:8000", "127.0.0.1:8000"],
        allowed_origins=[f"http://{config.lan_host}:8000"])
    external_security = TransportSecuritySettings(
        allowed_hosts=public_hosts,
        allowed_origins=[config.public_base_url])
    # Separate session managers: an unauthenticated LAN session cannot cross into the public listener.
    lan = mcp.streamable_http_app(transport_security=lan_security, stateless_http=False, json_response=False)
    external = mcp.streamable_http_app(transport_security=external_security, stateless_http=False, json_response=False)
    lan.add_middleware(ProtocolCompatibility)
    async def health(request):
        try:
            row = await run_in_threadpool(oauth.store.one, "SELECT 1 FROM settings WHERE name='signing_key'")
            if row:
                return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
        except sqlite3.Error:
            pass
        return JSONResponse({"status": "unavailable"}, status_code=503, headers={"Cache-Control": "no-store"})
    # Only LAN app: the reverse proxy on 8001 does not expose this endpoint.
    lan.router.routes.append(Route("/healthz", health))
    external.router.routes.extend(oauth.routes())
    external.add_middleware(ExternalAuthorization, oauth=oauth)
    return lan, external, oauth


async def serve(mcp):
    config = OAuthConfig.from_env()
    lan, external, oauth = create_apps(mcp, config)
    # Inside Docker bind both interfaces; Compose exposes 8001 exclusively on host loopback.
    class ListenerServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            # One process owns both listeners; do not install competing handlers.
            yield

    servers = [ListenerServer(uvicorn.Config(app, host="0.0.0.0", port=port,
                                           proxy_headers=False, access_log=False, log_level="info",
                                           timeout_graceful_shutdown=15))
               for app, port in ((lan, 8000), (external, 8001))]
    def stop(signum, frame):
        for server in servers:
            if server.should_exit:
                server.force_exit = True
            server.should_exit = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    logging.getLogger(__name__).info("LAN MCP :8000; OAuth / legacy bearer MCP :8001")
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        try:
            for server in servers:
                server.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                from exa_provider import close_shared_clients
                await close_shared_clients()
            except Exception:
                pass
            oauth.store.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main(mcp):
    try:
        asyncio.run(serve(mcp))
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise SystemExit("MCP startup failed: " + str(exc)) from None
