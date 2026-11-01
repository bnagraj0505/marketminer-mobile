"""Which public crypto feeds are switched on (stored in .markets.json)."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = ROOT / ".markets.json"
DEFAULTS = {"BINANCE": True, "BINANCEF": True, "SHARK": True, "DELTA": True}


def _read() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def market_enabled(m: str) -> bool:
    return bool(_read().get(m, DEFAULTS.get(m, False)))


def set_enabled(m: str, on: bool) -> None:
    data = _read()
    data[m] = bool(on)
    STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
