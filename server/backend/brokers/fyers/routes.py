from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .client import (app_public, config, data_get, exchange_auth_code, make_login_url, rate_status, renew_with_pin, save_app,
                     token_status)

router = APIRouter(prefix="/api/fyers", tags=["FYERS market data"])
# FYERS sends the login back to the app's registered redirect URL. SmartEntry's FYERS app is registered with
# http://127.0.0.1:8000/api/brokers/fyers/callback, so MarketMiner answers on that path too.
alias_router = APIRouter(tags=["FYERS market data"])
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


class AppIn(BaseModel):
    app_id: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9]+-[0-9]{3}$")
    redirect_uri: str = Field(min_length=10, max_length=300, pattern=r"^https?://")
    secret: str | None = Field(None, max_length=100)


@router.get("/app")
async def fyers_app():
    return app_public()


@router.post("/app")
async def fyers_app_save(body: AppIn, request: Request):
    # The secret key may only be entered from this PC, never over the LAN.
    if (request.client.host if request.client else "") not in LOCAL_HOSTS:
        raise HTTPException(403, "Enter the FYERS secret key on the PC running MarketMiner (http://127.0.0.1:8000).")
    if not app_public().get("editable", True):
        raise HTTPException(409, "FYERS settings come from .env; edit them there.")
    if not body.secret and not app_public().get("has_secret"):
        raise HTTPException(422, "Enter the secret key")
    return save_app(body.app_id, body.secret, body.redirect_uri)

class PinIn(BaseModel):
    pin: str = Field(min_length=4, max_length=6, pattern=r"^[0-9]+$")


@router.post("/renew")
async def fyers_renew(body: PinIn, request: Request):
    # The PIN may only be typed on the PC running MarketMiner; it is used once and never stored.
    if (request.client.host if request.client else "") not in LOCAL_HOSTS:
        raise HTTPException(403, "Renew FYERS on the PC running MarketMiner (http://127.0.0.1:8000).")
    return await renew_with_pin(body.pin)


@router.get("/status")
async def fyers_status():
    status = token_status()
    status["rate"] = rate_status()
    try:
        config()
        status["configured"] = True
    except HTTPException:
        status["configured"] = False
    return status

@router.get("/login")
async def fyers_login():
    try:
        return RedirectResponse(make_login_url(), status_code=302)
    except HTTPException as exc:
        return HTMLResponse(f"<h2>FYERS app not set up</h2><p>{exc.detail}</p><p><a href='/fyers.html'>Open the FYERS page</a></p>",
                            status_code=exc.status_code)

@alias_router.get("/api/brokers/fyers/callback", response_class=HTMLResponse)
@router.get("/callback", response_class=HTMLResponse)
async def fyers_callback(auth_code: str | None = None, code: str | None = None,
                         state: str | None = None, s: str | None = None,
                         error: str | None = None, message: str | None = None):
    auth_code = auth_code or code
    if error or not auth_code:
        detail = (message or error or "No auth_code received")[:250]
        return HTMLResponse(f"<h2>FYERS login was not completed</h2><p>{detail}</p><p>Return to MarketMiner and try Connect FYERS again.</p>", status_code=400)
    try:
        result = await exchange_auth_code(auth_code, state or s)
    except HTTPException as exc:
        return HTMLResponse(f"<h2>FYERS connection failed</h2><p>{exc.detail}</p><p>Return to MarketMiner and try again.</p>", status_code=exc.status_code)
    return HTMLResponse("<h2>FYERS connected</h2><p>Taking you back to MarketMiner…</p>"
                        "<script>setTimeout(()=>{location.href='/fyers.html?connected=1'},1000)</script>")

@router.post("/disconnect")
async def fyers_disconnect():
    from .client import TOKEN_FILE
    TOKEN_FILE.unlink(missing_ok=True)
    return {"connected": False, "message": "Local FYERS token removed"}

@router.get("/quotes")
async def fyers_quotes(symbols: str = Query(..., min_length=3, max_length=4000)):
    items = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not items or len(items) > 50:
        raise HTTPException(400, "Provide between 1 and 50 comma-separated FYERS symbols")
    if any(":" not in s or "-" not in s for s in items):
        raise HTTPException(400, "Use FYERS symbol format, e.g. NSE:SBIN-EQ or NSE:NIFTY50-INDEX")
    payload = await data_get("/quotes", {"symbols": ",".join(items)})
    return {"provider": "FYERS", "received_at": datetime.now(timezone.utc).isoformat(), "data": payload}

@router.get("/history")
async def fyers_history(symbol: str, resolution: str = "5", date_format: str = "1",
                        range_from: str = Query(...), range_to: str = Query(...), cont_flag: str = "1"):
    if ":" not in symbol or "-" not in symbol:
        raise HTTPException(400, "Use FYERS symbol format, e.g. NSE:NIFTY50-INDEX")
    allowed = {"1", "2", "3", "5", "10", "15", "20", "30", "60", "120", "240", "D", "1D", "W", "M"}
    if resolution not in allowed:
        raise HTTPException(400, "Unsupported candle resolution")
    payload = await data_get("/history", {"symbol": symbol, "resolution": resolution,
        "date_format": date_format, "range_from": range_from, "range_to": range_to, "cont_flag": cont_flag})
    return {"provider": "FYERS", "symbol": symbol, "received_at": datetime.now(timezone.utc).isoformat(), "data": payload}

@router.get("/option-chain")
async def fyers_option_chain(symbol: str = "NSE:NIFTY50-INDEX", strikecount: int = 10,
                             timestamp: str = "", greeks: int = 1):
    if ":" not in symbol or "-" not in symbol:
        raise HTTPException(400, "Use FYERS underlying symbol format")
    if not 1 <= strikecount <= 50:
        raise HTTPException(400, "strikecount must be between 1 and 50")
    payload = await data_get("/options-chain-v3", {"symbol": symbol, "strikecount": strikecount,
        "timestamp": timestamp, "greeks": str(1 if greeks else 0)})
    return {"provider": "FYERS", "symbol": symbol, "received_at": datetime.now(timezone.utc).isoformat(), "data": payload}

@router.get("/profile")
async def fyers_profile():
    from .client import config as get_config, read_token
    app_id, _, _ = get_config()
    token = read_token()
    if not token:
        raise HTTPException(401, "FYERS is not connected")
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get("https://api-t1.fyers.in/api/v3/profile", headers={"Authorization": f"{app_id}:{token['access_token']}"})
    data = r.json()
    if r.status_code >= 400 or data.get("s") == "error":
        raise HTTPException(401, data.get("message", "FYERS profile request failed"))
    # Deliberately return only basic profile fields, never credentials.
    return {"provider": "FYERS", "profile": data.get("data", data)}
