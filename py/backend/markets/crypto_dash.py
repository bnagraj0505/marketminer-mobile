"""Phone build: Delta Exchange option chain from js/engine.js (a port of the PC's crypto_dash.options)."""
from _bridge import js


async def options(coin="BTC", expiry=None, width=3):
    return await js("cryptoOptions", coin, expiry, width)
