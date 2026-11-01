"""Calls from Python (Pyodide) into the phone's JavaScript engine (js/engine.js, js/desk.js, js/lab.js).
Every bridge function takes and returns JSON strings: {"ok": true, "data": ...} or {"ok": false, "status": ..., "message": ...}."""
import json

from fastapi import HTTPException


async def js(name, *args):
    import mmbridge                                   # registered by js/lab.js with pyodide.registerJsModule
    raw = await getattr(mmbridge, name)(*[json.dumps(a) for a in args])
    r = json.loads(str(raw))
    if not r.get("ok"):
        raise HTTPException(int(r.get("status") or 502), str(r.get("message") or "request failed"))
    return r.get("data")
