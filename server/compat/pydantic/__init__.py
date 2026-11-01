"""Tiny stand-in for Pydantic (only when the real package is missing): enough for class definitions."""


def Field(default=None, *a, **k):                    # noqa: N802
    if default is ...:
        return None
    return k["default_factory"]() if "default_factory" in k else default


class BaseModel:
    def __init__(self, **kw):
        for k in getattr(self, "__annotations__", {}):
            setattr(self, k, kw.get(k, getattr(type(self), k, None)))

    def model_dump(self, exclude_none: bool = False, **_):
        d = {k: getattr(self, k, None) for k in getattr(self, "__annotations__", {})}
        return {k: v for k, v in d.items() if v is not None} if exclude_none else d

    dict = model_dump
