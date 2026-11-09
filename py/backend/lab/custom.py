"""Strategy workbench: your own rule-based strategies, built from the same market fields the ML model uses.

A custom strategy is data (stored in the Lab setting "custom_strategies"), never code:
  {"key": "custom_<slug>", "name", "description", "direction": "long" | "short" | "both",
   "conditions": [{"field": "rsi_dir", "op": ">", "value": 5}, {"field": "regime", "op": "in", "value": ["weak_bull"]}, ...],
   "stop": {"type": "swing" | "atr", "atr_mult": 1.5}, "stop_buffer_atr": 0.1, "min_rr": 1.5,
   "regimes": ["weak_bull", ...]   # regimes it suits (the agent declines it elsewhere)
   "status": "draft" | "approved"}
Every field is measured "in the trade's direction" (see mlfeat.DESCRIBE), so one rule set works for longs and shorts:
"ema50_dir > 0" means price is on the trade's side of EMA50. A missing value (e.g. volume on an index) fails the rule.
It then goes through the normal Lab pipeline: build_setup (stop buffer, targets from liquidity, minimum reward:risk),
scoring, backtests, the ML filter and the Risk Guardian. A draft is only backtested; the agent trades it only after you
approve it. Nothing here can change risk limits.
"""
from __future__ import annotations

import re

from . import mlfeat as MF
from . import regime as RG
from . import scoring as SC
from . import store
from . import strategies as ST

RULE_FIELDS = [f for f in MF.NUMERIC if f not in ("score", "rr", "rr_t1", "stop_atr")] + ["regime", "volatility", "market"]
OPS = (">", ">=", "<", "<=", "==", "!=", "between", "in", "not in")
_loaded = {"sig": None, "keys": []}


def _fields(F, i: int, d: int, ctx: dict) -> dict:
    cache = F.__dict__.setdefault("_wb_cache", {})
    k = (i, d)
    if k not in cache:
        ds = SC.directional(F, i, ctx)
        if len(cache) > 4000:
            cache.clear()
        cache[k] = MF.features(F, i, ctx, ds, {"direction": "LONG" if d > 0 else "SHORT", "strategy": "custom"})
    return cache[k]


def check(cond: dict, v) -> bool:
    op, x = cond.get("op"), cond.get("value")
    if v is None:
        return False
    try:
        if op == "in":
            return v in (x or [])
        if op == "not in":
            return v not in (x or [])
        if op == "==":
            return v == x if isinstance(v, str) else float(v) == float(x)
        if op == "!=":
            return v != x if isinstance(v, str) else float(v) != float(x)
        if op == "between":
            lo, hi = float(x[0]), float(x[1])
            return lo <= float(v) <= hi
        v, x = float(v), float(x)
        return {">": v > x, ">=": v >= x, "<": v < x, "<=": v <= x}[op]
    except (TypeError, ValueError, KeyError, IndexError):
        return False


def make_fn(spec: dict):
    def fn(F, i, d, ctx):
        want = spec.get("direction", "both")
        if (want == "long" and d < 0) or (want == "short" and d > 0):
            return None
        f = _fields(F, i, d, ctx)
        for c in spec.get("conditions") or []:
            if not check(c, f.get(c.get("field"))):
                return None
        st = spec.get("stop") or {"type": "swing"}
        at = F.atr[i]
        if not at:
            return None
        cl = F.c[i]
        if st.get("type") == "atr":
            anchor = cl - d * float(st.get("atr_mult", 1.5)) * at
        else:
            sw = F.swings_at(F.t[i], "low" if d > 0 else "high", True)
            sw = [x for x in sw if (x.price < cl if d > 0 else x.price > cl)]
            if not sw:
                return None
            anchor = sw[-1].price
        rs = [f"{c['field']} {c['op']} {c.get('value')} (now {f.get(c['field']) if not isinstance(f.get(c['field']), float) else round(f.get(c['field']), 2)})"
              for c in spec.get("conditions") or []]
        return {"anchor": anchor, "reasons": [f"Your rule '{spec.get('name')}'"] + rs}
    return fn


def validate(spec: dict) -> dict:
    """Clean a submitted strategy, or raise ValueError with the reason."""
    name = str(spec.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("give the strategy a name")
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or "rule"
    key = spec.get("key") if str(spec.get("key") or "").startswith("custom_") else f"custom_{slug}"
    if key in ST.META and not key.startswith("custom_"):
        raise ValueError("that name clashes with a built-in strategy")
    conds = []
    for c in spec.get("conditions") or []:
        f, op = c.get("field"), c.get("op")
        if f not in RULE_FIELDS:
            raise ValueError(f"unknown field {f!r}")
        if op not in OPS:
            raise ValueError(f"unknown operator {op!r}")
        v = c.get("value")
        if op in ("in", "not in"):
            v = [str(x) for x in (v if isinstance(v, list) else str(v).split(","))if str(x).strip()]
            v = [x.strip() for x in v]
        elif op == "between":
            if not isinstance(v, list) or len(v) != 2:
                raise ValueError(f"{f}: 'between' needs two numbers")
            v = [float(v[0]), float(v[1])]
        elif f in ("regime", "volatility", "market"):
            v = str(v)
        else:
            v = float(v)
        conds.append({"field": f, "op": op, "value": v})
    if not conds:
        raise ValueError("add at least one condition")
    st = spec.get("stop") or {}
    stop = {"type": "atr" if st.get("type") == "atr" else "swing", "atr_mult": min(4.0, max(0.3, float(st.get("atr_mult") or 1.5)))}
    regs = [r for r in (spec.get("regimes") or []) if r in RG.SUITS]
    return {"key": key, "name": name, "description": str(spec.get("description") or "")[:300],
            "direction": spec.get("direction") if spec.get("direction") in ("long", "short", "both") else "both",
            "conditions": conds, "stop": stop, "stop_buffer_atr": min(1.0, max(0.0, float(spec.get("stop_buffer_atr", 0.1)))),
            "min_rr": min(5.0, max(1.0, float(spec.get("min_rr", 1.5)))), "regimes": regs,
            "status": "approved" if spec.get("status") == "approved" else "draft"}


def all_specs() -> list[dict]:
    return store.get_setting("custom_strategies") or []


def save_all(specs: list[dict]) -> None:
    store.set_setting("custom_strategies", specs)
    ensure(force=True)


def ensure(force: bool = False) -> list[str]:
    """Register the stored custom strategies with the Lab (idempotent; cheap when nothing changed)."""
    try:
        specs = all_specs()
    except Exception:                                     # noqa: BLE001 - no database yet
        specs = []
    sig = repr(specs)
    if not force and sig == _loaded["sig"]:
        return _loaded["keys"]
    for k in _loaded["keys"]:                             # unregister the previous set
        ST.MODULES.pop(k, None)
        ST.META.pop(k, None)
        ST.PARAMS.pop(k, None)
        for lst in RG.SUITS.values():
            while k in lst:
                lst.remove(k)
    keys = []
    for sp in specs:
        k = sp["key"]
        ST.MODULES[k] = make_fn(sp)
        conds = "; ".join(f"{c['field']} {c['op']} {c['value']}" for c in sp["conditions"])
        ST.META[k] = (f"★ {sp['name']}", (sp.get("description") or "Your rule") + f" [{conds}]")
        ST.PARAMS[k] = {"enabled": sp.get("status") == "approved", "stop_buffer_atr": sp.get("stop_buffer_atr", 0.1),
                        "min_rr": sp.get("min_rr", 1.5), "min_avg_volume": 0, "custom": True}
        for r in sp.get("regimes") or []:
            if r in RG.SUITS and k not in RG.SUITS[r]:
                RG.SUITS[r].append(k)
        keys.append(k)
    _loaded.update(sig=sig, keys=keys)
    return keys
