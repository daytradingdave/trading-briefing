# Trading briefing – projectcontext

Eigenaar: Dave, intraday daytrader (prop firm: Alpha Capital), alleen EUR/USD en XAU/USD, tijdzone Europe/Amsterdam.
Communicatie: Nederlands. Dave leert Claude Code – leg stappen kort uit en benoem best practices.

## Wat dit project doet
`briefing.py` stuurt via Telegram: dagbriefing 08:00 (zondag: weekoverzicht) en een alert ±15 min voor restricted news.
Draait via GitHub Actions (`.github/workflows/`), niet op Dave's pc.

## Harde regels
- No-trade window = 5 min voor en 5 min na restricted news (`news_rules.WINDOW_MINUTES`).
- Welke events restricted zijn staat in `news_rules.py` (afgeleid van de Alpha Capital regels). Pas daar aan, niet verspreid in de code.
- Geen koop/verkoop-advies, geen entries of prijsvoorspellingen. Alleen scenario's en context.
- Dave handelt op liquidity (`liquidity.py`). Alleen EUR/USD, XAU/USD en DXY (context) – geen andere pairs.
  Niveaus: daily + weekly swing highs/lows en equal highs/lows (pool). Andere niveaus (PDH, Asia, ...) spot Dave zelf.
  Sweep = 15m candle wickt voorbij het niveau maar de body sluit terug; taken = 15m body close voorbij het niveau.
- GBP High-impact news telt mee voor goud (staat zo in de Alpha-regels); `IGNORE_CURRENCIES` in `news_rules.py` is leeg.
- Nooit secrets (Telegram token, API-keys) in code of chat. Alleen `.env` (lokaal, in .gitignore) of GitHub Secrets.

## Commando's
- `python briefing.py daily --dry-run` – print zonder te sturen
- `python briefing.py alert --dry-run`, `python briefing.py test`

## Bekende beperkingen
- Kalenderfeed (faireconomy) is gratis en rate-limit (429): daarom cache in `.state/`.
- Candledata: Twelve Data (spot) als `TWELVEDATA_API_KEY` gezet is, anders Yahoo (goud = futures GC=F). Twelve Data-pad is nog niet getest met een echte key.

## Volgende fases (één voor één)
1. Briefing live krijgen  2. Astrologieblok  3. Journal-dashboard  4. Prop firm rule tracker
