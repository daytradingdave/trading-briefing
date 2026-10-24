"""Alpha Capital news-restrictions voor EUR/USD en XAU/USD.

Bron: screenshots van de Alpha Capital regels (door Dave aangeleverd). Alpha past deze lijst soms aan,
controleer hem dus af en toe en pas de patronen hieronder aan. 

Aanpak: een event is "restricted" voor een instrument als
  - de valuta + impact (High) in de regels staat, OF
  - de titel overeenkomt met een onderwerp uit de lijst (ook als het op Forex Factory 'Medium' is,
    bijv. Jobless Claims, Empire State, bond auctions).
"""
import re

WINDOW_MINUTES = 5  # no-trade window: 5 min voor en 5 min na

# Valuta's die Dave bewust negeert. GBP staat bij goud letterlijk in de Alpha-regels
# ("ALL GBP HIGH-IMPACT NEWS/SPEECHES"), dus wordt het sinds 2026-10-24 weer meegenomen.
IGNORE_CURRENCIES = set()

# Onderwerpen die voor ALLE valuta gelden (rij 'ALL CURRENCIES' bij XAU/USD), alleen bij High impact.
GLOBAL = (r"interest rate|rate decision|rate statement|press conference|federal funds|fomc|main refinancing|"
          r"official cash rate|cash rate|bank rate|overnight rate|policy rate|monetary policy|"
          r"cpi|inflation|gdp|pce|pmi|unemployment|employment change|non-farm|nfp")

# USD-rij (geldt voor EUR/USD en XAU/USD)
USD_TOPICS = (r"pmi|ism|cpi|ppi|gdp|federal funds|fomc|press conference|trade balance|unemployment|jobless|"
              r"consumer confidence|consumer sentiment|uom|jolts|average hourly|non-farm|employment|"
              r"10-y bond auction|30-y bond auction|auction|existing home sales|new home sales|durable goods|empire state|"
              r"retail sales|pce|speaks|speech|testifies")

# EUR-rij (geldt voor EUR/USD)
EUR_TOPICS = (r"pmi|cpi|inflation|gdp|employment|unemployment|retail sales|trade balance|ecb|"
              r"main refinancing|deposit facility|economic sentiment|business climate|ifo|zew|"
              r"consumer confidence|speaks|speech")

SPEECH = r"speaks|speech|testifies"


def classify(title, currency, impact):
    """Geef de set instrumenten ({'EURUSD','XAUUSD'}) waarvoor dit event onder de news-regels valt."""
    t = title.lower()
    high = impact == "High"
    inst = set()
    if impact == "Holiday" or currency in IGNORE_CURRENCIES:
        return inst
    if currency == "USD":
        if high or re.search(USD_TOPICS, t):
            inst |= {"EURUSD", "XAUUSD"}
    elif currency == "EUR":
        if re.search(EUR_TOPICS, t):
            inst.add("EURUSD")
        if high and (re.search(SPEECH, t) or re.search(GLOBAL, t)):
            inst.add("XAUUSD")
    elif currency in ("AUD", "CAD", "GBP"):  # alle High-impact news/speeches tellen voor goud
        if high:
            inst.add("XAUUSD")
    else:  # JPY, CHF, NZD, CNY, ... (alleen de 'ALL CURRENCIES'-onderwerpen, alleen High impact)
        if high and re.search(GLOBAL, t):
            inst.add("XAUUSD")
    return inst


def label(inst):
    names = {"EURUSD": "EUR/USD", "XAUUSD": "XAU/USD"}
    return " + ".join(names[i] for i in ("EURUSD", "XAUUSD") if i in inst)
