#!/usr/bin/env python3
"""
fetch_fx.py — Fetch USD-anchored FX rates and write data/fx/rates.json

Standardizes on ONE pair convention per currency (USD<ccy>=X or <ccy>USD=X
depending on market quoting) so the app never sees a bare-form KRW=X with a
wrong 1.00 alongside a correct USDKRW=X. This was the root cause of foreign
equities (SK hynix, Hyundai) not converting to USD.

Run every 15-30 min via GitHub Actions since FX is a ~24h market.

Output path resolution: writes to <repo-root>/data/fx/rates.json regardless of
where the script is invoked from. The script lives in pipeline/ but the data
must land at the repo root so the app can fetch it from raw.githubusercontent.

Requires: yfinance  (pip install yfinance)
"""
import json
import datetime
import os
import sys

try:
    import yfinance as yf
except ImportError:
    print("yfinance not installed. Run: pip install yfinance", file=sys.stderr)
    sys.exit(1)

# Resolve repo root = parent of the directory this script lives in (pipeline/).
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)               # pipeline/ -> repo root
OUT_PATH = os.path.join(REPO_ROOT, "data", "fx", "rates.json")

# currency -> (yahoo symbol, quote_convention)
#   "inverse" = symbol is USD/<ccy> (units of ccy per USD), so usdPer = 1/price
#   "direct"  = symbol is <ccy>/USD (USD per ccy),           so usdPer = price
FX_SYMBOLS = {
    "EUR": ("EURUSD=X", "direct"),
    "GBP": ("GBPUSD=X", "direct"),
    "AUD": ("AUDUSD=X", "direct"),
    "NZD": ("NZDUSD=X", "direct"),
    "JPY": ("USDJPY=X", "inverse"),
    "KRW": ("USDKRW=X", "inverse"),
    "CNY": ("USDCNY=X", "inverse"),
    "HKD": ("USDHKD=X", "inverse"),
    "TWD": ("USDTWD=X", "inverse"),
    "INR": ("USDINR=X", "inverse"),
    "CAD": ("USDCAD=X", "inverse"),
    "CHF": ("USDCHF=X", "inverse"),
    "SGD": ("USDSGD=X", "inverse"),
    "SEK": ("USDSEK=X", "inverse"),
    "NOK": ("USDNOK=X", "inverse"),
    "DKK": ("USDDKK=X", "inverse"),
    "BRL": ("USDBRL=X", "inverse"),
    "MXN": ("USDMXN=X", "inverse"),
    "ZAR": ("USDZAR=X", "inverse"),
    "SAR": ("USDSAR=X", "inverse"),
    "ILS": ("USDILS=X", "inverse"),
    "THB": ("USDTHB=X", "inverse"),
    "IDR": ("USDIDR=X", "inverse"),
    "MYR": ("USDMYR=X", "inverse"),
    "RUB": ("USDRUB=X", "inverse"),
    "PLN": ("USDPLN=X", "inverse"),
    "TRY": ("USDTRY=X", "inverse"),
}

def last_close(symbol):
    """Most recent close for a symbol, or None."""
    try:
        h = yf.Ticker(symbol).history(period="5d")
        if h.empty:
            return None
        return float(h["Close"].dropna().iloc[-1])
    except Exception as e:
        print(f"  ! {symbol}: {e}", file=sys.stderr)
        return None

def main():
    rates = {}
    for ccy, (symbol, conv) in FX_SYMBOLS.items():
        price = last_close(symbol)
        if price is None or price <= 0:
            print(f"  skip {ccy} ({symbol}) — no price", file=sys.stderr)
            continue
        if conv == "inverse":
            per_usd = price            # units of ccy per USD
            usd_per = 1.0 / price      # USD per unit of ccy
        else:  # direct
            usd_per = price            # USD per unit of ccy
            per_usd = 1.0 / price      # units of ccy per USD
        rates[ccy] = {
            "usdPer": round(usd_per, 8),
            "perUsd": round(per_usd, 6),
            "pair": symbol,
        }
        print(f"  {ccy}: 1 {ccy} = ${usd_per:.6f}  (1 USD = {per_usd:.4f} {ccy})")

    out = {
        "_schema": "valuatio-fx-rates-v1",
        "_description": "USD-anchored FX rates, one convention per currency.",
        "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
        "base": "USD",
        "source": "fetch_fx.py (yfinance)",
        "rates": rates,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(rates)} FX rates to {OUT_PATH}")

if __name__ == "__main__":
    main()
