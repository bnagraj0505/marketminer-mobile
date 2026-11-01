"""Placeholder: the phone fetches through the browser (see _bridge.py); nothing here makes network calls."""


class Timeout:
    def __init__(self, *a, **k):
        pass


class AsyncClient:
    def __init__(self, *a, **k):
        raise RuntimeError("direct HTTP is not available in the phone build")
