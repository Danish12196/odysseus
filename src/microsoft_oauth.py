"""Microsoft identity for Outlook IMAP/SMTP (XOAUTH2).

Personal accounts and work tenants use the same authorize/token endpoints.
The tenant segment is ``consumers`` (personal), ``organizations`` or a tenant
id (work), or ``common`` when the app registration allows both. IMAP and SMTP
need the Outlook resource scopes. Those are a different token from Microsoft
Graph, so calendar and contacts stay on the optional MCP server.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

IMAP_HOST = "outlook.office365.com"
SMTP_HOST = "smtp.office365.com"
IMAP_HOSTS = frozenset({IMAP_HOST, "imap-mail.outlook.com"})
SMTP_HOSTS = frozenset({SMTP_HOST, "smtp-mail.outlook.com"})

_SCOPES = " ".join((
    "offline_access",
    "openid",
    "profile",
    "email",
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
))

_TENANT_RE = re.compile(
    r"^(?:common|consumers|organizations|[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


class MicrosoftOAuthError(RuntimeError):
    """User-facing configuration or token failure. The message has no secrets."""


def normalize_tenant(value: Optional[str], *, default: Optional[str] = None) -> str:
    """Return a tenant segment Microsoft will accept, or raise."""

    raw = str(value or "").strip().lower()
    if not raw:
        raw = str(default if default is not None else os.environ.get("MICROSOFT_OAUTH_TENANT", "common")).strip().lower()
    if not raw:
        raw = "common"
    if not _TENANT_RE.fullmatch(raw):
        raise MicrosoftOAuthError(
            "Microsoft tenant must be common, consumers, organizations, or a tenant id"
        )
    return raw


def client_config() -> tuple[str, str]:
    client_id = os.environ.get("MICROSOFT_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("MICROSOFT_OAUTH_CLIENT_SECRET", "").strip()
    return client_id, client_secret


def configured() -> bool:
    client_id, client_secret = client_config()
    return bool(client_id and client_secret)


def _authority(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


def authorize_url(*, tenant: str, client_id: str, redirect_uri: str, state: str, admin_consent: bool = False) -> str:
    tenant = normalize_tenant(tenant)
    if admin_consent:
        query = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        })
        return f"https://login.microsoftonline.com/{tenant}/adminconsent?{query}"
    query = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": _SCOPES,
        "state": state,
        "prompt": "select_account",
    })
    return f"{_authority(tenant)}/authorize?{query}"


def admin_consent_url(*, tenant: str, client_id: str, redirect_uri: str) -> str:
    return authorize_url(
        tenant=tenant,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state="admin-consent",
        admin_consent=True,
    )


def imap_transport_allowed(port: int, starttls: bool) -> bool:
    return (int(port) == 993 and not starttls) or (int(port) == 143 and starttls)


def smtp_transport_allowed(port: int, security: str) -> bool:
    return (int(port) == 465 and security == "ssl") or (int(port) == 587 and security == "starttls")


def _jwt_payload(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    segment = token.split(".")[1]
    pad = segment + ("=" * (-len(segment) % 4))
    try:
        data = json.loads(base64.urlsafe_b64decode(pad.encode()).decode())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _email_from_claims(claims: dict) -> str:
    for key in ("email", "preferred_username", "upn"):
        value = claims.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()
    return ""


def exchange_code(*, code: str, redirect_uri: str, tenant: str) -> dict:
    """Trade an auth code for tokens. Raises MicrosoftOAuthError without the code."""

    import httpx

    client_id, client_secret = client_config()
    if not client_id or not client_secret:
        raise MicrosoftOAuthError("Microsoft OAuth client is not configured")
    tenant = normalize_tenant(tenant)
    try:
        response = httpx.post(
            f"{_authority(tenant)}/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": _SCOPES,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except MicrosoftOAuthError:
        raise
    except Exception:
        logger.warning("Microsoft authorization-code exchange failed")
        raise MicrosoftOAuthError("Microsoft token exchange failed")
    if not payload.get("access_token") or not payload.get("refresh_token"):
        raise MicrosoftOAuthError("Microsoft did not return an offline token")
    return payload


def fetch_profile(access_token: str, id_token: str = "") -> dict:
    """Mailbox address and display name. Prefers the id token, then userinfo."""

    claims = _jwt_payload(id_token)
    email = _email_from_claims(claims)
    name = claims.get("name") if isinstance(claims.get("name"), str) else ""
    tenant = claims.get("tid") if isinstance(claims.get("tid"), str) else ""
    if email:
        return {"email": email, "name": name, "tenant": tenant}
    import httpx

    try:
        response = httpx.get(
            "https://graph.microsoft.com/oidc/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if response.is_success:
            data = response.json()
            if isinstance(data, dict):
                email = _email_from_claims(data) or email
                if isinstance(data.get("name"), str):
                    name = name or data["name"]
    except Exception:
        logger.warning("Microsoft userinfo lookup failed")
    return {"email": email, "name": name, "tenant": tenant}


def refresh_access_token(account_id: str) -> Optional[str]:
    """Refresh one mailbox and store the new token encrypted. None on failure."""

    import httpx
    from core.database import EmailAccount, SessionLocal
    from src.secret_storage import decrypt, encrypt

    client_id, client_secret = client_config()
    if not client_id or not client_secret or not account_id:
        return None
    db = SessionLocal()
    try:
        row = db.get(EmailAccount, account_id)
        if not row or (row.oauth_provider or "") != "microsoft" or not row.oauth_refresh_token:
            return None
        refresh_token = decrypt(row.oauth_refresh_token or "")
        if not refresh_token:
            return None
        try:
            tenant = normalize_tenant(getattr(row, "oauth_tenant", None))
        except MicrosoftOAuthError:
            return None
        try:
            response = httpx.post(
                f"{_authority(tenant)}/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": _SCOPES,
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.warning("Microsoft token refresh failed for account %s", account_id)
            return None
        access_token = payload.get("access_token") or ""
        if not access_token:
            return None
        row.oauth_access_token = encrypt(access_token)
        rotated = payload.get("refresh_token") or ""
        if rotated:
            row.oauth_refresh_token = encrypt(rotated)
        row.oauth_token_expiry = str(int(time.time()) + int(payload.get("expires_in") or 3600))
        db.commit()
        return access_token
    except Exception:
        logger.warning("Microsoft token refresh failed for account %s", account_id)
        return None
    finally:
        db.close()


def valid_access_token(account_id: str, cfg: dict) -> Optional[str]:
    """Return a usable access token, refreshing when the stored one is due."""

    from src.secret_storage import decrypt

    if (cfg or {}).get("oauth_provider") != "microsoft":
        return None
    access_token = decrypt((cfg or {}).get("oauth_access_token") or "")
    expiry = str((cfg or {}).get("oauth_token_expiry") or "")
    if access_token and expiry:
        try:
            if int(expiry) - 60 > time.time():
                return access_token
        except (TypeError, ValueError):
            pass
    return refresh_access_token(account_id or (cfg or {}).get("account_id") or "")
