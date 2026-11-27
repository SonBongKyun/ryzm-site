"""Snapshot + history store.

  data/snapshot.json            the MARKET SNAPSHOT values exactly as shown in the
                                index.html ticker (parsed, never re-fetched here)
  data/snapshot_history.json    the 7 snapshot series, last 60 observations each
                                (small; read by the browser for 1W/1M chips)
  data/pipeline/history_full.json  every pipeline series, about one year each
                                (read by prepare.py / aggregate.py)

If a source fails, its series keeps the last good history from
history_full.json and is marked "stale": true. Nothing is ever blanked.

Run:  python3 -m scripts.pipeline.snapshot  (from the repo root)
      or via scripts/update_ticker.py --json / --json-only
"""
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from pipeline import common as c  # noqa: E402
else:
    from . import common as c

BROWSER_KEEP = 60

# key, label, unit, orientation (+1: a rise = inflow to the asset as labeled), source, url, node
SERIES = [
    ("US2Y", "US2Y", "bp", "US Treasury daily par yield curve", "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve", "rates_funding"),
    ("US10Y", "US10Y", "bp", "US Treasury daily par yield curve", "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve", "rates_funding"),
    ("SOFR", "SOFR", "bp", "Federal Reserve Bank of New York", "https://www.newyorkfed.org/markets/reference-rates/sofr", "rates_funding"),
    ("ONRRP", "ON RRP", "usd_bn", "Federal Reserve Bank of New York", "https://www.newyorkfed.org/markets/desk-operations/reverse-repo", "rates_funding"),
    ("DXY", "DXY", "pct", "Yahoo Finance (DX-Y.NYB)", "https://finance.yahoo.com/quote/DX-Y.NYB/", "rates_funding"),
    ("USDKRW", "USD/KRW", "pct", "Yahoo Finance (KRW=X)", "https://finance.yahoo.com/quote/KRW%3DX/", "asia_early_signal"),
    ("KOSPI", "KOSPI", "pct", "Yahoo Finance (^KS11)", "https://finance.yahoo.com/quote/%5EKS11/", "asia_early_signal"),
    ("USDJPY", "USD/JPY", "pct", "Yahoo Finance (JPY=X)", "https://finance.yahoo.com/quote/JPY%3DX/", "asia_early_signal"),
    ("MEMORY", "Samsung + SK hynix", "pct", "Yahoo Finance (005930.KS, 000660.KS)", "https://finance.yahoo.com/quote/000660.KS/", "asia_early_signal"),
    ("SOX", "PHLX Semiconductor", "pct", "Yahoo Finance (^SOX)", "https://finance.yahoo.com/quote/%5ESOX/", "asia_early_signal"),
    ("HYOAS", "US high-yield OAS", "bp", "FRED (BAMLH0A0HYM2, ICE BofA)", "https://fred.stlouisfed.org/series/BAMLH0A0HYM2", "global_credit_macro"),
    ("IGOAS", "US investment-grade OAS", "bp", "FRED (BAMLC0A0CM, ICE BofA)", "https://fred.stlouisfed.org/series/BAMLC0A0CM", "global_credit_macro"),
    ("BTCFUND", "BTC perp funding", "level", "Binance USD-M futures funding history", "https://www.binance.com/en/futures/funding-history/perpetual/funding-fee-history", "crypto_leverage"),
    ("ETHFUND", "ETH perp funding", "level", "Binance USD-M futures funding history", "https://www.binance.com/en/futures/funding-history/perpetual/funding-fee-history", "crypto_leverage"),
]
SNAPSHOT_KEYS = ("US2Y", "US10Y", "SOFR", "ONRRP", "USDKRW", "KOSPI", "DXY")
META = {s[0]: s for s in SERIES}


def fetch_all():
    got, errors = {}, []

    def run(keys, fn):
        try:
            res = fn()
            if len(keys) == 1:
                res = (res,)
            for k, s in zip(keys, res):
                if s:
                    got[k] = s
        except Exception as e:  # one failing source never blocks the rest
            errors.append("%s: %s" % ("/".join(keys), str(e)[:120]))

    run(("US2Y", "US10Y"), c.treasury)
    run(("SOFR",), c.sofr)
    run(("ONRRP",), c.rrp)
    run(("DXY",), lambda: c.yahoo("DX-Y.NYB"))
    run(("USDKRW",), lambda: c.yahoo("KRW%3DX"))
    run(("KOSPI",), lambda: c.yahoo("%5EKS11"))
    run(("USDJPY",), lambda: c.yahoo("JPY%3DX"))
    run(("MEMORY",), lambda: c.basket(c.yahoo("005930.KS"), c.yahoo("000660.KS")))
    run(("SOX",), lambda: c.yahoo("%5ESOX"))
    run(("HYOAS",), lambda: c.fred("BAMLH0A0HYM2"))
    run(("IGOAS",), lambda: c.fred("BAMLC0A0CM"))
    run(("BTCFUND",), lambda: c.binance_funding("BTCUSDT"))
    run(("ETHFUND",), lambda: c.binance_funding("ETHUSDT"))
    return got, errors


def refresh(fetch=True):
    """Refresh history_full.json, snapshot_history.json and snapshot.json. Returns a status dict."""
    old = c.read_json(os.path.join(c.PIPE, "history_full.json"), {}) or {}
    prev = {s["key"]: s for s in old.get("series", [])}
    got, errors = fetch_all() if fetch else ({}, ["fetch disabled"])
    series, stale = [], []
    for key, label, unit, src, url, node in SERIES:
        s = got.get(key)
        is_stale = False
        if not s:
            p = prev.get(key) or {}
            s = [(h["date"], h["value"]) for h in p.get("history", [])]
            is_stale = True
            if s:
                stale.append(key)
        series.append({"key": key, "label": label, "unit": unit, "node_id": node,
                       "source": src, "source_url": url, "available": bool(s), "stale": is_stale,
                       "last_success": (prev.get(key) or {}).get("last_success") if is_stale else c.iso_now(),
                       "history": [{"date": d, "value": round(v, 5)} for d, v in s[-c.KEEP:]]})
    ticker, stamp = c.read_ticker()
    full = {"version": 1, "generated_at": c.iso_now(), "snapshot_stamp": stamp,
            "errors": errors, "stale_series": stale, "series": series}
    if not any(s["available"] for s in series):
        return {"ok": False, "errors": errors}
    c.write_json(os.path.join(c.PIPE, "history_full.json"), full)
    small = dict(full)
    small["series"] = [dict(s, history=s["history"][-BROWSER_KEEP:]) for s in series if s["key"] in SNAPSHOT_KEYS]
    c.write_json(os.path.join(c.DATA, "snapshot_history.json"), small)
    write_snapshot(ticker, stamp, series)
    return {"ok": True, "errors": errors, "stale": stale}


def write_snapshot(ticker=None, stamp=None, series=None):
    if ticker is None:
        ticker, stamp = c.read_ticker()
    if not ticker:
        return False
    if series is None:
        full = c.read_json(os.path.join(c.PIPE, "history_full.json"), {}) or {}
        series = full.get("series", [])
    last = {s["key"]: (s["history"][-1]["date"] if s.get("history") else c.now_kst().strftime("%Y-%m-%d")) for s in series}
    items = {}
    for key in SNAPSHOT_KEYS:
        t = ticker.get(key)
        if not t:
            continue
        m = META[key]
        items[key] = {"label": t["label"], "display": t["display"], "value": t["value"],
                      "unit": {"bp": "percent", "usd_bn": "usd_bn", "pct": "index"}[m[2]],
                      "date_label": t["date_label"],
                      "date": c.label_to_iso(t["date_label"], last.get(key) or c.now_kst().strftime("%Y-%m-%d")),
                      "source": m[3], "source_url": m[4]}
    c.write_json(os.path.join(c.DATA, "snapshot.json"), {
        "version": 1, "generated_at": c.iso_now(), "snapshot_stamp": stamp,
        "origin": "index.html MARKET SNAPSHOT block (written by scripts/update_ticker.py)",
        "items": items})
    return True


if __name__ == "__main__":
    r = refresh(fetch="--no-fetch" not in sys.argv)
    for e in r.get("errors", []):
        print("source error:", e, file=sys.stderr)
    print("SNAPSHOT_OK" if r["ok"] else "SNAPSHOT_FAILED", ",".join(r.get("stale") or []))
    sys.exit(0 if r["ok"] else 1)
