#!/usr/bin/env python3
"""
quad_nowcast.py — a FRESH, market-based nowcast of the growth/inflation Quad.

The pipeline's quad.json `current` is built from FRED GDP + CPI, which lag ~2
months (asOf is stale). This nowcast instead reads the DAILY market series that
update every session — oil, HY credit spreads, the 2s10s curve, VIX, nominal
yields, the dollar — and infers the *direction* of growth and inflation from
their recent rate-of-change. It answers "what quad does the market imply TODAY",
independent of the lagged government prints and independent of the quad-history
chart (which keeps using the GDP/CPI series).

It rewrites quad.json's `current` with the nowcast (asOf = today) and preserves
`history`, so the chart still shows the official monthly path while the hero shows
a fresh estimate. Growth↑Inflation↑=Q2, ↑↓=Q1, ↓↑=Q3, ↓↓=Q4.
"""
import json, os, math
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACRO = os.path.join(ROOT, "data", "macro")
QUAD = os.path.join(MACRO, "quad.json")

def _load(series):
    p = os.path.join(MACRO, series + ".json")
    if not os.path.exists(p): return []
    d = json.load(open(p))
    obs = d.get("observations") if isinstance(d, dict) else d
    out = []
    for o in (obs or []):
        try: out.append((o.get("date"), float(o.get("value"))))
        except (TypeError, ValueError): continue
    return out

def _zchange(series, n, lookback=260):
    """z-score of the current n-day change vs the distribution of n-day changes
    over the recent `lookback` window. Returns 0 if not enough data."""
    if len(series) < n + 5: return 0.0
    vals = [v for _, v in series]
    changes = [vals[i] - vals[i - n] for i in range(n, len(vals))]
    recent = changes[-lookback:] if len(changes) > lookback else changes
    if len(recent) < 8: return 0.0
    mu = sum(recent) / len(recent)
    var = sum((c - mu) ** 2 for c in recent) / len(recent)
    sd = math.sqrt(var) or 1e-9
    cur = vals[-1] - vals[-1 - n]
    return (cur - mu) / sd

def _sig(series, n21=21, n63=63):
    """Freshness-weighted directional signal in ~[-1,1]: 60% 1-month, 40% 3-month."""
    z = 0.6 * _zchange(series, n21) + 0.4 * _zchange(series, n63)
    return math.tanh(z / 1.5)

def nowcast():
    S = {k: _load(k) for k in ["DCOILWTICO", "BAMLH0A0HYM2", "T10Y2Y", "VIXCLS", "DGS10", "DGS2", "DEXUSEU"]}
    sig = {k: _sig(v) for k, v in S.items() if v}

    # ---- GROWTH: tighter credit (+), steeper curve (+), rising oil/demand (+),
    #      falling VIX (+), rising 2Y/hawkish-on-strength (+) ----
    gw = {"BAMLH0A0HYM2": -0.30, "T10Y2Y": 0.25, "DCOILWTICO": 0.15, "VIXCLS": -0.20, "DGS2": 0.10}
    growth = sum(w * sig.get(k, 0.0) for k, w in gw.items())
    # ---- INFLATION: rising oil (+, primary), rising 10Y (+), weaker dollar
    #      (EURUSD up = +), steeper curve as a term-premium proxy (+) ----
    iw = {"DCOILWTICO": 0.45, "DGS10": 0.25, "DEXUSEU": 0.20, "T10Y2Y": 0.10}
    inflation = sum(w * sig.get(k, 0.0) for k, w in iw.items())

    DEAD = 0.04
    g_up = growth >= 0 if abs(growth) < DEAD else growth > 0
    i_up = inflation >= 0 if abs(inflation) < DEAD else inflation > 0
    quad = 2 if (g_up and i_up) else 1 if (g_up and not i_up) else 3 if (not g_up and i_up) else 4
    names = {1: "Goldilocks (growth\u2191 inflation\u2193)", 2: "Reflation (growth\u2191 inflation\u2191)",
             3: "Stagflation (growth\u2193 inflation\u2191)", 4: "Deflation (growth\u2193 inflation\u2193)"}
    favors = {1: ["XLK", "XLY", "XLC"], 2: ["XLE", "XLF", "XLI", "XLB"], 3: ["XLE", "GLD", "XLU"], 4: ["XLU", "XLP", "TLT", "XLV"]}
    hurts = {1: ["XLE", "XLU"], 2: ["XLU", "XLP"], 3: ["XLK", "XLY"], 4: ["XLE", "XLF", "XLB"]}
    conf = min(1.0, (abs(growth) + abs(inflation)) / 0.5)

    def dir_lbl(v): return "accelerating" if (v > DEAD) else "decelerating" if (v < -DEAD) else "flat"
    drivers = sorted(({"signal": k, "value": round(v, 3)} for k, v in sig.items()), key=lambda d: abs(d["value"]), reverse=True)
    return {
        "quad": quad, "label": names[quad], "asOf": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "method": "market-nowcast", "source": "daily market ROC (oil, HY OAS, 2s10s, VIX, UST, USD)",
        "growthScore": round(growth, 4), "inflationScore": round(inflation, 4),
        "growthDir": dir_lbl(growth), "inflationDir": dir_lbl(inflation),
        "confidence": round(conf, 3), "favors": favors[quad], "hurts": hurts[quad],
        "drivers": drivers,
        "asOfInputs": {k: (v[-1][0] if v else None) for k, v in S.items()},
    }

def main():
    nc = nowcast()
    doc = {}
    if os.path.exists(QUAD):
        try: doc = json.load(open(QUAD))
        except Exception: doc = {}
    prevOfficial = doc.get("current")
    if prevOfficial and prevOfficial.get("method") != "market-nowcast":
        doc["officialLatest"] = prevOfficial          # keep the last GDP/CPI print for reference
    ref = doc.get("officialLatest") or (prevOfficial if (prevOfficial and prevOfficial.get("method") != "market-nowcast") else {})
    for k in ("growthYoY", "inflationYoY", "growthRoC", "inflationRoC"):
        if isinstance(ref, dict) and k in ref:
            nc[k] = ref[k]                              # last official value (context / back-compat)
    if isinstance(ref, dict):
        nc["officialAsOf"] = ref.get("asOf")
    doc.setdefault("_schema", "valuatio-macro-quad/v1")
    doc["_nowcast_note"] = "current = fresh market nowcast (quad_nowcast.py); history = FRED GDP/CPI monthly; officialLatest = last GDP/CPI print."
    doc["current"] = nc
    doc["nowcast_generated_at"] = datetime.now(timezone.utc).isoformat()
    json.dump(doc, open(QUAD, "w"), indent=1)
    print(f"NOWCAST → Quad {nc['quad']} · {nc['label']} · asOf {nc['asOf']} · conf {nc['confidence']}")
    print(f"  growth={nc['growthScore']:+.3f} ({nc['growthDir']}) · inflation={nc['inflationScore']:+.3f} ({nc['inflationDir']})")
    print("  drivers:", ", ".join(f"{d['signal']} {d['value']:+.2f}" for d in nc["drivers"]))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
