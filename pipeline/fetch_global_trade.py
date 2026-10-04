#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TRAPP2-1 repo  →  pipeline/fetch_global_trade.py  <<<
#  GoodGlobeLLC/TRAPP2-1/pipeline/fetch_global_trade.py
#
#  NEW FILE. Add it to TRAPP2-1, then add a step to that repo's nightly
#  workflow to run it (it writes data/global_trade.json which the app reads):
#      - name: Fresh global-trade GDP
#        run: python pipeline/fetch_global_trade.py
#  Keyless (IMF + World Bank public APIs). The frontend's loadGtFreshGdp()
#  reads the output and overrides the baked-in GDP with these fresher values.
# ============================================================
"""
fetch_global_trade.py — fresh macro data for Valuatio's Global Trade tab.

Writes data/global_trade.json with the freshest GDP available per country,
preferring IMF WEO estimates (which carry CURRENT-year and next-year forecasts)
over World Bank actuals (which lag ~1 year). The frontend reads this file and
overrides its hardcoded GDP/year, so the tab shows 2025/2026 figures instead of
2024, and those values normalize through the rest of the app.

Sources, in priority order:
  1. IMF WEO via the IMF DataMapper API (current + forecast years, no key)
  2. World Bank API (actuals, ~1y lag) as fallback
Both are free and keyless. Best-effort: any country that fails keeps the
frontend's baked-in value, clearly tier-labeled.

Run from a repo that has a data/ dir (TRAPP2-1 is the natural home since it
already holds the international/macro vehicles).
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "global_trade.json"

# ISO3 codes for the 12 economies the tab tracks.
COUNTRIES = {
    "USA": "United States", "CHN": "China", "DEU": "Germany", "JPN": "Japan",
    "IND": "India", "GBR": "United Kingdom", "FRA": "France", "BRA": "Brazil",
    "ITA": "Italy", "CAN": "Canada", "KOR": "South Korea", "MEX": "Mexico",
}
UA = {"User-Agent": "ValuatioGlobalTrade/1.0"}


def imf_gdp_usd():
    """IMF WEO nominal GDP (current US$, billions) via DataMapper.
    Indicator NGDPD = Gross domestic product, current prices, USD billions."""
    url = "https://www.imf.org/external/datamapper/api/v1/NGDPD/" + "/".join(COUNTRIES)
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        vals = d.get("values", {}).get("NGDPD", {})
        out = {}
        # Pick the most recent year that has a value for each country.
        this_year = datetime.now(timezone.utc).year
        for iso in COUNTRIES:
            series = vals.get(iso, {})
            if not series:
                continue
            # Prefer current year, then walk back; also allow +1 forecast.
            for y in [str(this_year), str(this_year - 1), str(this_year + 1), str(this_year - 2)]:
                if y in series and series[y]:
                    out[iso] = {"gdpUsdT": round(float(series[y]) / 1000.0, 3),
                                "gdpYear": int(y), "tier": "IMF WEO estimate", "source": "IMF"}
                    break
        return out
    except Exception as e:
        print(f"  IMF fetch failed: {e}", file=sys.stderr)
        return {}


def worldbank_gdp_usd():
    """World Bank nominal GDP (current US$) — actuals, ~1y lag. Fallback."""
    codes = ";".join(COUNTRIES)
    url = (f"https://api.worldbank.org/v2/country/{codes}/indicator/NY.GDP.MKTP.CD"
           f"?format=json&date=2023:2026&per_page=400")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if len(d) < 2 or not d[1]:
            return {}
        latest = {}
        for row in d[1]:
            iso = row["countryiso3code"]
            v = row.get("value")
            yr = int(row["date"])
            if iso in COUNTRIES and v is not None:
                if iso not in latest or yr > latest[iso]["gdpYear"]:
                    latest[iso] = {"gdpUsdT": round(float(v) / 1e12, 3),
                                   "gdpYear": yr, "tier": "World Bank actual", "source": "WorldBank"}
        return latest
    except Exception as e:
        print(f"  World Bank fetch failed: {e}", file=sys.stderr)
        return {}


def main():
    imf = imf_gdp_usd()
    wb = worldbank_gdp_usd()
    print(f"IMF: {len(imf)} countries · World Bank: {len(wb)} countries")

    countries = {}
    for iso, name in COUNTRIES.items():
        # Prefer IMF (newer), fall back to World Bank, then nothing (frontend keeps baked-in).
        rec = imf.get(iso) or wb.get(iso)
        if rec:
            rec["name"] = name
            countries[iso] = rec

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gdp": countries,
        "note": "GDP nominal USD trillions. IMF WEO estimates preferred for recency; "
                "World Bank actuals as fallback. Trade-partner and commodity-production "
                "data remain annual (Census/USGS) and are labeled in-app.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    yrs = sorted({c["gdpYear"] for c in countries.values()})
    print(f"✓ global_trade.json: {len(countries)} countries, GDP years {yrs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
