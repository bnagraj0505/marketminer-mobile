"""Minimal stand-in for FastAPI on the phone: the Signal Lab modules only need HTTPException."""


class HTTPException(Exception):
    def __init__(self, status_code: int = 500, detail: str = ""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail
