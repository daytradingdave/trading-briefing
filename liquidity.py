"""Liquidity-niveaus en sweeps voor EUR/USD en XAU/USD (+ DXY als context).

Niveaus (Dave's definitie):
  - Daily swing highs/lows   (fractal: 3 candles aan elke kant, laatste ~60 dagen)
  - Weekly swing highs/lows  (fractal: 2 candles aan elke kant, laatste ~52 weken)
  - Equal highs/lows         = 2+ swing highs (of lows) binnen EQUAL_TOL_PCT van elkaar -> 1 grote liquidity pool

Sweep versus break, gemeten op 15m candles (voor een swing HIGH; omgekeerd voor een low):
  - SWEEP = candle wickt boven het niveau, maar de BODY (close) sluit er weer onder
  - TAKEN = een 15m candle sluit met zijn body boven het niveau (liquidity genomen)
  - OPEN  = nog niet geraakt (resting liquidity)

Data: Twelve Data (spot, als TWELVEDATA_API_KEY gezet is) of anders Yahoo Finance (gratis; goud = futures GC=F).
Een handelsdag loopt van 17:00 New York tot 17:00 New York; de week begint zondag 17:00 NY.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
PRIMARY = ["EUR/USD", "XAU/USD"]
INSTRUMENTS = {
    "EUR/USD": {"yahoo": ["EURUSD=X"], "td": "EUR/USD", "dec": 5, "tol_pct": 0.03},
    "XAU/USD": {"yahoo": ["XAUUSD=X", "GC=F"], "td": "XAU/USD", "dec": 2, "tol_pct": 0.05},
    "DXY": {"yahoo": ["DX-Y.NYB"], "td": None, "dec": 3, "tol_pct": 0.03},  # context; bron altijd Yahoo
}
DAILY_N, DAILY_DAYS = 3, 60
WEEKLY_N, WEEKLY_WEEKS = 2, 52
HEADERS = {"User-Agent": "Mozilla/5.0"}


# ------------------------------------------------------------------ data
def fetch_yahoo(symbols, interval, rng):
    for sym in symbols:
        try:
            r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={interval}",
                             headers=HEADERS, timeout=25)
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            bars = [{"t": datetime.fromtimestamp(ts, timezone.utc), "h": h, "l": l, "c": c}
                    for ts, h, l, c in zip(res["timestamp"], q["high"], q["low"], q["close"])
                    if h is not None and l is not None and c is not None]
            if len(bars) > 100:
                return bars, "Yahoo" + (" (futures)" if sym == "GC=F" else "")
        except Exception as e:
            print(f"[waarschuwing] yahoo {sym} {interval}: {e}", file=sys.stderr)
    return [], None


def fetch_twelvedata(symbol, interval, size):
    key = os.environ.get("TWELVEDATA_API_KEY")
    if not key:
        return [], None
    try:
        r = requests.get("https://api.twelvedata.com/time_series",
                         params={"symbol": symbol, "interval": interval, "outputsize": size,
                                 "timezone": "UTC", "order": "ASC", "apikey": key}, timeout=25)
        data = r.json()
        if data.get("status") == "error":
            raise RuntimeError(data.get("message"))
        bars = [{"t": datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                 "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"])} for v in data["values"]]
        return (bars, "Twelve Data") if len(bars) > 100 else ([], None)
    except Exception as e:
        print(f"[waarschuwing] twelvedata {symbol} {interval}: {e}", file=sys.stderr)
        return [], None


def get_bars(name, tf):
    """tf: '1h' (lange historie voor daily/weekly swings) of '15m' (sweep/break-bepaling)."""
    cfg = INSTRUMENTS[name]
    if cfg["td"]:
        td_int, size = ("1h", 5000) if tf == "1h" else ("15min", 3000)
        bars, src = fetch_twelvedata(cfg["td"], td_int, size)
        if bars:
            return bars, src
    return fetch_yahoo(cfg["yahoo"], tf, "2y" if tf == "1h" else "30d")


# ------------------------------------------------------------ niveaus
def keys(t):
    """(dag, week) van een candle volgens de FX-handelsdag (17:00 NY = nieuwe dag)."""
    s = t.astimezone(NY) + timedelta(hours=7)
    return s.date(), s.isocalendar()[:2]


def agg(bars, idx):
    """-> geordende lijst [(key, high, low, eerste_t, laatste_t)] per dag (idx 0) of week (idx 1)."""
    out = {}
    for b in bars:
        k = keys(b["t"])[idx]
        if k not in out:
            out[k] = [b["h"], b["l"], b["t"], b["t"]]
        else:
            o = out[k]
            o[0], o[1], o[3] = max(o[0], b["h"]), min(o[1], b["l"]), b["t"]
    return [(k, *v) for k, v in sorted(out.items(), key=lambda kv: kv[1][2])]


def swings(series, n, max_age_days, now, tag, label):
    """Fractal swings op een dag-/weekserie."""
    lv = []
    for i in range(n, len(series) - n):
        k, h, l, first, last = series[i]
        if (now - last).days > max_age_days:
            continue
        neigh = [j for j in range(i - n, i + n + 1) if j != i]
        if all(h > series[j][1] for j in neigh):
            lv.append({"kind": "high", "price": h, "since": last, "tf": tag, "name": f"{label} swing high {last:%d-%m}"})
        if all(l < series[j][2] for j in neigh):
            lv.append({"kind": "low", "price": l, "since": last, "tf": tag, "name": f"{label} swing low {last:%d-%m}"})
    return lv


def build_levels(bars1h, tol_pct):
    now = bars1h[-1]["t"]
    days, weeks = agg(bars1h, 0), agg(bars1h, 1)
    raw = (swings(days, DAILY_N, DAILY_DAYS, now, "D", "Daily")
           + swings(weeks, WEEKLY_N, WEEKLY_WEEKS * 7, now, "W", "Weekly"))
    # zelfde prijs op daily én weekly = 1 niveau
    merged = []
    for L in raw:
        twin = next((M for M in merged if M["kind"] == L["kind"] and abs(M["price"] - L["price"]) < 1e-9 * abs(L["price"])), None)
        if twin:
            twin["tf"] = "".join(sorted(set(twin["tf"] + L["tf"]), reverse=True))  # 'WD'
            twin["name"] = twin["name"].replace("Daily", "Weekly+Daily").replace("Weekly swing", "Weekly+Daily swing") \
                if "+" not in twin["name"] else twin["name"]
        else:
            merged.append(dict(L))
    # equal highs/lows: clusters van swings binnen tol_pct
    out = []
    for kind in ("high", "low"):
        pts = sorted((L for L in merged if L["kind"] == kind), key=lambda x: x["price"])
        cluster = []
        def flush():
            if len(cluster) >= 2:
                price = max(c["price"] for c in cluster) if kind == "high" else min(c["price"] for c in cluster)
                tfs = "".join(sorted({t for c in cluster for t in c["tf"]}, reverse=True))
                out.append({"kind": kind, "price": price, "since": max(c["since"] for c in cluster),
                            "tf": tfs, "name": f"Equal {kind}s ×{len(cluster)} ({tfs})"})
            else:
                out.extend(cluster)
        for L in pts:
            if cluster and (L["price"] - cluster[0]["price"]) / cluster[0]["price"] * 100 > tol_pct:
                flush()
                cluster = []
            cluster.append(L)
        flush()
    return out, days


# ------------------------------------------------------------ analyse
def classify_level(L, bars1h, bars15, fresh_from):
    """-> None (negeren/spent), of dict met status OPEN | SWEEP | TAKEN en tijdstip."""
    p, high = L["price"], L["kind"] == "high"
    exceed = (lambda b: b["h"] > p) if high else (lambda b: b["l"] < p)
    beyond = (lambda b: b["c"] > p) if high else (lambda b: b["c"] < p)
    start15 = bars15[0]["t"]
    # periode vóór de 15m-data: elk geraakt niveau is 'spent' (oud nieuws)
    if any(exceed(b) for b in bars1h if L["since"] < b["t"] < start15):
        return None
    first = taken = None
    for b in bars15:
        if b["t"] <= L["since"]:
            continue
        if first is None and exceed(b):
            first = b
        if beyond(b):
            taken = b
            break
    if first is None:
        return {"status": "OPEN"}
    if taken is not None and taken["t"] >= fresh_from:
        return {"status": "TAKEN", "when": taken["t"]}
    if taken is None and first["t"] >= fresh_from:
        return {"status": "SWEEP", "when": first["t"]}
    return None


def analyse(name):
    cfg = INSTRUMENTS[name]
    bars1h, src = get_bars(name, "1h")
    bars15, src15 = get_bars(name, "15m")
    if not bars1h or not bars15:
        return None
    levels, days = build_levels(bars1h, cfg["tol_pct"])
    fresh_from = days[-2][3] if len(days) >= 2 else days[-1][3]  # begin van de vorige handelsdag
    last = bars15[-1]["c"]
    swept, above, below = [], [], []
    for L in levels:
        c = classify_level(L, bars1h, bars15, fresh_from)
        if not c:
            continue
        if c["status"] == "OPEN":
            (above if L["price"] > last else below).append(L)
        else:
            swept.append({**L, **c})
    above.sort(key=lambda x: x["price"])
    below.sort(key=lambda x: -x["price"])
    return {"name": name, "src": src15 or src, "last": last, "swept": sorted(swept, key=lambda x: x["when"]),
            "above": above[:2], "below": below[:2]}


def fmt(name, p):
    return f"{p:.{INSTRUMENTS[name]['dec']}f}"


def liquidity_report(tzinfo):
    """-> (HTML-tekst, ruwe data voor AI)."""
    results = {}
    for name in INSTRUMENTS:
        try:
            r = analyse(name)
        except Exception as e:
            print(f"[waarschuwing] liquidity {name}: {e}", file=sys.stderr)
            r = None
        if r:
            results[name] = r
    if not results:
        return "", {}
    lines = ["💧 <b>Liquidity</b> (daily/weekly swings + equal highs/lows · sweep = wick, body terug · taken = 15m body close)"]
    raw, sources = {}, set()
    for name, r in results.items():
        sources.add(r["src"])
        primary = name in PRIMARY
        raw[name] = {
            "laatste_koers": fmt(name, r["last"]),
            "genomen_of_gesweept": [f"{s['name']} {fmt(name, s['price'])} {s['status']} {s['when'].astimezone(tzinfo):%a %H:%M}" for s in r["swept"]],
            "open_boven": [f"{x['name']} {fmt(name, x['price'])}" for x in r["above"]],
            "open_onder": [f"{x['name']} {fmt(name, x['price'])}" for x in r["below"]]}
        lines.append(f"\n<b>{name}</b> {fmt(name, r['last'])}")
        for s in r["swept"]:
            arrow = "↑" if s["kind"] == "high" else "↓"
            if s["status"] == "SWEEP":
                txt = "🔄 SWEEP (wick, body terug binnen)"
            else:
                txt = "➡️ TAKEN (15m body close voorbij)"
            lines.append(f"  {arrow} {s['name']} {fmt(name, s['price'])} – {txt} · {s['when'].astimezone(tzinfo):%a %H:%M}")
        if not r["swept"]:
            lines.append("  geen swing-niveaus of equal highs/lows geraakt sinds gisteren")
        if primary:
            if r["above"]:
                a = r["above"][0]
                lines.append(f"  ⬆ open boven: {a['name']} {fmt(name, a['price'])}")
            if r["below"]:
                b = r["below"][0]
                lines.append(f"  ⬇ open onder: {b['name']} {fmt(name, b['price'])}")
    lines.append(f"\n<i>Candles: {', '.join(sorted(sources))}. Kan pips afwijken van je broker.</i>")
    return "\n".join(lines), raw
