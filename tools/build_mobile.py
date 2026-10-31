"""Build MarketMiner Mobile from the desktop pages.

    python tools/build_mobile.py <desktop static folder>

Copies the desktop index.html / crypto.html / fyers.html, loads js/engine.js first on every page (it answers the
pages' /api/... calls inside the browser, so no PC server is needed), drops panels that only the PC can feed (NSE
pages, Moneycontrol, Day Desk, server AI) and fixes links for GitHub Pages. Re-run after changing the desktop pages.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "../MarketMiner-FYERS/static")
ENGINE = '<script src="js/engine.js?v=__V__"></script>'
VERSION = "2026.11.01-1"

# Panels the phone can't feed (they need NSE's website or Moneycontrol through the PC's proxy)
DROP_CARDS = ["🌐 Global Indices", "🔔 Pre-Open", "⚡ F&amp;O Pre-Open", "⚡ F&O Pre-Open", "💰 Large Deals", "📈 OI Spurts Analysis",
              "📊 Volume Shockers", "🏛️ FII / DII Activity", "🔥 Active Contracts", "📅 Events Calendar", "🌟 Seasonal Picks", "🏦 Broker Picks"]
MOBILE_LOADERS = "ldSe(),ldC(),ldNW(),ldOC(),ldCPR(),ldOIT()"


def drop_block(html: str, start: int) -> str:
    """Remove the <div ...> element that starts at `start` (balanced div counting)."""
    i, depth = start, 0
    for m in re.finditer(r"<div\b|</div>", html[start:]):
        depth += 1 if m.group(0) == "<div" else -1
        if depth == 0:
            end = start + m.end()
            return html[:start] + html[end:]
    raise ValueError("unbalanced div")


def drop_card(html: str, title: str) -> tuple[str, bool]:
    k = html.find(title)
    if k < 0 or 'class="cd-t"' not in html[max(0, k - 400):k]:
        return html, False
    start = html.rfind('<div class="cd', 0, k)
    # the card's own opening div is the nearest '<div class="cd ' / '<div class="cd"' before the header
    while start > 0 and not re.match(r'<div class="cd[ "]', html[start:start + 16]):
        start = html.rfind('<div class="cd', 0, start)
    return drop_block(html, start), True


def common(html: str) -> str:
    html, n = re.subn(r'(<meta charset="utf-8">)', lambda m: m.group(1) + "\n" + ENGINE.replace("__V__", VERSION), html, count=1, flags=re.I)
    if not n:
        raise SystemExit("no <meta charset> to put the engine after")
    html = html.replace('href="/manifest.webmanifest"', 'href="manifest.webmanifest"').replace('href="/icons/', 'href="icons/')
    html = html.replace('href="/fyers.html"', 'href="fyers.html"').replace('href="/crypto.html"', 'href="crypto.html"').replace('href="/"', 'href="index.html"')
    html = html.replace('href="/desk.html"', 'href="desk.html"')
    html = html.replace("register('/sw.js')", "register('sw.js')")
    return html


def build_index(src: str) -> str:
    html = common(src)
    dropped = []
    for t in DROP_CARDS:
        html, ok = drop_card(html, t)
        if ok:
            dropped.append(t)
    # the right-hand column wrapper may now be empty of the FII/Active cards: fine, CPR stays in it
    html = re.sub(r"await Promise\.allSettled\(\[ldG\(\),[^\]]*\]\);", f"await Promise.allSettled([{MOBILE_LOADERS}]);", html, count=1)
    # Screener / Holdings pages read NSE: keep only the Overview tab
    html = re.sub(r'<div class="ptab" data-p="screener".*?</div>\s*<div class="ptab" data-p="holdings".*?</div>', "", html, flags=re.S, count=1)
    # server-only buttons
    html = re.sub(r'<button class="rbtn ai" id="aiBtn"[^>]*>.*?</button>', "", html, count=1)
    html = html.replace('<div class="tab" data-t="stk" onclick="swN(\'stk\')">Stock News</div>', "")
    html = html.replace(">Results & Targets</div>", ">🇮🇳 India</div>")
    html = html.replace("CN={GOLD1G:'Gold(1g)'", "CN={GOLD1G:'Gold (10g, MCX)'")
    html = html.replace("<title>", "<title>📱 ", 1) if "<title>📱" not in html else html
    print("index: dropped", len(dropped), "cards")
    return html


def build_crypto(src: str) -> str:
    html = common(src)
    html = re.sub(r'<button class="btn" id="aiBtn"[^>]*>.*?</button>', "", html, count=1)
    return html


def build_fyers(src: str) -> str:
    html = common(src)
    rep = [
        ("It is stored only on this PC and never shown again.", "It is stored only in this browser on this device and never shown again."),
        ("$('connect').onclick=()=>location.href='/api/fyers/login';",
         "$('connect').onclick=()=>{try{location.href=MM.FY.loginUrl()}catch(e){$('statusMsg').textContent=e.message}};"),
    ]
    for a, b in rep:
        if a not in html:
            raise SystemExit("fyers.html changed; update build_mobile.py: " + a[:60])
        html = html.replace(a, b)
    html = re.sub(r"The redirect URL must match.*?while MarketMiner is running\.",
                  "Register this exact page as the FYERS app's redirect URL (it is filled in for you). On iPhone/iPad, if the login "
                  "returns to Safari instead of the Home Screen app, copy the code Safari shows and tap <b>Paste login code</b> here.",
                  html, flags=re.S, count=1)
    html = html.replace('<button id="refreshStatus" class="secondary">Refresh status</button>',
                        '<button id="refreshStatus" class="secondary">Refresh status</button><button id="pasteCode" class="secondary">Paste login code</button>', 1)
    html = html.replace("Data only: MarketMiner never places orders on FYERS.", "Data only: MarketMiner Mobile never places orders. Your FYERS details stay on this device.")
    login_js = r"""
// ---- phone build: finish the FYERS login that comes back to this page
(async()=>{const r=await MM.FY.completeFromUrl();if(!r)return;
 if(r.handoff){document.querySelector('main').insertAdjacentHTML('afterbegin',`<section class="panel full"><div class="cd-h"><span class="cd-t">📋 Finish the FYERS login in the app</span></div><div class="cd-b"><p class="muted">The FYERS login came back to Safari, which can't see the settings saved in the Home Screen app. Copy this one-time code, open MarketMiner from your Home Screen, go to Connections and tap <b>Paste login code</b> (within 10 minutes).</p><textarea id="hcode" readonly style="width:100%;height:70px;margin-top:8px;background:var(--bg2);color:var(--t1);border:1px solid var(--bdr2);border-radius:6px;font:12px var(--mono);padding:8px">${r.handoff}</textarea><div class="row" style="margin-top:8px"><button id="hcopy">Copy login code</button><span id="hmsg" class="fine"></span></div></div></section>`);
  $('hcopy').onclick=async()=>{try{await navigator.clipboard.writeText(r.handoff);$('hmsg').textContent='Copied. Now open MarketMiner from your Home Screen.';}catch(e){$('hcode').select();$('hmsg').textContent='Select the code and copy it.';}};return;}
 if(r.ok){log('FYERS login completed on this device.');}else{log('FYERS login failed: '+r.reason);}
 await status();})();
$('pasteCode').onclick=async()=>{let x=null;try{x=await navigator.clipboard.readText()}catch(e){}if(!x||x.indexOf('.')<0)x=prompt('Paste the FYERS login code you copied in Safari');if(!x)return;
 try{await MM.FY.finishWithCode(x);log('FYERS login completed on this device.');await status();}catch(e){$('statusMsg').textContent=e.message;}};
"""
    html = html.replace("loadApp();status();", login_js + "loadApp();status();", 1)
    return html


def build_desk(src: str) -> str:
    html = common(src)
    html = html.replace(ENGINE.replace("__V__", VERSION), ENGINE.replace("__V__", VERSION) + '\n<script src="js/desk.js?v=' + VERSION + '"></script>', 1)
    # Shark Exchange does not answer phone browsers: drop its market button
    html = re.sub(r'\s*<button data-m="SHARK">.*?</button>', "", html, count=1, flags=re.S)
    return html


def main() -> None:
    out = ROOT
    (out / "icons").mkdir(exist_ok=True)
    (out / "index.html").write_text(build_index((SRC / "index.html").read_text(encoding="utf-8")), encoding="utf-8")
    (out / "crypto.html").write_text(build_crypto((SRC / "crypto.html").read_text(encoding="utf-8")), encoding="utf-8")
    (out / "fyers.html").write_text(build_fyers((SRC / "fyers.html").read_text(encoding="utf-8")), encoding="utf-8")
    (out / "desk.html").write_text(build_desk((SRC / "desk.html").read_text(encoding="utf-8")), encoding="utf-8")
    for icon in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
        if (SRC / "icons" / icon).exists():
            shutil.copy(SRC / "icons" / icon, out / "icons" / icon)
    print("built into", out)


if __name__ == "__main__":
    main()
