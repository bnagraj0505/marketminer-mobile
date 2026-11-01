"""Phone build: rolling aliases (NSE:NIFTY-FUT, NSE:NIFTY-ATM-CE, DELTA:BTC-ATM-CE) resolved by js/desk.js."""
from _bridge import js


async def resolve(symbol):
    s = symbol.upper()
    if not s.endswith(("-FUT", "-ATM-CE", "-ATM-PE")):
        return s
    return await js("resolve", s)
