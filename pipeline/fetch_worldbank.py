#!/usr/bin/env python3
"""
fetch_worldbank.py — Pull annual GDP + trade-composition indicators from the
World Bank API (free, no key) and write data/worldbank_countries.json.

Indicators:
  NY.GDP.MKTP.CD     GDP, current US$
  NE.EXP.GNFS.ZS     Exports of goods & services, % of GDP
  NE.IMP.GNFS.ZS     Imports of goods & services, % of GDP
  NE.TRD.GNFS.ZS     Trade (exports+imports), % of GDP  (openness)
  NY.GDP.MKTP.KD.ZG  GDP growth, annual %

Covers the major economies that map to the country ETFs in the Global Trade
tab (China→MCHI, Germany→EWG, etc.). Annual cadence — World Bank data is
typically 6-18 months lagged.

Output resolves to <repo-root>/data/worldbank_countries.json.
Requires: stdlib only.
"""
import json
import os
import sys
import datetime
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
OUT_PATH = os.path.join(REPO_ROOT, "data", "worldbank_countries.json")

# ISO3 -> {name, etf}  — the economies shown in the Global Trade tab.
COUNTRIES = {
    "USA": {"name": "United States", "etf": "SPY"},
    "CHN": {"name": "China", "etf": "MCHI"},
    "JPN": {"name": "Japan", "etf": "EWJ"},
    "DEU": {"name": "Germany", "etf": "EWG"},
    "IND": {"name": "India", "etf": "INDA"},
    "GBR": {"name": "United Kingdom", "etf": "EWU"},
    "FRA": {"name": "France", "etf": "EWQ"},
    "ITA": {"name": "Italy", "etf": "EWI"},
    "BRA": {"name": "Brazil", "etf": "EWZ"},
    "CAN": {"name": "Canada", "etf": "EWC"},
    "KOR": {"name": "South Korea", "etf": "EWY"},
    "MEX": {"name": "Mexico", "etf": "EWW"},
    "AUS": {"name": "Australia", "etf": "EWA"},
    "ESP": {"name": "Spain", "etf": "EWP"},
    "IDN": {"name": "Indonesia", "etf": "EIDO"},
    "SAU": {"name": "Saudi Arabia", "etf": "KSA"},
    "TWN": {"name": "Taiwan", "etf": "EWT"},
    "CHE": {"name": "Switzerland", "etf": "EWL"},
}

INDICATORS = {
    "NY.GDP.MKTP.CD":    "gdpUsd",
    "NE.EXP.GNFS.ZS":    "exportsPctGdp",
    "NE.IMP.GNFS.ZS":    "importsPctGdp",
    "NE.TRD.GNFS.ZS":    "tradePctGdp",
    "NY.GDP.MKTP.KD.ZG": "gdpGrowthPct",
}


def fetch_indicator(iso3, indicator):
    """Return the most recent non-null {year, value} for one indicator, or None.
    World Bank returns newest-first when sorted by date desc (default is desc)."""
    url = (f"https://api.worldbank.org/v2/country/{iso3}/indicator/{indicator}"
           f"?format=json&per_page=10&mrv=10")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "valuatio-worldbank"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"   ! {iso3}/{indicator}: {e}", file=sys.stderr)
        return None
    # data is [metadata, [observations]]
    if not isinstance(data, list) or len(data) < 2 or not data[1]:
        return None
    for obs in data[1]:   # newest-first
        if obs.get("value") is not None:
            return {"year": obs.get("date"), "value": obs["value"]}
    return None


def main():
    out_countries = {}
    for iso3, meta in COUNTRIES.items():
        row = {"name": meta["name"], "etf": meta["etf"], "iso3": iso3}
        for indicator, key in INDICATORS.items():
            res = fetch_indicator(iso3, indicator)
            if res:
                row[key] = res["value"]
                row[key + "Year"] = res["year"]
        out_countries[iso3] = row
        gdp = row.get("gdpUsd")
        print(f"   {meta['name']:16} GDP ${gdp/1e12:.2f}T" if gdp else f"   {meta['name']:16} (no GDP)")

    # Rank by GDP for convenience
    ranked = sorted(
        [c for c in out_countries.values() if c.get("gdpUsd")],
        key=lambda c: c["gdpUsd"], reverse=True
    )
    for i, c in enumerate(ranked):
        out_countries[c["iso3"]]["gdpRank"] = i + 1

    out = {
        "_schema": "valuatio-worldbank-v1",
        "_description": "Annual GDP + trade composition from the World Bank "
                        "(free API, no key). Data is typically 6-18 months lagged.",
        "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
        "source": "World Bank (api.worldbank.org)",
        "countries": out_countries,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(out_countries)} countries → {OUT_PATH}")


if __name__ == "__main__":
    main()
