/* RYZM Macro Compass & Priced-In / Tension Map (v3.1, motion pass 2026-11-28).
   Vanilla JS + SVG, editorial palette (ink + terracotta via CSS variables).
   Data flow:
     1. Endpoints = the MARKET SNAPSHOT values in this page's ticker block.
     2. Baselines = /data/snapshot_history.json (same sources), counted back
        20 observations from each snapshot date.
     3. Compass formula = scripts/pipeline/scoring.py (US2Y, US10Y, SOFR, ON RRP, DXY):
          rate_pressure  = 50*tanh((0.5*d2Y_bp + 0.3*d10Y_bp + 0.2*dSOFR_bp)/30)
          liquidity_flow = 50*tanh(-(0.6*dDXY_pct/2 + 0.4*dONRRP_bn/50))
     4. /data/dashboard_state.json supplies quadrants, scenarios, the ghost
        needle (prev_needle) and the bubbles (sourced prices, model scores).
   Motion (all light, off under prefers-reduced-motion): the needle sweeps in from the
   1W-ago position, scenario buttons and sliders glide needle + bubbles (exponential
   chase, ~0.3-0.7 s) and labels follow their bubbles, priced-in 80+ / tension 75+
   bubbles pulse a thin ring, and hover/focus/tap shows a short tooltip.
   Fallbacks: last-good JSON in localStorage; if nothing loads, the static
   summary already in the HTML stays and the dashboard shows a notice. */
(function () {
  'use strict';
  var root = document.getElementById('cockpit');
  if (!root || !window.fetch || !document.createElementNS) return;
  var NS = 'http://www.w3.org/2000/svg';
  var rmq = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)'), reduce = !!(rmq && rmq.matches);
  var $ = function (id) { return document.getElementById(id); };
  var MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var WINDOW = 20, WEEK = 5;
  var data, trends, base = { x: 0, y: 0 }, ghost = null, bubbles = [], quadrants = {}, selected = null;
  var state = { x: 0, y: 0 }, tgt = { x: 0, y: 0 }, needle = { x: 0, y: 0 }, loop = null, lastTs = 0, lastLayout = 0, labelsDirty = false,
      tcfg = { ts: 95, tn: 120, sync: true }, listKey = '', activeScenario = 'current', inputs = {}, touched = false;

  function el(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) if (attrs.hasOwnProperty(k)) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function sgn(v, d) { var r = +v.toFixed(d || 0); return (r > 0 ? '+' : r < 0 ? '\u2212' : '') + Math.abs(r).toFixed(d || 0); }
  function md(iso) { var p = iso.slice(0, 10).split('-'); return MON[+p[1] - 1] + ' ' + (+p[2]); }
  function quadrantOf(x, y) { return x >= 0 && y < 0 ? 'liquidity_rally' : x >= 0 ? 'selective_growth' : y >= 0 ? 'stagflation_defense' : 'safe_haven'; }
  function cache(key, val) { try { if (val === undefined) return JSON.parse(localStorage.getItem(key) || 'null'); localStorage.setItem(key, JSON.stringify(val)); } catch (e) { return null; } }
  function load(url, key) {
    return fetch(url, { cache: 'no-cache' }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) { cache(key, j); j._cached = false; return j; })
      .catch(function () { var c = cache(key); if (c) { c._cached = true; return c; } throw new Error('no data'); });
  }

  /* ---------------- snapshot -> compass (same formula as compute_regime.py) ---------------- */
  var KEYS = { 'US2Y': 'US2Y', 'US10Y': 'US10Y', 'SOFR': 'SOFR', 'ON RRP': 'ONRRP', 'USD/KRW': 'USDKRW', 'KOSPI': 'KOSPI', 'DXY': 'DXY' };
  function readTicker() {
    var out = {};
    [].forEach.call(document.querySelectorAll('.ticker-inner .ticker-item'), function (it) {
      var st = it.querySelector('strong'), em = it.querySelector('em');
      if (!st || !em) return;
      var key = KEYS[st.textContent.trim()]; if (!key) return;
      var txt = ''; [].forEach.call(it.childNodes, function (n) { if (n.nodeType === 3) txt += n.textContent; });
      var m = /-?[\d,]+(?:\.\d+)?/.exec(txt); if (!m) return;
      out[key] = { v: parseFloat(m[0].replace(/,/g, '')), d: em.textContent.trim(), el: it };
    });
    return out;
  }
  function anchor(hist, monDay) {
    if (!hist.length) return -1;
    var p = monDay.split(' '), mi = MON.indexOf(p[0]), y = +hist[hist.length - 1].date.slice(0, 4);
    if (mi < 0) return hist.length - 1;
    var want = y + '-' + ('0' + (mi + 1)).slice(-2) + '-' + ('0' + (+p[1])).slice(-2), idx = -1;
    hist.forEach(function (h, i) { if (h.date <= want) idx = i; });
    return idx;
  }
  function delta(key, v1, v0) {
    if (key === 'US2Y' || key === 'US10Y' || key === 'SOFR') return { v: (v1 - v0) * 100, u: 'bp' };
    if (key === 'ONRRP') return { v: v1 - v0, u: 'usd_bn' };
    return { v: (v1 / v0 - 1) * 100, u: 'pct' };
  }
  function changes(tk, back) {
    var out = {};
    (trends.series || []).forEach(function (s) {
      if (!s.available || !s.history) return;
      var h = s.history, t = tk[s.key], i = t ? anchor(h, t.d) : h.length - 1;
      if (i < 0) return;
      var v1, d1;
      if (!back && t) { v1 = t.v; d1 = t.d; } else { i -= back; if (i < 0) return; v1 = h[i].value; d1 = md(h[i].date); }
      var k = i - WINDOW, k1 = i - WEEK;
      if (k < 0) return;
      out[s.key] = { m: delta(s.key, v1, h[k].value), mFrom: md(h[k].date), w: k1 >= 0 ? delta(s.key, v1, h[k1].value) : null,
                     wFrom: k1 >= 0 ? md(h[k1].date) : '', to: d1, src: s.source, hist: h.slice(Math.max(0, i - WINDOW), i + 1), stale: s.stale };
    });
    return out;
  }
  function score(ch) {
    if (!ch.US2Y || !ch.US10Y || !ch.DXY) return null;
    var g = function (k) { return ch[k] ? ch[k].m.v : 0; };
    var y = 50 * Math.tanh((0.5 * g('US2Y') + 0.3 * g('US10Y') + 0.2 * g('SOFR')) / 30);
    var x = 50 * Math.tanh(-(0.6 * g('DXY') / 2 + 0.4 * g('ONRRP') / 50));
    if (!isFinite(x) || !isFinite(y)) return null;
    return { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 };
  }
  function fmtChg(c) {
    var a = Math.abs(c.v), s = c.v > 0.0005 ? '+' : c.v < -0.0005 ? '\u2212' : '\u00b1';
    return c.u === 'bp' ? s + a.toFixed(0) + ' bp' : c.u === 'usd_bn' ? s + '$' + a.toFixed(2) + 'bn' : s + a.toFixed(1) + '%';
  }
  /* ---------------- compass ---------------- */
  var C = 150, R = 130, cmp = {};
  function toXY(x, y) { var m = Math.sqrt(x * x + y * y), k = m > 50 ? 50 / m : 1; return [C + (x * k) / 50 * R, C - (y * k) / 50 * R]; }
  function buildCompass() {
    var svg = $('compass-svg'); svg.textContent = '';
    svg.setAttribute('viewBox', '2 2 296 296');
    var defs = el('defs', {}, svg), cp = el('clipPath', { id: 'cmp-clip' }, defs);
    el('circle', { cx: C, cy: C, r: R }, cp);
    /* bezel: outer ink ring, tick band (5 / 15 / 45 / 90 degrees), inner face ring */
    el('circle', { cx: C, cy: C, r: 146, class: 'cmp-bezel' }, svg);
    el('circle', { cx: C, cy: C, r: R, class: 'cmp-face' }, svg);
    var q = el('g', { 'clip-path': 'url(#cmp-clip)' }, svg);
    var quads = { selective_growth: [C, C - R], stagflation_defense: [C - R, C - R], safe_haven: [C - R, C], liquidity_rally: [C, C] };
    cmp.quad = {};
    for (var id in quads) cmp.quad[id] = el('rect', { x: quads[id][0], y: quads[id][1], width: R, height: R, class: 'cmp-q q-' + id }, q);
    [R / 3, 2 * R / 3].forEach(function (r) { el('circle', { cx: C, cy: C, r: r, class: 'cmp-ring' }, svg); });
    el('line', { x1: C - R, y1: C, x2: C + R, y2: C, class: 'cmp-axis' }, svg);
    el('line', { x1: C, y1: C - R, x2: C, y2: C + R, class: 'cmp-axis' }, svg);
    var ticks = el('g', { class: 'cmp-ticks' }, svg);
    for (var a = 0; a < 360; a += 5) {
      var card = a % 90 === 0, big = a % 45 === 0, mid = a % 15 === 0, t = a * Math.PI / 180, r1 = 146,
          r2 = card ? 133 : big ? 137 : mid ? 140 : 142.5;
      el('line', { x1: C + r1 * Math.cos(t), y1: C + r1 * Math.sin(t), x2: C + r2 * Math.cos(t), y2: C + r2 * Math.sin(t),
                   class: 'cmp-tick' + (card ? ' card' : big ? ' big' : mid ? ' mid' : '') }, ticks);
    }
    function txt(x, y, s, cls, anchorTo) { var n = el('text', { x: x, y: y, class: cls, 'text-anchor': anchorTo || 'middle' }, svg); n.textContent = s; return n; }
    txt(C, C - R + 17, 'TIGHTENING', 'cmp-ax'); txt(C, C + R - 10, 'EASING', 'cmp-ax');
    txt(C - R + 9, C - 6, 'CONTRACTION', 'cmp-ax', 'start'); txt(C + R - 9, C - 6, 'EXPANSION', 'cmp-ax', 'end');
    /* zone labels sit toward the rim (45-degree diagonals) so the needle rarely crosses them */
    var off = 64, ql = { selective_growth: [C + off, C - off], stagflation_defense: [C - off, C - off], safe_haven: [C - off, C + off], liquidity_rally: [C + off, C + off] };
    cmp.qlabel = {};
    (data.quadrants || []).forEach(function (qq) {
      var p = ql[qq.id]; if (!p) return;
      var w = qq.label.split(' '), lines = w.length > 1 ? [w.slice(0, Math.ceil(w.length / 2)).join(' '), w.slice(Math.ceil(w.length / 2)).join(' ')] : [qq.label];
      var t = el('text', { x: p[0], y: p[1] - (lines.length - 1) * 7 + 4, class: 'cmp-ql q-' + qq.id, 'text-anchor': 'middle' }, svg);
      lines.forEach(function (ln, i) { var s = el('tspan', { x: p[0], dy: i ? 14 : 0 }, t); s.textContent = ln; });
      cmp.qlabel[qq.id] = t;
    });
    cmp.ghost = el('g', { class: 'cmp-ghost' }, svg);
    cmp.gtrail = el('line', { x1: C, y1: C, x2: C, y2: C, class: 'cmp-trail' }, cmp.ghost);
    cmp.gn = el('path', { d: '', class: 'cmp-ghost-needle' }, cmp.ghost);
    cmp.gd = el('circle', { cx: C, cy: C, r: 4 }, cmp.ghost);
    cmp.gt = el('text', { x: C, y: C, class: 'cmp-ghost-t', 'text-anchor': 'middle' }, cmp.ghost); cmp.gt.textContent = '1W AGO';
    cmp.nl = el('path', { d: '', class: 'cmp-needle' }, svg);
    /* zone labels ride above the needle shaft (with a paper halo) and below the tip */
    for (var qk in cmp.qlabel) svg.appendChild(cmp.qlabel[qk]);
    cmp.halo = el('circle', { cx: C, cy: C, r: 10, class: 'cmp-halo' }, svg);
    cmp.tip = el('circle', { cx: C, cy: C, r: 4.5, class: 'cmp-tip' }, svg);
    el('circle', { cx: C, cy: C, r: 6, class: 'cmp-hub' }, svg);
    el('circle', { cx: C, cy: C, r: 1.8, class: 'cmp-hub-dot' }, svg);
  }
  /* tapered needle: tip at the reading, narrow waist at the hub, short counterweight tail */
  function needlePath(px, py, wide) {
    var dx = px - C, dy = py - C, L = Math.sqrt(dx * dx + dy * dy);
    if (L < 2) return '';
    var ux = dx / L, uy = dy / L, nx = -uy, ny = ux, w = wide || 3.2, tail = 13;
    var f = function (v) { return v.toFixed(2); };
    return 'M' + f(px) + ' ' + f(py) + 'L' + f(C + nx * w) + ' ' + f(C + ny * w) + 'L' + f(C - ux * tail) + ' ' + f(C - uy * tail) +
      'L' + f(C - nx * w) + ' ' + f(C - ny * w) + 'Z';
  }
  /* the needle shows `needle` (it chases `state`, so it sweeps in from the 1W-ago reading on load);
     the zone lights up where the needle is; the readout text follows `state` (the real reading) */
  function drawCompass() {
    var p = toXY(needle.x, needle.y);
    cmp.nl.setAttribute('d', needlePath(p[0], p[1]));
    ['tip', 'halo'].forEach(function (k) { cmp[k].setAttribute('cx', p[0].toFixed(2)); cmp[k].setAttribute('cy', p[1].toFixed(2)); });
    var nq = quadrantOf(needle.x, needle.y);
    for (var id in cmp.quad) { cmp.quad[id].classList.toggle('on', id === nq); if (cmp.qlabel[id]) cmp.qlabel[id].classList.toggle('on', id === nq); }
    if (ghost && cmp.gtrail) {
      var g = toXY(ghost.x, ghost.y), len = Math.hypot(p[0] - g[0], p[1] - g[1]);
      cmp.gtrail.setAttribute('x1', g[0].toFixed(2)); cmp.gtrail.setAttribute('y1', g[1].toFixed(2));
      cmp.gtrail.setAttribute('x2', p[0].toFixed(2)); cmp.gtrail.setAttribute('y2', p[1].toFixed(2));
      cmp.gtrail.style.opacity = len > 7 ? '' : '0';
    }
    return quadrantOf(state.x, state.y);
  }
  function drawGhost() {
    if (!ghost) { cmp.ghost.style.display = 'none'; return; }
    cmp.ghost.style.display = '';
    var p = toXY(ghost.x, ghost.y), dx = p[0] - C, dy = p[1] - C, L = Math.sqrt(dx * dx + dy * dy) || 1;
    cmp.gn.setAttribute('d', needlePath(p[0], p[1], 4.6)); cmp.gd.setAttribute('cx', p[0]); cmp.gd.setAttribute('cy', p[1]);
    /* label beyond the dot along its own direction; near the horizontal axis it drops below the dot,
       clear of the CONTRACTION / EXPANSION captions that sit just above the axis */
    var lx = p[0] + dx / L * 14, ly = p[1] + dy / L * 14 + 3.5, an = dx > 10 ? 'start' : dx < -10 ? 'end' : 'middle';
    if (Math.abs(dy) < 14) { lx = p[0]; ly = p[1] + 17; an = 'middle'; }
    lx = clamp(lx, C - R + 22, C + R - 22);
    cmp.gt.setAttribute('x', lx.toFixed(1)); cmp.gt.setAttribute('y', ly.toFixed(1)); cmp.gt.setAttribute('text-anchor', an);
  }

  /* ---------------- bubble map ---------------- */
  var bm = {}, W = 520, H = 300, PAD = { l: 40, r: 12, t: 14, b: 34 }, FD = 60;
  function sx(v) { return PAD.l + v / 100 * (W - PAD.l - PAD.r); }
  function sy(v) { return PAD.t + (FD - v) / (2 * FD) * (H - PAD.t - PAD.b); }
  /* move a published score v by a simulated shift d, saturating smoothly at [lo, hi]:
     d = 0 returns v exactly, so the current reading and a 1-tick slider move never jump */
  function push(v, d, lo, hi) {
    if (d >= 0) { var up = Math.max(hi - v, 1); return v + up * Math.tanh(d / up); }
    var dn = Math.max(v - lo, 1); return v - dn * Math.tanh(-d / dn);
  }
  /* keep a bubble of radius r inside the plot so 0 and 100 never cross the axis lines */
  function bx(heat, r) { return clamp(sx(heat), sx(0) + r + 2, sx(100) - r - 2); }
  function model(b, px, py) {
    var dx = (px === undefined ? state.x : px) - base.x, dy = (py === undefined ? state.y : py) - base.y, s = b.sens || {}, K = 0.6;
    var heat = push(clamp(b.priced_in, 0, 100), K * ((s.priced_in_x || 0) * dx + (s.priced_in_y || 0) * dy), 0, 100);
    var flow = push(clamp(b.flow, -58, 58), K * ((s.flow_x || 0) * dx + (s.flow_y || 0) * dy), -58, 58);
    var tension = clamp(b.disagreement + 0.12 * Math.sqrt(dx * dx + dy * dy), 0, 99);
    var conc = clamp(b.concentration + 0.3 * (heat - b.priced_in), 10, 100);
    return { heat: heat, flow: flow, tension: tension, conc: conc };
  }
  var SHORT = { us2y: 'US 2Y', us10y: 'US 10Y', usd: 'DXY', krw: 'KRW', kospi: 'KOSPI', memory: 'KR Memory', yen_carry: 'Yen Carry',
    sox: 'US Semis', hy: 'HY Credit', ig: 'IG Credit', btc_lev: 'BTC Lev', eth_lev: 'ETH Lev' };
  function shortOf(b) { return b.short && b.short !== b.label ? b.short : SHORT[b.id] || b.label.replace(/\s*\(.*\)\s*$/, ''); }
  /* signal groups (pipeline nodes): color is a second cue; labels, the inspector chip and the legend text carry the same information */
  var NODE_KEYS = { rates_funding: 'rates', global_credit_macro: 'credit', asia_early_signal: 'asia', crypto_leverage: 'crypto' };
  var NODE_NAMES = { rates: 'Rates & funding', credit: 'Credit & macro', asia: 'Asia early signal', crypto: 'Crypto leverage' };
  function nodeKey(b) { return NODE_KEYS[b.node_id] || 'asia'; }
  function buildNodeLegend() {
    var lg = root.querySelector('.bm-legend'); if (!lg) return;
    var old = root.querySelector('.bm-nodes'); if (old) old.parentNode.removeChild(old);
    var seen = {}, box = document.createElement('div'); box.className = 'bm-nodes'; box.setAttribute('aria-label', 'Signal groups');
    ['rates', 'credit', 'asia', 'crypto'].forEach(function (k) {
      if (!bubbles.some(function (b) { return nodeKey(b) === k; }) || seen[k]) return; seen[k] = 1;
      var sp = document.createElement('span'); sp.className = 'n-' + k; var i = document.createElement('i'); sp.appendChild(i);
      sp.appendChild(document.createTextNode(NODE_NAMES[k])); box.appendChild(sp);
    });
    lg.parentNode.insertBefore(box, lg);
  }
  function buildBubbles() {
    var svg = $('bubble-svg'); svg.textContent = '';
    var w = Math.max(280, Math.round(svg.parentNode.clientWidth || 520));
    var narrow = w < 460;
    W = w; H = narrow ? Math.round(clamp(w * 1.0, 300, 420)) : Math.round(clamp(parseFloat(getComputedStyle(svg.parentNode).getPropertyValue('--bm-h')) || 300, 180, 420));
    PAD = narrow ? { l: 26, r: 8, t: 14, b: 32 } : { l: 40, r: 12, t: 14, b: 34 };
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H); svg.setAttribute('height', H); svg.setAttribute('width', W);
    var defs = el('defs', {}, svg);
    var pat = el('pattern', { id: 'bm-hatch', width: 5, height: 5, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, defs);
    el('line', { x1: 0, y1: 0, x2: 0, y2: 5, class: 'bm-hatch-line' }, pat);
    /* one hatch per signal group, drawn in that group's ink (outflow = hatched, inflow = solid tint, neutral = light tint) */
    Object.keys(NODE_KEYS).forEach(function (nid) {
      var k = NODE_KEYS[nid], hp = el('pattern', { id: 'bm-hatch-' + k, width: 5, height: 5, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, defs);
      el('rect', { x: 0, y: 0, width: 5, height: 5, class: 'bm-hatch-bg n-' + k }, hp);
      el('line', { x1: 0, y1: 0, x2: 0, y2: 5, class: 'bm-hatch-line n-' + k }, hp);
    });
    var g = el('g', { class: 'bm-grid' }, svg);
    [25, 50, 75, 100].forEach(function (v) { el('line', { x1: sx(v), y1: PAD.t, x2: sx(v), y2: H - PAD.b }, g); });
    [-60, -30, 30].forEach(function (v) { el('line', { x1: PAD.l, y1: sy(v), x2: W - PAD.r, y2: sy(v) }, g); });
    var cz = el('g', { class: 'bm-crowd' }, svg);
    el('rect', { x: sx(75), y: PAD.t, width: sx(100) - sx(75), height: H - PAD.t - PAD.b }, cz);
    var czt = el('text', { x: sx(87.5), y: H - PAD.b - 7, 'text-anchor': 'middle' }, cz); czt.textContent = narrow ? 'CROWDED 75+' : 'CROWDED ZONE \u00b7 75+';
    el('line', { x1: PAD.l, y1: sy(0), x2: W - PAD.r, y2: sy(0), class: 'bm-zero' }, svg);
    var ax = el('g', { class: 'bm-axis' }, svg);
    el('line', { x1: PAD.l, y1: PAD.t, x2: PAD.l, y2: H - PAD.b }, ax);
    el('line', { x1: PAD.l, y1: H - PAD.b, x2: W - PAD.r, y2: H - PAD.b }, ax);
    [0, 25, 50, 75, 100].forEach(function (v) {
      el('line', { x1: sx(v), y1: H - PAD.b, x2: sx(v), y2: H - PAD.b + 4 }, ax);
      var t = el('text', { x: sx(v), y: H - PAD.b + 15, class: 'bm-tick', 'text-anchor': v === 0 ? 'start' : v === 100 ? 'end' : 'middle' }, svg); t.textContent = v + (v === 100 ? '%' : '');
    });
    [-30, 0, 30].forEach(function (v) { el('line', { x1: PAD.l - 4, y1: sy(v), x2: PAD.l, y2: sy(v) }, ax); });
    var a1 = el('text', { x: PAD.l + 6, y: PAD.t + 11, class: 'bm-axl' }, svg); a1.textContent = '\u2191 INFLOW';
    var a2 = el('text', { x: PAD.l + 6, y: H - PAD.b - 7, class: 'bm-axl' }, svg); a2.textContent = '\u2193 OUTFLOW';
    var a3 = el('text', { x: W - PAD.r, y: H - 3, class: 'bm-axl bm-axt', 'text-anchor': 'end' }, svg); a3.textContent = 'PRICED-IN, 1Y RANGE \u2192';
    var my = (PAD.t + H - PAD.b) / 2, a4 = el('text', { x: 11, y: my, class: 'bm-axl bm-axt', 'text-anchor': 'middle', transform: 'rotate(-90 11 ' + my + ')' }, svg);
    a4.textContent = 'FLOW MOMENTUM';
    bm.caps = [a1, a2, czt];
    bm.layer = el('g', { class: 'bm-layer' }, svg);
    makeTip();
    bm.nodes = bubbles.map(function (b, i) {
      var node = el('g', { class: 'bm-b', tabindex: '0', role: 'button', 'data-id': b.id, 'aria-label': b.label + (b.example ? ' (example data)' : '') + ': show details' }, bm.layer);
      var ring2 = el('circle', { r: 10, class: 'bm-ring bm-ring2' }, node), ring = el('circle', { r: 10, class: 'bm-ring' }, node), sel = el('circle', { r: 10, class: 'bm-sel' }, node), c = el('circle', { r: 10, class: 'bm-c' }, node);
      var t = el('text', { class: 'bm-label', 'text-anchor': 'middle' }, node); t.textContent = shortOf(b);
      var nd = { b: b, g: node, c: c, ring: ring, ring2: ring2, sel: sel, t: t, hover: false, cls: '' };
      /* stagger the pulse so rings do not beat in unison */
      var ph = ((i * 0.83) % 2.6).toFixed(2); node.style.setProperty('--pd', '-' + ph + 's');
      var hov = function (on, sticky) { nd.hover = on; node.classList.toggle('is-hover', on); bm.layer.classList.toggle('has-hover', on); if (on) showTip(nd, sticky); else hideTip(nd); };
      node.addEventListener('mouseenter', function () { hov(true); }); node.addEventListener('mouseleave', function () { hov(false); });
      node.addEventListener('focus', function () { hov(true); }); node.addEventListener('blur', function () { hov(false); });
      node.addEventListener('pointerdown', function (e) { nd.ptype = e.pointerType; });
      node.addEventListener('click', function () { select(b.id, true); if (nd.ptype === 'touch') { hov(true, true); } });
      node.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(b.id, true); } else if (e.key === 'Escape') hideTip(nd, true); });
      return nd;
    });
    drawBubbles(true);
  }
  var BK = function () { return W < 460 ? 0.62 : 0.85; };
  /* positions follow `state` (the tweened reading); labels are placed for the target reading and glide there */
  function paintBubbles() {
    if (!bm.nodes) return;
    var k = BK();
    bm.nodes.forEach(function (n) {
      var m = model(n.b), r = (8 + m.conc / 100 * 20) * k, x = bx(m.heat, r), y = sy(m.flow);
      n.g.setAttribute('transform', 'translate(' + x.toFixed(1) + ' ' + y.toFixed(1) + ')');
      var rs = r.toFixed(1), rr = (r + 3.5).toFixed(1);
      n.c.setAttribute('r', rs); n.sel.setAttribute('r', rr); n.ring.setAttribute('r', rr); n.ring2.setAttribute('r', rr);
      var tone = m.flow > 12 ? 'in' : m.flow < -12 ? 'out' : 'flat';
      var cls = 'bm-b n-' + nodeKey(n.b) + ' tone-' + tone + (m.heat >= 80 || m.tension >= 75 ? ' warn' : '') + (selected === n.b.id ? ' is-sel' : '') + (n.hover ? ' is-hover' : '');
      if (cls !== n.cls) { n.g.setAttribute('class', cls); n.cls = cls; }
      n.m = m; n.x = x; n.y = y; n.r = r; n.below = y + r + 13 < H - PAD.b;
    });
  }
  function drawBubbles(snap) {
    if (!bm.nodes) return;
    paintBubbles(); placeLabels(snap || reduce);
    if (selected) renderDetail();
  }
  /* target label placement: run declutter on the bubbles as they will be at the target reading */
  function placeLabels(snap) {
    if (!bm.nodes) return;
    var k = BK();
    var T = bm.nodes.map(function (n) {
      var m = model(n.b, tgt.x, tgt.y);
      var rr = (8 + m.conc / 100 * 20) * k;
      return { b: n.b, t: n.t, x: bx(m.heat, rr), y: sy(m.flow), r: rr, tw: 0 };
    });
    var win = declutter(T);
    bm.nodes.forEach(function (n, i) {
      var b = win[i], w = T[i].tw || 0;
      n.lab = { x: b[0], y: b[1], an: b[2], cx: b[0] + (b[2] === 'start' ? w / 2 : b[2] === 'end' ? -w / 2 : 0) };
      if (snap || !n.lc) n.lc = { x: n.lab.cx, y: n.lab.y };
    });
    labelsDirty = false; lastLayout = performance.now();
    applyLabels(snap);
    if (!snap && !reduce) kick();
  }
  /* settled: real anchor + exact spot. moving: centered on the label's own box so a glide never swaps anchors */
  function applyLabels(settled) {
    bm.nodes.forEach(function (n) {
      if (!n.lab) return;
      if (settled) { n.lc.x = n.lab.cx; n.lc.y = n.lab.y; n.t.setAttribute('x', n.lab.x.toFixed(1)); n.t.setAttribute('y', n.lab.y.toFixed(1)); n.t.setAttribute('text-anchor', n.lab.an); }
      else { n.t.setAttribute('x', n.lc.x.toFixed(1)); n.t.setAttribute('y', n.lc.y.toFixed(1)); n.t.setAttribute('text-anchor', 'middle'); }
      n.t.style.opacity = '';
    });
  }
  /* Labels: short names, always shown. Try 64 spots around each bubble (16 directions x 4 distances), measured
     with the real text size, and keep the lowest-cost one: no label/label contact, no caption cover, no bubble
     cover, and never nearer another bubble than its own. Re-run on every scenario/slider change. */
  function declutter(nodes) {
    var fs = W < 460 ? 5.9 : 6.5, lh = 12, GAP = 3;
    /* measure the real rendered size (font + halo stroke) so neighbouring labels never touch */
    var px = 0; try { px = parseFloat(getComputedStyle(nodes[0].t).fontSize) || 0; } catch (e) { px = 0; }
    if (px) lh = Math.ceil(px * 1.2 + 3);
    var caps = [[PAD.l + 2, PAD.t, PAD.l + 66, PAD.t + 15], [PAD.l + 2, H - PAD.b - 19, PAD.l + 74, H - PAD.b], [sx(75) + 4, H - PAD.b - 19, sx(100) - 4, H - PAD.b]];
    /* use the real caption boxes when the browser can measure them */
    if (bm.caps) { try { var cp = bm.caps.map(function (c) { var b = c.getBBox(); return [b.x - 2, b.y - 2, b.x + b.width + 2, b.y + b.height + 2]; }); if (cp.every(function (b) { return b[2] - b[0] > 6; })) caps = cp; } catch (e) { /* keep estimates */ } }
    var N = nodes.length, boxes = new Array(N);
    function hit(bx, p, gap) { var ox = Math.min(bx[2], p[2]) - Math.max(bx[0], p[0]) + gap, oy = Math.min(bx[3], p[3]) - Math.max(bx[1], p[1]) + gap; return ox > 0 && oy > 0 ? ox * oy : 0; }
    function edgeDist(bx, n) { var cx = clamp(n.x, bx[0], bx[2]), cy = clamp(n.y, bx[1], bx[3]); return Math.sqrt((cx - n.x) * (cx - n.x) + (cy - n.y) * (cy - n.y)) - n.r; }
    /* Cost of a label box. Hard rules: never touch another label (1000), never sit nearer another bubble than
       its own (500), never cover an axis caption (300); then avoid covering bubbles and stay inside the plot. */
    function cost(bx, k) {
      var own = nodes[k], o = 0, i, a;
      if (bx[0] < PAD.l - 2) o += 50 + PAD.l - 2 - bx[0]; if (bx[2] > W - 2) o += 50 + bx[2] - W + 2;
      if (bx[1] < PAD.t - 2) o += 50 + PAD.t - 2 - bx[1]; if (bx[3] > H - PAD.b + 2) o += 50 + bx[3] - H + PAD.b - 2;
      for (i = 0; i < N; i++) { if (i !== k && boxes[i]) { a = hit(bx, boxes[i][3], GAP); if (a) o += 1000 + a; } }
      for (i = 0; i < caps.length; i++) { a = hit(bx, caps[i], 1); if (a) o += 300 + a; }
      var dOwn = edgeDist(bx, own);
      for (i = 0; i < N; i++) {
        var n = nodes[i], d = edgeDist(bx, n);
        if (d < 0) o += 60 + Math.min(-d, n.r) * 4;
        else if (i !== k && d < dOwn + 1) o += 500 + (dOwn + 1 - d) * 4;
      }
      return o + dOwn * 0.4;
    }
    var cands = nodes.map(function (n) {
      var lw = shortOf(n.b).length * fs + 6, list = [];
      if (px) { var tw = 0; try { tw = n.t.getComputedTextLength(); } catch (e) { tw = 0; } if (tw > 0) lw = tw + 6; }
      n.tw = lw - 6;
      [0, 8, 16, 26].forEach(function (far) {
        var g = n.r + 5 + far;
        for (var s = 0; s < 16; s++) {
          var ang = s * Math.PI / 8, dx = Math.cos(ang), dy = Math.sin(ang);
          /* anchor the box so its nearest side faces the bubble */
          var anchor = dx > 0.38 ? 'start' : dx < -0.38 ? 'end' : 'middle';
          var ax = dx * g, ay = dy * g;
          var x0 = anchor === 'middle' ? n.x + ax - lw / 2 : anchor === 'start' ? n.x + ax : n.x + ax - lw;
          var top = dy > 0.38 ? n.y + ay : dy < -0.38 ? n.y + ay - lh : n.y + ay - lh / 2;
          var sh = 0; if (x0 < PAD.l - 2) sh = PAD.l - 2 - x0; if (x0 + lw > W - 2) sh = W - 2 - (x0 + lw);
          list.push([ax + sh, top + lh - 4 - n.y, anchor, [x0 + sh, top, x0 + sh + lw, top + lh], s % 4 ? 0.6 : (s === 0 ? 0 : 0.2)]);
        }
      });
      return list;
    });
    function bestFor(k) { var best = null, bestO = Infinity; cands[k].forEach(function (c) { var o = cost(c[3], k) + c[4]; if (o < bestO) { bestO = o; best = c; } }); return best; }
    function total() { var t = 0; for (var k = 0; k < N; k++) t += cost(boxes[k][3], k); return t; }
    var idx = nodes.map(function (n, i) { return i; }), sel = function (i) { return nodes[i].b.id === selected ? 1 : 0; };
    var crowd = nodes.map(function (n) { return nodes.reduce(function (c, m) { return c + (m !== n && Math.hypot(m.x - n.x, m.y - n.y) < n.r + m.r + 40 ? 1 : 0); }, 0); });
    var orders = [
      idx.slice().sort(function (a, b) { return sel(b) - sel(a) || nodes[b].r - nodes[a].r; }),
      idx.slice().sort(function (a, b) { return sel(b) - sel(a) || crowd[b] - crowd[a] || nodes[a].r - nodes[b].r; }),
      idx.slice().sort(function (a, b) { return nodes[a].r - nodes[b].r; }),
      idx.slice().sort(function (a, b) { return nodes[a].x - nodes[b].x; })
    ];
    var win = null, winT = Infinity;
    orders.forEach(function (ord) {
      boxes = new Array(N);
      ord.forEach(function (k) { boxes[k] = bestFor(k); });
      /* a few rounds of re-placing each label against all the others */
      for (var pass = 0; pass < 4; pass++) { var moved = false; ord.forEach(function (k) { var b = bestFor(k); if (b !== boxes[k]) { boxes[k] = b; moved = true; } }); if (!moved) break; }
      var t = total(); if (t < winT) { winT = t; win = boxes.slice(); }
    });
    return win;
  }

  /* ---------------- tooltip (hover, keyboard focus, tap) ---------------- */
  function makeTip() {
    var wrap = $('bubble-svg').parentNode;
    bm.tip = wrap.querySelector('.bm-tip');
    if (!bm.tip) { bm.tip = document.createElement('div'); bm.tip.className = 'bm-tip'; bm.tip.id = 'bm-tip'; bm.tip.setAttribute('role', 'tooltip'); wrap.appendChild(bm.tip); }
    bm.tipFor = null; bm.tip.classList.remove('on');
  }
  function flowWord(f) { return f > 12 ? 'inflow' : f < -12 ? 'outflow' : 'neutral'; }
  function showTip(n, sticky) {
    var t = bm.tip; if (!t || !n.m) return;
    clearTimeout(bm.tipTimer); bm.tipFor = n; bm.tipH = '';
    t.className = 'bm-tip n-' + nodeKey(n.b); n.g.setAttribute('aria-describedby', 'bm-tip');
    refreshTip(); t.classList.add('on');
    if (sticky) bm.tipTimer = setTimeout(function () { hideTip(n, true); }, 3600);
  }
  function refreshTip() {
    var n = bm.tipFor, t = bm.tip; if (!n || !t || !n.m) return;
    var m = n.m, b = n.b, row = function (k, v) { return '<span><i>' + k + '</i> <b>' + v + '</b></span>'; };
    var h = '<div class="tp-n">' + esc(b.label) + '</div>' +
      '<div class="tp-r">' + row('Priced-in', Math.round(m.heat) + '%') + row('Flow', sgn(m.flow) + ' ' + flowWord(m.flow)) + row('Tension', Math.round(m.tension)) + '</div>' +
      (b.metric_value != null ? '<div class="tp-v">' + esc(b.metric_value) + (b.delta ? ' \u00b7 ' + esc(b.delta) : '') + '</div>' : '') +
      (b.example ? '<div class="tp-x">Example data</div>' : '');
    if (h !== bm.tipH) { t.innerHTML = h; bm.tipH = h; }
    placeTip();
  }
  function hideTip(n, force) {
    if (!bm.tip) return;
    if (n && bm.tipFor && bm.tipFor !== n && !force) return;
    clearTimeout(bm.tipTimer);
    var f = bm.tipFor; bm.tipFor = null; bm.tip.classList.remove('on');
    if (f) { f.g.removeAttribute('aria-describedby'); if (f.hover) { f.hover = false; f.g.classList.toggle('is-hover', false); f.cls = ''; } }
    if (bm.layer) bm.layer.classList.remove('has-hover');
  }
  function esc(v) { return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function placeTip() {
    var n = bm.tipFor, t = bm.tip; if (!n || !t) return;
    var svg = $('bubble-svg'), wr = svg.parentNode.getBoundingClientRect(), sr = svg.getBoundingClientRect();
    if (!sr.width) return;
    var k = sr.width / W, px = sr.left - wr.left + n.x * k, py = sr.top - wr.top + n.y * k, rr = (n.r + 4) * k;
    var w = t.offsetWidth, h = t.offsetHeight, left = clamp(px - w / 2, 0, Math.max(0, wr.width - w)), top = py - rr - h - 7, below = top < -40;
    if (below) top = py + rr + 7;
    t.style.left = left.toFixed(1) + 'px'; t.style.top = top.toFixed(1) + 'px';
    t.classList.toggle('below', below);
  }

  /* ---------------- ranked list (mobile < 430px) ---------------- */
  var tt = {};
  function buildList() {
    var ol = $('tension-list'); if (!ol) return; ol.textContent = ''; tt = {}; listKey = '';
    bubbles.forEach(function (b) {
      var li = document.createElement('li'), btn = document.createElement('button');
      btn.type = 'button'; btn.setAttribute('aria-label', b.label + ': show details');
      var nm = document.createElement('span'); nm.className = 'tt-name'; nm.textContent = b.label;
      var sm = document.createElement('small'); nm.appendChild(sm);
      var v = document.createElement('span'); v.className = 'tt-val';
      var bar = document.createElement('span'); bar.className = 'tt-bar'; var fill = document.createElement('i'); bar.appendChild(fill);
      btn.appendChild(nm); btn.appendChild(v); btn.appendChild(bar); li.appendChild(btn); ol.appendChild(li);
      btn.addEventListener('click', function () { select(b.id, true); });
      tt[b.id] = { li: li, v: v, fill: fill, sm: sm, node: nodeKey(b) };
    });
  }
  function drawList() {
    var ol = $('tension-list'); if (!ol || !bm.nodes) return;
    var order = bm.nodes.filter(function (n) { return n.m; }).sort(function (a, b) { return b.m.tension - a.m.tension; });
    order.forEach(function (n) {
      var t = tt[n.b.id]; if (!t) return;
      t.v.textContent = Math.round(n.m.tension);
      t.fill.style.transform = 'scaleX(' + clamp(n.m.tension, 0, 100) / 100 + ')';
      t.sm.textContent = (n.m.flow > 12 ? 'inflow' : n.m.flow < -12 ? 'outflow' : 'neutral') + ' \u00b7 priced-in ' + Math.round(n.m.heat);
      t.li.className = 'n-' + t.node + (n.b.id === selected ? ' is-sel' : '') + (n.m.heat >= 80 || n.m.tension >= 75 ? ' warn' : '');
    });
    var key = order.map(function (n) { return n.b.id; }).join();
    if (key !== listKey) { listKey = key; order.forEach(function (n) { var t = tt[n.b.id]; if (t) ol.appendChild(t.li); }); }
  }

  /* ---------------- detail ---------------- */
  function put(e, v) { v = String(v); if (e && e.textContent !== v) e.textContent = v; }
  function bar(id, pct) { var e = $(id); if (e) e.style.transform = 'scaleX(' + clamp(pct, 0, 100) / 100 + ')'; }
  function select(id, fromUser) {
    selected = id; drawBubbles(false);
    if (fromUser && window.innerWidth < 900) { var d = $('asset-panel'); if (d && d.scrollIntoView) d.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' }); }
  }
  function renderDetail() {
    var n = null; bm.nodes.forEach(function (x) { if (x.b.id === selected) n = x; });
    if (!n || !n.m) return;
    var m = n.m;
    put($('ap-name'), n.b.label); put($('ap-group'), n.b.group);
    var ap = $('asset-panel'); if (ap) ap.setAttribute('data-node', nodeKey(n.b));
    $('ap-group').setAttribute('title', NODE_NAMES[nodeKey(n.b)] || '');
    $('ap-ex').hidden = !n.b.example;
    var src = $('ap-src'); src.textContent = '';
    if (n.b.metric_value != null) {
      var nb = function (t) { return String(t).replace(/ /g, '\u00a0'); };
      src.appendChild(document.createTextNode(nb(n.b.metric_value) + (n.b.delta ? ' \u00b7 ' + nb(n.b.delta) : '') + (n.b.published_at ? ' \u00b7 ' + nb(md(n.b.published_at)) : '') +
        (n.b.range_label && n.b.range_label !== '1-year' ? ' \u00b7 ' + nb(n.b.range_label + ' range') : '') + '\u00a0\u00b7 '));
      if (n.b.source_url) { var a = document.createElement('a'); a.href = n.b.source_url; a.rel = 'noopener'; a.target = '_blank'; a.textContent = n.b.stale ? 'source (cached)' : 'source'; src.appendChild(a); }
    }
    $('ap-read').textContent = n.b.read || n.b.note || '';
    put($('ap-heat'), Math.round(m.heat)); put($('ap-flow'), sgn(m.flow)); put($('ap-tension'), Math.round(m.tension));
    bar('ap-heat-bar', m.heat); bar('ap-tension-bar', m.tension);
    drawList();
    var fb = $('ap-flow-bar'), f = clamp(m.flow, -60, 60) / 60;
    fb.style.transform = 'scaleX(' + Math.abs(f) + ')'; fb.className = 'ap-fill ap-flow ' + (f < 0 ? 'neg' : 'pos');
    var flags = []; if (m.heat >= 80) flags.push('Priced-in 80+'); if (m.tension >= 75) flags.push('High tension');
    var st = $('ap-status'); st.textContent = flags.join(' \u00b7 '); st.hidden = !flags.length;
    $('ap-room').setAttribute('data-slot', n.b.slot || '');
  }

  /* ---------------- readouts & controls ---------------- */
  function readout(qid) {
    var q = quadrants[qid] || {};
    put($('regime-name'), q.label || '');
    put($('regime-read'), (q.read || []).join(' '));
    put($('ro-rates'), sgn(state.y, 1)); put($('ro-liq'), sgn(state.x, 1));
    var stEl = $('regime-static'), tag = $('regime-mode'), live = !touched;
    var mode = $('ck-mode'), rs = $('ck-reset');
    if (mode) { put(mode, live ? 'Current reading' : 'Simulation mode'); var mc = 'ck-mode ' + (live ? 'live' : 'sim'); if (mode.className !== mc) mode.className = mc; }
    if (rs) rs.hidden = live;
    root.classList.toggle('is-sim', !live);
    if (stEl) stEl.classList.toggle('is-sim', !live);
    if (live) { put(tag, 'Sourced \u00b7 snapshot ' + (data.snapshotStamp || '')); tag.className = 'rg-mode'; }
    else { put(tag, activeScenario === 'custom' ? 'Simulated position \u00b7 sliders' : 'Illustrative scenario'); tag.className = 'rg-mode sim'; }
  }
  function syncSliders(px, py) {
    var r = $('sl-rates'), l = $('sl-liq');
    r.value = Math.round(py); l.value = Math.round(px);
    put($('sl-rates-v'), sgn(py)); put($('sl-liq-v'), sgn(px));
    r.style.setProperty('--p', (+r.value + 50) + '%'); l.style.setProperty('--p', (+l.value + 50) + '%');
  }
  function render(snap) {
    var qid = drawCompass(); paintBubbles(); readout(qid);
    if (snap) { placeLabels(true); if (selected) renderDetail(); }
    if (snap || tcfg.sync) syncSliders(state.x, state.y);
  }
  /* Light tween: `state` (the reading that drives bubbles, numbers and sliders) chases the target with an
     exponential ease; the compass needle chases `state`, so it trails a touch and settles softly. Retargeting
     mid-flight (dragging a slider) just changes the target, so there is never a jump. Labels glide to the spots
     declutter() picked for the target reading, then snap to their exact anchors. No timers, no libraries. */
  function kick() { if (!loop) { lastTs = 0; loop = requestAnimationFrame(frame); } }
  function settle() {
    if (loop) { cancelAnimationFrame(loop); loop = null; }
    state.x = tgt.x; state.y = tgt.y; needle.x = state.x; needle.y = state.y;
    var qid = drawCompass(); paintBubbles(); readout(qid); placeLabels(true);
    if (tcfg.sync) syncSliders(state.x, state.y);
    if (selected) renderDetail();
    var side = root.querySelector('.ck-side'); if (side) side.setAttribute('aria-busy', 'false');
    refreshTip();
  }
  function frame(ts) {
    var dt = lastTs ? Math.min(64, ts - lastTs) : 16; lastTs = ts;
    var ks = 1 - Math.exp(-dt / tcfg.ts), kn = 1 - Math.exp(-dt / tcfg.tn), done = true;
    state.x += (tgt.x - state.x) * ks; state.y += (tgt.y - state.y) * ks;
    needle.x += (state.x - needle.x) * kn; needle.y += (state.y - needle.y) * kn;
    if (Math.abs(tgt.x - state.x) > 0.03 || Math.abs(tgt.y - state.y) > 0.03 || Math.abs(state.x - needle.x) > 0.03 || Math.abs(state.y - needle.y) > 0.03) done = false;
    if (bm.nodes) {
      if (labelsDirty && ts - lastLayout > 90) placeLabels(false);
      bm.nodes.forEach(function (n) {
        if (!n.lab) return;
        n.lc.x += (n.lab.cx - n.lc.x) * ks; n.lc.y += (n.lab.y - n.lc.y) * ks;
        if (Math.abs(n.lab.cx - n.lc.x) > 0.2 || Math.abs(n.lab.y - n.lc.y) > 0.2) done = false;
      });
    }
    if (done && !labelsDirty) { loop = null; settle(); return; }
    var qid = drawCompass(); paintBubbles(); readout(qid); applyLabels(false);
    if (tcfg.sync) syncSliders(state.x, state.y);
    if (selected) renderDetail();
    refreshTip();
    loop = requestAnimationFrame(frame);
  }
  /* o: { ts, tn } time constants in ms (state, needle); sync: move the slider thumbs too */
  function animateTo(x, y, o) {
    tcfg = { ts: (o && o.ts) || 95, tn: (o && o.tn) || 120, sync: !o || o.sync !== false };
    tgt.x = x; tgt.y = y; labelsDirty = true;
    if (reduce) { settle(); return; }
    var side = root.querySelector('.ck-side'); if (side) side.setAttribute('aria-busy', 'true');
    kick();
  }
  function pressed(id) { [].forEach.call(document.querySelectorAll('#scenario-pills button'), function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-id') === id ? 'true' : 'false'); }); }
  function setScenario(id, o) {
    var s = null; data.scenarios.forEach(function (x) { if (x.id === id) s = x; }); if (!s) return;
    activeScenario = id; touched = id !== 'current'; pressed(id); $('scenario-note').textContent = s.note || ''; animateTo(s.x, s.y, o);
  }
  function buildControls() {
    var wrap = $('scenario-pills'); wrap.textContent = '';
    data.scenarios.forEach(function (s) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'sc-pill'; b.setAttribute('data-id', s.id);
      b.setAttribute('aria-pressed', 'false'); b.textContent = s.label; b.addEventListener('click', function () { setScenario(s.id); }); wrap.appendChild(b);
    });
    function onSlide() {
      var y = +$('sl-rates').value, x = +$('sl-liq').value;
      activeScenario = 'custom'; touched = true; pressed('');
      $('scenario-note').textContent = 'Custom position from the sliders (illustrative).';
      syncSliders(x, y);
      animateTo(x, y, { ts: 55, tn: 80, sync: false });
    }
    $('sl-rates').addEventListener('input', onSlide); $('sl-liq').addEventListener('input', onSlide);
    [$('sl-rates'), $('sl-liq')].forEach(function (s) { s.disabled = false; });
    var rs = $('ck-reset'); if (rs) rs.addEventListener('click', function () { setScenario('current'); var p = document.querySelector('#scenario-pills [data-id="current"]'); if (p) p.focus(); });
  }

  function init(reg, tr) {
    data = reg; trends = tr || { series: [] };
    (reg.quadrants || []).forEach(function (q) { quadrants[q.id] = q; });
    var tk = readTicker(), ch = changes(tk, 0), now = score(ch);
    var stampEl = document.querySelector('.ticker-stamp');
    data.snapshotStamp = stampEl ? stampEl.textContent.replace(/^Updated\s*/, '') : (reg.as_of || '');
    var st = $('regime-static');
    if (!now && reg.compass) now = { x: reg.compass.x, y: reg.compass.y };
    if (!now && st && st.getAttribute('data-x') !== null) now = { x: +st.getAttribute('data-x'), y: +st.getAttribute('data-y') };
    base = now || { x: 0, y: 0 };
    ghost = (reg.compass && reg.compass.prev_needle) || score(changes(tk, WEEK)) || null;
    reg.scenarios.forEach(function (s) { if (s.id === 'current') { s.x = base.x; s.y = base.y; } });
    bubbles = (reg.bubble_map && reg.bubble_map.bubbles) || [];
    selected = (reg.bubble_map && reg.bubble_map.default_selected) || (bubbles[0] && bubbles[0].id);
    if (reg.bubble_map && reg.bubble_map.example) { var mt = $('map-tag'); if (mt) { mt.textContent = 'Example data'; mt.className = 'ck-tag ex'; } }
    /* open on the real reading: bubbles and numbers are already in place; only the needle sweeps in from 1W ago */
    state.x = tgt.x = base.x; state.y = tgt.y = base.y;
    needle.x = reduce || !ghost ? base.x : ghost.x; needle.y = reduce || !ghost ? base.y : ghost.y;
    buildCompass(); drawGhost(); buildControls(); buildNodeLegend(); buildBubbles(); buildList();
    $('ghost-note').textContent = ghost ? '1W ago: rates ' + sgn(ghost.y, 1) + ' \u00b7 liq. ' + sgn(ghost.x, 1) : 'n/a';
    var hist = (trends.series || []).filter(function (s) { return s.available && s.history && s.history.length; }).map(function (s) { return s.history[s.history.length - 1].date; }).sort();
    var p = reg.pipeline || {}, nStale = 0; for (var k in (p.nodes || {})) if (p.nodes[k].status !== 'ok') nStale++;
    /* the compass is recomputed from the ticker; the bubbles come from the last pipeline run: show both when they differ */
    var bubbleStamp = (reg.compass && reg.compass.snapshot_stamp) || reg.as_of || '';
    var stampText = !bubbleStamp || bubbleStamp === data.snapshotStamp ? 'Snapshot ' + data.snapshotStamp
      : 'Compass: snapshot ' + data.snapshotStamp + ' \u00b7 Bubbles: snapshot ' + bubbleStamp;
    $('data-stamp').textContent = stampText + (hist.length ? ' \u00b7 history to ' + md(hist[hist.length - 1]) : '') +
      (nStale ? ' \u00b7 ' + nStale + ' signal group' + (nStale > 1 ? 's' : '') + ' on cached data' : '') + (trends._cached || reg._cached ? ' \u00b7 cached copy' : '');
    render(true);
    root.classList.add('is-ready');
    setTimeout(function () { setScenario('current', { ts: 80, tn: 230 }); }, reduce ? 0 : 350);
    if (window.IntersectionObserver) { try { new IntersectionObserver(function (es) { root.classList.toggle('is-offscreen', !es[es.length - 1].isIntersecting); }, { threshold: 0 }).observe(root); } catch (e) { /* pulses keep running */ } }
    if (rmq && rmq.addEventListener) rmq.addEventListener('change', function (e) { reduce = e.matches; if (reduce) settle(); });
    document.addEventListener('click', function (e) { if (bm.tipFor && !(e.target.closest && e.target.closest('.bm-b'))) hideTip(null, true); });
    var rt = null, lw = window.innerWidth; window.addEventListener('resize', function () { if (window.innerWidth === lw) return; lw = window.innerWidth; clearTimeout(rt); rt = setTimeout(function () { buildBubbles(); }, 120); });
    $('ap-room').addEventListener('click', function () { $('ap-room-note').textContent = 'Not open yet'; });
  }
  var hist = load('/data/snapshot_history.json', 'ryzm.hist.v3').catch(function () { return null; });
  Promise.all([load('/data/dashboard_state.json', 'ryzm.state.v3'), hist])
    .then(function (r) { init(r[0], r[1]); })
    .catch(function () { root.classList.add('is-error'); var e = $('cockpit-error'); if (e) e.hidden = false; });
})();
