"""Phone build: FYERS data calls go through the browser's FYERS client (js/engine.js MM.FY.get), which holds the
token on this device. Same return shape as the PC's data_get."""
from _bridge import js


async def data_get(path, params):
    return await js("fyGet", path, {k: ("" if v is None else v) for k, v in params.items()})
