"""Transaction costs, slippage and spread used by the backtester and the paper ledger.

India (NSE/BSE/MCX), statutory rates as published by the exchanges/SEBI (verify before relying on them; they change):
  brokerage           Rs 20 per executed order (discount-broker flat fee; setting)
  STT                 equity intraday 0.025% sell; futures 0.02% sell; options 0.1% of premium sell
  exchange txn        equity 0.00297%; futures 0.00173%; options 0.03503% of premium; MCX futures 0.0021%
  SEBI fee            Rs 10 per crore of turnover
  stamp duty          equity intraday 0.003% buy; futures 0.002% buy; options 0.003% buy
  GST                 18% on brokerage + exchange txn + SEBI fee
Index spot symbols (NIFTY50-INDEX etc.) cannot be traded; their backtests use FUTURES costs as a proxy and say so.
Crypto: taker fee in basis points per side (Delta Exchange India 5 bps default; Shark: same setting), funding
  payments are NOT modelled (noted in results).
US / forex (Yahoo data): spread + commission settings only.
Slippage: `slip_bps` per side on top of half the assumed spread, applied against the trade.
"""
from __future__ import annotations

DEFAULTS = {
    "brokerage_inr": 20.0, "crypto_fee_bps": 5.0, "us_comm_bps": 1.0,
    "slip_bps": {"NSE": 2.0, "FNO": 2.0, "MCX": 2.0, "SHARK": 3.0, "DELTAF": 3.0, "DELTA": 10.0, "BINANCE": 2.0, "YF": 2.0},
    "spread_bps": {"NSE": 2.0, "FNO": 1.0, "MCX": 2.0, "SHARK": 2.0, "DELTAF": 2.0, "DELTA": 50.0, "BINANCE": 1.0, "YF": 2.0},
}
INDIA = {
    "equity": {"stt_sell": 0.00025, "txn": 0.0000297, "stamp_buy": 0.00003},
    "futures": {"stt_sell": 0.0002, "txn": 0.0000173, "stamp_buy": 0.00002},
    "options": {"stt_sell": 0.001, "txn": 0.0003503, "stamp_buy": 0.00003},
    "mcx": {"stt_sell": 0.0001, "txn": 0.000021, "stamp_buy": 0.00002},
}


def instrument_kind(symbol: str, market: str) -> tuple[str, str | None]:
    s = symbol.upper()
    if market == "MCX":
        return "mcx", None
    if market in ("NSE", "FNO"):
        if s.endswith("-EQ") or s.endswith("-BE"):
            return "equity", None
        if s.endswith("CE") or s.endswith("PE"):
            return "options", None
        if s.endswith("-INDEX"):
            return "futures", "Index spot is not tradable: futures costs used as a proxy"
        return "futures", None
    return "other", None


def slip_price(price: float, side: int, market: str, cfg: dict | None = None) -> float:
    """Price after slippage + half-spread, moved against a trade whose order side is +1 buy / -1 sell."""
    c = cfg or DEFAULTS
    bps = c["slip_bps"].get(market, 2.0) + c["spread_bps"].get(market, 2.0) / 2
    return price * (1 + side * bps / 10000)


def round_trip(symbol: str, market: str, qty: float, entry: float, exit_: float, long: bool, cfg: dict | None = None) -> float:
    """Total fees and taxes for one entry + one exit (currency of the instrument)."""
    c = cfg or DEFAULTS
    buy_px, sell_px = (entry, exit_) if long else (exit_, entry)
    buy_v, sell_v = abs(qty) * buy_px, abs(qty) * sell_px
    kind, _ = instrument_kind(symbol, market)
    if kind in INDIA:
        r = INDIA[kind]
        brk = 2 * c["brokerage_inr"]
        if kind == "equity":
            brk = min(brk, 0.0003 * (buy_v + sell_v))
        txn = r["txn"] * (buy_v + sell_v)
        sebi = (buy_v + sell_v) * 10 / 1e7
        return brk + r["stt_sell"] * sell_v + txn + sebi + r["stamp_buy"] * buy_v + 0.18 * (brk + txn + sebi)
    if market in ("SHARK", "DELTAF", "DELTA", "BINANCE"):
        return c["crypto_fee_bps"] / 10000 * (buy_v + sell_v)
    return c["us_comm_bps"] / 10000 * (buy_v + sell_v)
