"""5paisa TOTP session.

``TOTPLogin`` → ``RequestToken`` → ``GetAccessToken`` → a JWT whose ``exp`` claim is the real expiry.

Two hard-won rules from running this against the live broker:

* **One TOTP per 30-second window.** The code is single-use; a second login inside the same window
  is rejected, and the old stack needed a Redis lock plus a "which window did we last use" key
  because five JVMs and a FastAPI worker all logged in independently. One process needs no lock —
  it needs :attr:`_window_used`, which is the same idea with none of the machinery.
* **PublicIP must be the box's real outbound address** or the broker's RMS silently rejects orders.
  Auto-detected once per process and logged, never guessed as ``127.0.0.1``.

The TOTP *seed* is a credential: it is read from ``KN_FP_TOTP_SECRET`` and never logged, never
written to disk, and never included in an error message. The old repos committed it in a URI inside
two source files.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pyotp
import structlog

from ...config import Settings
from ..base import VenueError

log = structlog.get_logger(__name__)

TOTP_WINDOW_S = 30
#: A session younger than this is not the reason a call failed, so an error path may not drop
#: it. Longer than the 30s OTP window, so a genuine re-auth still gets a fresh code.
MIN_SESSION_AGE_S = 90.0
#: Vendor-wide subscription key that ships inside py5paisa; it identifies the API product, not the
#: user, and the historical-data host rejects the request without it.
APIM_KEY = "c89fab8d895a426d9e00db380b433027"


@dataclass(slots=True)
class Session:
    access_token: str
    client_code: str
    expires_at: float
    minted_at: float = field(default_factory=time.time)

    @property
    def valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - 1800  # refresh 30m early


def _decode_jwt_exp(jwt: str) -> float:
    """``exp`` out of the JWT payload, or 0 when it cannot be read. Never raises: an unreadable
    token still works, it just falls back to the conservative 6-hour TTL."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:  # noqa: BLE001 - an unreadable token still works; fall back to the TTL
        return 0.0


class Authenticator:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.s = settings
        self.http = client
        self.session: Session | None = None
        self._lock = asyncio.Lock()
        self._window_used: int | None = None
        self._public_ip: str | None = settings.fp_public_ip

    # -- public ---------------------------------------------------------------------------------

    async def token(self) -> Session:
        async with self._lock:
            if self.session and self.session.valid:
                return self.session
            self.session = await self._login()
            return self.session

    def invalidate(self, *, min_age_s: float = MIN_SESSION_AGE_S) -> bool:
        """Drop the session so the next call re-logs in. Returns whether it actually dropped.

        Guarded by age, because the caller is an error path. ``V2/NetPositionNetWise`` answers a
        *flat book* with ``head.status=1`` — the same code a dead session uses — and reconcile runs
        every 60s in LIVE. Ungated, a permanently flat account forces a full TOTP login every
        minute for the whole session: the OTP window is 30s and single-use, so most of those
        attempts fail, and the ones that do not churn the broker session the live feed is using.
        A session minted seconds ago is not the reason a call failed.
        """
        if self.session is not None and time.time() - self.session.minted_at < min_age_s:
            log.warning(
                "fivepaisa.reauth_suppressed",
                session_age_s=round(time.time() - self.session.minted_at, 1),
                min_age_s=min_age_s,
            )
            return False
        self.session = None
        return True

    async def public_ip(self) -> str:
        if self._public_ip:
            return self._public_ip
        for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
            try:
                r = await self.http.get(url, timeout=5)
                ip = r.text.strip()
                if ip:
                    self._public_ip = ip
                    log.info("fivepaisa.public_ip", ip=ip, source=url)
                    return ip
            except Exception as exc:  # noqa: BLE001 - any failure just means try the next source
                log.warning("fivepaisa.public_ip_failed", source=url, error=str(exc))
        raise VenueError(
            "could not determine the public IP — set KN_FP_PUBLIC_IP explicitly; "
            "a wrong value makes the broker's RMS reject every order"
        )

    # -- internals ------------------------------------------------------------------------------

    async def _wait_for_fresh_window(self) -> int:
        window = int(time.time()) // TOTP_WINDOW_S
        if self._window_used == window:
            sleep_s = TOTP_WINDOW_S - (int(time.time()) % TOTP_WINDOW_S) + 1
            log.info("fivepaisa.totp_window_wait", seconds=sleep_s)
            await asyncio.sleep(sleep_s)
            window = int(time.time()) // TOTP_WINDOW_S
        return window

    async def _login(self) -> Session:
        s = self.s
        if not s.has_credentials:
            raise VenueError(
                "5paisa credentials missing — set KN_FP_CLIENT_CODE / KN_FP_APP_KEY / "
                "KN_FP_ENCRYPT_KEY / KN_FP_USER_ID / KN_FP_PIN / KN_FP_TOTP_SECRET in backend/.env"
            )
        window = await self._wait_for_fresh_window()
        ip = await self.public_ip()
        code = pyotp.TOTP(s.fp_totp_secret.get_secret_value()).now()  # type: ignore[union-attr]
        request_token = await self._totp_login(code, ip)
        self._window_used = window
        access = await self._access_token(request_token, ip)
        exp = _decode_jwt_exp(access)
        expires_at = exp if exp > time.time() else time.time() + 6 * 3600
        log.info(
            "fivepaisa.session",
            client_code=s.fp_client_code,
            expires_in_h=round((expires_at - time.time()) / 3600, 2),
        )
        return Session(access_token=access, client_code=str(s.fp_client_code), expires_at=expires_at)

    def _head(self) -> dict[str, Any]:
        return {"Key": self.s.fp_app_key.get_secret_value()}  # type: ignore[union-attr]

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self.s.endpoints.rest + path
        r = await self.http.post(url, json={"head": self._head(), "body": body}, timeout=30)
        r.raise_for_status()
        return r.json()

    async def _totp_login(self, totp: str, ip: str) -> str:
        data = await self._post(
            "TOTPLogin",
            {
                "Email_ID": self.s.fp_client_code,
                "TOTP": totp,
                "PIN": self.s.fp_pin.get_secret_value(),  # type: ignore[union-attr]
                "PublicIP": ip,
                "LocalIP": ip,
            },
        )
        head, body = data.get("head") or {}, data.get("body") or {}
        if str(head.get("Status", body.get("Status"))) != "0":
            raise VenueError(f"TOTPLogin rejected: {body.get('Message', head)}", raw=head)
        token = body.get("RequestToken")
        if not token:
            raise VenueError("TOTPLogin returned no RequestToken", raw=body)
        return str(token)

    async def _access_token(self, request_token: str, ip: str) -> str:
        data = await self._post(
            "GetAccessToken",
            {
                "RequestToken": request_token,
                "EncryKey": self.s.fp_encrypt_key.get_secret_value(),  # type: ignore[union-attr]
                "UserId": self.s.fp_user_id.get_secret_value(),  # type: ignore[union-attr]
                "PublicIP": ip,
                "LocalIP": ip,
            },
        )
        body = data.get("body") or {}
        token = body.get("AccessToken")
        if not token:
            raise VenueError("GetAccessToken returned no AccessToken", raw=body)
        return str(token)
