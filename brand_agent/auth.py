from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .db import Database


class LinkedInAuthError(RuntimeError):
    pass


class TokenCipher:
    def __init__(self, key: str):
        if not key:
            raise LinkedInAuthError("TOKEN_ENCRYPTION_KEY is not configured")
        try:
            self.fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise LinkedInAuthError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise LinkedInAuthError("The stored LinkedIn token cannot be decrypted") from exc


class LinkedInOAuth:
    authorize_endpoint = "https://www.linkedin.com/oauth/v2/authorization"
    token_endpoint = "https://www.linkedin.com/oauth/v2/accessToken"
    userinfo_endpoint = "https://api.linkedin.com/v2/userinfo"

    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.client_id = os.getenv("LINKEDIN_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("LINKEDIN_CLIENT_SECRET", "").strip()
        self.admin_email = os.getenv("ADMIN_EMAIL", "").strip().casefold()
        self.cipher = TokenCipher(os.getenv("TOKEN_ENCRYPTION_KEY", ""))

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.base_url}/auth/linkedin/callback"

    def authorization_url(self, *, state: str, nonce: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "nonce": nonce,
                "scope": "openid profile email w_member_social",
            }
        )
        return f"{self.authorize_endpoint}?{query}"

    def complete(self, *, code: str) -> dict[str, str]:
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                token_response = client.post(
                    self.token_endpoint,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "redirect_uri": self.redirect_uri,
                    },
                )
                token_response.raise_for_status()
                token_data = token_response.json()
                access_token = str(token_data["access_token"])

                user_response = client.get(
                    self.userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                user_response.raise_for_status()
                user = user_response.json()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LinkedInAuthError(f"LinkedIn authorization failed: {type(exc).__name__}") from exc

        email = str(user.get("email", "")).strip().casefold()
        subject = str(user.get("sub", "")).strip()
        display_name = str(user.get("name", "LinkedIn member")).strip()
        if not email or not subject:
            raise LinkedInAuthError("LinkedIn did not return the required email and member identifier")
        if email != self.admin_email:
            raise LinkedInAuthError("This LinkedIn account is not authorized for this dashboard")

        expires_in = int(token_data.get("expires_in", 3600))
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
        self.database.save_oauth_credential(
            provider="linkedin",
            subject=subject,
            email=email,
            display_name=display_name,
            access_token_encrypted=self.cipher.encrypt(access_token),
            expires_at=expires_at,
            scope=str(token_data.get("scope", "")),
        )
        self.database.log_event("linkedin_connected", "oauth", subject, {"email": email, "expires_at": expires_at})
        return {"sub": subject, "email": email, "name": display_name}

    def credential(self) -> dict[str, Any] | None:
        stored = self.database.get_oauth_credential("linkedin")
        if not stored:
            return None
        result = dict(stored)
        result["access_token"] = self.cipher.decrypt(result.pop("access_token_encrypted"))
        return result

    def is_connected(self) -> bool:
        stored = self.database.get_oauth_credential("linkedin")
        if not stored:
            return False
        try:
            return datetime.fromisoformat(stored["expires_at"]) > datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return False


def new_oauth_state() -> tuple[str, str]:
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
