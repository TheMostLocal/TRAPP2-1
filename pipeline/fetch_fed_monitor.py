#!/usr/bin/env python3
"""
fetch_fed_monitor.py — QE/QT and Fed-reaction monitor for the app's Fed tab (z92).

Writes data/fed/monitor.json from four source groups:

  1. H.4.1 balance sheet (weekly, Thursday 4:30pm ET), via FRED:
       WALCL     total assets                 WSHOTSL  Treasuries held outright
       WSHOMCB   MBS held outright            WRESBAL  reserve balances
       WTREGEN   Treasury General Account     RRPONTSYD overnight reverse repo (daily)
     -> QE / QT state and pace (4-week and 13-week change, monthly run-rate,
        split Treasuries vs MBS) and "net liquidity" = assets - TGA - RRP.
  2. FOMC communication: statements, minutes, implementation notes, SEP
     (federalreserve.gov press_monetary RSS), plus SEP medians from FRED
     (FEDTARMD fed funds, FEDTARMDLR longer run, PCECTPIMD, UNRATEMD, GDPC1MD).
  3. Speeches by the Chair and Governors (federalreserve.gov speeches RSS).
  4. New York Fed SOMA holdings summary and the latest open-market operation
     results (markets.newyorkfed.org public API).

Every group is best-effort: a source that fails keeps the PREVIOUS run's data
for that group (marked stale with the error) instead of blanking the panel.
Stdlib only.
"""
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from fetch_macro import fetch_series          # same FRED client the macro job uses
except Exception:                                  # pragma: no cover
    fetch_series = None

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "fed" / "monitor.json"
UA = {"User-Agent": "Mozilla/5.0 (Valuatio fed monitor; github actions)"}

BS_SERIES = {"WALCL": "Total assets", "WSHOTSL": "Treasuries held outright", "WSHOMCB": "MBS held outright",
             "WRESBAL": "Reserve balances", "WTREGEN": "Treasury General Account", "RRPONTSYD": "Overnight reverse repo"}
SEP_SERIES = {"FEDTARMD": "Fed funds (median)", "FEDTARMDLR": "Fed funds, longer run (median)",
              "PCECTPIMD": "PCE inflation (median)", "UNRATEMD": "Unemployment (median)", "GDPC1MD": "Real GDP growth (median)"}
FEEDS = {"monetary": "https://www.federalreserve.gov/feeds/press_monetary.xml",
         "speeches": "https://www.federalreserve.gov/feeds/speeches.xml"}
NYFED = "https://markets.newyorkfed.org/api"
NYFED_ENDPOINTS = {
    "somaSummary": f"{NYFED}/soma/summary.json",
    "repoLatest": f"{NYFED}/rp/all/all/results/latest.json",
    "treasuryOpsLatest": f"{NYFED}/tsy/all/results/summary/latest.json",
}


def log(*a):
    print("[fed-monitor]", *a, flush=True)


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- 1. H.4.1 ----
def balance_sheet(api_key, start):
    if not (api_key and fetch_series):
        raise RuntimeError("FRED_API_KEY not set")
    series = {}
    for sid in BS_SERIES:
        d = fetch_series(sid, api_key, start=start)
        if d and d.get("observations"):
            series[sid] = [[o["date"], round(o["value"], 1)] for o in d["observations"]]
    if "WALCL" not in series:
        raise RuntimeError("WALCL unavailable")
    return series, summarize_balance_sheet(series)


def _val_on_or_before(rows, day):
    v = None
    for d, x in rows:
        if d <= day:
            v = x
        else:
            break
    return v


def summarize_balance_sheet(series):
    """$ millions in, $ billions out. QT = assets falling at a sustained pace."""
    w = series["WALCL"]
    last_d, last_v = w[-1]
    ago = lambda days: _val_on_or_before(w, (datetime.fromisoformat(last_d) - timedelta(days=days)).strftime("%Y-%m-%d"))
    v4, v13, v52 = ago(28), ago(91), ago(364)
    ch4 = (last_v - v4) / 1000 if v4 else None
    ch13 = (last_v - v13) / 1000 if v13 else None
    monthly = ch13 / 3 if ch13 is not None else None          # $bn per month, 13-week run-rate
    stance = "QT (shrinking)" if (monthly is not None and monthly < -10) else \
             "QE (expanding)" if (monthly is not None and monthly > 10) else "Stable / reinvesting"

    def comp(sid):
        rows = series.get(sid)
        if not rows:
            return None
        lv = rows[-1][1]
        p13 = _val_on_or_before(rows, (datetime.fromisoformat(rows[-1][0]) - timedelta(days=91)).strftime("%Y-%m-%d"))
        return {"asOf": rows[-1][0], "bn": round(lv / 1000, 1), "monthlyBn": round((lv - p13) / 3000, 1) if p13 else None}

    tga = series.get("WTREGEN", [])
    rrp = series.get("RRPONTSYD", [])
    net = []
    for d, a in w:
        t = _val_on_or_before(tga, d)
        r = _val_on_or_before(rrp, d)
        if t is not None and r is not None:
            net.append([d, round((a - t - r * 1000) / 1000, 1)])   # WALCL/WTREGEN are $mn, RRPONTSYD is $bn
    return {
        "asOf": last_d, "totalBn": round(last_v / 1000, 1),
        "change4wBn": round(ch4, 1) if ch4 is not None else None,
        "change13wBn": round(ch13, 1) if ch13 is not None else None,
        "monthlyPaceBn": round(monthly, 1) if monthly is not None else None,
        "change52wBn": round((last_v - v52) / 1000, 1) if v52 else None,
        "stance": stance,
        "treasuries": comp("WSHOTSL"), "mbs": comp("WSHOMCB"), "reserves": comp("WRESBAL"),
        "tga": comp("WTREGEN"),
        "rrp": ({"asOf": rrp[-1][0], "bn": round(rrp[-1][1], 1)} if rrp else None),
        "netLiquidityBn": net[-1][1] if net else None,
        "netLiquidity": net[-130:],                                   # ~2.5 years weekly
        "totalAssets": [[d, round(v / 1000, 1)] for d, v in w[-130:]],
    }


# ------------------------------------------------------------ 2/3. RSS ----
def classify(title):
    t = title.lower()
    if "minutes" in t:
        return "minutes"
    if "projection" in t or "summary of economic" in t:
        return "projections"
    if "implementation note" in t:
        return "implementation"
    if "fomc statement" in t or ("federal reserve issues fomc statement" in t) or ("statement" in t and "fomc" in t):
        return "statement"
    if "balance sheet" in t or "reinvest" in t or "runoff" in t or "redemption" in t:
        return "balance-sheet"
    return "other"


def parse_rss(raw, kind):
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        title = unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(it.findtext("description") or ""))).strip()
        ts = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(ts).astimezone(timezone.utc).isoformat(timespec="minutes") if ts else None
        except Exception:
            ts = None
        if not title or not link:
            continue
        row = {"title": title, "link": link, "date": ts, "summary": desc[:280]}
        if kind == "speeches":
            m = re.match(r"^\s*([A-Z][A-Za-z.\- ]+?)\s*,\s*(.+)$", title)        # "Powell, Economic Outlook"
            row["speaker"] = m.group(1) if m else None
            row["chair"] = bool(re.search(r"\bpowell\b|\bchair\b", title + " " + desc, re.I))
        else:
            row["type"] = classify(title)
        items.append(row)
    return items[:30]


# ---------------------------------------------------------------- 4. NY Fed ----
def nyfed():
    out = {}
    for k, url in NYFED_ENDPOINTS.items():
        try:
            out[k] = json.loads(get(url))
        except Exception as e:
            out[k] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    return out


def main():
    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text())
        except Exception:
            prev = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = {"generatedAt": now, "sources": {}}
    api_key = (os.environ.get("FRED_API_KEY") or os.environ.get("FED_API_KEY") or "").strip()
    start = (datetime.now(timezone.utc) - timedelta(days=3 * 365)).strftime("%Y-%m-%d")

    def keep(group, err):
        out[group] = prev.get(group)
        out["sources"][group] = {"ok": False, "error": err, "stale": True if prev.get(group) else False}
        log(f"  ✗ {group}: {err}" + (" (kept previous)" if prev.get(group) else ""))

    try:
        _, bs = balance_sheet(api_key, start)
        out["balanceSheet"] = bs
        out["sources"]["balanceSheet"] = {"ok": True}
        log(f"  ✓ H.4.1: total ${bs['totalBn']:,.0f}bn as of {bs['asOf']} · pace {bs['monthlyPaceBn']}bn/mo · {bs['stance']}")
    except Exception as e:
        keep("balanceSheet", f"{type(e).__name__}: {e}")

    try:
        sep = {}
        if api_key and fetch_series:
            for sid, label in SEP_SERIES.items():
                d = fetch_series(sid, api_key, start="2015-01-01")
                if d and d.get("observations"):
                    obs = d["observations"]
                    sep[sid] = {"label": label, "series": [[o["date"], o["value"]] for o in obs[-12:]]}
        if not sep:
            raise RuntimeError("no SEP series")
        out["projections"] = sep
        out["sources"]["projections"] = {"ok": True}
        log(f"  ✓ SEP medians: {', '.join(sep)}")
    except Exception as e:
        keep("projections", f"{type(e).__name__}: {e}")

    for group, url in FEEDS.items():
        try:
            items = parse_rss(get(url), group)
            if not items:
                raise RuntimeError("feed returned no items")
            out[group] = items
            out["sources"][group] = {"ok": True, "count": len(items)}
            log(f"  ✓ {group}: {len(items)} items · latest: {items[0]['title'][:70]}")
        except Exception as e:
            keep(group, f"{type(e).__name__}: {str(e)[:120]}")

    ny = nyfed()
    if all("error" in v for v in ny.values()):
        keep("nyfed", "; ".join(v["error"] for v in ny.values())[:240])
    else:
        out["nyfed"] = ny
        out["sources"]["nyfed"] = {"ok": True, "errors": {k: v["error"] for k, v in ny.items() if "error" in v}}
        log(f"  ✓ NY Fed: {', '.join(k for k, v in ny.items() if 'error' not in v)}")

    if not any(s.get("ok") for s in out["sources"].values()) and prev:
        log("::warning::every source failed - monitor.json left unchanged")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":"), allow_nan=False))
    log(f"✓ wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
