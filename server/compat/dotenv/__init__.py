import os


def load_dotenv(path=None, override=False, **_):
    path = path or ".env"
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return False
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if override or k not in os.environ:
            os.environ[k] = v
    return True
