"""/api/lab/* - Signal Lab endpoints (FastAPI wrappers around service.py). Analysis and PAPER trading only; no order
placement exists in this package."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import data as D
from . import service as SV

router = APIRouter(prefix="/api/lab", tags=["signal-lab"])
WATCH = SV.WATCH


def _local(req: Request) -> None:
    if (req.client.host if req.client else "") not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "Signal Lab write actions are allowed from this PC only")


async def _call(fn, *a):
    try:
        r = fn(*a)
        return (await r) if hasattr(r, "__await__") else r
    except D.DataUnavailable as exc:
        raise HTTPException(503, f"DATA UNAVAILABLE: {str(exc)[:300]}")
    except SV.BadRequest as exc:
        raise HTTPException(exc.status, str(exc))


@router.get("/meta")
async def meta():
    return await _call(SV.meta)


@router.get("/analyze")
async def analyze(symbol: str = Query(..., min_length=3, max_length=60), tf: str = "15m", bars: int = Query(600, ge=200, le=2000), fresh: bool = False, live: bool = False):
    return await _call(SV.analyze, symbol, tf, bars, fresh, live)


@router.get("/live")
async def live(symbol: str = Query(..., min_length=3, max_length=60), tf: str = "15m"):
    return await _call(SV.live, symbol, tf)


@router.get("/mtf")
async def mtf(symbol: str = Query(..., min_length=3, max_length=60)):
    return await _call(SV.mtf, symbol)


@router.get("/derivatives")
async def derivatives(symbol: str = Query(..., min_length=3, max_length=60)):
    return await _call(SV.derivatives, symbol)


class BacktestIn(BaseModel):
    symbol: str = Field(min_length=3, max_length=60)
    tf: str = "15m"
    bars: int = Field(3000, ge=400, le=20000)
    strategy: str | None = None
    min_score: float | None = Field(None, ge=0, le=100)


@router.post("/backtest")
async def backtest(body: BacktestIn, req: Request):
    _local(req)
    return await _call(SV.backtest, body.model_dump())


@router.get("/backtests")
async def backtests():
    return await _call(SV.backtests)


@router.get("/backtests/{bid}")
async def backtest_get(bid: int):
    return await _call(SV.backtest_get, bid)


class PaperIn(BaseModel):
    symbol: str
    tf: str = "15m"
    strategy: str
    direction: str = Field(pattern="^(LONG|SHORT)$")
    entry: float
    stop: float
    targets: list[float] = Field(min_length=3, max_length=3)
    score: float = Field(ge=0, le=100)


@router.post("/paper/open")
async def paper_open(body: PaperIn, req: Request):
    _local(req)
    return await _call(SV.paper_open, body.model_dump())


@router.post("/paper/close/{tid}")
async def paper_close(tid: int, req: Request):
    _local(req)
    return await _call(SV.paper_close, tid)


@router.get("/paper")
async def paper_list():
    return await _call(SV.paper_list)


@router.get("/protection")
async def protection(symbol: str = "NSE:NIFTY50-INDEX", direction: str = "LONG"):
    return await _call(SV.protection, symbol, direction)


@router.get("/alerts")
async def alerts(unseen: bool = False):
    return await _call(SV.alerts, unseen)


@router.post("/alerts/seen")
async def alerts_seen(req: Request):
    _local(req)
    return await _call(SV.alerts_seen)


@router.get("/journal")
async def journal():
    return await _call(SV.journal)


class SettingsIn(BaseModel):
    protection: dict | None = None
    strategies: dict | None = None
    recorder: dict | None = None
    costs: dict | None = None
    watch: list[str] | None = None


@router.post("/settings")
async def save_settings(body: SettingsIn, req: Request):
    _local(req)
    return await _call(SV.save_settings, body.model_dump())


@router.get("/recorder")
async def recorder_status():
    return await _call(SV.recorder_status)


@router.post("/recorder/run")
async def recorder_run(req: Request):
    _local(req)
    return await _call(SV.recorder_run)
