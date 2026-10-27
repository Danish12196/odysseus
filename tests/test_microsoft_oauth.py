"""Microsoft Outlook OAuth: tenant, token storage, and the live callback route."""

import base64
import json
import time
import unittest.mock as mock
from types import SimpleNamespace

import pytest


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


def _make_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory(), factory


def _make_account(session, account_id="acct-1", owner="alice", **kwargs):
    from core.database import EmailAccount

    row = EmailAccount(
        id=account_id,
        owner=owner,
        name=kwargs.get("name", "Outlook"),
        from_address=kwargs.get("from_address", ""),
        imap_host=kwargs.get("imap_host", ""),
        imap_port=kwargs.get("imap_port", 993),
        imap_user=kwargs.get("imap_user", ""),
        smtp_host=kwargs.get("smtp_host", ""),
        smtp_port=kwargs.get("smtp_port", 587),
        smtp_user=kwargs.get("smtp_user", ""),
    )
    for key, value in kwargs.items():
        if hasattr(row, key):
            setattr(row, key, value)
    session.add(row)
    session.commit()
    return row


class _FakeRequest:
    def __init__(self, scheme="http", host="localhost:7011"):
        self.headers = {"host": host}
        self.url = SimpleNamespace(scheme=scheme)


def _endpoint(path):
    from routes.email_routes import setup_email_routes

    router = setup_email_routes()
    for route in router.routes:
        if route.path == path and "GET" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{path} not found")


def test_tenant_validation():
    from src.microsoft_oauth import MicrosoftOAuthError, normalize_tenant

    assert normalize_tenant("organizations") == "organizations"
    assert normalize_tenant("CONSUMERS") == "consumers"
    assert normalize_tenant("", default="consumers") == "consumers"
    guid = "11111111-2222-3333-4444-555555555555"
    assert normalize_tenant(guid) == guid
    with pytest.raises(MicrosoftOAuthError):
        normalize_tenant("../common")
    with pytest.raises(MicrosoftOAuthError):
        normalize_tenant("https://evil.example")


def test_authorize_url_uses_outlook_scopes_and_tenant():
    from src.microsoft_oauth import authorize_url

    url = authorize_url(
        tenant="organizations",
        client_id="client-1",
        redirect_uri="http://localhost:7011/api/email/oauth/microsoft/callback",
        state="signed-state",
    )
    assert url.startswith("https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?")
    assert "IMAP.AccessAsUser.All" in url
    assert "SMTP.Send" in url
    assert "offline_access" in url
    assert "Mail.Send" not in url
    assert "prompt=select_account" in url
    admin = authorize_url(
        tenant="organizations",
        client_id="client-1",
        redirect_uri="http://localhost:7011/api/email/oauth/microsoft/callback",
        state="signed-state",
        admin_consent=True,
    )
    assert "/adminconsent?" in admin
    assert "IMAP.AccessAsUser.All" not in admin


def test_oauth_state_carries_tenant():
    from routes.email_helpers import make_oauth_state, verify_oauth_state

    payload = verify_oauth_state(make_oauth_state("acct-1", "alice", extra={"t": "organizations"}))
    assert payload["t"] == "organizations"
    assert payload["a"] == "acct-1"


def test_refresh_encrypts_and_rotates(monkeypatch):
    from src.microsoft_oauth import refresh_access_token
    from src.secret_storage import decrypt, encrypt
    from core.database import EmailAccount

    db, factory = _make_db()
    _make_account(
        db,
        oauth_provider="microsoft",
        oauth_tenant="organizations",
        oauth_refresh_token=encrypt("refresh-old"),
    )
    db.close()
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-1")
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_SECRET", "secret-1")
    token_resp = mock.MagicMock()
    token_resp.raise_for_status = mock.MagicMock()
    token_resp.json.return_value = {
        "access_token": "access-new",
        "refresh_token": "refresh-new",
        "expires_in": 3600,
    }
    with mock.patch("httpx.post", return_value=token_resp) as posted, mock.patch("core.database.SessionLocal", factory):
        access = refresh_access_token("acct-1")
    assert access == "access-new"
    body = posted.call_args.kwargs["data"]
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "refresh-old"
    assert "access-new" not in json.dumps(body)
    verify = factory()
    row = verify.query(EmailAccount).filter(EmailAccount.id == "acct-1").first()
    assert decrypt(row.oauth_access_token) == "access-new"
    assert decrypt(row.oauth_refresh_token) == "refresh-new"
    assert row.oauth_access_token != "access-new"
    verify.close()


def test_two_accounts_refresh_independently(monkeypatch):
    from src.microsoft_oauth import refresh_access_token
    from src.secret_storage import decrypt, encrypt
    from core.database import EmailAccount

    db, factory = _make_db()
    _make_account(
        db,
        account_id="work",
        oauth_provider="microsoft",
        oauth_tenant="organizations",
        oauth_refresh_token=encrypt("work-refresh"),
    )
    _make_account(
        db,
        account_id="home",
        oauth_provider="microsoft",
        oauth_tenant="consumers",
        oauth_refresh_token=encrypt("home-refresh"),
    )
    db.close()
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-1")
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_SECRET", "secret-1")
    token_resp = mock.MagicMock()
    token_resp.raise_for_status = mock.MagicMock()
    token_resp.json.return_value = {"access_token": "work-access", "refresh_token": "work-refresh-2", "expires_in": 1800}
    with mock.patch("httpx.post", return_value=token_resp) as posted, mock.patch("core.database.SessionLocal", factory):
        assert refresh_access_token("work") == "work-access"
    assert posted.call_args.args[0].endswith("/organizations/oauth2/v2.0/token")
    verify = factory()
    work = verify.query(EmailAccount).filter(EmailAccount.id == "work").first()
    home = verify.query(EmailAccount).filter(EmailAccount.id == "home").first()
    assert decrypt(work.oauth_refresh_token) == "work-refresh-2"
    assert decrypt(home.oauth_refresh_token) == "home-refresh"
    assert home.oauth_access_token is None
    verify.close()


def test_imap_and_smtp_use_xoauth2_for_microsoft():
    from routes.email_helpers import _imap_connect, _send_smtp_message
    from src.secret_storage import encrypt

    future = str(int(time.time()) + 7200)
    cfg = {
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_starttls": False,
        "imap_user": "ada@contoso.com",
        "imap_password": "",
        "oauth_provider": "microsoft",
        "account_id": "acct-ms",
        "oauth_access_token": encrypt("ms-access"),
        "oauth_token_expiry": future,
        "owner": "alice",
    }
    conn = mock.MagicMock()
    with mock.patch("routes.email_helpers._open_imap_connection", return_value=conn), \
         mock.patch("routes.email_helpers._get_email_config", return_value=cfg):
        _imap_connect("acct-ms", owner="alice")
    assert conn.authenticate.call_args[0][0] == "XOAUTH2"
    conn.login.assert_not_called()

    smtp = mock.MagicMock()
    smtp_cfg = dict(cfg)
    smtp_cfg.update({
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "smtp_user": "ada@contoso.com",
        "smtp_password": "",
    })
    with mock.patch("routes.email_helpers._PolicySMTP", return_value=smtp):
        smtp.__enter__.return_value = smtp
        _send_smtp_message(smtp_cfg, "ada@contoso.com", ["bob@contoso.com"], "raw")
    smtp.starttls.assert_called_once()
    assert smtp.auth.call_args.args[0] == "XOAUTH2"


@pytest.mark.asyncio
async def test_authorize_route_redirects_to_microsoft(monkeypatch):
    db, factory = _make_db()
    _make_account(db, account_id="acct-ms", owner="alice")
    db.close()
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-1")
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_SECRET", "secret-1")
    monkeypatch.delenv("MICROSOFT_OAUTH_REDIRECT_URI", raising=False)
    authorize = _endpoint("/api/email/oauth/microsoft/authorize")
    with mock.patch("core.database.SessionLocal", factory):
        resp = await authorize(
            account_id="acct-ms",
            tenant="organizations",
            admin_consent=0,
            request=_FakeRequest(),
            owner="alice",
        )
    location = resp.headers["location"]
    assert "login.microsoftonline.com/organizations/oauth2/v2.0/authorize" in location
    assert "localhost%3A7011" in location or "localhost:7011" in location
    assert "acct-ms" not in location


@pytest.mark.asyncio
async def test_callback_fills_empty_mailbox_and_rejects_mismatch(monkeypatch):
    from routes.email_helpers import make_oauth_state
    from src.secret_storage import decrypt
    from core.database import EmailAccount

    db, factory = _make_db()
    _make_account(db, account_id="empty", owner="alice", imap_user="", smtp_user="")
    _make_account(
        db,
        account_id="mismatch",
        owner="alice",
        imap_user="other@contoso.com",
        smtp_user="other@contoso.com",
    )
    db.close()
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_ID", "client-1")
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_SECRET", "secret-1")
    token_resp = mock.MagicMock()
    token_resp.raise_for_status = mock.MagicMock()
    token_resp.json.return_value = {
        "access_token": "ms-access",
        "refresh_token": "ms-refresh",
        "expires_in": 3600,
        "id_token": _jwt({"email": "ada@contoso.com", "name": "Ada", "tid": "organizations"}),
    }
    callback = _endpoint("/api/email/oauth/microsoft/callback")

    async def _run(account_id):
        state = make_oauth_state(account_id, "alice", extra={"t": "organizations"})
        with mock.patch("httpx.post", return_value=token_resp), mock.patch("core.database.SessionLocal", factory):
            return await callback(
                code="auth-code",
                state=state,
                error=None,
                admin_consent=None,
                request=_FakeRequest(),
            )

    ok = await _run("empty")
    assert "email_oauth_success=1" in ok.headers["location"]
    assert "ms-access" not in ok.headers["location"]

    bad = await _run("mismatch")
    assert "identity_verification_failed" in bad.headers["location"]

    verify = factory()
    empty = verify.query(EmailAccount).filter(EmailAccount.id == "empty").first()
    mismatch = verify.query(EmailAccount).filter(EmailAccount.id == "mismatch").first()
    assert empty.oauth_provider == "microsoft"
    assert empty.oauth_tenant == "organizations"
    assert empty.imap_host == "outlook.office365.com"
    assert empty.smtp_host == "smtp.office365.com"
    assert empty.smtp_security == "starttls"
    assert empty.imap_user == "ada@contoso.com"
    assert decrypt(empty.oauth_refresh_token) == "ms-refresh"
    assert mismatch.oauth_access_token is None
    verify.close()


@pytest.mark.asyncio
async def test_admin_consent_does_not_store_a_token():
    callback = _endpoint("/api/email/oauth/microsoft/callback")
    resp = await callback(
        code=None,
        state=None,
        error=None,
        admin_consent="true",
        request=_FakeRequest(),
    )
    assert "email_oauth_success=admin_consent" in resp.headers["location"]


def test_password_error_still_points_at_microsoft_signin():
    from routes.email_helpers import _friendly_email_auth_error

    msg = _friendly_email_auth_error(
        "SMTP",
        "smtp.office365.com",
        "(535, b'5.7.139 Authentication unsuccessful, basic authentication is disabled.')",
    )
    assert "Microsoft no longer accepts normal mailbox passwords" in msg
    assert "OAuth/Graph" in msg
    assert "Outlook/Office 365" in msg
    assert "535" not in msg
