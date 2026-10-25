#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-1
#  >>> FILE PATH:   pipeline/fetch_market_extras.py
#
#  z158 - official / exchange sources the app and the bot didn't have, all free:
#    vol.json      CBOE: VIX, VIX9D, VIX3M, VIX6M (term structure: contango / backwardation) and SKEW,
#                  with one-year percentiles and 260 days of history
#    treasury.json Treasury FiscalData: the latest auctions by term (high yield, bid-to-cover), debt to the penny
#    bls.json      BLS: CPI (headline, core, shelter, energy), unemployment, payrolls, hourly earnings - the
#                  source itself (BLS_API_KEY optional; without it the public v1 limits apply)
#    gdelt.json    GDELT: news tone and volume for recession, inflation, tariffs, layoffs, war, bank stress
#    checks.json   cross-checks of our own FX (vs the ECB reference via Frankfurter) and crypto (vs Coinbase
#                  and CoinGecko): any gap beyond 0.5 % FX / 1 % crypto is flagged
#  Each part runs on its own - one source down never blocks the others.  -> data/extras/
# ============================================================
import csv
import io
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "extras"
UA = {"User-Agent": "Mozilla/5.0 (valuatio extras)"}


def log(*a):
    print("[extras]", *a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=dict(UA, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def write(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, separators=(",", ":")))


def pctile(series, v):
    s = sorted(series)
    return round(sum(1 for x in s if x <= v) / len(s) * 100) if s else None


# ---------------------------------------------------------------- CBOE
def cboe(name):
    txt = get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv").decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(txt)))
    hdr = [h.strip().upper() for h in rows[0]]
    ci = hdr.index("CLOSE") if "CLOSE" in hdr else (hdr.index(name.upper()) if name.upper() in hdr else len(hdr) - 1)
    out = []
    for r in rows[1:]:
        try:
            d = datetime.strptime(r[0].strip(), "%m/%d/%Y").date().isoformat()
        except ValueError:
            try:
                d = date.fromisoformat(r[0].strip()[:10]).isoformat()
            except ValueError:
                continue
        try:
            out.append((d, float(r[ci])))
        except (ValueError, IndexError):
            continue
    return out


def vol_part():
    ser = {}
    for n in ("VIX", "VIX9D", "VIX3M", "VIX6M", "SKEW"):
        try:
            ser[n] = cboe(n)
        except Exception as e:
            log(f"CBOE {n}: {e}")
    if "VIX" not in ser:
        return
    last = {n: s[-1] for n, s in ser.items() if s}
    vix, v3m = last["VIX"][1], (last.get("VIX3M") or (None, None))[1]
    ratio = round(vix / v3m, 3) if v3m else None
    one_y = lambda n: [v for d, v in ser.get(n, [])[-252:]]
    doc = {"asOf": now_iso(), "date": last["VIX"][0], "latest": {n: v for n, (d, v) in last.items()},
           "vixTo3m": ratio, "structure": None if ratio is None else ("backwardation" if ratio > 1.0 else "contango"),
           "vixPctile1y": pctile(one_y("VIX"), vix), "skewPctile1y": pctile(one_y("SKEW"), last["SKEW"][1]) if "SKEW" in last else None,
           "history": {n: s[-260:] for n, s in ser.items()}}
    write("vol.json", doc)
    log(f"CBOE: VIX {vix} ({doc['vixPctile1y']} pctile) · VIX/VIX3M {ratio} {doc['structure']} · SKEW {last.get('SKEW', (0, None))[1]}")


# ---------------------------------------------------------------- Treasury FiscalData
FD = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"


def treasury_part():
    try:
        since = (date.today() - timedelta(days=120)).isoformat()
        j = json.loads(get(f"{FD}/v1/accounting/od/auctions_query?sort=-auction_date&page[size]=200&filter=auction_date:gte:{since}").decode())
        latest = {}
        for r in j.get("data") or []:
            if not r.get("high_yield") and not r.get("high_investment_rate") and not r.get("high_discnt_rate"):
                continue
            key = f"{r.get('security_type')} {r.get('security_term')}"
            if key not in latest:
                latest[key] = {"date": r.get("auction_date"), "type": r.get("security_type"), "term": r.get("security_term"),
                               "highYield": r.get("high_yield") or r.get("high_investment_rate") or r.get("high_discnt_rate"),
                               "bidToCover": r.get("bid_to_cover_ratio"), "offering": r.get("offering_amt")}
        debt = json.loads(get(f"{FD}/v2/accounting/od/debt_to_penny?sort=-record_date&page[size]=2").decode()).get("data") or []
        write("treasury.json", {"asOf": now_iso(), "auctions": sorted(latest.values(), key=lambda x: x["date"] or "", reverse=True),
                                "debt": {"date": debt[0].get("record_date"), "total": debt[0].get("tot_pub_debt_out_amt")} if debt else None})
        log(f"Treasury: {len(latest)} auction terms · debt to the penny {debt[0].get('tot_pub_debt_out_amt') if debt else '—'}")
    except Exception as e:
        log(f"Treasury: {e}")


# ---------------------------------------------------------------- BLS
BLS = {"CUSR0000SA0": "CPI (all items)", "CUSR0000SA0L1E": "Core CPI", "CUSR0000SAH1": "CPI shelter", "CUSR0000SA0E": "CPI energy",
       "LNS14000000": "Unemployment rate", "CES0000000001": "Nonfarm payrolls (thousands)", "CES0500000003": "Avg hourly earnings"}


def bls_part():
    key = (os.environ.get("BLS_API_KEY") or "").strip()
    y = date.today().year
    body = {"seriesid": list(BLS), "startyear": str(y - 2), "endyear": str(y)}
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/" if key else "https://api.bls.gov/publicAPI/v1/timeseries/data/"
    if key:
        body["registrationkey"] = key
    try:
        j = json.loads(get(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}).decode())
        out = {}
        for s in (j.get("Results") or {}).get("series") or []:
            rows = [(f"{r['year']}-{r['period'][1:]}", float(r["value"])) for r in s.get("data") or [] if str(r.get("period", "")).startswith("M") and r["period"] != "M13"]
            rows.sort()
            if not rows:
                continue
            last, by = rows[-1], dict(rows)
            ya = f"{int(last[0][:4]) - 1}{last[0][4:]}"
            pm = rows[-2][1] if len(rows) >= 2 else None
            sid = s.get("seriesID")
            level = sid in ("LNS14000000",)
            out[sid] = {"name": BLS.get(sid, sid), "month": last[0], "value": last[1],
                        "yoyPct": None if level or ya not in by else round((last[1] / by[ya] - 1) * 100, 2),
                        "momPct": None if level or not pm else round((last[1] / pm - 1) * 100, 2),
                        "change": round(last[1] - pm, 1) if (pm is not None and sid in ("CES0000000001", "LNS14000000")) else None,
                        "history": rows[-25:]}
        if out:
            write("bls.json", {"asOf": now_iso(), "series": out, "source": "api.bls.gov " + ("v2" if key else "v1")})
        log(f"BLS: {len(out)} series ({j.get('status')}{' · ' + '; '.join(j.get('message') or [])[:120] if j.get('message') else ''})")
    except Exception as e:
        log(f"BLS: {e}")


# ---------------------------------------------------------------- GDELT
THEMES = {"recession": "recession", "inflation": "inflation", "tariffs": "tariffs OR \"trade war\"", "layoffs": "layoffs",
          "war": "war OR invasion OR missile", "banking": "\"bank failure\" OR \"bank run\" OR \"banking crisis\""}


def gdelt_part():
    out = {}
    for k, q in THEMES.items():
        try:
            base = "https://api.gdeltproject.org/api/v2/doc/doc?format=json&timespan=60d&query=" + urllib.parse.quote(f"{q} sourcelang:english")
            tone = json.loads(get(base + "&mode=timelinetone").decode() or "{}")
            time.sleep(5.5)                                         # GDELT asks for one request every 5 s
            volj = json.loads(get(base + "&mode=timelinevol").decode() or "{}")
            time.sleep(5.5)
            t = [(p["date"][:8], p["value"]) for p in ((tone.get("timeline") or [{}])[0].get("data") or [])]
            v = [(p["date"][:8], p["value"]) for p in ((volj.get("timeline") or [{}])[0].get("data") or [])]
            if not t:
                continue
            last7 = statistics.mean(x for _, x in t[-7:])
            prev = statistics.mean(x for _, x in t[-37:-7]) if len(t) > 14 else None
            vol7 = statistics.mean(x for _, x in v[-7:]) if v else None
            vol_prev = statistics.mean(x for _, x in v[-37:-7]) if len(v) > 14 else None
            out[k] = {"tone7d": round(last7, 2), "tonePrior30d": round(prev, 2) if prev is not None else None,
                      "volume7d": round(vol7, 4) if vol7 is not None else None, "volumePrior30d": round(vol_prev, 4) if vol_prev is not None else None,
                      "volumeChangePct": round((vol7 / vol_prev - 1) * 100) if (vol7 is not None and vol_prev) else None, "tone": t[-60:]}
        except Exception as e:
            log(f"GDELT {k}: {e}")
    if out:
        write("gdelt.json", {"asOf": now_iso(), "themes": out, "note": "tone: average article tone (negative = more negative coverage); volume: share of all monitored coverage"})
    log(f"GDELT: {', '.join(f'{k} {v['tone7d']} ({v['volumeChangePct']:+}% vol)' if v.get('volumeChangePct') is not None else f'{k} {v['tone7d']}' for k, v in out.items())}")


# ---------------------------------------------------------------- cross-checks
def checks_part():
    res = {"asOf": now_iso(), "fx": [], "crypto": []}
    try:
        ours = json.loads((ROOT / "data" / "fx" / "rates.json").read_text()).get("rates") or {}
        ecb = json.loads(get("https://api.frankfurter.app/latest?from=USD").decode())
        for ccy, per_usd in (ecb.get("rates") or {}).items():
            o = ours.get(ccy)
            if not (o and o.get("perUsd")):
                continue
            gap = (o["perUsd"] / per_usd - 1) * 100
            res["fx"].append({"ccy": ccy, "ours": o["perUsd"], "ecb": per_usd, "gapPct": round(gap, 3), "flag": abs(gap) > 0.5})
        res["fxDate"] = ecb.get("date")
    except Exception as e:
        log(f"FX check: {e}")
    try:
        ours = json.loads((ROOT / "data" / "crypto" / "prices.json").read_text()).get("prices") or {}
        cg = json.loads(get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,ripple,cardano,dogecoin&vs_currencies=usd").decode())
        ids = {"BTC-USD": "bitcoin", "ETH-USD": "ethereum", "SOL-USD": "solana", "XRP-USD": "ripple", "ADA-USD": "cardano", "DOGE-USD": "dogecoin"}
        for t, i in ids.items():
            o = (ours.get(t) or {}).get("price")
            ref = (cg.get(i) or {}).get("usd")
            cb = None
            try:
                cb = float(json.loads(get(f"https://api.exchange.coinbase.com/products/{t}/ticker").decode()).get("price"))
            except Exception:
                pass
            if o and (ref or cb):
                r = cb or ref
                gap = (o / r - 1) * 100
                res["crypto"].append({"ticker": t, "ours": o, "coinbase": cb, "coingecko": ref, "gapPct": round(gap, 3), "flag": abs(gap) > 1.0})
    except Exception as e:
        log(f"crypto check: {e}")
    write("checks.json", res)
    bad = [x["ccy"] for x in res["fx"] if x["flag"]] + [x["ticker"] for x in res["crypto"] if x["flag"]]
    log(f"checks: FX {len(res['fx'])} currencies vs ECB · crypto {len(res['crypto'])} coins vs Coinbase / CoinGecko · flagged: {', '.join(bad) or 'none'}")


def main():
    parts = (os.environ.get("EXTRAS_PARTS") or "vol,treasury,bls,gdelt,checks").split(",")
    for name, fn in (("vol", vol_part), ("treasury", treasury_part), ("bls", bls_part), ("gdelt", gdelt_part), ("checks", checks_part)):
        if name in parts:
            try:
                fn()
            except Exception as e:
                log(f"{name}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
