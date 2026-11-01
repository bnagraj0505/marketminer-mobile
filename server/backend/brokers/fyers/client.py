"""FYERS data-only integration for MarketMiner (API v3). Read-only: no order code.

Login logic follows SmartEntry AI's FYERS adapter:
  * the browser opens FYERS's generate-authcode page with a one-time `state`;
  * FYERS redirects back to the app's registered redirect URL with an auth_code;
  * the backend swaps it for an access token (appIdHash = SHA-256 of "app_id:secret");
  * the token is kept in a local file with issued_at / expires_at and expires at the next 06:30 IST.

App settings: FYERS_APP_ID / FYERS_APP_SECRET / FYERS_REDIRECT_URI in .env win if all are set.
Otherwise they come from .fyers_app.json, which the FYERS page fills in (the secret is typed there and
never sent back to a browser).
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[3]
TOKEN_FILE = ROOT / ".fyers_token.json"
STATE_FILE = ROOT / ".fyers_oauth_state.json"
APP_FILE = ROOT / ".fyers_app.json"
AUTH_BASE = "https://api-t1.fyers.in/api/v3"
DATA_BASE = "https://api-t1.fyers.in/data"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TOKEN_ERROR_CODES = {-8, -15, -16, -17, -18}   # expired / invalid access token


def _private_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


# ---------------------------------------------------------------- app settings

def read_app() -> dict[str, str]:
    try:
        data = json.loads(APP_FILE.read_text(encoding="utf-8"))
        return {k: str(data.get(k) or "").strip() for k in ("app_id", "secret", "redirect_uri")}
    except (OSError, ValueError, TypeError):
        return {"app_id": "", "secret": "", "redirect_uri": ""}


def save_app(app_id: str | None = None, secret: str | None = None, redirect_uri: str | None = None) -> dict:
    cur = read_app()
    if app_id is not None:
        cur["app_id"] = app_id.strip()
    if redirect_uri is not None:
        cur["redirect_uri"] = redirect_uri.strip()
    if secret:                       # blank keeps the saved secret
        cur["secret"] = secret.strip()
    changed_app = cur["app_id"] != read_app()["app_id"]
    _private_write(APP_FILE, cur)
    if changed_app:                  # a token belongs to one app
        TOKEN_FILE.unlink(missing_ok=True)
    return app_public()


def _env_config() -> tuple[str, str, str] | None:
    vals = tuple(os.getenv(k, "").strip() for k in ("FYERS_APP_ID", "FYERS_APP_SECRET", "FYERS_REDIRECT_URI"))
    return vals if all(vals) else None


def app_public() -> dict[str, Any]:
    """What the UI may see: never the secret itself."""
    env = _env_config()
    if env:
        return {"source": ".env", "app_id": env[0], "redirect_uri": env[2], "has_secret": True, "editable": False}
    a = read_app()
    return {"source": "app", "app_id": a["app_id"], "redirect_uri": a["redirect_uri"],
            "has_secret": bool(a["secret"]), "editable": True}


def config() -> tuple[str, str, str]:
    env = _env_config()
    if env:
        return env
    a = read_app()
    missing = [n for n, v in (("App ID", a["app_id"]), ("Secret key", a["secret"]),
                              ("Redirect URL", a["redirect_uri"])) if not v]
    if missing:
        raise HTTPException(503, f"FYERS app not set up: enter the {', '.join(missing)} on the FYERS page.")
    return a["app_id"], a["secret"], a["redirect_uri"]


# ---------------------------------------------------------------- token (same rules as SmartEntry)

def token_expiry(issued_at: dt.datetime) -> dt.datetime:
    """FYERS access tokens expire at the next 06:30 IST."""
    local = issued_at.astimezone(IST)
    cutoff = local.replace(hour=6, minute=30, second=0, microsecond=0)
    if local >= cutoff:
        cutoff += dt.timedelta(days=1)
    return cutoff.astimezone(dt.timezone.utc)


def _load_token() -> dict[str, Any] | None:
    try:
        raw = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        if not raw.get("access_token"):
            return None
        if "issued_at" in raw:
            issued = dt.datetime.fromisoformat(raw["issued_at"])
        else:                                            # older MarketMiner token format
            issued = dt.datetime.fromtimestamp(int(raw.get("created_at", 0)), dt.timezone.utc)
        expires = dt.datetime.fromisoformat(raw["expires_at"]) if raw.get("expires_at") else token_expiry(issued)
        return {**raw, "issued_at": issued, "expires_at": expires}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def read_token() -> dict[str, Any] | None:
    tok = _load_token()
    if not tok or dt.datetime.now(dt.timezone.utc) >= tok["expires_at"]:
        return None
    try:
        if tok.get("app_id") and tok["app_id"] != config()[0]:
            return None
    except HTTPException:
        return None
    return tok


def token_status() -> dict[str, Any]:
    return {**_token_status(), **refresh_status()}


def _token_status() -> dict[str, Any]:
    tok = _load_token()
    if not tok:
        return {"connected": False, "provider": "FYERS", "message": "Not connected"}
    now = dt.datetime.now(dt.timezone.utc)
    if now >= tok["expires_at"]:
        return {"connected": False, "provider": "FYERS", "token_present": True, "locally_expired": True,
                "message": "Login expired (FYERS tokens end at 06:30 IST). Connect FYERS again."}
    return {"connected": True, "provider": "FYERS", "token_present": True, "locally_expired": False,
            "created_at": tok["issued_at"].isoformat(), "expires_at": tok["expires_at"].astimezone(IST).isoformat(),
            "message": "Connected until " + tok["expires_at"].astimezone(IST).strftime("%d %b %H:%M IST")}


def make_login_url() -> str:
    app_id, _, redirect_uri = config()
    state = secrets.token_urlsafe(24)
    _private_write(STATE_FILE, {"state": state, "created_at": time.time()})
    params = {"client_id": app_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state}
    return f"{AUTH_BASE}/generate-authcode?{urlencode(params)}"


async def exchange_auth_code(code: str, state: str | None) -> dict[str, Any]:
    app_id, app_secret, _ = config()
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(400, "OAuth state is missing. Start login again from MarketMiner.")
    if not state or not secrets.compare_digest(str(saved.get("state", "")), state):
        raise HTTPException(400, "OAuth state mismatch. Start login again from MarketMiner.")
    if time.time() - float(saved.get("created_at", 0)) > 600:
        raise HTTPException(400, "OAuth session expired. Start login again.")
    app_hash = hashlib.sha256(f"{app_id}:{app_secret}".encode()).hexdigest()
    payload = {"grant_type": "authorization_code", "appIdHash": app_hash, "code": code}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8)) as client:
        response = await client.post(f"{AUTH_BASE}/validate-authcode", json=payload)
    try:
        result = response.json()
    except ValueError:
        raise HTTPException(502, f"FYERS token endpoint returned HTTP {response.status_code} with a non-JSON response")
    access_token = result.get("access_token")
    if response.status_code >= 400 or not access_token:
        # Never return or log secrets from the token exchange.
        message = result.get("message") or result.get("error") or f"FYERS token exchange failed (HTTP {response.status_code})"
        if "hash" in str(message).lower() or result.get("code") in (-371, -413):
            message = f"{message} — check the secret key entered on the FYERS page."
        raise HTTPException(502, str(message)[:300])
    issued = dt.datetime.now(dt.timezone.utc)
    record = {"access_token": access_token, "issued_at": issued.isoformat(),
              "expires_at": token_expiry(issued).isoformat(), "app_id": app_id}
    if result.get("refresh_token"):
        # FYERS refresh tokens last 15 days; with the user's PIN they buy a new day's access token without a full login.
        record["refresh_token"] = result["refresh_token"]
        record["refresh_expires_at"] = (issued + dt.timedelta(days=REFRESH_DAYS) - dt.timedelta(minutes=30)).isoformat()
    _private_write(TOKEN_FILE, record)
    STATE_FILE.unlink(missing_ok=True)
    return {"connected": True, "message": "FYERS authentication completed"}


REFRESH_DAYS = 15


def _raw_token() -> dict[str, Any]:
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def refresh_status() -> dict[str, Any]:
    raw = _raw_token()
    rt, exp = raw.get("refresh_token"), raw.get("refresh_expires_at")
    if not rt or not exp:
        return {"can_renew": False}
    try:
        end = dt.datetime.fromisoformat(exp)
        if raw.get("app_id") and raw["app_id"] != config()[0]:
            return {"can_renew": False}
    except (ValueError, HTTPException):
        return {"can_renew": False}
    now = dt.datetime.now(dt.timezone.utc)
    if now >= end:
        return {"can_renew": False, "renew_expired": True}
    return {"can_renew": True, "renew_until": end.astimezone(IST).isoformat(),
            "renew_days_left": round((end - now).total_seconds() / 86400, 1)}


async def renew_with_pin(pin: str) -> dict[str, Any]:
    """Swap the saved refresh token + the user's FYERS PIN for a new access token. The PIN is used once, never stored or logged."""
    if not (isinstance(pin, str) and pin.isdigit() and 4 <= len(pin) <= 6):
        raise HTTPException(422, "Enter your FYERS PIN (digits only)")
    st = refresh_status()
    if not st.get("can_renew"):
        raise HTTPException(409, "No valid refresh token: use Connect FYERS (full login) once; after that the PIN renews it for 15 days.")
    raw = _raw_token()
    app_id, app_secret, _ = config()
    app_hash = hashlib.sha256(f"{app_id}:{app_secret}".encode()).hexdigest()
    payload = {"grant_type": "refresh_token", "appIdHash": app_hash, "refresh_token": raw["refresh_token"], "pin": pin}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8)) as client:
        response = await client.post(f"{AUTH_BASE}/validate-refresh-token", json=payload)
    try:
        result = response.json()
    except ValueError:
        raise HTTPException(502, f"FYERS renew endpoint returned HTTP {response.status_code} with a non-JSON response")
    access_token = result.get("access_token")
    if response.status_code >= 400 or not access_token:
        message = str(result.get("message") or result.get("error") or f"FYERS renew failed (HTTP {response.status_code})")
        if "pin" in message.lower():
            message = "FYERS rejected the PIN. Check it and try again (several wrong PINs can lock the FYERS login)."
        elif "refresh" in message.lower() or "expired" in message.lower():
            message += " — use Connect FYERS (full login) to get a fresh 15-day refresh token."
        raise HTTPException(502, message[:300])
    issued = dt.datetime.now(dt.timezone.utc)
    raw.update({"access_token": access_token, "issued_at": issued.isoformat(), "expires_at": token_expiry(issued).isoformat(),
                "app_id": app_id, "renewed_at": issued.isoformat()})
    if result.get("refresh_token"):
        raw["refresh_token"] = result["refresh_token"]
    _private_write(TOKEN_FILE, raw)
    return {"connected": True, "message": "FYERS renewed with PIN until " + token_expiry(issued).astimezone(IST).strftime("%d %b %H:%M IST")}


# ---------------------------------------------------------------- rate limit
# FYERS data limits are per account (Standard plan: 5/s, 50/min, 5,000/day; going over the per-minute
# limit 3 times blocks the account for the day). SmartEntry uses the same FYERS account, so MarketMiner
# keeps well under: 3 requests/second and 30/minute by default.
_PER_SEC = int(os.getenv("MARKETMINER_FYERS_PER_SECOND", "3") or 3)
_PER_MIN = int(os.getenv("MARKETMINER_FYERS_PER_MINUTE", "30") or 30)
_calls: collections.deque[float] = collections.deque()
_rl_lock = asyncio.Lock()


async def _rate_limit(max_wait: float = 150.0) -> None:
    start = time.monotonic()
    async with _rl_lock:
        while True:
            now = time.monotonic()
            while _calls and now - _calls[0] >= 60:
                _calls.popleft()
            last_sec = sum(1 for t in _calls if now - t < 1)
            if len(_calls) < _PER_MIN and last_sec < _PER_SEC:
                _calls.append(now)
                return
            wait = 1 - (now - _calls[-last_sec]) if last_sec >= _PER_SEC else 60 - (now - _calls[0])
            if now - start + wait > max_wait:
                raise HTTPException(429, "MarketMiner is pacing FYERS requests to protect your account limit. "
                                         "Try again in a minute.")
            await asyncio.sleep(max(0.05, wait))


def rate_status() -> dict[str, int]:
    now = time.monotonic()
    return {"last_minute": sum(1 for t in _calls if now - t < 60), "per_minute_cap": _PER_MIN}


async def data_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    app_id, _, _ = config()
    token = read_token()
    if not token:
        raise HTTPException(401, "FYERS is not connected. Use Connect FYERS first.")
    await _rate_limit()
    headers = {"Authorization": f"{app_id}:{token['access_token']}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8)) as client:
        response = await client.get(f"{DATA_BASE}{path}", params=params, headers=headers)
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(502, f"FYERS returned non-JSON response (HTTP {response.status_code})")
    if response.status_code in (401, 403) or payload.get("code") in TOKEN_ERROR_CODES:
        raise HTTPException(401, "FYERS rejected the login (expired or missing Market Data permission). Connect FYERS again.")
    if response.status_code == 429 or payload.get("code") == 429:
        raise HTTPException(429, "FYERS rate limit reached. Wait a minute before scanning again.")
    if response.status_code >= 400 or payload.get("s") == "error":
        raise HTTPException(502, str(payload.get("message") or f"FYERS data request failed (HTTP {response.status_code})")[:300])
    return payload
