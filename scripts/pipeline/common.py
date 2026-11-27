"""Shared helpers for the RYZM pipeline: paths, JSON I/O, the home-page
ticker parser, public data fetchers and the node-item validator.

Python 3 standard library only. No API keys.
"""
import csv
import io
import json
import math
import os
import re
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "data")
NODES = os.path.join(DATA, "nodes")
LASTGOOD = os.path.join(NODES, "_lastgood")
PIPE = os.path.join(DATA, "pipeline")
INDEX = os.path.join(ROOT, "index.html")
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "ryzm-pipeline/1.0 (+https://ryzm.app)"}
NODE_IDS = ("rates_funding", "global_credit_macro", "asia_early_signal", "crypto_leverage")
WINDOW = 20   # observations for the "1M" change used by the compass
WEEK = 5      # observations for the "1W" change and the ghost needle
KEEP = 260    # history points kept per series (about one year of sessions)


def now_kst():
    return datetime.now(KST)


def iso_now():
    return now_kst().isoformat(timespec="minutes")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    """Atomic write so a crash never leaves half a file behind."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def get(url, timeout=25, ua=None):
    req = urllib.request.Request(url, headers=ua or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


# ---------------------------------------------------------------- ticker
MONTHS = {m: i + 1 for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
TICKER_KEYS = {"US2Y": "US2Y", "US10Y": "US10Y", "SOFR": "SOFR", "ON RRP": "ONRRP",
               "USD/KRW": "USDKRW", "KOSPI": "KOSPI", "DXY": "DXY"}
TICKER_ITEM_RE = re.compile(
    r'<span class="ticker-item"><strong>([^<]+)</strong>\s*([^<]*?)\s*<em>([^<]+)</em>')


def read_ticker(text=None):
    """Parse the MARKET SNAPSHOT block of index.html.

    Returns ({key: {"label", "display", "value", "date_label"}}, "Nov 27 15:40 KST")."""
    if text is None:
        try:
            text = open(INDEX, encoding="utf-8").read()
        except OSError:
            return {}, None
    block = re.search(r"<!-- ticker -->(.*?)<!-- /ticker -->", text, re.S)
    if not block:
        return {}, None
    out = {}
    for label, rest, d in TICKER_ITEM_RE.findall(block.group(1)):
        key = TICKER_KEYS.get(label.strip())
        m = re.search(r"-?[\d,]+(?:\.\d+)?", rest)
        if key and m:
            out[key] = {"label": label.strip(), "display": rest.strip(),
                        "value": float(m.group(0).replace(",", "")), "date_label": d.strip()}
    stamp = re.search(r"Updated ([^<]+)</span>", block.group(1))
    return out, (stamp.group(1).strip() if stamp else None)


def label_to_iso(mon_day, ref_iso):
    """'Nov 27' -> ISO date, using the year of a nearby reference date."""
    try:
        mon, day = mon_day.split()
        y = int(ref_iso[:4])
        d = "%04d-%02d-%02d" % (y, MONTHS[mon], int(day))
        if d > ref_iso and MONTHS[mon] == 12 and ref_iso[5:7] == "01":
            d = "%04d-%02d-%02d" % (y - 1, MONTHS[mon], int(day))
        return d
    except Exception:
        return None


# ---------------------------------------------------------------- sources
def treasury():
    now = now_kst()
    rows = []
    for year in (now.year, now.year - 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               "daily-treasury-rates.csv/%d/all?type=daily_treasury_yield_curve"
               "&field_tdr_date_value=%d&page&_format=csv" % (year, year))
        rows += list(csv.DictReader(io.StringIO(get(url))))
    two, ten = {}, {}
    for r in rows:
        d = datetime.strptime(r["Date"], "%m/%d/%Y").strftime("%Y-%m-%d")
        if r.get("2 Yr"):
            two[d] = float(r["2 Yr"])
        if r.get("10 Yr"):
            ten[d] = float(r["10 Yr"])
    return sorted(two.items()), sorted(ten.items())


def sofr():
    data = json.loads(get("https://markets.newyorkfed.org/api/rates/secured/sofr/last/%d.json" % KEEP))
    return sorted((r["effectiveDate"], float(r["percentRate"])) for r in data["refRates"])


def rrp():
    start = (now_kst() - timedelta(days=380)).strftime("%Y-%m-%d")
    data = json.loads(get("https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json?startDate=" + start))
    out = {}
    for o in data["repo"]["operations"]:
        if o.get("totalAmtAccepted") is None:
            continue
        d = o["operationDate"]
        out[d] = out.get(d, 0.0) + float(o["totalAmtAccepted"]) / 1e9
    return sorted(out.items())


def yahoo(symbol):
    data = json.loads(get("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=1y&interval=1d" % symbol,
                          ua={"User-Agent": "Mozilla/5.0 (compatible; ryzm-pipeline/1.0)"}))
    res = data["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    off = timezone(timedelta(seconds=res["meta"].get("gmtoffset", 0)))
    out = {}
    for ts, c in zip(res["timestamp"], closes):
        if c is not None:
            out[datetime.fromtimestamp(int(ts), off).strftime("%Y-%m-%d")] = float(c)
    return sorted(out.items())


def fred(series_id):
    """FRED keyless CSV download (no API key). Missing values are '.'."""
    start = (now_kst() - timedelta(days=400)).strftime("%Y-%m-%d")
    text = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (series_id, start), timeout=40)
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}$", row[0]):
            continue
        try:
            out.append((row[0], float(row[1])))
        except ValueError:
            continue
    return out


def binance_funding(symbol):
    """Daily mean of Binance USD-M perpetual funding rates, in basis points per 8 hours."""
    data = json.loads(get("https://fapi.binance.com/fapi/v1/fundingRate?symbol=%s&limit=1000" % symbol))
    days = {}
    for r in data:
        d = datetime.fromtimestamp(int(r["fundingTime"]) / 1000, timezone.utc).strftime("%Y-%m-%d")
        days.setdefault(d, []).append(float(r["fundingRate"]) * 1e4)
    return sorted((d, sum(v) / len(v)) for d, v in days.items())


def basket(*series):
    """Equal-weight index (start = 100) of several price series on common dates."""
    maps = [dict(s) for s in series]
    common = sorted(set.intersection(*[set(m) for m in maps]))
    if not common:
        return []
    first = common[0]
    return [(d, round(100 * sum(m[d] / m[first] for m in maps) / len(maps), 4)) for d in common]


# ---------------------------------------------------------------- math
def change(series, back, unit, end_index=None):
    """Change over `back` observations ending at end_index (default: last)."""
    if end_index is None:
        end_index = len(series) - 1
    k = end_index - back
    if k < 0 or end_index >= len(series):
        return None
    (d1, v1), (d0, v0) = series[end_index], series[k]
    return {"value": delta(v1, v0, unit), "unit": unit, "from_date": d0, "to_date": d1,
            "from": round(v0, 4), "to": round(v1, 4)}


def delta(v1, v0, unit):
    if unit == "bp":
        return round((v1 - v0) * 100, 1)
    if unit in ("usd_bn", "level"):
        return round(v1 - v0, 3)
    return round((v1 / v0 - 1) * 100, 3) if v0 else 0.0


def finite(*vals):
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals)


# ---------------------------------------------------------------- validation
ITEM_FIELDS = {
    "node_id": str, "asset_or_theme": str, "metric_value": (int, float, str, type(None)),
    "delta": (int, float, str, type(None)), "priced_in_score": (int, float),
    "momentum_score": (int, float), "tension_score": (int, float), "claim": str,
    "source_url": str, "published_at": str,
}
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$")
FORBIDDEN = re.compile(r"x\.com|twitter\.com|@Goeun_6121|savepick|세이브픽", re.I)


def validate_item(it, node_id=None):
    """Return a list of problems (empty list = valid). Mirrors schema/item.schema.json."""
    errs = []
    if not isinstance(it, dict):
        return ["item is not an object"]
    for k, typ in ITEM_FIELDS.items():
        if k not in it:
            errs.append("missing %s" % k)
        elif not isinstance(it[k], typ) or isinstance(it[k], bool):
            errs.append("%s has wrong type" % k)
    if errs:
        return errs
    if node_id and it["node_id"] != node_id:
        errs.append("node_id %r != %r" % (it["node_id"], node_id))
    if it["node_id"] not in NODE_IDS:
        errs.append("unknown node_id")
    for k, lo, hi in (("priced_in_score", 0, 100), ("momentum_score", -50, 50), ("tension_score", 0, 100)):
        if not finite(it[k]) or not lo <= it[k] <= hi:
            errs.append("%s out of range [%s, %s]" % (k, lo, hi))
    if not re.match(r"^https://[^\s\"'<>]+$", it["source_url"]):
        errs.append("source_url must be an https URL")
    if not ISO_RE.match(it["published_at"]):
        errs.append("published_at must be ISO 8601")
    if not 8 <= len(it["claim"]) <= 400:
        errs.append("claim must be 8-400 characters")
    if not 2 <= len(it["asset_or_theme"]) <= 60:
        errs.append("asset_or_theme must be 2-60 characters")
    for k in ("claim", "source_url", "asset_or_theme"):
        if FORBIDDEN.search(str(it[k])):
            errs.append("%s contains a forbidden reference" % k)
    return errs


def validate_node(doc, node_id=None):
    errs = []
    if not isinstance(doc, dict):
        return ["node file is not an object"]
    if node_id and doc.get("node_id") != node_id:
        errs.append("node_id mismatch")
    if not isinstance(doc.get("items"), list):
        return errs + ["items must be a list"]
    if not doc["items"]:
        errs.append("items is empty")
    for i, it in enumerate(doc["items"]):
        errs += ["items[%d]: %s" % (i, e) for e in validate_item(it, doc.get("node_id"))]
    return errs
