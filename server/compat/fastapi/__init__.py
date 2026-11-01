"""Tiny stand-in for FastAPI, used only when the real package is not installed (e.g. on Termux).
The bot never serves HTTP: the MarketMiner engine just needs these names to import."""
from __future__ import annotations


class HTTPException(Exception):
    def __init__(self, status_code: int = 500, detail: str = "", headers=None):
        super().__init__(detail)
        self.status_code, self.detail, self.headers = status_code, detail, headers


class _Router:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):                     # get/post/put/delete/api_route/... -> no-op decorators
        def deco(*a, **k):
            return lambda f: f
        return deco

    def include_router(self, *a, **k):
        pass


APIRouter = FastAPI = _Router


class Request:
    pass


def Query(default=None, *a, **k):                    # noqa: N802 - FastAPI's own spelling
    return None if default is ... else default


Body = Path = Header = Depends = Form = Query
