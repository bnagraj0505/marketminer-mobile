"""Machine-learning layer: learns which Signal Lab setups tend to reach lot 1's 1:2 target before the stop.

WHAT THE MODEL MAY DO (and nothing else)
  filter     skip a setup whose calibrated probability is below the skip threshold        -> Guardian check "ML filter"
  rank       order the eligible setups on one candle by probability (best first)
  size down  a "half" setup trades at HALF the per-trade risk (crypto / cash shares); F&O and options are always your
             2 lots, which is already the minimum, so there it trades at 2 lots
  budget     when the part's risk budget has room for only one more trade, that room is kept for a "full" setup
It can never add a trade the rules did not find, raise size, loosen a Guardian check or change any risk setting
(enforced in guardian.py; tested in tests/test_ml.py).

MODES  off | shadow (predicts and records, changes nothing) | active (needs a model YOU approved)

DATA   ml_samples: setups from Signal Lab history (same Engine, same features as live: lab/mlfeat.py) labelled by
       mlfeat.label, plus the agent's own live decisions once their outcome is known (source "live").
TRAINING (HistGradientBoosting, small and regularised; scikit-learn)
  * time-ordered 5 blocks; walk-forward: train on blocks before k (minus a 1-day embargo), predict block k (k = 1..4)
  * probabilities calibrated (isotonic) on blocks 1-3's out-of-sample predictions; thresholds chosen on blocks 1-3
  * block 4 = untouched held-out test, used once for the promotion gates
PROMOTION GATES (all on the held-out block): >= 400 samples, >= 80 held-out; mean walk-forward AUC >= 0.55 and
  held-out AUC >= 0.53; held-out Brier better than the base rate; filtered expectancy > 0 and >= unfiltered + 0.05R;
  keeps >= 25% of setups. A model that passes is ELIGIBLE and still waits for your Approve. Nothing is auto-activated.
DRIFT  once active: over the last 40 resolved live predictions, if the Brier score is worse than predicting the base
  rate, the mode drops back to shadow (a step toward caution, logged).
"""
from __future__ import annotations

import asyncio
import json
import math
import pickle
import time
from pathlib import Path

from backend.lab import costs as C
from backend.lab import data as D
from backend.lab import mlfeat as MF
from backend.lab import scoring as SC
from backend.lab import store
from backend.lab import strategies as ST
from backend.lab.candle import TF_SECONDS
from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, tf TEXT, strategy TEXT, side TEXT, t INTEGER,
  features TEXT, y INTEGER, r REAL, source TEXT, UNIQUE(symbol, tf, strategy, side, t, source));
CREATE TABLE IF NOT EXISTS ml_models (id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER, tf TEXT, status TEXT, metrics TEXT, note TEXT,
  approved_at INTEGER);
CREATE TABLE IF NOT EXISTS ml_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER, decision_id INTEGER, symbol TEXT, tf TEXT,
  strategy TEXT, side TEXT, bar_time INTEGER, entry REAL, stop REAL, atr REAL, p REAL, action TEXT, mode TEXT, model_id INTEGER,
  features TEXT, y INTEGER, r REAL, resolved_at INTEGER, UNIQUE(symbol, tf, strategy, side, bar_time));
"""
WARMUP = 210
MIN_SAMPLES, MIN_HOLDOUT = 400, 80
EMBARGO = 86400
DEFAULT_CFG = {"mode": "off", "model_id": None}
_ready = {"db": None}
_bundle: dict = {}
STATE = {"training": False, "last": None, "progress": None}


def _init() -> None:
    if _ready["db"] == str(store.DB):
        return
    db.init()
    with store._lock, store.conn() as c:
        c.executescript(SCHEMA)
    _ready["db"] = str(store.DB)


def q(sql, args=(), one=False):
    _init()
    return db.q(sql, args, one)


def ex(sql, args=()):
    _init()
    return db.execute(sql, args)


def available() -> tuple[bool, str]:
    try:
        import sklearn  # noqa: F401
        import numpy  # noqa: F401
        return True, f"scikit-learn {sklearn.__version__}"
    except Exception as exc:                              # noqa: BLE001
        return False, f"scikit-learn not installed ({type(exc).__name__}); restart launch_marketminer.cmd to install it"


def cfg() -> dict:
    return {**DEFAULT_CFG, **(store.get_setting("agent_ml") or {})}


def set_mode(mode: str, who: str = "user") -> dict:
    if mode not in ("off", "shadow", "active"):
        raise ValueError("mode must be off, shadow or active")
    c = cfg()
    if mode == "active":
        m = q("SELECT * FROM ml_models WHERE status='approved' ORDER BY approved_at DESC LIMIT 1", one=True)
        if not m:
            raise PermissionError("No approved model: train one, check its held-out results and press Approve first")
        c["model_id"] = m["id"]
    elif mode == "shadow" and not c.get("model_id"):
        m = q("SELECT id FROM ml_models WHERE status IN ('approved','eligible','candidate') ORDER BY id DESC LIMIT 1", one=True)
        if not m:
            raise PermissionError("No model yet: press Train first")
        c["model_id"] = m["id"]
    new = {**c, "mode": mode}
    db.config_change(who, "agent_ml", c, new)
    store.set_setting("agent_ml", new)
    db.log("learning", f"ML mode set to {mode} by {who}" + (f" (model #{new['model_id']})" if new.get("model_id") else ""))
    return new


def _dir() -> Path:
    p = Path(store.DB).parent / "ml"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- data set
def samples_from_engine(E, symbol: str, settings: dict | None = None, cost_cfg: dict | None = None, min_score: float = 45) -> list[dict]:
    """Every regime-suited Lab setup in this history with its features and label (one open idea per strategy at a time,
    as the backtester does)."""
    F = E.F
    st = settings or {}
    out, busy = [], {}
    n = len(F.bars)
    for i in range(WARMUP, n - 1):
        if not ST.in_session(F, i, E.market):
            continue
        ctx = None
        for key, fn in ST.MODULES.items():
            p = {**ST.PARAMS[key], **(st.get(key) or {})}
            if key == "options_momentum" or not (p.get("enabled", True) or p.get("custom")) or busy.get(key, -1) >= i:
                continue
            ctx = ctx or E.ctx(i)
            if key not in ctx["suits"]:
                continue
            for d in (1, -1):
                try:
                    raw = fn(F, i, d, ctx)
                except (IndexError, ZeroDivisionError, TypeError):
                    raw = None
                if not raw:
                    continue
                s, _ = ST.build_setup(key, raw, F, i, d, p, E.market)
                if not s:
                    continue
                s["regime_ok"] = True
                ds = SC.directional(F, i, ctx)
                s.update(SC.setup_score(s, ds, ctx))
                if s["score"] < min_score:
                    continue
                lab = MF.label(E, i, s, cost_cfg)
                if not lab:
                    continue
                out.append({"symbol": symbol, "tf": E.tf, "strategy": key, "side": s["direction"], "t": F.t[i],
                            "features": MF.features(F, i, ctx, ds, s), "y": lab["y"], "r": lab["r"]})
                busy[key] = i + lab["bars"]
                break
    return out


def store_samples(rows: list[dict], source: str, replace_for: tuple | None = None) -> int:
    if replace_for:
        ex("DELETE FROM ml_samples WHERE symbol=? AND tf=? AND source=?", (*replace_for, source))
    n = 0
    for r in rows:
        try:
            ex("INSERT OR REPLACE INTO ml_samples(symbol, tf, strategy, side, t, features, y, r, source) VALUES(?,?,?,?,?,?,?,?,?)",
               (r["symbol"], r["tf"], r["strategy"], r["side"], r["t"], json.dumps(r["features"]), int(r["y"]), float(r["r"]), source))
            n += 1
        except Exception:                                 # noqa: BLE001
            pass
    return n


def training_symbols() -> list[str]:
    from . import config as CFG
    from . import options as OP
    c = CFG.agent()
    o = c.get("options") or {}
    syms = list(c.get("symbols") or [])
    if o.get("enabled"):
        syms += [OP.spot_symbol(u) for u in (o.get("index") or []) + (o.get("stocks") or [])]
    seen, out = set(), []
    for s in syms:
        if s not in seen and D.market_of(s) not in ("YF", "DELTA"):
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------- model
def _vocab(rows: list[dict]) -> dict:
    v = {}
    for c in MF.CATEGORICAL:
        vals = sorted({str(r["features"].get(c)) for r in rows if r["features"].get(c) is not None})
        v[c] = {x: k for k, x in enumerate(vals[:250])}
    return v


def _matrix(feats: list[dict], vocab: dict):
    import numpy as np
    X = np.full((len(feats), len(MF.FIELDS)), np.nan)
    for i, f in enumerate(feats):
        for j, k in enumerate(MF.NUMERIC):
            x = f.get(k)
            if x is not None:
                X[i, j] = float(x)
        for j, k in enumerate(MF.CATEGORICAL):
            code = vocab[k].get(str(f.get(k)))
            if code is not None:
                X[i, len(MF.NUMERIC) + j] = code
    return X


class Model:
    """HistGradientBoosting on the columns that have data (a feature with no values at all, e.g. volume for indices,
    is left out rather than imputed)."""

    def __init__(self):
        self.cols, self.m = None, None

    def fit(self, X, y):
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingClassifier
        self.cols = [j for j in range(X.shape[1]) if np.isfinite(X[:, j]).any() and np.nanstd(X[:, j]) > 0]
        mask = [j >= len(MF.NUMERIC) for j in self.cols]
        self.m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=3, min_samples_leaf=40, l2_regularization=1.0,
                                                early_stopping=False, categorical_features=mask, random_state=7)
        self.m.fit(X[:, self.cols], y)
        self.classes_ = self.m.classes_
        return self

    def predict_proba(self, X):
        return self.m.predict_proba(X[:, self.cols])


def _model():
    return Model()


def _auc(y, p):
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p)) if len(set(y)) == 2 else None


def _brier(y, p) -> float:
    return float(sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / len(y)) if len(y) else float("nan")


def _mean(x) -> float | None:
    x = list(x)
    return float(sum(x) / len(x)) if x else None


def choose_thresholds(p: list[float], r: list[float]) -> dict:
    """Skip threshold maximising expectancy of the kept setups while keeping >= 40% of them; 'full' = median of the kept."""
    import numpy as np
    pa, ra = np.asarray(p), np.asarray(r)
    best = (float(np.mean(ra)), 0.0)
    for qn in range(0, 61, 5):
        tau = float(np.quantile(pa, qn / 100)) if qn else 0.0
        keep = pa >= tau
        if keep.mean() < 0.4 or keep.sum() < 20:
            continue
        e = float(ra[keep].mean())
        if e > best[0] + 1e-9:
            best = (e, tau)
    tau = best[1]
    full = float(np.median(pa[pa >= tau])) if (pa >= tau).any() else tau
    return {"skip_below": round(tau, 4), "full_from": round(max(full, tau), 4)}


def _action(p: float, th: dict) -> str:
    return "skip" if p < th["skip_below"] else "half" if p < th["full_from"] else "full"


def _perm_importance(model, X, y, repeats: int = 5, seed: int = 7) -> list[dict]:
    """Drop in held-out AUC when one feature's values are shuffled (bigger = the model leans on it more)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    base = _auc(y, model.predict_proba(X)[:, 1]) or 0.5
    out = []
    for j, f in enumerate(MF.FIELDS):
        if not np.isfinite(X[:, j]).any():
            out.append({"feature": f, "importance": 0.0})
            continue
        drops = []
        for _ in range(repeats):
            Xp = X.copy()
            Xp[:, j] = rng.permutation(Xp[:, j])
            drops.append(base - (_auc(y, model.predict_proba(Xp)[:, 1]) or 0.5))
        out.append({"feature": f, "importance": round(float(np.mean(drops)), 4)})
    return sorted(out, key=lambda x: -x["importance"])


def train_rows(rows: list[dict], tf: str) -> dict:
    """Walk-forward training + gates. Returns the metrics; saves a model row and file when there is enough data."""
    import numpy as np
    from sklearn.isotonic import IsotonicRegression
    rows = sorted(rows, key=lambda r: r["t"])
    n = len(rows)
    if n < MIN_SAMPLES:
        return {"ok": False, "error": f"only {n} labelled setups for {tf}; at least {MIN_SAMPLES} are needed (add symbols or history)"}
    vocab = _vocab(rows)
    X = _matrix([r["features"] for r in rows], vocab)
    y = np.array([int(r["y"]) for r in rows])
    rr = np.array([float(r["r"]) for r in rows])
    t = np.array([int(r["t"]) for r in rows])
    edges = [int(n * k / 5) for k in range(6)]
    oos = np.full(n, np.nan)
    folds = []
    last_model = None
    for k in range(1, 5):
        a, b = edges[k], edges[k + 1]
        tr = np.arange(0, a)
        tr = tr[t[tr] < t[a] - EMBARGO]
        if len(tr) < 100 or len(set(y[tr])) < 2:
            folds.append({"fold": k, "error": "not enough training data before this block"})
            continue
        m = _model().fit(X[tr], y[tr])
        oos[a:b] = m.predict_proba(X[a:b])[:, 1]
        folds.append({"fold": k, "train": int(len(tr)), "test": int(b - a), "auc": _auc(y[a:b], oos[a:b]), "base_rate": float(y[a:b].mean())})
        last_model = m
    cal_idx = np.arange(edges[1], edges[4])
    hold = np.arange(edges[4], edges[5])
    ok_cal = cal_idx[~np.isnan(oos[cal_idx])]
    if len(ok_cal) < 100 or np.isnan(oos[hold]).any():
        return {"ok": False, "error": "walk-forward could not produce enough out-of-sample predictions", "folds": folds}
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(oos[ok_cal], y[ok_cal])
    th = choose_thresholds(list(iso.predict(oos[ok_cal])), list(rr[ok_cal]))
    ph = iso.predict(oos[hold])
    yh, rh = y[hold], rr[hold]
    base = float(y[:edges[4]].mean())
    acts = [_action(p, th) for p in ph]
    kept = np.array([a != "skip" for a in acts])
    wts = np.array([0 if a == "skip" else 0.5 if a == "half" else 1.0 for a in acts])
    held = {"n": int(len(hold)), "auc": _auc(yh, ph), "brier": _brier(yh, ph), "brier_base": _brier(yh, [base] * len(yh)),
            "base_rate_train": round(base, 4), "hit_rate": float(yh.mean()), "expectancy_all": float(rh.mean()),
            "expectancy_kept": float(rh[kept].mean()) if kept.any() else None, "kept_share": float(kept.mean()),
            "expectancy_sized": float((rh * wts).sum() / wts.sum()) if wts.sum() else None,
            "hit_rate_kept": float(yh[kept].mean()) if kept.any() else None,
            "by_action": {a: {"n": int(sum(1 for x in acts if x == a)), "expectancy": _mean(rh[i] for i, x in enumerate(acts) if x == a),
                              "hit_rate": _mean(yh[i] for i, x in enumerate(acts) if x == a)} for a in ("skip", "half", "full")}}
    calib = []
    for lo in (0, 0.2, 0.3, 0.4, 0.5, 0.6):
        hi = {0: 0.2, 0.2: 0.3, 0.3: 0.4, 0.4: 0.5, 0.5: 0.6, 0.6: 1.01}[lo]
        sel = (ph >= lo) & (ph < hi)
        if sel.sum():
            calib.append({"bin": f"{lo:.1f}-{min(hi, 1):.1f}", "n": int(sel.sum()), "predicted": float(ph[sel].mean()), "actual": float(yh[sel].mean())})
    aucs = [f["auc"] for f in folds if f.get("auc") is not None]
    gates = []

    def gate(cond, text):
        gates.append({"ok": bool(cond), "text": text})
    gate(n >= MIN_SAMPLES and len(hold) >= MIN_HOLDOUT, f"{n} samples (>= {MIN_SAMPLES}), held-out {len(hold)} (>= {MIN_HOLDOUT})")
    gate(aucs and _mean(aucs) >= 0.55, f"mean walk-forward AUC {(_mean(aucs) or 0):.3f} >= 0.55 (0.5 = coin flip)")
    gate(held["auc"] is not None and held["auc"] >= 0.53, f"held-out AUC {held['auc'] or 0:.3f} >= 0.53")
    gate(held["brier"] < held["brier_base"], f"held-out Brier {held['brier']:.4f} < base-rate Brier {held['brier_base']:.4f}")
    ek = held["expectancy_kept"]
    gate(ek is not None and ek > 0 and ek >= held["expectancy_all"] + 0.05,
         f"held-out expectancy of kept setups {ek if ek is None else round(ek, 3)}R > 0 and >= all setups {held['expectancy_all']:.3f}R + 0.05")
    gate(held["kept_share"] >= 0.25, f"keeps {held['kept_share'] * 100:.0f}% of setups (>= 25%)")
    eligible = all(g["ok"] for g in gates)
    # importance on the held-out block (permutation, AUC), overall and per regime
    imp, by_regime = [], {}
    try:
        if last_model is not None and held["auc"] is not None:
            imp = [{**x, "describe": MF.DESCRIBE.get(x["feature"], x["feature"])} for x in _perm_importance(last_model, X[hold], yh, 5)]
            regs = [str(rows[i]["features"].get("regime")) for i in hold]
            for rg in sorted(set(regs)):
                idx = np.array([j for j, x in enumerate(regs) if x == rg])
                if len(idx) >= 60 and len(set(yh[idx])) == 2:
                    by_regime[rg] = {"n": int(len(idx)), "top": _perm_importance(last_model, X[hold][idx], yh[idx], 3)[:6]}
    except Exception as exc:                              # noqa: BLE001
        imp = [{"feature": "n/a", "importance": 0, "describe": f"importance unavailable: {exc}"}]
    by_strategy = {}
    for i in hold:
        sk = rows[i]["strategy"]
        b = by_strategy.setdefault(sk, {"n": 0, "hits": 0, "r": 0.0})
        b["n"] += 1
        b["hits"] += int(y[i])
        b["r"] += float(rr[i])
    # final model on everything; calibrator on all walk-forward out-of-sample predictions
    final = _model().fit(X, y)
    ok_all = ~np.isnan(oos)
    iso_all = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(oos[ok_all], y[ok_all])
    metrics = {"tf": tf, "samples": n, "positives": int(y.sum()), "base_rate": float(y.mean()), "from": int(t[0]), "to": int(t[-1]),
               "symbols": sorted({r["symbol"] for r in rows}), "sources": {s: sum(1 for r in rows if r.get("source") == s) for s in ("backtest", "live")},
               "folds": folds, "held_out": held, "calibration": calib, "thresholds": th, "gates": gates, "eligible": eligible,
               "importance": imp[:15], "importance_by_regime": by_regime,
               "held_out_by_strategy": {k: {"n": v["n"], "hit_rate": round(v["hits"] / v["n"], 3), "expectancy": round(v["r"] / v["n"], 3)} for k, v in by_strategy.items()},
               "label": "reaches 2R (lot 1's 1:2) before the stop, entry at the next open, after slippage"}
    mid = ex("INSERT INTO ml_models(t, tf, status, metrics, note) VALUES(?,?,?,?,?)",
             (db.now(), tf, "eligible" if eligible else "rejected", json.dumps(metrics, default=float), "walk-forward HistGradientBoosting"))
    with open(_dir() / f"model_{mid}.pkl", "wb") as fh:
        pickle.dump({"model": final, "calibrator": iso_all, "vocab": vocab, "thresholds": th, "fields": MF.FIELDS, "tf": tf,
                     "base_rate": float(y.mean())}, fh)
    return {"ok": True, "model_id": mid, **metrics}


async def train(symbols: list[str] | None = None, tf: str | None = None, bars: int = 3000) -> dict:
    """Rebuild the Lab history samples for these symbols, then train on all samples of the timeframe."""
    from . import config as CFG
    from . import research as RS
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why}
    if STATE["training"]:
        return {"ok": False, "error": "training is already running"}
    tf = tf or CFG.agent()["tf"]
    symbols = symbols or training_symbols()
    STATE.update(training=True, progress=f"0/{len(symbols)} symbols")
    errors = {}
    try:
        cost_cfg = {**C.DEFAULTS, **(store.get_setting("costs") or {})}
        settings = store.get_setting("strategies") or {}
        for k, sym in enumerate(symbols):
            STATE["progress"] = f"{k}/{len(symbols)} symbols · {sym}"
            try:
                E, _ = await RS._engine(sym, tf, bars)
                rows = await asyncio.to_thread(samples_from_engine, E, sym, settings, cost_cfg)
                store_samples(rows, "backtest", (sym, tf))
            except Exception as exc:                      # noqa: BLE001
                errors[sym] = str(getattr(exc, "detail", exc))[:160]
            await asyncio.sleep(0.5)
        rows = [{**r, "features": json.loads(r["features"])} for r in q("SELECT * FROM ml_samples WHERE tf=?", (tf,))]
        STATE["progress"] = f"training on {len(rows)} setups"
        res = await asyncio.to_thread(train_rows, rows, tf)
        res["data_errors"] = errors
        db.log("learning", f"ML training {tf}: " + (f"model #{res['model_id']} {'ELIGIBLE (waiting for your approval)' if res.get('eligible') else 'did not pass the gates'}"
                                                    if res.get("ok") else res.get("error", "failed")), data={"errors": errors})
        if res.get("ok") and cfg()["mode"] == "off":
            store.set_setting("agent_ml", {**cfg(), "mode": "shadow", "model_id": res["model_id"]})   # shadow changes nothing
        elif res.get("ok") and cfg()["mode"] == "shadow":
            store.set_setting("agent_ml", {**cfg(), "model_id": res["model_id"]})
        STATE["last"] = {"t": db.now(), "ok": res.get("ok"), "model_id": res.get("model_id"), "error": res.get("error")}
        return res
    finally:
        STATE.update(training=False, progress=None)


def approve(model_id: int, who: str = "user") -> dict:
    m = q("SELECT * FROM ml_models WHERE id=?", (model_id,), one=True)
    if not m:
        raise ValueError("no such model")
    if m["status"] != "eligible":
        raise PermissionError(f"model #{model_id} is {m['status']}: only a model that passed every held-out gate can be approved")
    ex("UPDATE ml_models SET status='retired' WHERE status='approved'")
    ex("UPDATE ml_models SET status='approved', approved_at=? WHERE id=?", (db.now(), model_id))
    db.config_change(who, "ml_model", None, model_id)
    db.log("learning", f"ML model #{model_id} approved by {who}")
    c = cfg()
    if c["mode"] == "active":
        store.set_setting("agent_ml", {**c, "model_id": model_id})
    return {"ok": True, "model_id": model_id, "mode": cfg()["mode"]}


def reject(model_id: int) -> dict:
    ex("UPDATE ml_models SET status='rejected_by_user' WHERE id=? AND status IN ('eligible','approved')", (model_id,))
    c = cfg()
    if c.get("model_id") == model_id and c["mode"] == "active":
        store.set_setting("agent_ml", {**c, "mode": "shadow"})
    return {"ok": True}


def _load(model_id: int):
    if model_id in _bundle:
        return _bundle[model_id]
    p = _dir() / f"model_{model_id}.pkl"
    if not p.exists():
        return None
    with open(p, "rb") as fh:
        b = pickle.load(fh)
    _bundle.clear()
    _bundle[model_id] = b
    return b


def predict(features: dict | None, model_id: int | None = None) -> dict | None:
    """Calibrated probability and action for one setup's features, or None (no model / no features / no library)."""
    if not features:
        return None
    c = cfg()
    mid = model_id or c.get("model_id")
    if not mid or c["mode"] == "off":
        return None
    try:
        b = _load(mid)
        if not b:
            return None
        X = _matrix([features], b["vocab"])
        raw = float(b["model"].predict_proba(X)[0, 1])
        p = float(b["calibrator"].predict([raw])[0])
    except Exception as exc:                              # noqa: BLE001 - a model problem must never stop the agent
        db.log("learning", f"ML prediction failed: {str(exc)[:160]}", level="warn")
        return None
    th = b["thresholds"]
    return {"p": round(p, 4), "action": _action(p, th), "thresholds": th, "model_id": mid, "mode": c["mode"], "base_rate": round(b["base_rate"], 4)}


def rank(eligible: list[dict]) -> tuple[list[dict], dict]:
    """Predictions for every eligible setup; in ACTIVE mode they are re-ordered by probability (the set is unchanged)."""
    preds = {}
    for k, s in enumerate(eligible):
        preds[k] = predict(s.get("ml_features"))
    if cfg()["mode"] == "active" and any(preds.values()):
        order = sorted(range(len(eligible)), key=lambda k: (-(preds[k] or {}).get("p", -1), -eligible[k].get("score", 0)))
        return [{**eligible[k], "_ml": preds[k]} for k in order], {"ranked": True}
    return [{**s, "_ml": preds[k]} for k, s in enumerate(eligible)], {"ranked": False}


def record(decision_id: int | None, symbol: str, tf: str, s: dict, pred: dict | None) -> None:
    if not pred or not s.get("ml_features"):
        return
    try:
        ex("INSERT OR IGNORE INTO ml_predictions(t, decision_id, symbol, tf, strategy, side, bar_time, entry, stop, atr, p, action, mode, model_id, features) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (db.now(), decision_id, symbol, tf, s["strategy"], s["direction"], s["time"], s.get("entry"), s.get("stop"), s.get("atr"),
            pred["p"], pred["action"], pred["mode"], pred["model_id"], json.dumps(s["ml_features"])))
    except Exception:                                     # noqa: BLE001
        pass


def label_bars(bars, bar_time: int, step: int, side: str, stop: float, market: str, k_r: float = 2.0, max_bars: int = 96) -> dict | None:
    """Live outcome with the same rule as mlfeat.label, from the candles after the signal candle."""
    after = [b for b in bars if b.time > bar_time]
    if not after:
        return None
    d = 1 if side == "LONG" else -1
    entry = C.slip_price(after[0].open, d, market)
    if (entry - stop) * d <= 0:
        return {"y": 0, "r": 0.0, "exit": "opened through the stop", "skip": True}
    slip_out = abs(C.slip_price(stop, -d, market) - stop)
    risk = (entry - stop) * d + slip_out
    tgt = entry + d * (k_r * risk + slip_out)
    india = market in MF.INDIA
    import datetime as dt
    day = lambda t: dt.datetime.fromtimestamp(t, MF.IST).date()
    for j, b in enumerate(after):
        if (b.low <= stop) if d > 0 else (b.high >= stop):
            return {"y": 0, "r": -1.0, "exit": "stop"}
        if (b.high >= tgt) if d > 0 else (b.low <= tgt):
            return {"y": 1, "r": k_r, "exit": "target"}
        last_of_day = india and j + 1 < len(after) and day(after[j + 1].time) != day(b.time)
        if j + 1 >= max_bars or last_of_day:
            r = (C.slip_price(b.close, -d, market) - entry) * d / risk
            return {"y": 0, "r": max(-1.0, min(k_r, r)), "exit": "time / session end"}
    return None


async def resolve_live(limit: int = 80) -> int:
    rows = q("SELECT * FROM ml_predictions WHERE y IS NULL AND t > ? ORDER BY id LIMIT ?", (db.now() - 10 * 86400, limit))
    cache, n = {}, 0
    for r in rows:
        k = (r["symbol"], r["tf"])
        if k not in cache:
            try:
                cache[k] = (await D.candles(r["symbol"], r["tf"], 500))[0]
            except Exception:                             # noqa: BLE001
                cache[k] = None
        if not cache[k] or r["stop"] is None:
            continue
        lab = label_bars(cache[k], r["bar_time"], TF_SECONDS.get(r["tf"], 900), r["side"], r["stop"], D.market_of(r["symbol"]))
        if not lab:
            continue
        ex("UPDATE ml_predictions SET y=?, r=?, resolved_at=? WHERE id=?", (lab["y"], lab["r"], db.now(), r["id"]))
        if not lab.get("skip"):
            store_samples([{"symbol": r["symbol"], "tf": r["tf"], "strategy": r["strategy"], "side": r["side"], "t": r["bar_time"],
                            "features": json.loads(r["features"]), "y": lab["y"], "r": lab["r"]}], "live")
        n += 1
    if n:
        drift_check()
    return n


def drift_check(window: int = 40) -> dict:
    c = cfg()
    rows = q("SELECT p, y FROM ml_predictions WHERE y IS NOT NULL AND model_id=? ORDER BY resolved_at DESC LIMIT ?", (c.get("model_id") or -1, window))
    if len(rows) < window:
        return {"checked": False, "resolved": len(rows)}
    b = _load(c["model_id"]) or {}
    base = b.get("base_rate", _mean(r["y"] for r in rows))
    br, bb = _brier([r["y"] for r in rows], [r["p"] for r in rows]), _brier([r["y"] for r in rows], [base] * len(rows))
    out = {"checked": True, "brier": br, "brier_base": bb, "resolved": len(rows)}
    if c["mode"] == "active" and br > bb:
        store.set_setting("agent_ml", {**c, "mode": "shadow"})
        db.log("learning", f"ML drift: last {window} live predictions Brier {br:.4f} worse than the base rate {bb:.4f} -> back to SHADOW "
                           "(the rules trade without the model until you re-approve)", level="warn")
        out["reverted"] = True
    return out


def status() -> dict:
    ok, why = available()
    c = cfg()
    models = q("SELECT id, t, tf, status, metrics, approved_at FROM ml_models ORDER BY id DESC LIMIT 20")
    for m in models:
        m["metrics"] = db.jload(m["metrics"], {})
    counts = q("SELECT tf, source, COUNT(*) n, SUM(y) pos FROM ml_samples GROUP BY tf, source")
    preds = q("SELECT id, t, symbol, tf, strategy, side, bar_time, p, action, mode, model_id, y, r FROM ml_predictions ORDER BY id DESC LIMIT 60")
    res = [p for p in q("SELECT p, y, r, action, mode FROM ml_predictions WHERE y IS NOT NULL ORDER BY id DESC LIMIT 300")]
    live = {}
    if res:
        live = {"resolved": len(res), "hit_rate": _mean(p["y"] for p in res), "mean_p": _mean(p["p"] for p in res),
                "by_action": {a: {"n": len([p for p in res if p["action"] == a]), "hit_rate": _mean(p["y"] for p in res if p["action"] == a),
                                  "expectancy": _mean(p["r"] for p in res if p["action"] == a)} for a in ("skip", "half", "full")}}
    return {"library": {"ok": ok, "detail": why}, "config": c, "training": dict(STATE), "models": models, "samples": counts,
            "predictions": preds, "live": live, "drift": drift_check() if c.get("model_id") else None}
