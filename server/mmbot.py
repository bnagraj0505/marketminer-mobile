"""MarketMiner always-on paper bot + Telegram alerts and remote control.

Runs MarketMiner's own Signal Lab engine (the PC's backend/ code) on a small Linux box: a cloud VM or an Android phone
with Termux. PAPER ONLY: nothing here can place a real order.

  * Entries: confirmed signals only (score >= Signal Lab minimum, on a CLOSED candle, fresh = the last closed candle).
  * Alerts: setups forming / watch, entries, skipped entries, T1/T2 hits, exit warnings, opposite-signal exits, closes.
  * Telegram commands (only from your chat): /status /trades /pnl /pause /resume /symbols /add /remove /tf
    /login /code /pin /help.  /pin renews FYERS for the day; the message is deleted and the PIN is never stored.

Config: server/.env (see .env.example). State: server/.lab/ (SQLite ledger + bot memory).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE / "compat"))               # stand-ins for fastapi/pydantic/dotenv, used only if not installed

from dotenv import load_dotenv                      # noqa: E402

load_dotenv(str(HERE / ".env"))
os.environ.setdefault("FYERS_REDIRECT_URI", "https://bnagraj0505.github.io/marketminer-mobile/fyers.html")

import httpx                                         # noqa: E402
from fastapi import HTTPException                    # noqa: E402
from backend.lab import service as SV, data as D     # noqa: E402
from backend.brokers.fyers import client as FY       # noqa: E402

log = logging.getLogger("mmbot")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TFSEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
STATE_FILE = HERE / ".lab" / "bot_state.json"
INDIA = ("NSE", "FNO", "MCX")


def env_list(k: str, d: str) -> list[str]:
    return [x.strip().upper() for x in (os.getenv(k) or d).split(",") if x.strip()]


CFG = {
    "token": os.getenv("TELEGRAM_TOKEN", "").strip(),
    "chat": os.getenv("TELEGRAM_CHAT", "").strip(),
    "symbols": env_list("BOT_SYMBOLS", "SHARK:BTCUSDT,SHARK:ETHUSDT,NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX"),
    "tf": os.getenv("BOT_TF", "15m"),
    "every": max(30, int(os.getenv("BOT_EVERY", "60") or 60)),
    "watch_min": float(os.getenv("BOT_WATCH_MIN", "45") or 45),
    "exit_opposite": (os.getenv("BOT_EXIT_OPPOSITE", "1") or "1") not in ("0", "false", "no"),
    "reco": (os.getenv("BOT_ALERT_SETUPS", "1") or "1") not in ("0", "false", "no"),
}


# ---------------------------------------------------------------- state
def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("seen", {}); st.setdefault("trades", {}); st.setdefault("errs", {}); st.setdefault("offset", 0)
    st.setdefault("paused", False); st.setdefault("symbols", CFG["symbols"]); st.setdefault("tf", CFG["tf"])
    st.setdefault("notes", {})
    return st


ST = load_state()


def save_state() -> None:
    cut = time.time() - 3 * 86400
    ST["seen"] = {k: v for k, v in ST["seen"].items() if v >= cut}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(ST), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- Telegram
TG = None


def tg_url(m: str) -> str:
    return f"https://api.telegram.org/bot{CFG['token']}/{m}"


async def tg(text: str) -> None:
    if not (CFG["token"] and CFG["chat"]):
        log.info("TG (not set up): %s", text)
        return
    try:
        r = await TG.post(tg_url("sendMessage"), data={"chat_id": CFG["chat"], "text": text[:4000], "parse_mode": "HTML",
                                                         "disable_web_page_preview": "true"})
        j = r.json()
        if not j.get("ok"):
            log.warning("Telegram refused: %s", j.get("description"))
    except Exception as e:                            # noqa: BLE001 - alerts must never stop the bot
        log.warning("Telegram send failed: %s", e)


E = html.escape
short = lambda s: str(s).split(":")[-1]              # noqa: E731
arrow = lambda d: "▲" if d == "LONG" else "▼"        # noqa: E731


def f(x, d=None) -> str:
    if x is None:
        return "—"
    x = float(x)
    if d is None:
        d = 2 if abs(x) >= 1 else 6
    s = f"{x:,.{d}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def lv(s: dict) -> str:
    t = s["targets"]
    rr = f" · {float(s['rr']):.1f}R" if s.get("rr") else ""
    return f"Entry {f(s['entry'])} · SL {f(s['stop'])}\nT1 {f(t[0])} · T2 {f(t[1])} · T3 {f(t[2])}{rr}"


# ---------------------------------------------------------------- market hours (IST) so Indian symbols don't waste FYERS calls
def india_open(market: str, now: dt.datetime | None = None) -> bool:
    n = now or dt.datetime.now(IST)
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return (540 <= m <= 1415) if market == "MCX" else (555 <= m <= 935)


def fyers_ok() -> bool:
    try:
        return bool(FY.read_token())
    except Exception:                                 # noqa: BLE001
        return False


# ---------------------------------------------------------------- one scan
def bot_trade(sym: str):
    for tid, t in ST["trades"].items():
        if t["sym"] == sym and t["status"] in ("open", "closing"):
            return tid, t
    return None


async def scan() -> None:
    minS = SV.meta()["protection"]["min_score"]
    for sym in list(ST["symbols"]):
        m = D.market_of(sym)
        if m in INDIA:
            if not india_open(m):
                continue
            if not fyers_ok():
                day = dt.datetime.now(IST).strftime("%Y-%m-%d")
                if ST["notes"].get("fyers") != day:
                    ST["notes"]["fyers"] = day
                    await tg("🔑 <b>FYERS login needed</b>\nIndian symbols are skipped until you renew: send <code>/pin 1234</code> "
                             "(your FYERS PIN; the message is deleted and never stored).\nIf the 15-day login has ended, send /login.")
                continue
        try:
            d = await SV.analyze(sym, ST["tf"], 600, False, True)
        except Exception as e:                        # noqa: BLE001
            msg = getattr(e, "detail", None) or str(e)
            k, t = "err:" + sym, time.time()
            if t - ST["errs"].get(k, 0) > 6 * 3600:
                ST["errs"][k] = t
                await tg(f"⚠️ <b>{E(short(sym))}</b>: data unavailable, skipped until it returns.\n{E(str(msg)[:200])}")
            continue
        ST["errs"].pop("err:" + sym, None)
        await handle(sym, d, minS)
    await track()


async def handle(sym: str, d: dict, minS: float) -> None:
    tfs, t, setups = TFSEC.get(d["tf"], 900), time.time(), d.get("setups") or []
    confirmed = [s for s in setups if s.get("signal") and not s.get("provisional")
                 and (not s.get("locked") or ((s.get("hits") or 0) == 0 and s.get("status") != "closed"))
                 and s["time"] >= t - 2.5 * tfs]
    forming = sorted([s for s in setups if (not s.get("signal") or s.get("provisional")) and s["score"] >= CFG["watch_min"]],
                     key=lambda s: -s["score"])
    tag = f"{E(short(sym))} {d['tf']}"
    mine = bot_trade(sym)

    if mine and CFG["exit_opposite"]:                 # in-between exit on a confirmed opposite signal
        opp = next((s for s in confirmed if s["direction"] != mine[1]["dir"]), None)
        if opp:
            k = f"xo|{sym}|{opp['time']}"
            if k not in ST["seen"]:
                ST["seen"][k] = t
                r = await SV.paper_close(int(mine[0]))
                if r.get("ok"):
                    mine[1]["status"], mine[1]["reason"] = "closing", f"opposite {opp['direction']} signal"
                    await tg(f"🔻 <b>EXIT · opposite signal</b> #{mine[0]} · {tag}\n{arrow(mine[1]['dir'])} {mine[1]['dir']} closed at {f(r.get('exit'))}\n"
                             f"Reason: {arrow(opp['direction'])} {opp['direction']} confirmed · {E(opp['name'])} · score {opp['score']:.0f}")
    still = bot_trade(sym)
    if still and still[1]["status"] == "open":       # exit warning
        w = next((s for s in forming if s["direction"] != still[1]["dir"]), None)
        if w:
            k = f"xw|{sym}|{w['direction']}|{w['time']}"
            if k not in ST["seen"]:
                ST["seen"][k] = t
                await tg(f"⚠️ <b>Exit watch</b> #{still[0]} · {tag}\nOpen {arrow(still[1]['dir'])} {still[1]['dir']} · an opposite "
                         f"{arrow(w['direction'])} {w['direction']} setup is forming ({E(w['name'])}, score {w['score']:.0f}).\n"
                         + ("The bot closes the trade if it confirms on candle close." if CFG["exit_opposite"] else "Auto-exit on opposite signals is off."))
    for s in confirmed:                               # entries
        k = f"en|{sym}|{d['tf']}|{s['strategy']}|{s['direction']}|{s['time']}"
        if k in ST["seen"]:
            continue
        ST["seen"][k] = t
        cur = bot_trade(sym)
        if cur and cur[1]["dir"] == s["direction"]:
            continue
        try:
            r = await SV.paper_open({"symbol": d["symbol"], "tf": d["tf"], "strategy": s["strategy"], "direction": s["direction"],
                                     "entry": s["entry"], "stop": s["stop"], "targets": s["targets"], "score": s["score"]})
        except Exception as e:                        # noqa: BLE001
            r = {"ok": False, "blocks": [str(getattr(e, "detail", e))]}
        if r.get("ok"):
            ST["trades"][str(r["id"])] = {"sym": d["symbol"], "tf": d["tf"], "dir": s["direction"], "name": s["name"], "entry": r["fill"],
                                          "stop": s["stop"], "targets": s["targets"], "hit": [], "status": "open", "opened": t}
            await tg(f"✅ <b>PAPER ENTRY</b> #{r['id']} · {tag}\n{arrow(s['direction'])} <b>{s['direction']}</b> filled {f(r['fill'])} · qty {f(r['qty'])} · "
                     f"risk {f(r['risk'])} {E(r['currency'])}\n{lv(s)}\n{E(s['name'])} · score {s['score']:.0f}")
            break
        await tg(f"⛔ <b>Entry skipped</b> · {tag}\n{arrow(s['direction'])} {s['direction']} · {E(s['name'])} · score {s['score']:.0f}\n"
                 f"Why: {E(' · '.join(r.get('blocks') or []))}")
    if CFG["reco"]:                                    # recommendations
        best = next((s for s in forming if not (still and s["direction"] != still[1]["dir"])), None)
        if best:
            k = f"rc|{sym}|{d['tf']}|{best['direction']}|{best['time']}"
            if k not in ST["seen"]:
                ST["seen"][k] = t
                why = "live, candle still forming" if best.get("provisional") else f"watch: below the entry minimum {minS:g}"
                await tg(f"👀 <b>Setup</b> · {tag}\n{arrow(best['direction'])} <b>{best['direction']}</b> · {E(best['name'])} · score {best['score']:.0f} ({why})\n"
                         f"{lv(best)}\nThe bot enters only if it confirms at candle close with score ≥ {minS:g}.")


async def track() -> None:
    live = [k for k, v in ST["trades"].items() if v["status"] != "closed"]
    if not live:
        return
    L = await SV.paper_list()
    for r in L.get("trades") or []:
        b = ST["trades"].get(str(r["id"]))
        if not b or b["status"] == "closed":
            continue
        for h in r.get("hit") or []:
            if h in b["hit"]:
                continue
            b["hit"].append(h)
            if h == "T3":
                continue
            k = {"T1": 0, "T2": 1}[h]
            await tg(f"🎯 <b>{h} HIT</b> #{r['id']} · {E(short(b['sym']))} {b['tf']}\n{arrow(b['dir'])} {b['dir']} · {h} {f(b['targets'][k])} · booked 1/3, "
                     f"{'2/3' if h == 'T1' else '1/3'} left\n" + (f"Stop moved to breakeven {f(r.get('stop_now'))}" if h == "T1" else f"Stop now {f(r.get('stop_now'))}"))
        if r["status"] == "closed":
            b["status"] = "closed"
            pnl, rr, why = r.get("pnl") or 0, r.get("r_result") or 0, b.get("reason") or r.get("exit_reason")
            icon = "🏆" if r.get("exit_reason") == "T3" else "🏁" if pnl >= 0 else "🛑"
            await tg(f"{icon} <b>CLOSED</b> #{r['id']} · {E(short(b['sym']))} {b['tf']}\n{arrow(b['dir'])} {b['dir']} · {E(str(why))} · avg exit {f(r.get('exit'))}\n"
                     f"P&amp;L {'+' if pnl >= 0 else ''}{f(pnl)} {E(r.get('currency') or '')} · {'+' if rr >= 0 else ''}{rr:.2f}R (after costs)")
    for k in live:
        if ST["trades"][k]["status"] == "closing":
            ST["trades"][k]["status"] = "open"


# ---------------------------------------------------------------- Telegram commands
HELP = ("🤖 <b>MarketMiner bot commands</b>\n"
        "/status – running state, markets, FYERS\n/trades – open paper trades\n/pnl – results so far\n"
        "/pause · /resume – stop or restart new entries and alerts\n/symbols – list markets · /add SYM · /remove SYM\n"
        "/tf 15m – timeframe (1m 3m 5m 15m 30m 1h 4h 1d)\n"
        "/pin 1234 – renew FYERS for today (message deleted, PIN never stored)\n"
        "/login – full FYERS login (every 15 days) · then /code &lt;code&gt;")


async def command(msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    if cmd == "/pin":
        try:                                          # delete the PIN message first
            await TG.post(tg_url("deleteMessage"), data={"chat_id": CFG["chat"], "message_id": msg["message_id"]})
        except Exception:                             # noqa: BLE001
            pass
        try:
            r = await FY.renew_with_pin(arg)
            ST["notes"].pop("fyers", None)
            await tg(f"🔑 {E(r['message'])}. (Your PIN message was deleted.)")
        except HTTPException as e:
            await tg(f"❌ {E(str(e.detail))}")
        return
    if cmd == "/login":
        try:
            url = FY.make_login_url()
            await tg(f"🔐 <b>FYERS login</b> (valid 10 min)\n1. <a href=\"{E(url)}\">Open the FYERS login</a> and sign in.\n"
                     "2. You land on MarketMiner's Connections page showing a <b>one-time code</b>: copy it.\n"
                     "3. Send it here as <code>/code &lt;the code&gt;</code>.")
        except HTTPException as e:
            await tg(f"❌ {E(str(e.detail))}\nSet FYERS_APP_ID and FYERS_APP_SECRET in the server's .env.")
        return
    if cmd == "/code":
        state, _, code = arg.partition(".")
        if not code:
            state, code = None, arg
        try:
            await FY.exchange_auth_code(code, state)
            ST["notes"].pop("fyers", None)
            await tg("✅ FYERS connected for 15 days. Each morning send <code>/pin 1234</code> to renew for the day.")
        except HTTPException as e:
            await tg(f"❌ {E(str(e.detail))}")
        return
    if cmd in ("/pause", "/resume"):
        ST["paused"] = cmd == "/pause"
        await tg("⏸ Paused: no new entries or alerts. Open trades are still followed and closed." if ST["paused"] else "▶️ Resumed.")
        return
    if cmd == "/symbols":
        await tg("📋 <b>Markets</b> (" + ST["tf"] + ")\n" + "\n".join(E(s) for s in ST["symbols"]) +
                 "\n\nExamples: /add SHARK:SOLUSDT · /add NSE:SBIN-EQ · /add MCX:CRUDEOIL-FUT · /remove NSE:NIFTY50-INDEX")
        return
    if cmd in ("/add", "/remove"):
        s = arg.upper()
        if not s or ":" not in s:
            await tg("Send it with the exchange, e.g. <code>/add SHARK:SOLUSDT</code> or <code>/add NSE:SBIN-EQ</code>")
            return
        ST["symbols"] = [x for x in ST["symbols"] if x != s] + ([s] if cmd == "/add" else [])
        await tg(f"{'Added' if cmd == '/add' else 'Removed'} {E(s)}. Now: {E(', '.join(map(short, ST['symbols'])))}")
        return
    if cmd == "/tf":
        if arg not in TFSEC:
            await tg("Use one of: " + " ".join(TFSEC))
            return
        ST["tf"] = arg
        await tg(f"Timeframe set to {arg}.")
        return
    if cmd == "/trades":
        open_ = [(k, v) for k, v in ST["trades"].items() if v["status"] != "closed"]
        await tg("📂 <b>Open paper trades</b>\n" + ("\n\n".join(
            f"#{k} {arrow(v['dir'])} {v['dir']} {E(short(v['sym']))} {v['tf']}\nEntry {f(v['entry'])} · SL {f(v['stop'])} · T1 {f(v['targets'][0])} · "
            f"T2 {f(v['targets'][1])} · T3 {f(v['targets'][2])}" + (f" · {' '.join(v['hit'])} hit" if v["hit"] else "") for k, v in open_) or "None."))
        return
    if cmd == "/pnl":
        L = await SV.paper_list()
        s = L.get("summary") or {}
        lines = [f"{cur}: {v['trades']} trades · win {v['win_rate']}% · P&amp;L {'+' if v['pnl'] >= 0 else ''}{f(v['pnl'])} · avg {v['avg_r']:+.2f}R · costs {f(v['costs'])}"
                 for cur, v in s.items()]
        await tg("📊 <b>Paper results</b> (closed trades, after costs)\n" + ("\n".join(lines) or "No closed trades yet."))
        return
    if cmd == "/status":
        fs = FY.token_status() if hasattr(FY, "token_status") else {}
        await tg(f"🤖 <b>MarketMiner bot</b> {'⏸ paused' if ST['paused'] else '▶️ running'}\nTimeframe {ST['tf']} · scan every {CFG['every']}s\n"
                 f"Markets: {E(', '.join(map(short, ST['symbols'])))}\nOpen trades: {sum(1 for v in ST['trades'].values() if v['status'] != 'closed')}\n"
                 f"FYERS: {E(str(fs.get('message') or ('connected' if fyers_ok() else 'not connected')))}\n"
                 f"Last scan: {ST.get('last_scan') or '—'}\nPaper trading only.")
        return
    await tg(HELP)


async def poll() -> None:
    while True:
        try:
            r = await TG.get(tg_url("getUpdates"), params={"timeout": 50, "offset": ST["offset"], "allowed_updates": '["message"]'},
                             timeout=httpx.Timeout(70, connect=15))
            for u in r.json().get("result") or []:
                ST["offset"] = u["update_id"] + 1
                m = u.get("message") or {}
                if str((m.get("chat") or {}).get("id")) != CFG["chat"]:
                    continue                          # only you can control the bot
                try:
                    await command(m)
                except Exception:                     # noqa: BLE001
                    log.error("command failed\n%s", traceback.format_exc())
                    await tg("❌ That command failed; see the server log.")
            save_state()
        except Exception as e:                        # noqa: BLE001
            log.warning("Telegram poll: %s", e)
            await asyncio.sleep(10)


async def scanner() -> None:
    while True:
        t0 = time.time()
        if not ST["paused"]:
            try:
                await scan()
            except Exception:                         # noqa: BLE001
                log.error("scan failed\n%s", traceback.format_exc())
        else:
            try:
                await track()                         # still follow open trades while paused
            except Exception:                         # noqa: BLE001
                log.error("track failed\n%s", traceback.format_exc())
        ST["last_scan"] = dt.datetime.now(IST).strftime("%d %b %H:%M:%S IST")
        save_state()
        await asyncio.sleep(max(5, CFG["every"] - (time.time() - t0)))


async def main() -> None:
    global TG
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not (CFG["token"] and CFG["chat"]):
        log.error("Set TELEGRAM_TOKEN and TELEGRAM_CHAT in %s", HERE / ".env")
        sys.exit(2)
    TG = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10))
    await tg(f"🤖 <b>MarketMiner server bot started</b>\n{ST['tf']} · {E(', '.join(map(short, ST['symbols'])))}\nRuns without your phone or browser. "
             "Paper trading only. Send /help for commands.")
    await asyncio.gather(poll(), scanner())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
