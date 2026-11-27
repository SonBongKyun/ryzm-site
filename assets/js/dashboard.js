/* RYZM Macro Compass & Priced-In / Tension Map (v3, 2026-11-27).
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
   Fallbacks: last-good JSON in localStorage; if nothing loads, the static
   summary already in the HTML stays and the dashboard shows a notice. */
(function () {
  'use strict';
  var root = document.getElementById('cockpit');
  if (!root || !window.fetch || !document.createElementNS) return;
  var NS = 'http://www.w3.org/2000/svg';
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var $ = function (id) { return document.getElementById(id); };
  var MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var WINDOW = 20, WEEK = 5;
  var data, trends, base = { x: 0, y: 0 }, ghost = null, bubbles = [], quadrants = {}, selected = null;
  var state = { x: 0, y: 0 }, anim = null, activeScenario = 'current', inputs = {}, touched = false;

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
    for (var id in quads) cmp.quad[id] = el('rect', { x: quads[id][0], y: quads[id][1], width: R, height: R, class: 'cmp-q' }, q);
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
      var t = el('text', { x: p[0], y: p[1] - (lines.length - 1) * 7 + 4, class: 'cmp-ql', 'text-anchor': 'middle' }, svg);
      lines.forEach(function (ln, i) { var s = el('tspan', { x: p[0], dy: i ? 14 : 0 }, t); s.textContent = ln; });
      cmp.qlabel[qq.id] = t;
    });
    cmp.ghost = el('g', { class: 'cmp-ghost' }, svg);
    cmp.gl = el('line', { x1: C, y1: C, x2: C, y2: C }, cmp.ghost);
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
  function needlePath(px, py) {
    var dx = px - C, dy = py - C, L = Math.sqrt(dx * dx + dy * dy);
    if (L < 2) return '';
    var ux = dx / L, uy = dy / L, nx = -uy, ny = ux, w = 3.2, tail = 13;
    var f = function (v) { return v.toFixed(2); };
    return 'M' + f(px) + ' ' + f(py) + 'L' + f(C + nx * w) + ' ' + f(C + ny * w) + 'L' + f(C - ux * tail) + ' ' + f(C - uy * tail) +
      'L' + f(C - nx * w) + ' ' + f(C - ny * w) + 'Z';
  }
  function drawCompass() {
    var p = toXY(state.x, state.y);
    cmp.nl.setAttribute('d', needlePath(p[0], p[1]));
    ['tip', 'halo'].forEach(function (k) { cmp[k].setAttribute('cx', p[0].toFixed(2)); cmp[k].setAttribute('cy', p[1].toFixed(2)); });
    var qid = quadrantOf(state.x, state.y);
    for (var id in cmp.quad) { cmp.quad[id].classList.toggle('on', id === qid); if (cmp.qlabel[id]) cmp.qlabel[id].classList.toggle('on', id === qid); }
    return qid;
  }
  function drawGhost() {
    if (!ghost) { cmp.ghost.style.display = 'none'; return; }
    cmp.ghost.style.display = '';
    var p = toXY(ghost.x, ghost.y), dx = p[0] - C, dy = p[1] - C, L = Math.sqrt(dx * dx + dy * dy) || 1;
    cmp.gl.setAttribute('x2', p[0]); cmp.gl.setAttribute('y2', p[1]); cmp.gd.setAttribute('cx', p[0]); cmp.gd.setAttribute('cy', p[1]);
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
  function soft(v, mid, half) { return mid + half * Math.tanh((v - mid) / half); }
  function model(b) {
    var dx = state.x - base.x, dy = state.y - base.y, s = b.sens || {}, K = 0.6;
    var heat = soft(b.priced_in + K * ((s.priced_in_x || 0) * dx + (s.priced_in_y || 0) * dy), 50, 47);
    var flow = soft(b.flow + K * ((s.flow_x || 0) * dx + (s.flow_y || 0) * dy), 0, 55);
    var tension = clamp(b.disagreement + 0.12 * Math.sqrt(dx * dx + dy * dy), 0, 99);
    var conc = clamp(b.concentration + 0.3 * (heat - b.priced_in), 10, 100);
    return { heat: heat, flow: flow, tension: tension, conc: conc };
  }
  var SHORT = { us2y: 'US 2Y', us10y: 'US 10Y', usd: 'DXY', krw: 'KRW', kospi: 'KOSPI', memory: 'KR Memory', yen_carry: 'Yen Carry',
    sox: 'US Semis', hy: 'HY Credit', ig: 'IG Credit', btc_lev: 'BTC Lev', eth_lev: 'ETH Lev' };
  function shortOf(b) { return b.short && b.short !== b.label ? b.short : SHORT[b.id] || b.label.replace(/\s*\(.*\)\s*$/, ''); }
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
    bm.layer = el('g', {}, svg);
    bm.nodes = bubbles.map(function (b) {
      var node = el('g', { class: 'bm-b', tabindex: '0', role: 'button', 'data-id': b.id, 'aria-label': b.label + (b.example ? ' (example data)' : '') + ': show details' }, bm.layer);
      var ring = el('circle', { r: 10, class: 'bm-ring' }, node), sel = el('circle', { r: 10, class: 'bm-sel' }, node), c = el('circle', { r: 10, class: 'bm-c' }, node);
      el('title', {}, node).textContent = b.label;
      var t = el('text', { class: 'bm-label', 'text-anchor': 'middle' }, node); t.textContent = shortOf(b);
      var full = function (on) { t.textContent = on ? b.label : shortOf(b); node.classList.toggle('is-hover', on); if (on) bm.layer.appendChild(node); };
      node.addEventListener('mouseenter', function () { full(true); }); node.addEventListener('mouseleave', function () { full(false); });
      node.addEventListener('focus', function () { full(true); }); node.addEventListener('blur', function () { full(false); });
      node.addEventListener('click', function () { select(b.id, true); });
      node.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(b.id, true); } });
      return { b: b, g: node, c: c, ring: ring, sel: sel, t: t };
    });
    drawBubbles();
  }
  function drawBubbles() {
    if (!bm.nodes) return;
    var k = W < 460 ? 0.62 : 0.85;
    bm.nodes.forEach(function (n) {
      var m = model(n.b), r = (8 + m.conc / 100 * 20) * k, x = sx(m.heat), y = sy(m.flow);
      n.g.setAttribute('transform', 'translate(' + x.toFixed(1) + ' ' + y.toFixed(1) + ')');
      n.c.setAttribute('r', r.toFixed(1)); n.sel.setAttribute('r', (r + 3.5).toFixed(1)); n.ring.setAttribute('r', (r + 3.5).toFixed(1));
      var tone = m.flow > 12 ? 'in' : m.flow < -12 ? 'out' : 'flat';
      n.g.setAttribute('class', 'bm-b tone-' + tone + (m.heat >= 80 || m.tension >= 75 ? ' warn' : '') + (selected === n.b.id ? ' is-sel' : ''));
      n.m = m; n.x = x; n.y = y; n.r = r; n.below = y + r + 13 < H - PAD.b;
    });
    declutter();
    if (selected) renderDetail();
  }
  /* Labels: short names, always shown. Try 12 spots around each bubble (below, above, sides, diagonals,
     near then far) and take the first with no overlap with labels, bubbles, axis captions or the plot edge;
     if none is clear, take the spot with the least overlap. */
  function declutter() {
    var fs = W < 460 ? 5.9 : 6.5, lh = 12;
    var placed = [[PAD.l + 2, PAD.t, PAD.l + 66, PAD.t + 15], [PAD.l + 2, H - PAD.b - 19, PAD.l + 74, H - PAD.b], [sx(75) + 4, H - PAD.b - 19, sx(100) - 4, H - PAD.b]];
    var order = bm.nodes.slice().sort(function (a, b) { return (b.b.id === selected) - (a.b.id === selected) || b.r - a.r; });
    function overlap(bx) {
      var o = 0;
      if (bx[0] < PAD.l - 2) o += PAD.l - 2 - bx[0]; if (bx[2] > W - 2) o += bx[2] - W + 2;
      if (bx[1] < PAD.t - 2) o += PAD.t - 2 - bx[1]; if (bx[3] > H - PAD.b + 2) o += bx[3] - H + PAD.b - 2;
      for (var i = 0; i < placed.length; i++) { var p = placed[i], ox = Math.min(bx[2], p[2]) - Math.max(bx[0], p[0]), oy = Math.min(bx[3], p[3]) - Math.max(bx[1], p[1]); if (ox > 0 && oy > 0) o += ox * oy / 4 + 20; }
      for (var j = 0; j < bm.nodes.length; j++) { var n = bm.nodes[j], cx = clamp(n.x, bx[0], bx[2]), cy = clamp(n.y, bx[1], bx[3]), d2 = (cx - n.x) * (cx - n.x) + (cy - n.y) * (cy - n.y); if (d2 < n.r * n.r) o += 10 + (n.r * n.r - d2) / 8; }
      return o;
    }
    order.forEach(function (n) {
      var lw = shortOf(n.b).length * fs + 6, cands = [];
      [0, 9].forEach(function (far) {
        var g = n.r + 5 + far;
        cands.push([0, g + lh / 2, 'middle'], [0, -g - lh / 2 + 2, 'middle'], [g, 3, 'start'], [-g, 3, 'end'],
                   [g * 0.72, g * 0.72 + 4, 'start'], [-g * 0.72, g * 0.72 + 4, 'end'], [g * 0.72, -g * 0.72, 'start'], [-g * 0.72, -g * 0.72, 'end']);
      });
      var best = null, bestO = Infinity;
      for (var k = 0; k < cands.length; k++) {
        var c = cands[k], x0 = c[2] === 'middle' ? n.x + c[0] - lw / 2 : c[2] === 'start' ? n.x + c[0] : n.x + c[0] - lw, y1 = n.y + c[1];
        var sh = 0; if (x0 < PAD.l - 2) sh = PAD.l - 2 - x0; if (x0 + lw > W - 2) sh = W - 2 - (x0 + lw);
        var bx = [x0 + sh, y1 - lh + 2, x0 + sh + lw, y1 + 2], o = overlap(bx);
        if (o < bestO) { bestO = o; best = [c[0] + sh, c[1], c[2], bx]; if (!o) break; }
      }
      n.t.setAttribute('x', best[0].toFixed(1)); n.t.setAttribute('y', best[1].toFixed(1)); n.t.setAttribute('text-anchor', best[2]); n.t.style.opacity = '';
      placed.push(best[3]);
    });
  }

  /* ---------------- ranked list (mobile < 430px) ---------------- */
  var tt = {};
  function buildList() {
    var ol = $('tension-list'); if (!ol) return; ol.textContent = ''; tt = {};
    bubbles.forEach(function (b) {
      var li = document.createElement('li'), btn = document.createElement('button');
      btn.type = 'button'; btn.setAttribute('aria-label', b.label + ': show details');
      var nm = document.createElement('span'); nm.className = 'tt-name'; nm.textContent = b.label;
      var sm = document.createElement('small'); nm.appendChild(sm);
      var v = document.createElement('span'); v.className = 'tt-val';
      var bar = document.createElement('span'); bar.className = 'tt-bar'; var fill = document.createElement('i'); bar.appendChild(fill);
      btn.appendChild(nm); btn.appendChild(v); btn.appendChild(bar); li.appendChild(btn); ol.appendChild(li);
      btn.addEventListener('click', function () { select(b.id, true); });
      tt[b.id] = { li: li, v: v, fill: fill, sm: sm };
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
      t.li.className = (n.b.id === selected ? 'is-sel' : '') + (n.m.heat >= 80 || n.m.tension >= 75 ? ' warn' : '');
      ol.appendChild(t.li);
    });
  }

  /* ---------------- detail ---------------- */
  function bar(id, pct) { var e = $(id); if (e) e.style.transform = 'scaleX(' + clamp(pct, 0, 100) / 100 + ')'; }
  function select(id, fromUser) {
    selected = id; drawBubbles();
    if (fromUser && window.innerWidth < 900) { var d = $('asset-panel'); if (d && d.scrollIntoView) d.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' }); }
  }
  function renderDetail() {
    var n = null; bm.nodes.forEach(function (x) { if (x.b.id === selected) n = x; });
    if (!n || !n.m) return;
    var m = n.m;
    $('ap-name').textContent = n.b.label; $('ap-group').textContent = n.b.group;
    $('ap-ex').hidden = !n.b.example;
    var src = $('ap-src'); src.textContent = '';
    if (n.b.metric_value != null) {
      var nb = function (t) { return String(t).replace(/ /g, '\u00a0'); };
      src.appendChild(document.createTextNode(nb(n.b.metric_value) + (n.b.delta ? ' \u00b7 ' + nb(n.b.delta) : '') + (n.b.published_at ? ' \u00b7 ' + nb(md(n.b.published_at)) : '') + '\u00a0\u00b7 '));
      if (n.b.source_url) { var a = document.createElement('a'); a.href = n.b.source_url; a.rel = 'noopener'; a.target = '_blank'; a.textContent = n.b.stale ? 'source (cached)' : 'source'; src.appendChild(a); }
    }
    $('ap-read').textContent = n.b.read || n.b.note || '';
    $('ap-heat').textContent = Math.round(m.heat); $('ap-flow').textContent = sgn(m.flow); $('ap-tension').textContent = Math.round(m.tension);
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
    $('regime-name').textContent = q.label || '';
    $('regime-read').textContent = (q.read || []).join(' ');
    $('ro-rates').textContent = sgn(state.y, 1); $('ro-liq').textContent = sgn(state.x, 1);
    var stEl = $('regime-static'), tag = $('regime-mode'), live = !touched;
    var mode = $('ck-mode'), rs = $('ck-reset');
    if (mode) { mode.textContent = live ? 'Live snapshot' : 'Simulation mode'; mode.className = 'ck-mode ' + (live ? 'live' : 'sim'); }
    if (rs) rs.hidden = live;
    root.classList.toggle('is-sim', !live);
    if (stEl) stEl.classList.toggle('is-sim', !live);
    if (live) { tag.textContent = 'Sourced \u00b7 snapshot ' + (data.snapshotStamp || ''); tag.className = 'rg-mode'; }
    else { tag.textContent = activeScenario === 'custom' ? 'Simulated position \u00b7 sliders' : 'Illustrative scenario'; tag.className = 'rg-mode sim'; }
  }
  function syncSliders() {
    var r = $('sl-rates'), l = $('sl-liq');
    r.value = Math.round(state.y); l.value = Math.round(state.x);
    $('sl-rates-v').textContent = sgn(state.y); $('sl-liq-v').textContent = sgn(state.x);
    r.style.setProperty('--p', (+r.value + 50) + '%'); l.style.setProperty('--p', (+l.value + 50) + '%');
  }
  function render() { var qid = drawCompass(); drawBubbles(); readout(qid); syncSliders(); }
  function ease(t) { return 1 - Math.pow(1 - t, 4); }
  function animateTo(x, y) {
    if (anim) cancelAnimationFrame(anim);
    if (reduce) { state.x = x; state.y = y; render(); return; }
    var x0 = state.x, y0 = state.y, t0 = null, dur = 850;
    function step(ts) { if (t0 === null) t0 = ts; var t = Math.min(1, (ts - t0) / dur), e = ease(t);
      state.x = x0 + (x - x0) * e; state.y = y0 + (y - y0) * e; render(); anim = t < 1 ? requestAnimationFrame(step) : null; }
    anim = requestAnimationFrame(step);
  }
  function pressed(id) { [].forEach.call(document.querySelectorAll('#scenario-pills button'), function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-id') === id ? 'true' : 'false'); }); }
  function setScenario(id) {
    var s = null; data.scenarios.forEach(function (x) { if (x.id === id) s = x; }); if (!s) return;
    activeScenario = id; touched = id !== 'current'; pressed(id); $('scenario-note').textContent = s.note || ''; animateTo(s.x, s.y);
  }
  function buildControls() {
    var wrap = $('scenario-pills'); wrap.textContent = '';
    data.scenarios.forEach(function (s) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'sc-pill'; b.setAttribute('data-id', s.id);
      b.setAttribute('aria-pressed', 'false'); b.textContent = s.label; b.addEventListener('click', function () { setScenario(s.id); }); wrap.appendChild(b);
    });
    function onSlide() {
      if (anim) { cancelAnimationFrame(anim); anim = null; }
      state.y = +$('sl-rates').value; state.x = +$('sl-liq').value; activeScenario = 'custom'; touched = true; pressed('');
      $('scenario-note').textContent = 'Custom position from the sliders (illustrative).'; render();
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
    buildCompass(); drawGhost(); buildControls(); buildBubbles(); buildList();
    $('ghost-note').textContent = ghost ? 'rates ' + sgn(ghost.y, 1) + ' \u00b7 liq. ' + sgn(ghost.x, 1) : 'n/a';
    var hist = (trends.series || []).filter(function (s) { return s.available && s.history && s.history.length; }).map(function (s) { return s.history[s.history.length - 1].date; }).sort();
    var p = reg.pipeline || {}, nStale = 0; for (var k in (p.nodes || {})) if (p.nodes[k].status !== 'ok') nStale++;
    $('data-stamp').textContent = 'Snapshot ' + data.snapshotStamp + (hist.length ? ' \u00b7 history to ' + md(hist[hist.length - 1]) : '') +
      (nStale ? ' \u00b7 ' + nStale + ' signal group' + (nStale > 1 ? 's' : '') + ' on cached data' : '') + (trends._cached || reg._cached ? ' \u00b7 cached copy' : '');
    state.x = 0; state.y = 0; render();
    root.classList.add('is-ready');
    setTimeout(function () { setScenario('current'); }, reduce ? 0 : 200);
    var rt = null, lw = window.innerWidth; window.addEventListener('resize', function () { if (window.innerWidth === lw) return; lw = window.innerWidth; clearTimeout(rt); rt = setTimeout(function () { buildBubbles(); }, 120); });
    $('ap-room').addEventListener('click', function () { $('ap-room-note').textContent = 'Not open yet \u00b7 slot ' + ($('ap-room').getAttribute('data-slot') || 'n/a'); });
  }
  var hist = load('/data/snapshot_history.json', 'ryzm.hist.v3').catch(function () { return null; });
  Promise.all([load('/data/dashboard_state.json', 'ryzm.state.v3'), hist])
    .then(function (r) { init(r[0], r[1]); })
    .catch(function () { root.classList.add('is-error'); var e = $('cockpit-error'); if (e) e.hidden = false; });
})();
