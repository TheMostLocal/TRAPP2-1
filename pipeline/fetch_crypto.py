#!/usr/bin/env python3
"""
fetch_crypto.py — Fetch crypto spot prices and write data/crypto/prices.json

Crypto is a 24h market, so this runs on the same fast cadence as FX.
Symbols use the app's -USD convention (BTC-USD, ETH-USD).

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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
OUT_PATH = os.path.join(REPO_ROOT, "data", "crypto", "prices.json")

# Mirror the crypto tickers in your tickers.txt
CRYPTO_SYMBOLS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD",
    "ADA-USD", "DOGE-USD", "AVAX-USD", "DOT-USD", "LINK-USD",
    "MATIC-USD", "LTC-USD",
]

def fetch_one(symbol):
    try:
        tk = yf.Ticker(symbol)
        h = tk.history(period="2d")
        if h.empty:
            return None
        closes = h["Close"].dropna()
        price = float(closes.iloc[-1])
        prev = float(closes.iloc[-2]) if len(closes) >= 2 else price
        change = ((price - prev) / prev * 100) if prev else 0.0
        info = {}
        try:
            info = tk.info or {}
        except Exception:
            pass
        return {
            "price": round(price, 6),
            "change24hPct": round(change, 2),
            "marketCap": int(info.get("marketCap") or 0),
            "volume24h": int(info.get("volume24Hr") or info.get("volume") or 0),
        }
    except Exception as e:
        print(f"  ! {symbol}: {e}", file=sys.stderr)
        return None

def main():
    prices = {}
    for sym in CRYPTO_SYMBOLS:
        rec = fetch_one(sym)
        if rec is None:
            print(f"  skip {sym} — no data", file=sys.stderr)
            continue
        prices[sym] = rec
        print(f"  {sym}: ${rec['price']:,.2f}  ({rec['change24hPct']:+.2f}%)")

    out = {
        "_schema": "valuatio-crypto-prices-v1",
        "_description": "Crypto spot prices in USD, 24h market.",
        "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
        "quote": "USD",
        "source": "fetch_crypto.py (yfinance)",
        "prices": prices,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {len(prices)} crypto prices to {OUT_PATH}")

if __name__ == "__main__":
    main()
