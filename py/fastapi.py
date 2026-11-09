"""Minimal stand-in for FastAPI on the phone (Pyodide).

The Signal Lab modules only need HTTPException. The AI agent's routes (backend/agent/routes.py, the PC's own file)
also use APIRouter, Query, Body and Request: here a router just records its handlers, and `dispatch` calls the right
one with arguments taken from the path, the query string and the JSON body, the way FastAPI would. Requests on the
phone come from this device, so Request.client.host is 127.0.0.1 (the PC's "this PC only" checks pass)."""
from __future__ import annotations

import inspect
import re
from types import SimpleNamespace


class HTTPException(Exception):
    def __init__(self, status_code: int = 500, detail: str = ""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class _Param:
    def __init__(self, default=..., *args, **kw):
        self.default = default


class _BodyParam(_Param):
    pass


def Query(default=..., *args, **kw):      # noqa: N802 - FastAPI's names
    return _Param(default)


def Body(default=..., *args, **kw):       # noqa: N802
    return _BodyParam(default)


def Path(default=..., *args, **kw):       # noqa: N802
    return _Param(default)


class Request:
    def __init__(self, body: dict | None = None):
        self.client = SimpleNamespace(host="127.0.0.1")
        self._body = body or {}

    async def json(self):
        return self._body


def _conv(v, ann: str):
    a = (ann or "").replace(" ", "")
    if v is None:
        return None
    try:
        if a.startswith("int"):
            return int(v)
        if a.startswith("float"):
            return float(v)
        if a.startswith("bool"):
            return str(v).lower() in ("1", "true", "yes", "on")
    except (TypeError, ValueError):
        raise HTTPException(422, f"bad value {v!r}")
    return v


class APIRouter:
    def __init__(self, prefix: str = "", tags=None, **kw):
        self.prefix = prefix
        self.routes: list[tuple[str, re.Pattern, object]] = []

    def _add(self, method: str, path: str):
        rx = re.compile("^" + re.sub(r"\{(\w+)(?::[^}]*)?\}", r"(?P<\1>[^/]+)", self.prefix + path) + "$")

        def deco(fn):
            self.routes.append((method, rx, fn))
            return fn
        return deco

    def get(self, path, **kw):
        return self._add("GET", path)

    def post(self, path, **kw):
        return self._add("POST", path)

    def put(self, path, **kw):
        return self._add("PUT", path)

    def delete(self, path, **kw):
        return self._add("DELETE", path)

    async def dispatch(self, method: str, path: str, query: dict, body: dict):
        allowed = False
        for m, rx, fn in self.routes:
            mt = rx.match(path)
            if not mt:
                continue
            allowed = True
            if m != method:
                continue
            kwargs = {}
            for name, p in inspect.signature(fn).parameters.items():
                ann = p.annotation if isinstance(p.annotation, str) else getattr(p.annotation, "__name__", "")
                d = p.default
                if "Request" in ann or name in ("req", "request"):
                    kwargs[name] = Request(body)
                elif name in mt.groupdict():
                    kwargs[name] = _conv(mt.group(name), ann)
                elif isinstance(d, _BodyParam) or ann.startswith("dict"):
                    kwargs[name] = body if body is not None else ({} if d is inspect.Parameter.empty else d.default)
                elif name in query:
                    kwargs[name] = _conv(query[name], ann)
                elif isinstance(d, _Param):
                    if d.default is ...:
                        raise HTTPException(422, f"missing {name}")
                    kwargs[name] = d.default
                elif d is not inspect.Parameter.empty:
                    kwargs[name] = d
                else:
                    raise HTTPException(422, f"missing {name}")
            out = fn(**kwargs)
            return await out if inspect.isawaitable(out) else out
        raise HTTPException(405 if allowed else 404, f"Not available in the phone version: {method} {path}")
