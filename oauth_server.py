"""Authlib authorization-code/PKCE and refresh grants, adapted to Starlette.

CIMD is restricted to OpenAI's HTTPS metadata origin for this private server.
No DCR endpoint, implicit grant, password grant or bearer-token forwarding.
"""
import html
import json
import logging
import re
import secrets
import time
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit, parse_qsl, urlunsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from authlib.oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import ClientMixin, OAuth2Request, OAuth2Error
from authlib.oauth2.rfc6749.errors import InvalidRequestError, InvalidGrantError, InvalidScopeError
from authlib.oauth2.rfc6749.requests import BasicOAuth2Payload
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc7636 import CodeChallenge
import httpx
import jwt
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route

from oauth_store import Store, digest

SCOPES = ["mcp:read", "mcp:write", "offline_access"]
COOKIE = "__Host-mcp-consent"
SECURITY_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}
CONSENT_CSP = SECURITY_HEADERS["Content-Security-Policy"].replace(
    "form-action 'self'", "form-action 'self' https://chatgpt.com")


def valid_redirect(uri):
    # Only currently documented ChatGPT callback formats, then exact metadata match.
    return bool(isinstance(uri, str) and len(uri) <= 2048 and re.fullmatch(
        r"https://chatgpt\.com(?::443)?/(?:connector_platform_oauth_redirect|connector/oauth/[A-Za-z0-9_-]+)", uri))


class Client(ClientMixin):
    def __init__(self, client_id, metadata):
        self.client_id, self.metadata = client_id, metadata

    def get_client_id(self):
        return self.client_id

    def get_default_redirect_uri(self):
        return None  # Require an explicit exact redirect on every authorization.

    def check_redirect_uri(self, uri):
        return uri in self.metadata["redirect_uris"]

    def get_allowed_scope(self, scope):
        scopes = scope.split() if scope else ["mcp:read"]
        if not set(scopes) <= set(SCOPES):
            raise InvalidScopeError()
        return " ".join(scopes)

    def check_client_secret(self, secret):
        return False

    def check_endpoint_auth_method(self, method, endpoint):
        return method == "none"

    def check_response_type(self, response_type):
        return response_type == "code"

    def check_grant_type(self, grant_type):
        # A refresh still requires an issued, client-bound, unexpired credential.
        # Trusted CIMD documents may omit refresh_token despite using refresh.
        return (grant_type in {"authorization_code", "refresh_token"}
                and "authorization_code" in self.metadata.get("grant_types", ["authorization_code"]))


class Credential(SimpleNamespace):
    def get_redirect_uri(self):
        return self.redirect

    def get_scope(self):
        return self.scope

    def check_client(self, client):
        return self.client == client.client_id

    @property
    def code_challenge(self):
        return self.challenge

    @property
    def code_challenge_method(self):
        return "S256"


class Request(OAuth2Request):
    def __init__(self, method, uri, pairs, headers=None):
        super().__init__(method, uri, headers=headers)
        data = dict(pairs)
        self.payload = BasicOAuth2Payload(data)
        self._form = data
        # Retain duplicate parameters for Authlib's validation.
        self.payload._datalist = {}
        for key, value in pairs:
            self.payload._datalist.setdefault(key, []).append(value)
        if any(len(values) != 1 for values in self.payload.datalist.values()):
            raise InvalidRequestError("Duplicate request parameter")

    @property
    def form(self):
        return self._form

    @property
    def args(self):
        return self._form


class S256(CodeChallenge):
    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]
    DEFAULT_CODE_CHALLENGE_METHOD = "S256"

    def validate_code_challenge(self, grant, redirect_uri):
        data = grant.request.payload.data
        if data.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", data.get("code_challenge", "")):
            raise InvalidRequestError("PKCE S256 is required", redirect_uri=redirect_uri)
        return super().validate_code_challenge(grant, redirect_uri)


class CodeGrant(AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]

    def validate_authorization_request(self):
        redirect = super().validate_authorization_request()
        data = self.request.payload.data
        if not data.get("state") or len(data["state"]) > 2048:
            raise InvalidRequestError("state is required", redirect_uri=redirect)
        self.server.validate_resource(data, redirect)
        return redirect

    def save_authorization_code(self, code, request):
        data = request.payload.data
        self.server.store.execute(
            "INSERT INTO codes (hash,client,redirect,scope,challenge,resource,username,expires) VALUES (?,?,?,?,?,?,?,?)",
            (digest(code), request.client.client_id, data["redirect_uri"], request.scope,
             data["code_challenge"], self.server.config.resource, request.user,
             int(time.time()) + self.server.config.code_seconds))

    def query_authorization_code(self, code, client):
        row = self.server.store.one("SELECT * FROM codes WHERE hash=? AND client=?", (digest(code), client.client_id))
        if not row or row["expires"] <= time.time():
            return None
        if row["resource"] != self.server.config.resource:
            return None
        if row["used"]:
            self.server.store.execute("UPDATE tokens SET revoked=1 WHERE family=?", (row["hash"],))
            return None
        self.server.validate_resource(self.request.payload.data)
        return Credential(**dict(row))

    def delete_authorization_code(self, code):
        self.server.store.execute("UPDATE codes SET used=1 WHERE hash=?", (code.hash,))

    def authenticate_user(self, code):
        return code.username if code.username == self.server.config.username else None


class RefreshGrant(RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(self, token):
        row = self.server.store.one("SELECT * FROM tokens WHERE refresh_hash=?", (digest(token),))
        if not row or row["refresh_expires"] <= time.time():
            return None
        if row["resource"] != self.server.config.resource:
            return None
        if row["client"] != self.request.client.client_id:
            return None
        if row["refresh_used"]:
            self.server.store.execute("UPDATE tokens SET revoked=1 WHERE family=?", (row["family"],))
            return None
        if row["revoked"]:
            return None
        data = self.request.payload.data
        if "resource" in data and data["resource"] != row["resource"]:
            raise InvalidRequestError("resource must match the stored MCP endpoint")
        return Credential(**dict(row))

    def authenticate_user(self, token):
        return token.username if token.username == self.server.config.username else None

    def revoke_old_credential(self, token):
        self.server.store.execute("UPDATE tokens SET refresh_used=1 WHERE jti=?", (token.jti,))


class OAuthService(AuthorizationServer):
    def __init__(self, config):
        super().__init__(scopes_supported=SCOPES)
        self.config = config
        self.store = Store(config.database)
        self.register_grant(CodeGrant, [S256(required=True)])
        self.register_grant(RefreshGrant)
        self.register_token_generator("default", self.generate_bearer)
        # Authlib's debug logging includes issued tokens; disable it explicitly.
        logging.getLogger("authlib.oauth2").setLevel(logging.WARNING)
        logging.getLogger("authlib.oauth2.rfc6749.grants.authorization_code").setLevel(logging.WARNING)
        logging.getLogger("authlib.oauth2.rfc6749.grants.refresh_token").setLevel(logging.WARNING)

    def query_client(self, client_id):
        if not isinstance(client_id, str) or not re.fullmatch(r"https://chatgpt\.com/oauth/(?:[A-Za-z0-9_-]+/)?client\.json", client_id):
            return None
        cached = self.store.one("SELECT * FROM clients WHERE id=?", (client_id,))
        if cached and cached["fetched"] + 3600 > time.time():
            return Client(client_id, json.loads(cached["metadata"]))
        try:
            # Exact trusted origin, no redirects, no ambient proxy credentials, bounded body.
            with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
                with client.stream("GET", client_id, headers={"Accept": "application/json"}) as response:
                    response.raise_for_status()
                    if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                        return None
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            return None
            metadata = json.loads(body)
            if metadata.get("client_id") != client_id:
                return None
            redirects = metadata.get("redirect_uris")
            if not isinstance(redirects, list) or not 1 <= len(redirects) <= 20 or not all(valid_redirect(u) for u in redirects):
                return None
            methods = metadata.get("token_endpoint_auth_methods_supported")
            if methods is None:
                methods = [metadata.get("token_endpoint_auth_method", "none")]
            if not isinstance(methods, list) or "none" not in methods:
                return None
            grants = metadata.get("grant_types", ["authorization_code"])
            response_types = metadata.get("response_types", ["code"])
            if (not isinstance(grants, list) or "authorization_code" not in grants
                    or any(not isinstance(grant, str) for grant in grants)
                    or not isinstance(response_types, list) or "code" not in response_types):
                return None
            self.store.cache_client(client_id, metadata)
            return Client(client_id, metadata)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None

    def create_oauth2_request(self, request):
        return request

    def send_signal(self, name, *args, **kwargs):
        pass

    def handle_response(self, status, body, headers):
        headers = dict(headers)
        if "Location" in headers:
            parts = urlsplit(headers["Location"])
            params = parse_qsl(parts.query, keep_blank_values=True)
            params.append(("iss", self.config.public_base_url))
            headers["Location"] = urlunsplit(parts._replace(query=urlencode(params)))
        headers.update(SECURITY_HEADERS)
        if "Location" in headers:
            # Chromium checks form-action across the POST redirect chain.
            headers["Content-Security-Policy"] = CONSENT_CSP
        if isinstance(body, dict):
            return JSONResponse(body, status_code=status, headers=headers)
        return Response(body, status_code=status, headers=headers)

    def validate_resource(self, data, redirect_uri=None):
        if data.get("resource") != self.config.resource:
            raise InvalidRequestError("resource must match the public MCP endpoint", redirect_uri=redirect_uri)

    def generate_bearer(self, grant_type, client, user=None, scope=None, expires_in=None, include_refresh_token=True):
        now = int(time.time())
        claims = {"iss": self.config.public_base_url, "aud": self.config.resource, "sub": user,
                  "client_id": client.client_id, "iat": now, "exp": now + self.config.access_seconds,
                  "jti": secrets.token_urlsafe(32), "scope": scope or ""}
        result = {"access_token": jwt.encode(claims, self.store.private_key, algorithm="RS256",
                                             headers={"typ": "at+jwt"}),
                  "token_type": "Bearer", "expires_in": self.config.access_seconds, "scope": scope or ""}
        if include_refresh_token and "offline_access" in (scope or "").split():
            result["refresh_token"] = secrets.token_urlsafe(48)
        return result

    def save_token(self, token, request):
        claims = jwt.decode(token["access_token"], self.store.public_key, algorithms=["RS256"],
                            audience=self.config.resource, issuer=self.config.public_base_url)
        old = request.refresh_token
        family = old.family if old else request.authorization_code.hash
        refresh_expires = old.refresh_expires if old else int(time.time()) + self.config.refresh_seconds
        self.store.execute(
            "INSERT INTO tokens (jti,client,username,scope,resource,expires,refresh_hash,refresh_expires,family) VALUES (?,?,?,?,?,?,?,?,?)",
            (claims["jti"], request.client.client_id, request.user, token.get("scope", ""),
             self.config.resource, claims["exp"], digest(token["refresh_token"]) if token.get("refresh_token") else None,
             refresh_expires if token.get("refresh_token") else 0, family))

    def verify_access(self, token):
        claims = jwt.decode(token, self.store.public_key, algorithms=["RS256"],
                            audience=self.config.resource, issuer=self.config.public_base_url,
                            options={"require": ["iss", "aud", "exp", "iat", "sub", "jti", "scope", "client_id"]})
        row = self.store.one("SELECT * FROM tokens WHERE jti=?", (claims["jti"],))
        if (not row or row["revoked"] or row["username"] != self.config.username
                or claims["sub"] != row["username"] or claims["client_id"] != row["client"]
                or claims["scope"] != row["scope"] or not set(claims["scope"].split()) <= set(SCOPES)):
            raise jwt.InvalidTokenError("Unrecognized or revoked access token")
        return claims

    def oauth_request(self, method, path, pairs, headers=None):
        # Internal HTTP is intentional. Never trust Host or forwarded headers to create OAuth URLs.
        return Request(method, self.config.public_base_url + path, pairs, headers)

    def error(self, code, status=400, description=None):
        body = {"error": code}
        if description is not None:
            body["error_description"] = description
        return JSONResponse(body, status_code=status, headers=SECURITY_HEADERS)

    async def resource_metadata(self, request):
        return JSONResponse({"resource": self.config.resource,
                             "authorization_servers": [self.config.public_base_url],
                             "scopes_supported": ["mcp:read", "mcp:write"],
                             "bearer_methods_supported": ["header"]}, headers=SECURITY_HEADERS)

    async def server_metadata(self, request):
        base = self.config.public_base_url
        return JSONResponse({"issuer": base, "authorization_endpoint": base + "/oauth/authorize",
                             "token_endpoint": base + "/oauth/token", "revocation_endpoint": base + "/oauth/revoke",
                             "scopes_supported": SCOPES, "response_types_supported": ["code"],
                             "grant_types_supported": ["authorization_code", "refresh_token"],
                             "code_challenge_methods_supported": ["S256"],
                             "token_endpoint_auth_methods_supported": ["none"],
                             "revocation_endpoint_auth_methods_supported": ["none"],
                             "client_id_metadata_document_supported": True,
                             "authorization_response_iss_parameter_supported": True}, headers=SECURITY_HEADERS)

    def authorize_get_sync(self, pairs):
        if self.store.limited("authorize", 60):
            return self.error("temporarily_unavailable", 429)
        self.store.cleanup()
        try:
            req = self.oauth_request("GET", "/oauth/authorize", pairs)
            self.get_consent_grant(req)
        except OAuth2Error as error:
            if 'req' in locals():
                return self.handle_error_response(req, error)
            return self.error("invalid_request")
        request_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.store.execute("INSERT INTO pending VALUES (?,?,?,?)",
                           (request_id, json.dumps(pairs), digest(csrf), int(time.time()) + 300))
        scope = req.scope or "mcp:read"
        escape = html.escape
        labels = {"mcp:read": "MCP-Tools zum Lesen und Abrufen verwenden",
                  "mcp:write": "Schreibzugriff (derzeit keine Schreibtools vorhanden)",
                  "offline_access": "Verbindung ohne erneute Anmeldung automatisch erneuern"}
        permissions = "".join("<li>" + escape(labels[s]) + "</li>" for s in scope.split()
                              if s != "offline_access")
        page = f'''<!doctype html><html lang="de"><meta charset="utf-8"><title>MCP Anmeldung</title>
<h1>Synology MCP Server</h1><p>Ein MCP-Client möchte auf deinen privaten MCP-Server zugreifen.</p>
<p>Mit deiner Anmeldung erlaubst du folgende Berechtigungen:</p><ul>{permissions}</ul>
<form method="post" action="{escape(self.config.public_base_url)}/oauth/authorize">
<input type="hidden" name="request_id" value="{request_id}">
<input type="hidden" name="csrf" value="{csrf}">
<label>Benutzername <input name="username" autocomplete="username" required maxlength="200"></label>
<label>Passwort <input type="password" name="password" autocomplete="current-password" required maxlength="1024"></label>
<label><input type="checkbox" name="offline_access" value="1" checked>
Verbindung ohne erneute Anmeldung automatisch erneuern</label>
<p>Erlaubt die automatische Erneuerung der Verbindung über ein rotierendes Refresh-Token.</p>
<button name="decision" value="allow">Zugriff erlauben</button>
<button name="decision" value="deny" formnovalidate>Abbrechen</button></form></html>'''
        # Form POSTs need the real Origin for CSRF validation. Never disclose
        # the authorization URL's path or query in the Referer header.
        response = HTMLResponse(page, headers={**SECURITY_HEADERS,
            "Referrer-Policy": "strict-origin", "Content-Security-Policy": CONSENT_CSP})
        response.set_cookie(COOKIE, csrf, max_age=300, secure=True, httponly=True, samesite="lax", path="/")
        return response

    async def authorize(self, request):
        if request.method == "GET":
            return await run_in_threadpool(self.authorize_get_sync, list(request.query_params.multi_items()))
        form = await self.read_form(request)
        if form is None:
            return self.error("invalid_request", description="consent_form_invalid")
        return await run_in_threadpool(self.authorize_post_sync, dict(form),
                                      request.cookies.get(COOKIE, ""), request.headers.get("origin"))

    def authorize_post_sync(self, form, cookie, origin):
        if origin != self.config.public_base_url:
            return self.error("invalid_request", 403, "consent_origin_mismatch")
        if not cookie:
            return self.error("invalid_request", 403, "consent_cookie_missing")
        if not secrets.compare_digest(cookie.encode(), form.get("csrf", "").encode()):
            return self.error("invalid_request", 403, "consent_form_cookie_mismatch")
        if form.get("decision") not in ("allow", "deny"):
            return self.error("invalid_request", 403, "consent_decision_missing_or_invalid")
        if self.store.limited("login", 10):
            return self.error("temporarily_unavailable", 429)
        with self.store.transaction():
            pending = self.store.one("SELECT * FROM pending WHERE id=?", (form.get("request_id", ""),))
            if not pending:
                return self.error("invalid_request", 403, "consent_request_missing_or_already_used")
            if pending["expires"] <= time.time():
                return self.error("invalid_request", 403, "consent_request_expired")
            if not secrets.compare_digest(pending["csrf_hash"], digest(cookie)):
                return self.error("invalid_request", 403, "consent_request_cookie_mismatch")
            req = self.oauth_request("GET", "/oauth/authorize", json.loads(pending["params"]))
            # A failed login consumes the pending request; retry starts a fresh flow.
            self.store.execute("DELETE FROM pending WHERE id=?", (pending["id"],))
            if form.get("decision") == "deny":
                user = None
            else:
                try:
                    valid = PasswordHasher().verify(self.config.password_hash, form.get("password", ""))
                except (VerificationError, InvalidHashError):
                    valid = False
                if not valid or not secrets.compare_digest(form.get("username", "").encode(), self.config.username.encode()):
                    return self.error("access_denied", 403)
                user = self.config.username
            try:
                # Revalidate the stored original request before applying consent.
                # POST fields cannot replace resource scopes or protocol bindings.
                grant = self.get_consent_grant(req)
                if user is not None:
                    if form.get("offline_access") not in (None, "1"):
                        raise InvalidRequestError("Invalid offline access consent")
                    scopes = list(dict.fromkeys((req.scope or "mcp:read").split()))
                    scopes = [scope for scope in scopes if scope != "offline_access"]
                    if form.get("offline_access") == "1":
                        scopes.append("offline_access")
                    if not scopes:
                        raise InvalidScopeError("At least one scope must be approved")
                    pairs = [(key, value) for key, value in json.loads(pending["params"])
                             if key != "scope"]
                    pairs.append(("scope", " ".join(scopes)))
                    req = self.oauth_request("GET", "/oauth/authorize", pairs)
                    grant = self.get_consent_grant(req)
                response = self.create_authorization_response(req, grant_user=user, grant=grant)
            except OAuth2Error as error:
                response = self.handle_error_response(req, error)
            response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
            return response

    async def read_form(self, request):
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            return None
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return None
        try:
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=30)
            return pairs if len(dict(pairs)) == len(pairs) else None
        except (UnicodeError, ValueError):
            return None

    async def token(self, request):
        pairs = await self.read_form(request)
        if pairs is None or request.query_params:
            return self.error("invalid_request")
        return await run_in_threadpool(self.token_sync, pairs, dict(request.headers))

    def token_sync(self, pairs, headers):
        if self.store.limited("token", 120):
            return self.error("temporarily_unavailable", 429)
        try:
            req = self.oauth_request("POST", "/oauth/token", pairs, headers)
            # Serialize validation, issuance, code consumption and refresh rotation across processes.
            with self.store.transaction():
                return self.create_token_response(req)
        except OAuth2Error as error:
            return self.handle_error_response(req, error) if 'req' in locals() else self.error("invalid_request")

    async def revoke(self, request):
        pairs = await self.read_form(request)
        if pairs is None:
            return self.error("invalid_request")
        return await run_in_threadpool(self.revoke_sync, dict(pairs))

    def revoke_sync(self, form):
        if self.store.limited("revoke", 120):
            return self.error("temporarily_unavailable", 429)
        if not self.query_client(form.get("client_id")):
            return self.error("invalid_client", 401)
        token = form.get("token", "")
        if not token:
            return self.error("invalid_request")
        with self.store.transaction():
            row = self.store.one("SELECT * FROM tokens WHERE refresh_hash=? AND client=?",
                                 (digest(token), form["client_id"]))
            if not row:
                try:
                    claims = jwt.decode(token, self.store.public_key, algorithms=["RS256"],
                                        audience=self.config.resource, issuer=self.config.public_base_url)
                    row = self.store.one("SELECT * FROM tokens WHERE jti=? AND client=?",
                                         (claims["jti"], form["client_id"]))
                except (jwt.InvalidTokenError, KeyError):
                    pass
            if row:
                self.store.execute("UPDATE tokens SET revoked=1 WHERE family=?", (row["family"],))
        return Response(status_code=200, headers=SECURITY_HEADERS)

    def routes(self):
        return [Route("/.well-known/oauth-protected-resource", self.resource_metadata),
                Route("/.well-known/oauth-protected-resource/mcp", self.resource_metadata),
                Route("/.well-known/oauth-authorization-server", self.server_metadata),
                Route("/oauth/authorize", self.authorize, methods=["GET", "POST"]),
                Route("/oauth/token", self.token, methods=["POST"]),
                Route("/oauth/revoke", self.revoke, methods=["POST"])]
