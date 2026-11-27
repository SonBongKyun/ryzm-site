#!/usr/bin/env python3
"""Insert (or refresh) the RYZM dashboard block on the home page.

The home layout is kept as it is: header and nav, the MARKET SNAPSHOT bar, the
"Frameworks for markets / Not forecasts" intro, then BREAKING. This script only
  1. adds <link rel="stylesheet" href="/assets/css/dashboard.css"> before </head>,
  2. places the block between <!-- ryzm-dash --> and <!-- /ryzm-dash --> right
     after the intro section (or replaces it if it is already there),
  3. adds <script src="/assets/js/dashboard.js" defer></script> before </body>.
The ticker, ul.breaking-list and div.feed are never touched (asserted below).
The <!-- regime-summary --> block inside is filled by scripts/pipeline/render.py.

Theme: the block carries data-theme="paper"; all colors are --rd-* tokens in
assets/css/dashboard.css, so switching themes is one attribute.

Run from the repo root:  python3 scripts/build_dashboard.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
CSS_LINK = '<link rel="stylesheet" href="/assets/css/dashboard.css">'
JS_TAG = '<script src="/assets/js/dashboard.js" defer></script>'
PLACEHOLDER = """<!-- regime-summary -->
      <div class="rg-static" id="regime-static">
        <div class="rg-mode" id="regime-mode">Sourced reading</div>
        <div class="rg-name" id="regime-name">Macro Compass</div>
        <dl class="rg-nums"><div><dt>Rate pressure</dt><dd id="ro-rates">0</dd></div><div><dt>Liquidity flow</dt><dd id="ro-liq">0</dd></div></dl>
        <ul class="rg-drivers" id="regime-drivers"></ul>
        <p class="rg-read" id="regime-read">Run scripts/pipeline/render.py to fill this summary.</p>
        <p class="rg-note" id="regime-note"></p>
      </div>
      <!-- /regime-summary -->"""

BLOCK = """<!-- ryzm-dash -->
<section class="ryzm-dash" id="cockpit" data-theme="paper" aria-labelledby="ck-title">
  <div class="ck-head">
    <div><span class="ck-kicker">Not a forecast &middot; a reading of where markets stand</span><h2 class="ck-title" id="ck-title">RYZM Macro Compass &amp; Priced-In Bubble Map</h2><span class="ck-subtitle">Computed from the market snapshot above.</span></div>
    <div class="ck-tags"><span class="ck-tag ex">Simulation prototype</span></div>
  </div>

  <div class="ck-controls">
    <div class="sc-wrap">
      <div class="sc-row" id="scenario-pills" role="group" aria-label="Scenarios"></div>
      <div class="sc-note" id="scenario-note" aria-live="polite"></div>
    </div>
    <div class="ck-slider">
      <label class="sl-lab" for="sl-rates">Rates / Tightening Pressure</label><output class="sl-out" id="sl-rates-v" for="sl-rates">0</output>
      <input type="range" id="sl-rates" min="-50" max="50" step="1" value="0" disabled aria-describedby="sl-rates-scale">
      <div class="sl-scale" id="sl-rates-scale"><span>Easing</span><span>Neutral</span><span>Tightening</span></div>
    </div>
    <div class="ck-slider">
      <label class="sl-lab" for="sl-liq">Global Liquidity / Risk Appetite</label><output class="sl-out" id="sl-liq-v" for="sl-liq">0</output>
      <input type="range" id="sl-liq" min="-50" max="50" step="1" value="0" disabled aria-describedby="sl-liq-scale">
      <div class="sl-scale" id="sl-liq-scale"><span>Contraction</span><span>Neutral</span><span>Expansion</span></div>
    </div>
  </div>

  <div class="ck-grid">
    <div class="ck-compass">
      <div class="ck-panel-h"><h3>Macro Compass</h3><span class="cap">US rates and funding regime</span></div>
      <svg id="compass-svg" viewBox="0 0 300 300" role="img" aria-labelledby="regime-name" focusable="false"></svg>
      <div class="cmp-legend"><span class="lg-now">Now</span><span class="lg-ghost" id="ghost-note">1W ago</span></div>
      <div class="cmp-cap">US rates and funding regime</div>
    </div>
    <div class="ck-map">
      <div class="ck-panel-h"><h3>Priced-In &amp; Tension Map</h3><span class="ck-tag" id="map-tag" title="Positions are deterministic scores computed from sourced price history; they are model proxies, not measured positioning.">Sourced prices &middot; model scores</span></div>
      <div class="bm-wrap"><svg id="bubble-svg" role="group" aria-label="Priced-in and tension bubble map. Select a bubble for details."></svg></div>
      <div class="tt-wrap">
        <div class="tt-head">Top Tension Signals</div>
        <ol class="tt-list" id="tension-list" aria-label="Top tension signals, ranked"></ol>
      </div>
      <div class="bm-legend" aria-hidden="true">
        <span><i class="in"></i>Inflow</span><span><i class="flat"></i>Neutral</span><span><i class="out"></i>Outflow</span><span><i class="warn"></i>Priced-in 80+ or tension 75+</span><span>Size = tension</span>
      </div>
    </div>
    <div class="ck-side" aria-live="polite">
      %(regime)s
      <div class="asset-panel" id="asset-panel">
        <div class="ap-kicker">Selected asset</div>
        <div class="ap-name-row"><span class="ap-name" id="ap-name">Memory chips (Samsung + SK hynix)</span><span class="ap-chip" id="ap-group">Korea</span><span class="ap-chip ex" id="ap-ex" hidden>Example</span><span class="ap-status" id="ap-status" hidden></span></div>
        <p class="ap-src" id="ap-src"></p>
        <p class="ap-read" id="ap-read">Select a bubble to read its structure.</p>
        <div class="ap-bottom">
          <div class="ap-metrics">
            <div class="ap-m"><div class="k" title="Priced-in: 1-year range position">Heat</div><div class="v" id="ap-heat">&middot;</div><div class="ap-track"><span class="ap-fill" id="ap-heat-bar"></span></div></div>
            <div class="ap-m"><div class="k">Flow</div><div class="v" id="ap-flow">&middot;</div><div class="ap-track ap-flow-track"><span class="ap-fill ap-flow pos" id="ap-flow-bar"></span></div></div>
            <div class="ap-m"><div class="k">Tension</div><div class="v" id="ap-tension">&middot;</div><div class="ap-track"><span class="ap-fill" id="ap-tension-bar"></span></div></div>
          </div>
          <div class="ap-room-row"><button type="button" class="ap-room" id="ap-room">Analysis room</button><span class="ap-room-note" id="ap-room-note">Coming later</span></div>
        </div>
      </div>
    </div>
  </div>

  <p class="ck-error" id="cockpit-error" hidden>Live dashboard data could not load; the reading shown is the last published one.</p>
  <div class="ck-fine">
    <span>Reads market structure. Not a forecast, signal or investment advice.</span>
    <span><span id="data-stamp">Compass: computed from the market snapshot.</span> &middot; Bubble scores: model proxies &middot; Scenarios and sliders: illustrative &middot; <a href="/data/dashboard_state.json">Data</a></span>
  </div>
  <div class="ck-cta">
    <p>Get the weekly brief: new frameworks and the week&rsquo;s key changes, free.</p>
    <a class="ck-cta-btn" href="/subscribe/">Subscribe</a>
    <a class="ck-cta-link" href="/membership/">Membership plans</a>
  </div>
</section>
<!-- /ryzm-dash -->"""

LIVE = [
    ("ticker", re.compile(r"<!-- ticker -->.*?<!-- /ticker -->", re.S)),
    ("breaking", re.compile(r'<ul class="breaking-list">.*?</ul>', re.S)),
    ("feed", re.compile(r'<div class="feed">.*?\n      </div>', re.S)),
]


def main():
    s = open(INDEX, encoding="utf-8").read()
    before = {k: rx.findall(s) for k, rx in LIVE}
    m = re.search(r"<!-- regime-summary -->.*?<!-- /regime-summary -->", s, re.S)
    block = BLOCK % {"regime": m.group(0) if m else PLACEHOLDER}
    if "<!-- ryzm-dash -->" in s:
        s = re.sub(r"<!-- ryzm-dash -->.*?<!-- /ryzm-dash -->", lambda _m: block, s, count=1, flags=re.S)
    else:
        intro = re.search(r'<section class="intro">.*?</section>\n', s, re.S)
        if not intro:
            raise SystemExit("intro section not found; refusing to write")
        s = s[:intro.end()] + "\n" + block + "\n" + s[intro.end():]
    if CSS_LINK not in s:
        s = s.replace("</head>", CSS_LINK + "\n</head>", 1)
    if JS_TAG not in s:
        s = s.replace("</body>", JS_TAG + "\n</body>", 1)
    after = {k: rx.findall(s) for k, rx in LIVE}
    for k in before:
        if before[k] != after[k] or len(after[k]) != 1:
            raise SystemExit("live block %s changed or not unique; refusing to write" % k)
    open(INDEX, "w", encoding="utf-8").write(s)
    print("DASHBOARD_BLOCK_WRITTEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
