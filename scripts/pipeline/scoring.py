"""Deterministic scoring (pure code, no LLM).

Compass (both axes on -50..+50, 20-observation changes, about one month):
  rate_pressure  = 50 * tanh((0.5*dUS2Y_bp + 0.3*dUS10Y_bp + 0.2*dSOFR_bp) / 30)
  liquidity_flow = 50 * tanh(-(0.6*dDXY_pct/2 + 0.4*dONRRP_usd_bn/50))
Endpoints are the MARKET SNAPSHOT values shown on ryzm.app (data/snapshot.json);
baselines are the history observation 20 sessions before each snapshot date.
A falling dollar and a falling ON RRP balance read as easier liquidity. With
ON RRP near zero, that term is close to inert today.

Bubble scores for a price series s (orientation o = +1 when a rise means money
flowing INTO the asset as labeled, -1 otherwise):
  priced_in  = 1-year range position of the latest level, 0-100
               (o = -1 uses 100 - position)
  z20        = 20-obs change / std of all 20-obs changes in the window
  z5         = 5-obs change  / std of all 5-obs changes in the window
  momentum   = 50 * tanh(o * z20 / 2)                         (-50..+50)
  T_move     = 100 * (1 - exp(-max(|z5|, |z20|) / 1.5))
  T_div      = 2*|priced_in - 50| if (priced_in - 50) and momentum have opposite signs, else 0
  tension    = 0.6*T_move + 0.4*T_div                         (0..100)
These are model proxies computed from sourced prices, not measured positioning.
"""
import math

from . import common as c

ALPHA_RATES = {"US2Y": 0.5, "US10Y": 0.3, "SOFR": 0.2}
COMPASS_KEYS = ("US2Y", "US10Y", "SOFR", "ONRRP", "DXY")
UNITS = {"US2Y": "bp", "US10Y": "bp", "SOFR": "bp", "ONRRP": "usd_bn", "DXY": "pct"}
FORMULA = {
    "rate_pressure": "50*tanh((0.5*dUS2Y_bp + 0.3*dUS10Y_bp + 0.2*dSOFR_bp)/30)",
    "liquidity_flow": "50*tanh(-(0.6*dDXY_pct/2 + 0.4*dONRRP_usd_bn/50))",
    "window_observations": c.WINDOW,
    "ghost_offset_observations": c.WEEK,
    "inputs": list(COMPASS_KEYS),
}


def quadrant(x, y):
    if x >= 0 and y < 0:
        return "liquidity_rally"
    if x >= 0:
        return "selective_growth"
    if y >= 0:
        return "stagflation_defense"
    return "safe_haven"


QUADRANTS = [
    {"id": "liquidity_rally", "label": "Liquidity Rally", "x": 1, "y": -1,
     "read": ["Easier rates and ample dollar liquidity lift risk appetite, and foreign money reaches Korea."]},
    {"id": "selective_growth", "label": "Selective Growth", "x": 1, "y": 1,
     "read": ["Risk appetite holds as rates rise, and flows narrow to themes with earnings, like memory chips."]},
    {"id": "stagflation_defense", "label": "Stagflation Defense", "x": -1, "y": 1,
     "read": ["Tighter rates meet thinner dollar liquidity, and the pressure runs into USD/KRW."]},
    {"id": "safe_haven", "label": "Safe-Haven Bond Crowding", "x": -1, "y": -1,
     "read": ["Yields ease while dollar liquidity tightens, so money crowds into bonds."]},
]
QLABEL = {q["id"]: q["label"] for q in QUADRANTS}


def _anchor(hist, iso):
    idx = None
    for i, (d, _v) in enumerate(hist):
        if d <= iso:
            idx = i
    return idx


def compass(history, snapshot, back=0):
    """history: {key: [(date, value)]}; snapshot: data/snapshot.json items.
    back=0 uses snapshot endpoints; back=N uses history ending N observations earlier."""
    inputs = {}
    for key in COMPASS_KEYS:
        h = history.get(key) or []
        if not h:
            continue
        snap = (snapshot or {}).get(key)
        i = _anchor(h, snap["date"]) if snap and snap.get("date") else len(h) - 1
        if i is None:
            continue
        if back == 0 and snap:
            v1, d1, from_snap = snap["value"], snap["date"], True
        else:
            i -= back
            if i < 0:
                continue
            v1, d1, from_snap = h[i][1], h[i][0], False
        k = i - c.WINDOW
        if k < 0:
            continue
        unit = UNITS[key]
        inputs[key] = {"change": c.delta(v1, h[k][1], unit), "unit": unit, "end": round(v1, 4),
                       "end_date": d1, "base": round(h[k][1], 4), "base_date": h[k][0],
                       "end_from_snapshot": from_snap}
    if not all(k in inputs for k in ("US2Y", "US10Y", "DXY")):
        return None
    g = lambda k: inputs[k]["change"] if k in inputs else 0.0
    rate_raw = sum(w * g(k) for k, w in ALPHA_RATES.items()) / 30.0
    liq_raw = -(0.6 * g("DXY") / 2.0 + 0.4 * g("ONRRP") / 50.0)
    y = 50 * math.tanh(rate_raw)
    x = 50 * math.tanh(liq_raw)
    if not c.finite(x, y):
        return None
    contrib = {
        "US2Y": 0.5 * g("US2Y") / 30.0, "US10Y": 0.3 * g("US10Y") / 30.0, "SOFR": 0.2 * g("SOFR") / 30.0,
        "DXY": -0.6 * g("DXY") / 2.0, "ONRRP": -0.4 * g("ONRRP") / 50.0,
    }
    q = quadrant(x, y)
    return {"x": round(x, 1), "y": round(y, 1), "rate_pressure": round(y, 1), "liquidity_flow": round(x, 1),
            "angle": round(math.degrees(math.atan2(y, x)), 1), "magnitude": round(min(50.0, math.hypot(x, y)), 1),
            "quadrant": q, "quadrant_label": QLABEL[q],
            "as_of_data": max(v["end_date"] for v in inputs.values()),
            "inputs": inputs, "contributions": {k: round(v, 4) for k, v in contrib.items() if k in inputs}}


def _std(xs):
    if len(xs) < 3:
        return None
    m = sum(xs) / len(xs)
    v = sum((a - m) ** 2 for a in xs) / (len(xs) - 1)
    return math.sqrt(v) if v > 0 else None


def _changes(vals, n, unit):
    return [c.delta(vals[i], vals[i - n], unit) for i in range(n, len(vals))]


def bubble_scores(hist, unit, orient, last_value=None):
    vals = [v for _d, v in hist][-250:]
    if last_value is not None:
        vals = vals[:-1] + [last_value] if vals else [last_value]
    if len(vals) < 45:
        return None
    lo, hi = min(vals), max(vals)
    pos = 50.0 if hi == lo else 100.0 * (vals[-1] - lo) / (hi - lo)
    priced_in = pos if orient > 0 else 100.0 - pos
    ch20, ch5 = _changes(vals, 20, unit), _changes(vals, 5, unit)
    s20, s5 = _std(ch20), _std(ch5)
    z20 = ch20[-1] / s20 if s20 else 0.0
    z5 = ch5[-1] / s5 if s5 else 0.0
    momentum = 50 * math.tanh(orient * z20 / 2.0)
    t_move = 100 * (1 - math.exp(-max(abs(z5), abs(z20)) / 1.5))
    t_div = 2 * abs(priced_in - 50) if (priced_in - 50) * momentum < 0 else 0.0
    tension = 0.6 * t_move + 0.4 * t_div
    return {"priced_in_score": round(priced_in, 1), "momentum_score": round(momentum, 1),
            "tension_score": round(min(100.0, tension), 1), "z20": round(z20, 2), "z5": round(z5, 2),
            "change_20": ch20[-1], "change_5": ch5[-1]}
