#!/usr/bin/env python3
"""
fetch_macro_quad.py — compute the REAL macro quad from FRED and persist it.

The app derives its growth/inflation "quad" from FRED at runtime (Real GDP YoY +
CPI YoY, then the rate-of-change of each). The bot runner can't fetch FRED the
same way, so it was using a market-implied PROXY. This script writes the actual
quad to a file the runner reads, giving true macro parity:

    data/macro/quad.json

It replicates the app's logic exactly:
  - GDPC1  (Real GDP, quarterly)  → YoY = value / value[-4] - 1
  - CPIAUCSL (CPI, monthly)       → YoY = value / value[-12] - 1
  - align GDP YoY onto CPI dates (buildQuadHistory)
  - growthRoC    = gdpYoY - priorGdpYoY
  - inflationRoC = cpiYoY - cpiYoY 3 months prior
  - quad = classifyQuad(growthRoC, inflationRoC):
        growth↑ inflation↓ → 1 (Goldilocks)
        growth↑ inflation↑ → 2 (Reflation)
        growth↓ inflation↑ → 3 (Stagflation)
        growth↓ inflation↓ → 4 (Deflation)

Requires the FRED key you already use, as a repo secret:  FED_API_KEY
(Free at https://fred.stlouisfed.org/docs/api/api_key.html)

Stdlib only.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "macro"
OUT = OUT_DIR / "quad.json"

FRED_KEY = os.environ.get("FED_API_KEY", "").strip()
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

SERIES = {"gdp": "GDPC1", "cpi": "CPIAUCSL"}

QUAD_LABELS = {
    1: "Goldilocks (growth↑ inflation↓)",
    2: "Reflation (growth↑ inflation↑)",
    3: "Stagflation (growth↓ inflation↑)",
    4: "Deflation (growth↓ inflation↓)",
}
# Which sectors each quad favors / hurts (mirrors the app's SECTOR_ETFS map),
# embedded so the runner (and you) can read it straight from the file.
QUAD_SECTOR_FAVORS = {
    1: {"favors": ["XLK", "XLY", "XLC", "XLI"], "hurts": ["XLP", "XLU"]},
    2: {"favors": ["XLE", "XLF", "XLI", "XLB"], "hurts": ["XLU", "XLP"]},
    3: {"favors": ["XLE", "XLU", "XLB", "XLV"], "hurts": ["XLY", "XLK", "XLF"]},
    4: {"favors": ["XLP", "XLV", "XLU"], "hurts": ["XLK", "XLB", "XLE"]},
}


def log(*a):
    print("[macro_quad]", *a, flush=True)


def fetch_fred(series_id):
    params = {
        "series_id": series_id,
        "api_key": FRED_KEY,
        "file_type": "json",
        "observation_start": "2005-01-01",
    }
    url = FRED_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "valuatio-macro-quad"})
    with urllib.request.urlopen(req, timeout=45) as r:
        payload = json.loads(r.read().decode("utf-8"))
    obs = payload.get("observations", [])
    out = []
    for o in obs:
        v = o.get("value")
        if v in (None, ".", ""):
            continue
        try:
            out.append({"date": o["date"], "value": float(v)})
        except (TypeError, ValueError):
            continue
    return out  # oldest → newest


def yoy_series(raw, periods_per_year):
    """value / value[-periodsPerYear] - 1 — identical to computeYoYSeriesFromFred."""
    out = []
    for i in range(periods_per_year, len(raw)):
        cur = raw[i]["value"]
        prior = raw[i - periods_per_year]["value"]
        if prior > 0:
            out.append({"date": raw[i]["date"], "yoy": cur / prior - 1})
    return out


def months_shift(date_str, k):
    """'YYYY-MM-01' moved k months (k > 0 back in time)."""
    y, m = int(date_str[:4]), int(date_str[5:7]) - k
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}-01"


def build_quad_history(gdp_yoy, cpi_yoy, lookback=30):
    """z177: the inflation RoC is CPI YoY minus CPI YoY of the month THREE CALENDAR MONTHS earlier - looked up by date.
    It used to step back three ROWS, so across a missing month (October 2025: no CPI was published) it reached four
    months back, and the first rows of the window used one- and two-month windows. A missing comparison month is
    interpolated from its two neighbours (flagged); with no neighbours the row is left out."""
    by_date = {c["date"]: c["yoy"] for c in cpi_yoy}

    def yoy_at(d):
        if d in by_date:
            return by_date[d], False
        a, b = months_shift(d, 1), months_shift(d, -1)
        if a in by_date and b in by_date:
            return (by_date[a] + by_date[b]) / 2, True
        return None, False

    recent = cpi_yoy[-lookback:]
    result = []
    for cpi_point in recent:
        prior, interp = yoy_at(months_shift(cpi_point["date"], 3))
        if prior is None:
            continue
        inflation_roc = cpi_point["yoy"] - prior

        # latest GDP YoY at or before this CPI date (FRED dates a quarter by its first day)
        gdp_point = next((g for g in reversed(gdp_yoy) if g["date"] <= cpi_point["date"]), None)
        gdp_prior = None
        if gdp_point:
            gdp_prior = next((g for g in reversed(gdp_yoy) if g["date"] < gdp_point["date"]), None)
        if not gdp_point or not gdp_prior:
            continue
        growth_roc = gdp_point["yoy"] - gdp_prior["yoy"]

        row = {
            "date": cpi_point["date"],
            "growthYoY": round(gdp_point["yoy"], 6),
            "inflationYoY": round(cpi_point["yoy"], 6),
            "growthRoC": round(growth_roc, 6),
            "inflationRoC": round(inflation_roc, 6),
            "quad": classify_quad(growth_roc, inflation_roc),
            "gdpQuarter": gdp_point["date"],
        }
        if interp:
            row["interpolated"] = "comparison month missing - average of its neighbours"
        result.append(row)
    return result


def classify_quad(growth_roc, inflation_roc):
    g_up = growth_roc > 0
    i_up = inflation_roc > 0
    if g_up and not i_up:
        return 1
    if g_up and i_up:
        return 2
    if not g_up and i_up:
        return 3
    return 4


# ---------------------------------------------------------------------------------------------- z177: one shape
# quad.json used to be REWRITTEN by each script: this one at 0915 ET (current = the official GDP/CPI quad), the market
# nowcast after the close (current = its own quad). So "the quad" read by the bot and the app flipped twice a day
# (10-26..11-06: official Q3 by day, nowcast Q4 by night), the certainty score lost its official reference every
# morning, and the nowcast's track record logged official prints as if they were calls. Now both scripts keep both:
#   official   the last GDP/CPI quad (this script)            nowcast   the market nowcast (quad_nowcast.py)
#   current    the nowcast while it is fresh (<= 4 days), else the official quad - one answer all day
#   agree      same quad? which axes (growth / inflation) agree?          officialLatest   = official (back-compat)
def load_doc():
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {}


def _et_date():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def agreement(official, nowcast):
    if not (official and nowcast and official.get("quad") in QUAD_LABELS and nowcast.get("quad") in QUAD_LABELS):
        return None
    axes = {1: (1, -1), 2: (1, 1), 3: (-1, 1), 4: (-1, -1)}
    a, b = axes[official["quad"]], axes[nowcast["quad"]]
    return {"sameQuad": a == b, "growth": a[0] == b[0], "inflation": a[1] == b[1], "axesShared": (a[0] == b[0]) + (a[1] == b[1]),
            "note": ("the market nowcast and the official GDP/CPI quad agree" if a == b else
                     f"the market nowcast (Q{nowcast['quad']}) and the official GDP/CPI quad (Q{official['quad']}) disagree on "
                     + " and ".join(x for x, ok in (("growth", a[0] == b[0]), ("inflation", a[1] == b[1])) if not ok))}


def merge_doc(doc, official=None, nowcast=None, history=None, fresh_days=4):
    doc = dict(doc or {})
    if official is None:
        cur = doc.get("current") or {}
        official = doc.get("official") or doc.get("officialLatest") or (cur if cur.get("method") != "market-nowcast" and cur.get("quad") else None)
    if nowcast is None:
        cur = doc.get("current") or {}
        nowcast = doc.get("nowcast") or (cur if cur.get("method") == "market-nowcast" else None)
    fresh = False
    if nowcast and nowcast.get("asOf"):
        try:
            fresh = (_et_date() - datetime.strptime(str(nowcast["asOf"])[:10], "%Y-%m-%d").date()).days <= fresh_days
        except ValueError:
            fresh = False
    doc.update({"_schema": "valuatio-macro-quad/v2",
                "_description": "Growth/inflation quad (1 Goldilocks, 2 Reflation, 3 Stagflation, 4 Deflation). official = FRED GDPC1 + "
                                "CPIAUCSL (lags ~1-2 months); nowcast = daily market rates of change; current = the nowcast while fresh, "
                                "else official.",
                "official": official, "officialLatest": official, "nowcast": nowcast,
                "current": nowcast if (nowcast and fresh) else official,
                "agree": agreement(official, nowcast),
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": "FRED (GDPC1, CPIAUCSL) + market nowcast"})
    if history is not None:
        doc["history"] = history
    doc.pop("_nowcast_note", None)
    return doc


def main():
    if not FRED_KEY:
        log("⚠ FED_API_KEY not set — DRY RUN (no FRED calls).")
        log("Set the secret to compute the real quad. Would write data/macro/quad.json.")
        return

    log("Fetching GDPC1 (Real GDP)…")
    gdp_raw = fetch_fred(SERIES["gdp"])
    log(f"  {len(gdp_raw)} GDP observations")
    log("Fetching CPIAUCSL (CPI)…")
    cpi_raw = fetch_fred(SERIES["cpi"])
    log(f"  {len(cpi_raw)} CPI observations")

    if len(gdp_raw) < 8 or len(cpi_raw) < 16:
        log("✗ insufficient FRED data — not writing")
        sys.exit(1)

    gdp_yoy = yoy_series(gdp_raw, 4)     # quarterly
    cpi_yoy = yoy_series(cpi_raw, 12)    # monthly
    history = build_quad_history(gdp_yoy, cpi_yoy, lookback=30)
    if not history:
        log("✗ could not align GDP and CPI — not writing")
        sys.exit(1)

    current = history[-1]
    quad = current["quad"]
    official = {
        "quad": quad,
        "label": QUAD_LABELS[quad],
        "method": "official-fred",
        "source": "FRED GDPC1 + CPIAUCSL",
        "asOf": current["date"],
        "gdpQuarter": current.get("gdpQuarter"),
        "growthYoY": current["growthYoY"],
        "inflationYoY": current["inflationYoY"],
        "growthRoC": current["growthRoC"],
        "inflationRoC": current["inflationRoC"],
        "growthDir": "accelerating" if current["growthRoC"] > 0 else "decelerating",
        "inflationDir": "accelerating" if current["inflationRoC"] > 0 else "decelerating",
        "favors": QUAD_SECTOR_FAVORS[quad]["favors"],
        "hurts": QUAD_SECTOR_FAVORS[quad]["hurts"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out = merge_doc(load_doc(), official=official, history=history[-18:])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    # z109: the FULL official quad history (FRED from 2005) for backtests - e.g.
    # pattern success by macro quad. knownLagMonths: a month's quad isn't known
    # until its GDP / CPI print, so consumers should label a date with the quad
    # from knownLagMonths earlier (no lookahead). Uses today's revised FRED data,
    # not the first-release vintage - a known, documented limitation.
    full = build_quad_history(gdp_yoy, cpi_yoy, lookback=len(cpi_yoy))
    (OUT_DIR / "quad_history_full.json").write_text(json.dumps({
        "method": "GDPC1 YoY RoC (vs prior quarter) x CPIAUCSL YoY RoC (3 months), monthly on CPI dates",
        "knownLagMonths": 3, "vintage": "latest revised (not first release)",
        "rows": [{"date": h["date"], "quad": h["quad"], "growthRoC": h["growthRoC"], "inflationRoC": h["inflationRoC"]} for h in full],
    }, separators=(",", ":")))
    log(f"Wrote quad_history_full.json ({len(full)} months, {full[0]['date'] if full else '-'} -> {full[-1]['date'] if full else '-'})")
    c = out["official"]
    log(f"Official quad {quad} — {c['label']} · as of {c['asOf']} · "
        f"growth {c['growthYoY']*100:.1f}% ({c['growthDir']}), "
        f"inflation {c['inflationYoY']*100:.1f}% ({c['inflationDir']})")
    cur = out["current"] or {}
    log(f"current (what the app and the bot read): Quad {cur.get('quad')} · {cur.get('method')} · as of {cur.get('asOf')}"
        + (f" · {out['agree']['note']}" if out.get("agree") else ""))
    log(f"Wrote {OUT}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        log(f"FATAL HTTP {e.code} — check FED_API_KEY")
        sys.exit(1)
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
