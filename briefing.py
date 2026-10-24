"""Dagelijkse trading briefing voor EUR/USD en XAU/USD.

Gebruik:
    python briefing.py daily   [--dry-run]   # dagbriefing (zondag: weekoverzicht)
    python briefing.py weekly  [--dry-run]   # weekoverzicht forceren
    python briefing.py alert   [--dry-run]   # alert voor high impact events binnen ~15 min
    python briefing.py test                  # stuurt een testbericht naar Telegram

--dry-run print het bericht i.p.v. het naar Telegram te sturen.
"""
import argparse
import html
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import liquidity
import news_rules

ROOT = Path(__file__).parent
STATE_FILE = ROOT / ".state" / "alerts_sent.json"
FEED_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]
CLAUDE_MODEL = "claude-sonnet-5-5"
DAYS_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def load_env():
    """Lees .env (lokaal gebruik); in GitHub Actions komen de waarden uit secrets."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def tz():
    return ZoneInfo(os.environ.get("TIMEZONE") or "Europe/Amsterdam")


# ---------------------------------------------------------------- kalender
CACHE_DIR = ROOT / ".state"
CACHE_TTL = timedelta(hours=4)  # de gratis feed rate-limit snel (429), dus cachen


def fetch_feed(url):
    """Haal een feed op met cache; bij fouten (429/404) val terug op oude cache."""
    cache = CACHE_DIR / (url.rsplit("/", 1)[-1])
    if cache.exists() and datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime) < CACHE_TTL:
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        data = r.json()
        CACHE_DIR.mkdir(exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
        return data
    except Exception as e:  # nextweek bestaat pas later in de week
        print(f"[waarschuwing] feed {url}: {e}", file=sys.stderr)
        return json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else []


def fetch_events():
    """Alle events (deze + volgende week) met Alpha-classificatie, tijden in jouw tijdzone."""
    seen, events = set(), []
    for url in FEED_URLS:
        for e in fetch_feed(url):
            key = (e["title"], e["date"], e["country"])
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "title": e["title"],
                "currency": e["country"],
                "impact": e["impact"],
                "forecast": e.get("forecast") or "-",
                "previous": e.get("previous") or "-",
                "time": datetime.fromisoformat(e["date"]).astimezone(tz()),
                "instruments": news_rules.classify(e["title"], e["country"], e["impact"]),
            })
    events.sort(key=lambda x: x["time"])
    return events


def on_day(events, day):
    return [e for e in events if e["time"].date() == day]


def restricted(events):
    """Events die onder de Alpha Capital news-regels vallen voor EUR/USD en/of XAU/USD."""
    return [e for e in events if e["instruments"]]


# ------------------------------------------------------------------- AI
SYSTEM_PROMPT = """Je bent de assistent van een intraday daytrader die alleen EUR/USD en XAU/USD (goud) handelt via prop firms.
Schrijf in het Nederlands, kort en praktisch, voor een Telegram-bericht. Gebruik ALLEEN deze HTML-tags: <b> en <i>. Geen markdown, geen koppen met #.
Regels:
- Geef GEEN koop/verkoop-advies en geen prijsvoorspelling. Beschrijf scenario's: wat een hoger of lager cijfer dan de forecast doorgaans betekent voor USD, EUR/USD en goud, en hoe volatiel het kan worden.
- Verzin geen cijfers. Gebruik alleen de aangeleverde data (forecast, previous, marktsnapshot).
- Als forecast ontbreekt (bijv. bij sprekers/speeches), zeg dan dat de impact zit in toon/woordkeuze (hawkish vs dovish).
- De trader handelt op liquidity (genomen highs/lows, sweeps, resting liquidity). Duid sweeps en open niveaus in die termen; geef geen entries of signalen.
- Reken: hogere USD-data / hawkish Fed = meestal USD sterker = EUR/USD omlaag en goud omlaag; zwakkere USD-data / dovish = omgekeerd. Voor EUR-data geldt het omgekeerde voor EUR/USD. Benoem dat dit een tendens is, geen zekerheid."""


def ai(prompt, max_tokens=1800):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=max(max_tokens, 4000), system=SYSTEM_PROMPT,
            # laag effort = weinig denk-tokens (goedkoop); max_tokens ruim, want denken telt mee
            extra_body={"output_config": {"effort": "low"}},
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as e:
        print(f"[waarschuwing] AI mislukt: {e}", file=sys.stderr)
        return None


def events_for_ai(events):
    return [{"tijd": e["time"].strftime("%H:%M"), "valuta": e["currency"], "event": e["title"],
             "impact": e["impact"], "instrumenten": news_rules.label(e["instruments"]), "forecast": e["forecast"], "previous": e["previous"]} for e in events]


# --------------------------------------------------------------- berichten
def esc(s):
    return html.escape(str(s), quote=False)


def fmt_event_line(e):
    flag = "🔴" if e["impact"] == "High" else "🟠"
    return (f"{flag} <b>{e['time'].strftime('%H:%M')}</b> {e['currency']} – {esc(e['title'])}"
            f"  (fc: {esc(e['forecast'])} | vorig: {esc(e['previous'])}) · <i>{news_rules.label(e['instruments'])}</i>")


def no_trade_window(t):
    m = timedelta(minutes=news_rules.WINDOW_MINUTES)
    return f"{(t - m).strftime('%H:%M')}–{(t + m).strftime('%H:%M')}"


def build_daily(events, today):
    day_events = on_day(events, today)
    rs = restricted(day_events)
    holidays = [e for e in day_events if e["impact"] == "Holiday" and e["currency"] in ("USD", "EUR")]
    head = f"📅 <b>Briefing {DAYS_NL[today.weekday()]} {today.strftime('%d-%m-%Y')}</b> (EUR/USD · XAU/USD)\n"
    if holidays:
        head += "\n🏖 " + " · ".join(f"{h['currency']}: {esc(h['title'])}" for h in holidays) + " (dunne liquiditeit mogelijk)\n"

    liq_text, liq_raw = liquidity.liquidity_report(tz())
    if rs:
        body = (f"\n⛔ <b>RESTRICTED NEWS (Alpha) – {len(rs)} event{'s' if len(rs) != 1 else ''} vandaag</b>\n"
                f"<i>Geen trades {news_rules.WINDOW_MINUTES} min voor en {news_rules.WINDOW_MINUTES} min na elke release.</i>\n")
        for e in rs:
            body += (f"\n🔴 <b>{e['time'].strftime('%H:%M')}</b> · {e['currency']} · {esc(e['title'])}"
                     f"\n     ⛔ <b>No trading between {no_trade_range(e["time"])}</b>"
                     f"\n     fc: {esc(e['forecast'])} | vorig: {esc(e['previous'])} · <i>{news_rules.label(e['instruments'])}</i>")
        body += "\n"
    elif not day_events:  # doordeweeks staat er altijd wel iets in de feed -> de kalender ontbreekt dus
        body = ("\n⚠️ <b>Kalender niet beschikbaar</b> (feed leeg of niet bereikbaar). "
                "Check de news handmatig op Forex Factory voordat je gaat traden!\n")
    else:
        body = "\n✅ <b>Geen restricted news vandaag</b> voor EUR/USD of XAU/USD.\n"

    analysis = ai(
        "Maak de briefing voor vandaag.\n"
        + ("LET OP: de economische kalender is NIET beschikbaar. Zeg niets over events en NIET dat er geen news is; "
           "sla punt 2 en 3 over en noem alleen dat de news handmatig gecheckt moet worden.\n"
           if not day_events else f"Restricted news-events (lokale tijd): {json.dumps(events_for_ai(rs))}\n")
        + f"Liquidity-data (genomen highs/lows sinds gisteren + dichtstbijzijnde open niveaus): {json.dumps(liq_raw)}\n\n"
        "Structuur:\n"
        "1) <b>🧭 Dagtoon</b>: max 3 zinnen op basis van de liquidity-data (wat is er genomen, wat ligt er nog open, "
        "beweegt DXY mee of tegen EUR/USD en goud?).\n"
        "2) Per restricted event: <b>tijd – event</b>, wat de verwachting is (forecast vs previous) en het scenario hoger vs lager "
        "dan forecast voor EUR/USD en/of XAU/USD (alleen de instrumenten die bij het event staan), 1 zin volatiliteit.\n"
        "3) <b>⚠️ Belangrijkste moment van de dag</b>: 1-2 zinnen.\n"
        "Compact, max ~2200 tekens. Noem geen no-trade windows; die voeg ik zelf toe.", 1600)
    if liq_text:
        body += f"\n{liq_text}\n"
    if analysis:
        body += f"\n{analysis}\n"
    else:
        body += "\n<i>(AI-duiding niet beschikbaar – voeg een ANTHROPIC_API_KEY toe.)</i>\n"
    return head + body


def build_weekly(events, start):
    """Weekoverzicht van `start` (de komende maandag, of vandaag op maandag) t/m vrijdag."""
    days = [start + timedelta(days=i) for i in range(5)]
    rs = [e for e in restricted(events) if e["time"].date() in days]
    head = f"🗓 <b>Weekoverzicht {days[0].strftime('%d-%m')} t/m {days[-1].strftime('%d-%m-%Y')}</b> (EUR/USD · XAU/USD)\n"
    if not [e for e in events if e["time"].date() in days]:
        return head + "\nDe kalender voor volgende week is nog niet gepubliceerd. Check morgen de dagbriefing."
    body = f"\n<i>Alle events hieronder vallen onder de Alpha news-regels (no-trade ±{news_rules.WINDOW_MINUTES} min).</i>\n"
    for d in days:
        de = [e for e in rs if e["time"].date() == d]
        body += f"\n<b>{DAYS_NL[d.weekday()].capitalize()} {d.strftime('%d-%m')}</b>"
        body += "\n" + ("\n".join(fmt_event_line(e) for e in de) if de else "  ✅ geen restricted news") + "\n"
    analysis = ai(
        "Maak een weekvooruitblik.\n"
        f"Restricted events: {json.dumps([{**x, 'dag': DAYS_NL[e['time'].weekday()]} for x, e in zip(events_for_ai(rs), rs)])}\n"
        "Geef: 1) <b>🎯 Thema van de week</b> (max 3 zinnen), 2) <b>De 3 belangrijkste momenten</b> met dag/tijd en "
        "waarom ze EUR/USD en goud kunnen bewegen, 3) <b>🧠 Aandachtspunt</b>: dagen waarop news clustert. Max ~1800 tekens.", 1400)
    if analysis:
        body += f"\n{analysis}\n"
    return head + body


def no_trade_range(t):
    m = timedelta(minutes=news_rules.WINDOW_MINUTES)
    return f"{(t - m).strftime('%H:%M')} & {(t + m).strftime('%H:%M')}"


def build_alert(group, now):
    """Eén bericht per releasemoment (events op dezelfde tijd worden samengevoegd)."""
    t = group[0]["time"]
    mins = max(1, round((t - now).total_seconds() / 60))
    lines = [f"🚨 <b>HIGH IMPACT NEWS REMINDER</b> – release over {mins} min"]
    for e in group:
        lines.append(f"\n<b>{esc(e['title'])} {e['currency']}</b> Release @ <b>{t.strftime('%H:%M')}</b>"
                     f"\n   fc: {esc(e['forecast'])} | vorig: {esc(e['previous'])} · <i>{news_rules.label(e['instruments'])}</i>")
    lines.append(f"\n⛔ <b>No trading between {no_trade_range(t)}</b>")
    return "\n".join(lines)


# --------------------------------------------------------------- telegram
def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        sys.exit("TELEGRAM_BOT_TOKEN en/of TELEGRAM_CHAT_ID ontbreken (zie .env.example).")
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += para + "\n"
    chunks.append(cur)
    for c in chunks:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": c.strip(), "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20)
        if not r.ok:  # val terug op platte tekst als de HTML niet klopt
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": html.unescape(c.replace("<b>", "").replace("</b>", "")
                                .replace("<i>", "").replace("</i>", ""))}, timeout=20).raise_for_status()


def deliver(text, dry):
    if dry:
        print(text)
    else:
        send_telegram(text)
        print("Verstuurd naar Telegram.")


# ------------------------------------------------------------------- main
def run_alert(dry, now=None):
    now = now or datetime.now(tz())
    sent = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
    # venster 2-17 min vooraf: de check draait elke 5 min, dus de melding komt ~12-17 min van tevoren binnen
    due = [e for e in fetch_events()
           if e["instruments"] and timedelta(minutes=2) <= e["time"] - now <= timedelta(minutes=17)
           and f"{e['title']}|{e['time'].isoformat()}" not in sent]
    groups = {}
    for e in due:  # events op dezelfde tijd samen in 1 bericht
        groups.setdefault(e["time"], []).append(e)
    for t, group in sorted(groups.items()):
        deliver(build_alert(group, now), dry)
        sent.update(f"{e['title']}|{e['time'].isoformat()}" for e in group)
    if not dry:
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(sorted(sent)[-200:]))
    if not due:
        print("Geen alerts nodig.")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["daily", "weekly", "alert", "test"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="negeer de 08:00-check (voor testen)")
    p.add_argument("--now", help="alert-test: doe alsof het nu deze lokale tijd is, YYYY-MM-DDTHH:MM")
    p.add_argument("--date", help="doe alsof het deze datum is, YYYY-MM-DD (voor testen)")
    a = p.parse_args()
    now = datetime.now(tz())
    today = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else now.date()

    if a.mode == "test":
        return send_telegram("✅ Test: je trading briefing bot is verbonden.")
    if a.mode == "alert":
        sim = datetime.fromisoformat(a.now).replace(tzinfo=tz()) if a.now else None
        return run_alert(a.dry_run, sim)

    # GitHub Actions draait 2x (zomer/wintertijd); verstuur alleen als het hier 08:xx is.
    if a.mode == "daily" and not a.force and not a.dry_run and now.hour != 8:
        return print(f"Lokale tijd is {now:%H:%M}, geen 08:xx – overgeslagen.")

    events = fetch_events()
    if a.mode == "weekly" or (a.mode == "daily" and today.weekday() == 6):  # zondag = weekoverzicht
        start = today + timedelta(days=(7 - today.weekday()) % 7 or 7) if today.weekday() != 0 else today
        deliver(build_weekly(events, start), a.dry_run)
    elif today.weekday() == 5:  # zaterdag: markt dicht
        print("Zaterdag – geen briefing.")
    else:
        deliver(build_daily(events, today), a.dry_run)


if __name__ == "__main__":
    main()
