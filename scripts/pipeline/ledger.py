"""Regime Ledger: one public record per published Regime Dial reading.

append(state)  adds the current reading to data/regime_ledger.json (one entry per snapshot stamp).
build_page()   writes /regime/index.html from the ledger, newest first. Readings published before
               the ledger existed are back-filled from data/compass_history.json (position only).

The page shell (header, nav, footer, page styles) is copied from about/index.html so the ledger
always matches the rest of the site. Run by render.py after every dashboard refresh; can also be
run alone:  python3 scripts/pipeline/ledger.py
"""
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import common as c  # noqa: E402

LEDGER = os.path.join(c.DATA, "regime_ledger.json")
HISTORY = os.path.join(c.DATA, "compass_history.json")
ROOT = os.path.dirname(c.DATA)
SHELL = os.path.join(ROOT, "about", "index.html")
OUT = os.path.join(ROOT, "regime", "index.html")
URL = "https://ryzm.app/regime/"
TITLE = "Regime Ledger · RYZM"
DESC = ("Every published RYZM Regime Dial reading, newest first: the regime, rate pressure and liquidity "
        "flow, the drivers and the highest-tension signals. A record of readings, not forecasts.")
MAX_ENTRIES = 4000


def _sign(v):
    return ("%+.1f" % v).replace("-", "−")


def entry_from_state(state):
    cp, rg = state.get("compass") or {}, state.get("regime") or {}
    bubbles = (state.get("bubble_map") or {}).get("bubbles") or []
    top = sorted(bubbles, key=lambda b: -b.get("tension", 0))[:3]
    note = rg.get("note") or {}
    return {
        "date": (cp.get("as_of_data") or state.get("generated_at", ""))[:10],
        "snapshot_stamp": cp.get("snapshot_stamp") or "",
        "recorded_at": state.get("generated_at"),
        "regime": rg.get("label") or "", "quadrant": rg.get("quadrant"),
        "x": cp.get("x"), "y": cp.get("y"), "stale": bool(cp.get("stale")),
        "drivers": [d.get("text", "") for d in (rg.get("drivers") or [])[:3]],
        "top_tension": [{"label": b.get("label"), "short": b.get("short") or b.get("label"),
                         "tension": round(b.get("tension", 0)), "priced_in": round(b.get("priced_in", 0)),
                         "flow": round(b.get("flow", 0)), "delta": b.get("delta")} for b in top],
        "note": {"text": note.get("text"), "source_url": note.get("source_url")},
    }


def append(state):
    led = c.read_json(LEDGER, {}) or {}
    entries = led.get("entries", [])
    e = entry_from_state(state)
    if not e["snapshot_stamp"] or e["x"] is None:
        return False
    entries = [x for x in entries if x.get("snapshot_stamp") != e["snapshot_stamp"]] + [e]
    entries.sort(key=lambda x: x.get("recorded_at") or "")
    c.write_json(LEDGER, {"version": 1, "description": DESC, "entries": entries[-MAX_ENTRIES:]})
    return True


def _rows():
    entries = list((c.read_json(LEDGER, {}) or {}).get("entries", []))
    have = {x.get("snapshot_stamp") for x in entries}
    labels = {}
    try:
        from pipeline import scoring
        labels = scoring.QLABEL
    except Exception:  # noqa: BLE001
        pass
    for h in (c.read_json(HISTORY, {}) or {}).get("entries", []):
        if h.get("snapshot_stamp") in have:
            continue
        entries.append({"date": h.get("date"), "snapshot_stamp": h.get("snapshot_stamp"), "recorded_at": h.get("recorded_at"),
                        "regime": labels.get(h.get("quadrant"), h.get("quadrant") or ""), "x": h.get("x"), "y": h.get("y"),
                        "drivers": [], "top_tension": [], "note": {}, "backfill": True})
    entries.sort(key=lambda x: x.get("recorded_at") or "", reverse=True)
    return entries


def _row_html(e):
    tops = ", ".join("%s %d" % (html.escape(t["short"] or ""), t["tension"]) for t in e.get("top_tension") or [])
    drivers = "".join("<li>%s</li>" % html.escape(d) for d in e.get("drivers") or [])
    note = e.get("note") or {}
    note_html = ""
    if note.get("text"):
        src = (' <a href="%s" rel="noopener">source</a>' % html.escape(note["source_url"])) if note.get("source_url") else ""
        note_html = '<p class="lg-note">%s%s</p>' % (html.escape(note["text"]), src)
    detail = ""
    if drivers or note_html:
        detail = '<details class="lg-more"><summary>Drivers</summary><ul>%s</ul>%s</details>' % (drivers, note_html)
    return ('<tr><td class="lg-when">%s</td><td class="lg-reg">%s%s%s</td><td class="lg-num">%s</td><td class="lg-num">%s</td><td class="lg-top">%s</td></tr>'
            % (html.escape(e.get("snapshot_stamp") or e.get("date") or ""), html.escape(e.get("regime") or ""),
               ' <span class="lg-flag">cached</span>' if e.get("stale") else "",
               detail, _sign(e["y"]) if e.get("y") is not None else "", _sign(e["x"]) if e.get("x") is not None else "",
               tops or '<span class="lg-dim">position only</span>'))


STYLE = """<style>
  .ledger { max-width: 980px; margin: 0 auto; padding: 8px 28px 64px; }
  .ledger .lead { font-size: 17px; color: var(--text-2); line-height: 1.6; max-width: 66ch; margin: 10px 0 8px; }
  .ledger .fine { font-size: 13px; color: var(--text-muted); margin-bottom: 28px; }
  .ledger .fine a, .ledger .lg-note a { color: var(--accent-ink); text-decoration: underline; text-underline-offset: 2px; }
  .ledger table { width: 100%; border-collapse: collapse; font-size: 14px; }
  .ledger th { text-align: left; font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--text-muted); font-weight: 600; padding: 0 12px 10px 0; border-bottom: 1px solid var(--text); }
  .ledger td { padding: 14px 12px 14px 0; border-bottom: 1px solid var(--border); vertical-align: top; }
  .ledger .lg-when { font-family: var(--mono); font-size: 12px; color: var(--text-muted); white-space: nowrap; }
  .ledger .lg-reg { font-family: var(--serif); font-size: 17px; color: var(--text); }
  .ledger .lg-num { font-family: var(--mono); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .ledger .lg-top { color: var(--text-2); }
  .ledger .lg-dim, .ledger .lg-flag { color: var(--text-muted); font-size: 12px; }
  .ledger .lg-more { margin-top: 6px; font-family: var(--sans); font-size: 13px; color: var(--text-2); }
  .ledger .lg-more summary { cursor: pointer; color: var(--accent-ink); }
  .ledger .lg-more ul { margin: 6px 0 0 16px; }
  .ledger .lg-note { margin-top: 6px; }
  @media (max-width: 700px) {
    .ledger { padding: 4px 16px 48px; }
    .ledger table, .ledger tbody, .ledger tr, .ledger td { display: block; width: 100%; }
    .ledger thead { display: none; }
    .ledger tr { padding: 14px 0; border-bottom: 1px solid var(--border); }
    .ledger td { border: 0; padding: 2px 0; }
    .ledger .lg-num::before { content: attr(data-k) " "; color: var(--text-muted); font-family: var(--sans); font-size: 12px; }
  }
</style>"""


def build_page():
    shell = open(SHELL, encoding="utf-8").read()
    head, rest = shell.split("<main>", 1)
    _old_main, tail = rest.split("</main>", 1)
    old_title = re.search(r"<title>(.*?)</title>", head).group(1)
    old_desc = re.search(r'<meta name="description" content="([^"]*)"', head).group(1)
    head = head.replace(old_title, html.escape(TITLE)).replace(old_desc, html.escape(DESC))
    head = head.replace("https://ryzm.app/about/", URL)
    head = re.sub(r'<script type="application/ld\+json"[^>]*>.*?</script>', "", head, flags=re.S)
    rows = _rows()
    body_rows = "\n".join(_row_html(e).replace('<td class="lg-num">', '<td class="lg-num" data-k="Rates">', 1)
                          .replace('<td class="lg-num">', '<td class="lg-num" data-k="Liquidity">', 1) for e in rows)
    ld = {"@context": "https://schema.org", "@type": "Dataset", "name": "RYZM Regime Ledger", "description": DESC, "url": URL,
          "creator": {"@id": "https://ryzm.app/#org"}, "isAccessibleForFree": True,
          "dateModified": rows[0].get("recorded_at") if rows else None,
          "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://ryzm.app/data/regime_ledger.json"}]}
    main = """<main>
%s
<section class="ledger">
  <h1 class="title">Regime Ledger</h1>
  <p class="lead">Every published reading of the RYZM Regime Dial, kept in order. Each row is what the dial showed at that snapshot: the regime, rate pressure and liquidity flow on a &minus;50 to +50 scale, and the signals under the most tension.</p>
  <p class="fine">A record of readings, not forecasts. Scores are computed from sourced market data with published formulas. <a href="/#cockpit">Current reading</a> &middot; <a href="/data/regime_ledger.json">Data (JSON)</a></p>
  <table>
    <thead><tr><th>Snapshot</th><th>Regime</th><th>Rates</th><th>Liquidity</th><th>Highest tension</th></tr></thead>
    <tbody>
%s
    </tbody>
  </table>
  <script type="application/ld+json">%s</script>
</section>
</main>""" % (STYLE, body_rows, json.dumps(ld, ensure_ascii=False).replace("</", "<\\/"))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(head + main + tail)
    return len(rows)


if __name__ == "__main__":
    st = c.read_json(os.path.join(c.DATA, "dashboard_state.json"))
    if st and "--no-append" not in sys.argv:
        append(st)
    print("LEDGER rows=%d" % build_page())
