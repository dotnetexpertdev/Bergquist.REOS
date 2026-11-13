"""Item 2: OAuth callback URLs are consistent across routes, authorize URLs and token exchange."""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.auth import make_oauth_state
from app.config import settings
from app.database import async_session_maker
from app.main import app
from app.models import ProviderConnection
from app.providers import gmail_provider, quickbooks_provider

BACKEND = Path(__file__).resolve().parent.parent


def _route_paths():
    return {getattr(r, "path", None): getattr(r, "endpoint", None) for r in app.routes}


def test_registered_callback_routes_are_the_configured_ones():
    routes = _route_paths()
    assert settings.gmail_oauth_callback == "/api/gmail/callback"
    assert settings.quickbooks_oauth_callback == "/api/quickbooks/callback"
    assert routes["/api/gmail/callback"].__name__ == "gmail_callback"
    assert routes["/api/quickbooks/callback"].__name__ == "quickbooks_callback"


def test_authorize_urls_use_exact_registered_redirect_uri():
    g = urlparse(gmail_provider.get_gmail_auth_url("s"))
    assert parse_qs(g.query)["redirect_uri"] == ["http://testserver/api/gmail/callback"]
    assert "gmail.readonly" in parse_qs(g.query)["scope"][0]
    q = urlparse(quickbooks_provider.get_quickbooks_auth_url("s"))
    assert parse_qs(q.query)["redirect_uri"] == ["http://testserver/api/quickbooks/callback"]
    assert parse_qs(q.query)["scope"] == ["com.intuit.quickbooks.accounting"]


@pytest.mark.parametrize("path,param", [("/api/gmail/callback", "gmail_error"),
                                        ("/api/quickbooks/callback", "qb_error")])
async def test_callback_reaches_handler_and_rejects_bad_state(client, path, param):
    r = await client.get(path, params={"code": "abc", "state": "forged", "realmId": "TEST-REALM"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/settings?{param}=invalid_or_expired_state"


def _capture_transport(captured, json_body):
    def handler(request: httpx.Request):
        captured.append(request)
        return httpx.Response(200, json=json_body)
    return httpx.MockTransport(handler)


async def test_gmail_callback_exchanges_code_with_same_redirect_uri(client, monkeypatch):
    captured = []
    real = httpx.AsyncClient
    transport = _capture_transport(captured, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                                              "email": "owner@example.test"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(transport=transport))
    r = await client.get("/api/gmail/callback", params={"code": "the-code", "state": make_oauth_state("gmail", "owner")})
    assert r.status_code == 303 and r.headers["location"] == "/settings?gmail_connected=true"
    token_req = captured[0]
    assert str(token_req.url) == "https://oauth2.googleapis.com/token"
    form = parse_qs(token_req.content.decode())
    assert form["redirect_uri"] == ["http://testserver/api/gmail/callback"]
    assert form["code"] == ["the-code"]
    async with async_session_maker() as db:
        conn = (await db.execute(select(ProviderConnection).where(ProviderConnection.provider == "gmail"))).scalar_one()
        assert conn.is_valid and conn.connected_account == "owner@example.test"
        assert conn.access_token_encrypted and "at" != conn.access_token_encrypted  # encrypted at rest


async def test_quickbooks_callback_exchanges_code_and_checks_company(client, monkeypatch):
    captured = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request):
        captured.append(request)
        if "tokens/bearer" in str(request.url):
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
        return httpx.Response(200, json={"CompanyInfo": {"CompanyName": "Sandbox Test Company"}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    state = make_oauth_state("quickbooks", "owner")
    wrong = await client.get("/api/quickbooks/callback", params={"code": "c", "state": state, "realmId": "OTHER"})
    assert wrong.status_code == 303 and "qb_error=wrong_company" in wrong.headers["location"]
    assert captured == []  # wrong company: no token exchange at all

    ok = await client.get("/api/quickbooks/callback", params={"code": "c", "state": state, "realmId": "TEST-REALM"})
    assert ok.headers["location"] == "/settings?qb_connected=true"
    form = parse_qs(captured[0].content.decode())
    assert form["redirect_uri"] == ["http://testserver/api/quickbooks/callback"]
    async with async_session_maker() as db:
        conn = (await db.execute(select(ProviderConnection).where(ProviderConnection.provider == "quickbooks"))).scalar_one()
        assert conn.realm_id == "TEST-REALM" and conn.company_name == "Sandbox Test Company"


def test_legacy_configured_callback_paths_reach_handlers():
    """An older .env with /api/auth/... callbacks still works end to end."""
    code = r"""
from fastapi.testclient import TestClient
from urllib.parse import urlparse, parse_qs
from app.main import app
from app.providers.gmail_provider import get_gmail_auth_url
from app.providers.quickbooks_provider import get_quickbooks_auth_url
with TestClient(app) as c:
    g = c.get('/api/auth/gmail/callback', params={'code': 'x', 'state': 'bad'}, follow_redirects=False)
    q = c.get('/api/auth/quickbooks/callback', params={'code': 'x', 'state': 'bad', 'realmId': 'r'}, follow_redirects=False)
    print(g.status_code, g.headers['location'])
    print(q.status_code, q.headers['location'])
    print(parse_qs(urlparse(get_gmail_auth_url('s')).query)['redirect_uri'][0])
    print(parse_qs(urlparse(get_quickbooks_auth_url('s')).query)['redirect_uri'][0])
"""
    env = dict(os.environ, GMAIL_OAUTH_CALLBACK="/api/auth/gmail/callback",
               QUICKBOOKS_OAUTH_CALLBACK="/api/auth/quickbooks/callback")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(BACKEND),
                         timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()[-4:]  # after the startup banner
    assert lines[0] == "303 /settings?gmail_error=invalid_or_expired_state"
    assert lines[1] == "303 /settings?qb_error=invalid_or_expired_state"
    assert lines[2] == "http://testserver/api/auth/gmail/callback"
    assert lines[3] == "http://testserver/api/auth/quickbooks/callback"


def test_full_url_in_callback_setting_is_rejected():
    from app.config import Settings
    with pytest.raises(Exception):
        Settings(gmail_oauth_callback="http://localhost:8000/api/gmail/callback")
