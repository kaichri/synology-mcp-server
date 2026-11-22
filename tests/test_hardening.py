import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import sys

from argon2 import PasswordHasher
import httpx
import pytest
from starlette.testclient import TestClient

import listeners
import oauth_admin
from oauth_config import OAuthConfig, validate_password_hash
from oauth_server import OAuthService, valid_redirect
from oauth_store import Store
from server import mcp
from test_oauth import apps, config, password_hash, BASE, CLIENT, META, PASSWORD, initialize, tokens, tools_list, auth_params


def test_security_metadata_is_request_local(apps):
    lan, public, _ = apps
    token = tokens(public)["access_token"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        lan_future = pool.submit(tools_list, lan)
        public_future = pool.submit(tools_list, public, token)
        lan_tools, _ = lan_future.result()
        public_tools, _ = public_future.result()
    assert len(lan_tools) == len(public_tools) == 12
    for tool in public_tools:
        assert tool["securitySchemes"] == tool["_meta"]["securitySchemes"] == [{"type": "oauth2", "scopes": ["mcp:read"]}]
    for tool in lan_tools:
        assert tool["securitySchemes"] == tool["_meta"]["securitySchemes"] == [{"type": "noauth"}]


@pytest.mark.parametrize("kind", ["missing", "invalid", "scope"])
def test_tool_auth_challenge_keeps_transport_status(apps, kind):
    public = apps[1]
    headers = {"Accept": "application/json, text/event-stream"}
    if kind == "invalid":
        headers["Authorization"] = "Bearer test-invalid-token"
    elif kind == "scope":
        headers["Authorization"] = "Bearer " + tokens(public, scope="mcp:write")["access_token"]
    response = public.post("/mcp", json={"jsonrpc": "2.0", "id": 44, "method": "tools/call",
                                        "params": {"name": "current_time", "arguments": {}}}, headers=headers)
    assert response.status_code == (403 if kind == "scope" else 401)
    body = response.json()
    assert body["id"] == 44 and body["jsonrpc"] == "2.0" and body["result"]["isError"] is True
    challenge = body["result"]["_meta"]["mcp/www_authenticate"][0]
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource"' in challenge
    assert 'scope="mcp:read"' in challenge and 'error_description="' in challenge
    assert ('error="insufficient_scope"' if kind == "scope" else 'error="invalid_token"') in challenge
    assert "test-invalid-token" not in response.text
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource"' in response.headers["www-authenticate"]


def test_legacy_switch_does_not_disable_oauth_or_lan(config):
    lan_app, public_app, oauth = listeners.create_apps(mcp, replace(config, allow_legacy_token=False))
    oauth.store.cache_client(CLIENT, META)
    with TestClient(lan_app, base_url="http://192.168.1.50:8000") as lan, TestClient(public_app, base_url=BASE) as public:
        assert initialize(public, config.legacy_token).status_code == 401
        assert initialize(lan).status_code == 200
        assert initialize(public, tokens(public)["access_token"]).status_code == 200
        assert public.get("/.well-known/oauth-authorization-server", headers={"Authorization": "Bearer " + config.legacy_token}).status_code == 200
        # A legacy bearer cannot replace the OAuth code/PKCE/client validation.
        response = public.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": CLIENT,
                                                    "code": "invalid", "resource": BASE + "/mcp"},
                               headers={"Authorization": "Bearer " + config.legacy_token})
        assert response.status_code in (400, 401)
    oauth.store.close()


def set_environment(monkeypatch, config):
    values = {"MCP_PUBLIC_BASE_URL": BASE, "MCP_OAUTH_USERNAME": "owner",
              "MCP_OAUTH_PASSWORD_HASH": config.password_hash, "MCP_OAUTH_DATABASE": config.database,
              "MCP_ACCESS_TOKEN_SECONDS": "900", "MCP_REFRESH_TOKEN_SECONDS": "2592000", "MCP_AUTH_CODE_SECONDS": "120"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
        monkeypatch.delenv(name + "_FILE", raising=False)


def test_legacy_env_switch(config, monkeypatch):
    set_environment(monkeypatch, config)
    monkeypatch.delenv("MCP_ALLOW_LEGACY_TOKEN", raising=False)
    assert OAuthConfig.from_env().allow_legacy_token is True
    monkeypatch.setenv("MCP_ALLOW_LEGACY_TOKEN", "false")
    assert OAuthConfig.from_env().allow_legacy_token is False
    monkeypatch.setenv("MCP_ALLOW_LEGACY_TOKEN", "maybe")
    with pytest.raises(ValueError, match="MCP_ALLOW_LEGACY_TOKEN"):
        OAuthConfig.from_env()


@pytest.mark.parametrize("changes", [
    {"MCP_PUBLIC_BASE_URL": ""}, {"MCP_OAUTH_USERNAME": ""},
    {"MCP_OAUTH_PASSWORD_HASH": "$argon2id$broken"}, {"MCP_OAUTH_DATABASE": ":memory:"},
])
def test_fail_closed_before_listeners_start(config, monkeypatch, changes):
    set_environment(monkeypatch, config)
    for name, value in changes.items():
        monkeypatch.setenv(name, value)
    calls = []
    monkeypatch.setattr(listeners.uvicorn, "Config", lambda *a, **kw: calls.append(kw))
    with pytest.raises(ValueError):
        asyncio.run(listeners.serve(mcp))
    assert calls == []


def test_unwritable_database_fails_before_listeners(config, monkeypatch, tmp_path):
    set_environment(monkeypatch, config)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    monkeypatch.setenv("MCP_OAUTH_DATABASE", str(blocked / "oauth.sqlite3"))
    calls = []
    monkeypatch.setattr(listeners.uvicorn, "Config", lambda *a, **kw: calls.append(kw))
    with pytest.raises(OSError):
        asyncio.run(listeners.serve(mcp))
    assert calls == []


def test_complete_hash_and_weak_parameters_rejected(config):
    assert validate_password_hash(config.password_hash) == config.password_hash
    for value in [config.password_hash + "\nextra-content", "$argon2id$broken", config.password_hash.replace("$argon2id$", "$argon2i$")]:
        with pytest.raises(ValueError):
            validate_password_hash(value)
    weak = PasswordHasher(time_cost=1, memory_cost=8192).hash(PASSWORD)
    with pytest.raises(ValueError):
        replace(config, password_hash=weak)


def test_hash_password_file_and_verify_command(tmp_path, monkeypatch, capsys):
    filename = tmp_path / "secrets" / "oauth-password-hash.txt"
    passwords = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(oauth_admin.getpass, "getpass", lambda prompt: next(passwords))
    monkeypatch.setattr(sys, "argv", ["oauth_admin.py", "hash-password", "--output", str(filename)])
    oauth_admin.main()
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err and "$argon2id$" not in output.out
    content = filename.read_text()
    assert len(content.splitlines()) == 1 and content.endswith("\n")
    assert PasswordHasher().verify(validate_password_hash(content.strip()), PASSWORD)
    monkeypatch.setattr(sys, "argv", ["oauth_admin.py", "verify-password", "--hash-file", str(filename)])
    monkeypatch.setattr(oauth_admin.getpass, "getpass", lambda prompt: PASSWORD)
    oauth_admin.main()
    assert capsys.readouterr().out == "Password verified.\n"
    monkeypatch.setattr(oauth_admin.getpass, "getpass", lambda prompt: "incorrect-password")
    with pytest.raises(SystemExit) as error:
        oauth_admin.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "incorrect-password" not in output.out + output.err and content.strip() not in output.out + output.err


def test_verify_password_rejects_corrupt_file(tmp_path, monkeypatch, capsys):
    filename = tmp_path / "hash.txt"
    filename.write_text("$argon2id$corrupt\nextra")
    monkeypatch.setattr(sys, "argv", ["oauth_admin.py", "verify-password", "--hash-file", str(filename)])
    with pytest.raises(SystemExit) as error:
        oauth_admin.main()
    assert error.value.code == 2
    assert "$argon2id$corrupt" not in capsys.readouterr().err


def test_health_endpoint_is_lan_only_and_reports_db_failure(apps):
    lan, public, oauth = apps
    assert lan.get("/healthz").json() == {"status": "ok"}
    assert public.get("/healthz").status_code == 404
    oauth.store.close()
    response = lan.get("/healthz")
    assert response.status_code == 503 and response.json() == {"status": "unavailable"}
    assert "sqlite" not in response.text


def test_sqlite_key_and_rate_limit_persist(config):
    store = Store(config.database)
    assert store.one("PRAGMA journal_mode")[0] == "wal"
    assert store.one("PRAGMA busy_timeout")[0] == 5000
    assert store.private_key.key_size == 3072
    key = store.public_key.public_numbers()
    assert not store.limited("test-persisted", 1)
    store.close()
    reopened = Store(config.database)
    assert reopened.public_key.public_numbers() == key
    assert reopened.limited("test-persisted", 1)
    if os.name != "nt":
        for suffix in ("", "-wal", "-shm"):
            path = Path(config.database + suffix)
            if path.exists():
                assert path.stat().st_mode & 0o777 == 0o600
    reopened.close()


@pytest.mark.parametrize("uri", ["https://chatgpt.com/anything", "https://chatgpt.com:0443/connector_platform_oauth_redirect",
                                "https://chatgpt.com:444/connector_platform_oauth_redirect",
                                "https://u:p@chatgpt.com/connector_platform_oauth_redirect",
                                "https://chatgpt.com/connector_platform_oauth_redirect#fragment",
                                "https://chatgpt.com/connector_platform_oauth_redirect?target=evil"])
def test_redirect_format_is_restricted(uri):
    assert not valid_redirect(uri)


def test_callback_specific_cimd_and_redirect(config, monkeypatch):
    client_id = "https://chatgpt.com/oauth/test_callback-id/client.json"
    redirect = "https://chatgpt.com/connector/oauth/test_callback-id"
    real_client = httpx.Client
    metadata = dict(META, client_id=client_id, redirect_uris=[redirect])
    monkeypatch.setattr("oauth_server.httpx.Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=metadata)), **kwargs))
    oauth = OAuthService(config)
    client = oauth.query_client(client_id)
    assert client and client.check_redirect_uri(redirect) and not client.check_redirect_uri(redirect + "-other")
    oauth.store.close()


@pytest.mark.parametrize("mime", ["text/x-application/json", "text/html", ""])
def test_cimd_exact_json_content_type(config, monkeypatch, mime):
    real_client = httpx.Client
    monkeypatch.setattr("oauth_server.httpx.Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=META, headers={"Content-Type": mime})), **kwargs))
    oauth = OAuthService(config)
    assert oauth.query_client(CLIENT) is None
    oauth.store.close()


def test_oauth_security_headers_and_login_ux(apps):
    public = apps[1]
    responses = [public.get("/oauth/authorize", params=auth_params()),
                 public.get("/.well-known/oauth-protected-resource"),
                 public.get("/.well-known/oauth-protected-resource/mcp"),
                 public.get("/.well-known/oauth-authorization-server"),
                 public.post("/oauth/token", data={"grant_type": "unsupported"})]
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        expected_policy = "strict-origin" if response is responses[0] else "no-referrer"
        assert response.headers["referrer-policy"] == expected_policy
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    page = responses[0].text
    assert "Synology MCP Server" in page and "Ein MCP-Client möchte" in page
    assert "Zugriff erlauben" in page and "Abbrechen" in page
    assert "automatisch erneuern" in page and "Lesen und Abrufen" in page
    assert CLIENT not in page and "<script" not in page
