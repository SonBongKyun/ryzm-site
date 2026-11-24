#!/usr/bin/env python3
"""Build in-site article pages for ryzm.app from WordPress and local Close folders.

Usage (from the repo root):
  python3 scripts/build_articles.py
  python3 scripts/build_articles.py --retemplate
      Re-render existing pages with the current template; no WordPress fetch.
  python3 scripts/build_articles.py --local-close /path/to/korea-close-YYYYMMDD --retemplate
      Ingest one Close folder (repeat --local-close as needed), persist it under
      sources/closes/, and rebuild. Prefer --retemplate so a Close publish does
      not depend on a live WordPress fetch.
  python3 scripts/build_articles.py --local-closes-dir /path/to/closes --retemplate
      Ingest every korea-close-YYYYMMDD and us-close-YYYYMMDD folder in a directory.

Python 3 standard library only. Idempotent: safe to run every day.

Local Close posts are written to sources/closes/<slug>.json and
assets/articles/<slug>.png.b64. Later WordPress-driven runs merge those files
back in and will not delete or overwrite them.

This script never edits ul.breaking-list or div.feed in index.html. Those
sections are committed by Gemini via Apps Script. Before pushing index.html,
re-fetch main and rebase so those live sections are the latest version.

What it does
  1. Fetches every post from the WordPress.com public API (ryzm9.wordpress.com).
  2. Keeps long-form market articles (research summaries, framework notes,
     Korea/US close, key issues, rates/FX/funding analysis). Skips Daily Briefs,
     Market Wire news notes, off-topic series, and very short posts.
  3. Writes /articles/<slug>/index.html for each article, /articles/index.html,
     and /articles/<category>/ for each category that has articles
     (frameworks, rates, funding, fx, korea, credit).
     Daily Briefs get their own pages at /notes/<slug>/ plus /notes/index.html
     (kept out of the Articles list). The WordPress About page is rendered at /about/.
  4. Rewrites links to those WordPress posts in index.html and llms.txt
     (including the JSON-LD block) so they point to the in-site pages.
  5. Regenerates sitemap.xml and the "## Articles" section of llms.txt.
  6. Assigns each article a research category (see CATEGORY RULES below), writes
     data-cat / data-search attributes for the /articles/ search and filter, adds
     2-3 "Related articles" to every article page, and stores the categories in
     articles/manifest.json ("items").
  7. Keeps the home page Breaking cards and Feed cards newest-first (by KST time).
"""
import base64
import html
import json
import os
import re
import shutil
import struct
import sys
import textwrap
import urllib.request
from datetime import datetime, timedelta, timezone
import email.utils
from html.parser import HTMLParser

SITE = "https://ryzm.app"
WP_SITE = "ryzm9.wordpress.com"
API = "https://public-api.wordpress.com/rest/v1.1/sites/%s/posts/" % WP_SITE
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "articles")
MANIFEST = os.path.join(OUT, "manifest.json")
NOTES_OUT = os.path.join(ROOT, "notes")
NOTES_MANIFEST = os.path.join(NOTES_OUT, "manifest.json")
ABOUT_OUT = os.path.join(ROOT, "about")
ABOUT_SLUG = "about-2"
BRIEF_CAT = "Daily Brief"
SOURCES = os.path.join(ROOT, "sources", "closes")
ASSET_ARTICLES = os.path.join(ROOT, "assets", "articles")
KST = timezone(timedelta(hours=9))

INCLUDE_CATS = {"Key Issues", "Framework Note", "Korea Close", "US Close",
                "Rates & Bonds", "FX", "Funding & Liquidity", "Korea Bridge", "Credit"}
EXCLUDE_CATS = {"Daily Brief", "Market Wire", "Young Trends Daily", "Science & Tech Daily",
                "Short Reflections", "Tech Idea", "Everyday Explainers"}
MIN_TEXT_CHARS = 900

FAVICON_LINKS = """<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="alternate" type="application/rss+xml" title="RYZM RSS" href="/rss.xml">"""
OG_TAGS = """<meta property="og:image" content="%s/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:image" content="%s/og-image.png">
""" % (SITE, SITE)
WP_SUBSCRIBE = "https://ryzm9.wordpress.com/subscribe/"



# ---------------------------------------------------------------- fetch
def fetch_posts():
    posts, page = [], 1
    fields = "ID,title,date,modified,URL,slug,content,excerpt,categories,tags,status"
    while True:
        url = "%s?number=100&page=%d&fields=%s" % (API, page, fields)
        req = urllib.request.Request(url, headers={"User-Agent": "ryzm-site-build/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        batch = data.get("posts", [])
        posts.extend(batch)
        if not batch or len(posts) >= data.get("found", 0):
            break
        page += 1
    return [p for p in posts if p.get("status", "publish") == "publish"]


FRAMEWORK_NOTE_CAT_ID = 199488472  # WordPress category "Framework Note"


def cats_of(p):
    """Return WordPress category names, unescaped. Also treat category ID 199488472 as Framework Note."""
    raw = p.get("categories") or {}
    names = set()
    items = raw.items() if isinstance(raw, dict) else [(None, x) for x in (raw or [])]
    for key, meta in items:
        if isinstance(meta, dict):
            name = html.unescape(meta.get("name") or (key or "") or "")
            cid = meta.get("ID") or meta.get("id")
        else:
            name = html.unescape(key if isinstance(key, str) else (meta or ""))
            cid = None
        if name:
            names.add(name)
        try:
            if cid is not None and int(cid) == FRAMEWORK_NOTE_CAT_ID:
                names.add("Framework Note")
        except (TypeError, ValueError):
            pass
    return names


def plain(s):
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()
    return re.sub(r"\s+([.,;:!?)])", r"\1", t)


def is_article(p):
    cats = cats_of(p)
    if cats & EXCLUDE_CATS or not cats & INCLUDE_CATS:
        return False
    return len(plain(p.get("content"))) >= MIN_TEXT_CHARS


def is_brief(p):
    return BRIEF_CAT in cats_of(p) and len(plain(p.get("content"))) >= 200


def fetch_page(slug):
    url = "%sslug:%s?type=page&fields=ID,title,date,modified,URL,slug,content" % (API, slug)
    req = urllib.request.Request(url, headers={"User-Agent": "ryzm-site-build/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def type_label(p):
    t, cats = plain(p["title"]), cats_of(p)
    if re.search(r"Goldman|Hartnett|Flow Show", t):
        return "Research Summary"
    if "Framework Note" in cats:
        return "Framework"
    if "Korea Close" in cats:
        return "Korea Close"
    if "US Close" in cats:
        return "US Close"
    if "Key Issues" in cats:
        return "Key Issue"
    return "Analysis"


# ---------------------------------------------------------------- categories
# CATEGORY RULES (deterministic; same input -> same output)
# Pills on /articles/: All, Rates, Funding, FX, Korea, Credit, Frameworks.
# A pill is dropped when no article is in it.
#
# An article can sit in several categories (data-cat="Rates Frameworks"); the
# filter matches any of them. The first one ("primary") is the label shown on
# the list and drives "Related articles".
#
# 1. Membership. An article is in a category when ANY of these match:
#    Frameworks  WordPress category "Framework Note" (ID 199488472)
#    Korea       WordPress category "Korea Close"; or "Korea Bridge" unless the post
#                is a "US Close"; or a title keyword: Korea, KOSPI, KOSDAQ, BOK, won, KRW, KTB
#    Funding     WordPress category "Funding & Liquidity"; or title keyword: SOFR, IORB,
#                repo, reserves, funding, liquidity, bill(s), collateral
#    FX          WordPress category "FX"; or title keyword: FX, dollar, currency, hedging
#    Credit      WordPress category "Credit"; or title keyword: credit, spread(s), default,
#                high yield, private credit, IPO, debt, leverage/leveraged, payout
#    Rates       WordPress category "Rates & Bonds" or "US Close"; or title keyword:
#                Treasury, yield(s), curve, term premium, bond(s), long end, rate(s),
#                Fed, FOMC, mortgage
# 2. Primary category (the label):
#    a. "Frameworks" for Framework Notes;
#    b. else the first category whose TITLE keyword matched, checked in the order
#       Korea, Funding, Credit, FX, Rates;
#    c. else, by post type: Korea Close -> Korea, US Close -> Rates;
#    d. else the first WordPress-category match in the order Funding, FX, Credit, Rates, Korea;
#    e. else "Rates".
CATEGORY_ORDER = ["Rates", "Funding", "FX", "Korea", "Credit", "Frameworks"]
CATEGORY_DESCRIPTIONS = {
    "Frameworks": "RYZM Frameworks: reading frameworks on how US rates, dollar funding, and FX move into Korean markets. Not forecasts.",
    "Rates": "RYZM articles on US rates, Treasuries, and the yield curve, and how they reach Korean markets.",
    "Funding": "RYZM articles on dollar funding, SOFR, reserves, and liquidity plumbing.",
    "FX": "RYZM articles on the dollar, the won, and FX hedging.",
    "Korea": "RYZM articles on Korean markets, the won, and how US rates and funding reach Korea.",
    "Credit": "RYZM articles on credit, spreads, and related market structure.",
}


def cat_slug(name):
    """Stable URL slug for a library category (Frameworks -> frameworks)."""
    return (name or "").strip().lower()


CATEGORY_SLUGS = {cat_slug(c): c for c in CATEGORY_ORDER}


def cat_href(name):
    """In-site library URL: All -> /articles/, Frameworks -> /articles/frameworks/."""
    if not name or name == "All":
        return "/articles/"
    return "/articles/%s/" % cat_slug(name)

TITLE_KEYWORDS = [
    ("Korea", r"\b(korea|korean|kospi|kosdaq|bok|won|krw|ktb)\b|\bbok[\u2019']s\b"),
    ("Funding", r"\b(sofr|iorb|repo|reserves|funding|liquidity|bills?|collateral)\b"),
    ("Credit", r"\b(credit|spreads?|default|high[- ]yield|ipo|debt|leverag\w*|payout)\b"),
    ("FX", r"\b(fx|dollar|currency|hedging)\b"),
    ("Rates", r"\b(treasury|yields?|curve|term premium|bonds?|long end|rates?|fed|fomc|mortgage)\b"),
]
WP_CATEGORY_MAP = [("Funding", {"Funding & Liquidity"}), ("FX", {"FX"}), ("Credit", {"Credit"}),
                   ("Rates", {"Rates & Bonds", "US Close"}), ("Korea", {"Korea Close"}),
                   ("Frameworks", {"Framework Note"})]


def categorize(title, wp_cats):
    """Return (primary, [categories in CATEGORY_ORDER]) for an article. See CATEGORY RULES."""
    t = title.lower().replace("\u2019", "'")
    wp = set(wp_cats or ())
    title_hits = [c for c, rx in TITLE_KEYWORDS if re.search(rx, t)]
    wp_hits = [c for c, names in WP_CATEGORY_MAP if wp & names]
    if "Korea Bridge" in wp and "US Close" not in wp:
        wp_hits.append("Korea")
    cats = set(title_hits) | set(wp_hits)
    if "Framework Note" in wp:
        cats.add("Frameworks")
        primary = "Frameworks"
    elif title_hits:
        primary = title_hits[0]
    elif "Korea Close" in wp:
        primary = "Korea"
    elif "US Close" in wp:
        primary = "Rates"
    elif wp_hits:
        primary = wp_hits[0]
    else:
        primary = "Rates"
    cats.add(primary)
    ordered = [primary] + [c for c in CATEGORY_ORDER if c in cats and c != primary]
    return primary, ordered


def apply_categories(a):
    a["category"], a["categories"] = categorize(a["title"], a.get("wp_cats") or [])
    return a


def related_for(a, arts, n=3):
    """Same primary category first, then most recent; never the article itself."""
    others = [b for b in arts if b["slug"] != a["slug"]]
    others.sort(key=lambda b: b["date"], reverse=True)
    same = [b for b in others if b["category"] == a["category"]]
    shared = [b for b in others if b not in same and set(b["categories"]) & set(a["categories"])]
    rest = [b for b in others if b not in same and b not in shared]
    return (same + shared + rest)[:n]


def norm_url(u):
    u = u.split("#")[0].split("?")[0]
    u = re.sub(r"^http://", "https://", u)
    return u.rstrip("/") + "/"


# ---------------------------------------------------------------- sanitize
ALLOWED = {"p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "b", "i",
           "a", "blockquote", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
           "figure", "figcaption", "img", "br", "hr", "code", "pre", "sup", "sub", "caption"}
VOID = {"img", "br", "hr"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "noscript", "form", "object", "embed",
                     "svg", "button", "input", "select", "textarea", "video", "audio"}
DROP_CLASS = re.compile(r"sharedaddy|sd-|jp-relatedposts|wpcnt|wp-block-buttons|like|share|"
                        r"subscribe|jetpack", re.I)
ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "width", "height"},
         "th": {"colspan", "rowspan", "scope"}, "td": {"colspan", "rowspan"}}


class Sanitizer(HTMLParser):
    def __init__(self, link_map):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.stack, self.link_map = [], 0, [], link_map

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.skip:
            if tag not in VOID:
                self.skip += 1
            return
        if tag in DROP_WITH_CONTENT or DROP_CLASS.search(a.get("class") or "") or \
                (a.get("id") or "").startswith(("jp-", "sharing", "like-post")):
            if tag not in VOID:
                self.skip = 1
            return
        if tag == "h1":
            tag = "h2"
        if tag not in ALLOWED:
            self.stack.append(None)  # unwrap (div, span, section ...)
            return
        keep = []
        for k in sorted(ATTRS.get(tag, ())):
            v = a.get(k)
            if not v:
                continue
            if k in ("href", "src"):
                v = v.strip()
                if v.startswith("//"):
                    v = "https:" + v
                if k == "href" and norm_url(v) in self.link_map:
                    v = self.link_map[norm_url(v)]
                elif not re.match(r"^(https?:|mailto:|/)", v):
                    continue
            keep.append('%s="%s"' % (k, html.escape(v, quote=True)))
        if tag == "a":
            href = a.get("href") or ""
            if not any(x.startswith("href=") for x in keep):
                self.stack.append(None)
                return
            if not any(x.startswith('href="/') for x in keep):
                keep += ['target="_blank"', 'rel="noopener"']
        if tag == "img":
            if not any(x.startswith("src=") for x in keep):
                return
            keep.append('loading="lazy"')
        self.out.append("<%s%s>" % (tag, "".join(" " + x for x in keep)))
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and not self.skip and self.stack:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.skip:
            self.skip -= 1
            return
        if not self.stack:
            return
        t = self.stack.pop()
        if t:
            self.out.append("</%s>" % t)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


def sanitize(content, link_map):
    s = Sanitizer(link_map)
    s.feed(content or "")
    s.close()
    body = "".join(s.out)
    body = re.sub(r"<p>\s*</p>", "", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def description(p):
    paras = [plain(x) for x in re.findall(r"<p[^>]*>(.*?)</p>", p.get("content") or "", re.S)]
    for t in paras:
        if len(t) > 60 and not re.match(r"(Source note|Sources checked|Sources:)", t, re.I):
            break
    else:
        t = plain(p.get("excerpt"))
    t = t.replace("[…]", "").replace("[&hellip;]", "").strip()
    if len(t) > 200:
        t = t[:197].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return t


def fmt_date(iso):
    d = datetime.fromisoformat(iso)
    return d.strftime("%b %-d, %Y")


def esc(s):
    return html.escape(s, quote=True)


def jsonld(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1).replace("</", "<\\/")


# ---------------------------------------------------------------- templates
CSS = """
  :root {
    color-scheme: light dark;
    --bg: #f6f2ec;
    --surface: #fffdfa;
    --surface-2: #f2ece4;
    --border: #e6ddd2;
    --border-strong: #d4c7b8;
    --text: #1a1512;
    --text-2: #463c35;
    --text-muted: #7b6f65;
    --accent: #c47b4a;
    --accent-ink: #9c5729;
    --accent-soft: rgba(196, 123, 74, 0.12);
    --ink-band: #1a1512;
    --ink-band-text: #d9cfc4;
    --green: #2f7a5c;
    --red: #b5402f;
    --amber: #a87708;
    --blue: #3c6c8c;
    --purple: #6f5283;
    --serif: "Newsreader", "Iowan Old Style", Georgia, "Times New Roman", serif;
    --sans: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --shadow: 0 1px 2px rgba(26, 21, 18, 0.04), 0 8px 24px -12px rgba(26, 21, 18, 0.10);
    --radius: 12px;
    --wrap: 1240px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12100e;
      --surface: #1a1714;
      --surface-2: #221e1a;
      --border: #2f2924;
      --border-strong: #433a33;
      --text: #f3ede6;
      --text-2: #d3c9bf;
      --text-muted: #9a8e84;
      --accent: #d38d5c;
      --accent-ink: #e5a677;
      --accent-soft: rgba(211, 141, 92, 0.14);
      --ink-band: #0b0a09;
      --green: #5fb08e;
      --red: #e0705e;
      --amber: #d9a93a;
      --blue: #7aa8c7;
      --purple: #a98bbd;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 8px 24px -12px rgba(0, 0, 0, 0.5);
    }
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    line-height: 1.55;
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    font-feature-settings: "cv11", "ss01";
    letter-spacing: -0.006em;
  }
  a { color: inherit; text-decoration: none; }
  button { font-family: inherit; cursor: pointer; border: none; background: none; color: inherit; }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 4px; }
  ::selection { background: var(--accent-soft); }

  /* ---------- header ---------- */
  header {
    position: sticky;
    top: 0;
    z-index: 100;
    background: color-mix(in srgb, var(--bg) 88%, transparent);
    -webkit-backdrop-filter: saturate(160%) blur(14px);
    backdrop-filter: saturate(160%) blur(14px);
    border-bottom: 1px solid var(--border);
  }
  .header-top {
    max-width: var(--wrap);
    margin: 0 auto;
    height: 64px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
    padding: 0 28px;
  }
  .logo, a.logo {
    display: inline-flex;
    align-items: center;
    white-space: nowrap;
    font-weight: 700;
    font-size: 19px;
    letter-spacing: -0.04em;
    color: var(--text);
  }
  .logo::before {
    content: "";
    width: 26px;
    height: 26px;
    border-radius: 7px;
    background: #1a1512 url("/favicon.svg") center / cover no-repeat;
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.06);
    flex: none;
    margin-right: 10px;
  }
  .logo span { color: var(--accent); font-weight: 700; }
  nav {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 4px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface);
    font-size: 13.5px;
    font-weight: 500;
    color: var(--text-muted);
  }
  nav a {
    padding: 6px 14px;
    border-radius: 999px;
    transition: color 0.15s, background-color 0.15s;
    white-space: nowrap;
  }
  nav a:hover { color: var(--text); background: var(--surface-2); }
  nav a.active { color: var(--surface); background: var(--text); font-weight: 600; }
  .header-cta {
    background: var(--text);
    color: var(--surface);
    padding: 9px 16px;
    border-radius: 999px;
    font-size: 13px;
    font-weight: 600;
    white-space: nowrap;
    transition: background-color 0.15s, transform 0.15s;
  }
  .header-cta::before {
    content: "";
    display: inline-block;
    vertical-align: 1px;
    margin-right: 8px;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 30%, transparent);
  }
  .header-cta:hover { background: var(--accent-ink); }

  /* ---------- footer ---------- */
  footer {
    background: var(--ink-band);
    color: var(--ink-band-text);
    padding: 40px 28px;
    margin-top: 72px;
  }
  .footer-inner {
    max-width: var(--wrap);
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px 32px;
    flex-wrap: wrap;
    font-size: 13px;
  }
  .footer-inner > div:first-child { font-family: var(--serif); font-size: 18px; color: #fffdf9; letter-spacing: -0.01em; }
  .footer-links { display: flex; gap: 24px; }
  .footer-links a { color: var(--ink-band-text); border-bottom: 1px solid transparent; transition: color 0.15s, border-color 0.15s; }
  .footer-links a:hover { color: #fffdf9; border-color: var(--accent); }

  /* ---------- section titles ---------- */
  .section-title {
    font-size: 11.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--text-muted);
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .section-title::before {
    content: "";
    width: 8px;
    height: 8px;
    border-radius: 2px;
    background: var(--accent);
    flex: none;
  }
  .section-title::after { content: ""; flex: 1; height: 1px; background: var(--border); }

  .tag {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--accent-soft);
    color: var(--accent-ink);
    padding: 3px 9px 3px 8px;
    border-radius: 999px;
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 0.02em;
    line-height: 1.3;
  }
  .tag::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .tag.high { background: color-mix(in srgb, var(--red) 12%, transparent); color: var(--red); }
  .tag.mid { background: color-mix(in srgb, var(--amber) 14%, transparent); color: var(--amber); }
  .tag.green { background: color-mix(in srgb, var(--green) 12%, transparent); color: var(--green); }
  .tag.blue { background: color-mix(in srgb, var(--blue) 12%, transparent); color: var(--blue); }

  @media (max-width: 900px) {
    .header-top { flex-wrap: wrap; height: auto; padding: 10px 16px 0; gap: 10px 16px; }
    nav {
      order: 3;
      width: calc(100% + 32px);
      margin: 0 -16px;
      border: none;
      border-top: 1px solid var(--border);
      border-radius: 0;
      background: transparent;
      padding: 8px 12px;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
    }
    nav::-webkit-scrollbar { display: none; }
    nav a { padding: 6px 12px; }
    .header-cta { padding: 7px 13px; font-size: 12.5px; }
    footer { padding: 32px 20px; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: 0.001ms !important; animation-iteration-count: 1 !important; transition-duration: 0.001ms !important; }
  }

  main { max-width: var(--wrap); margin: 0 auto; padding: 48px 28px 32px; }
  .back {
    display: inline-flex; align-items: center; gap: 8px;
    font-size: 13px; font-weight: 500; color: var(--text-muted);
    transition: color 0.15s;
  }
  .back:hover { color: var(--accent-ink); }

  /* ---------- article page ---------- */
  .article-wrap { max-width: 720px; margin: 0 auto; }
  .article-head { padding: 28px 0 32px; margin-bottom: 40px; border-bottom: 1px solid var(--border); }
  .meta { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; font-size: 13px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
  .meta .dot { width: 3px; height: 3px; border-radius: 50%; background: var(--border-strong); }
  .article-head .meta { margin-bottom: 18px; }
  h1.title {
    font-family: var(--serif);
    font-size: clamp(32px, 5vw, 50px);
    line-height: 1.08;
    letter-spacing: -0.025em;
    font-weight: 500;
    color: var(--text);
    text-wrap: balance;
    font-optical-sizing: auto;
  }
  .prose {
    font-family: var(--serif);
    font-size: 20px;
    line-height: 1.7;
    color: var(--text-2);
    max-width: 68ch;
    font-optical-sizing: auto;
    hanging-punctuation: first;
  }
  .prose > * + * { margin-top: 1.1em; }
  .prose strong, .prose b { font-weight: 600; color: var(--text); }
  .prose h2 {
    font-family: var(--serif); font-size: 30px; line-height: 1.2; letter-spacing: -0.02em;
    font-weight: 500; color: var(--text); margin-top: 2.1em; scroll-margin-top: 96px;
  }
  .prose h3 {
    font-family: var(--sans); font-size: 18px; line-height: 1.4; font-weight: 600;
    letter-spacing: -0.01em; color: var(--text); margin-top: 1.9em;
  }
  .prose h4, .prose h5, .prose h6 {
    font-family: var(--sans); font-size: 13px; font-weight: 700; letter-spacing: 0.12em;
    text-transform: uppercase; color: var(--text-muted); margin-top: 1.8em;
  }
  .prose h2 + *, .prose h3 + *, .prose h4 + * { margin-top: 0.6em; }
  .prose a {
    color: var(--accent-ink); text-decoration: underline; text-underline-offset: 3px;
    text-decoration-thickness: 1px; text-decoration-color: color-mix(in srgb, var(--accent) 45%, transparent);
    overflow-wrap: anywhere; transition: text-decoration-color 0.15s;
  }
  .prose a:hover { text-decoration-color: var(--accent); }
  .prose ul, .prose ol { padding-left: 1.3em; }
  .prose li { padding-left: 0.2em; }
  .prose li + li { margin-top: 0.45em; }
  .prose li::marker { color: var(--accent); }
  .prose blockquote {
    position: relative; margin-left: 0; padding: 4px 0 4px 26px;
    border-left: 2px solid var(--accent); font-style: italic; color: var(--text);
    font-size: 1.08em; line-height: 1.55;
  }
  .prose hr { border: none; height: 1px; background: var(--border); margin: 2.4em 0; }
  .prose table {
    width: 100%; border-collapse: collapse; font-family: var(--sans); font-size: 14.5px; line-height: 1.5;
    display: block; overflow-x: auto; font-variant-numeric: tabular-nums;
    border: 1px solid var(--border); border-radius: 10px; background: var(--surface);
  }
  .prose th, .prose td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
  .prose tr:last-child td { border-bottom: none; }
  .prose th { background: var(--surface-2); font-weight: 600; color: var(--text); font-size: 12.5px; letter-spacing: 0.02em; white-space: nowrap; }
  .prose td { color: var(--text-2); }
  .prose tbody tr:nth-child(even) td { background: color-mix(in srgb, var(--surface-2) 45%, transparent); }
  .prose img { max-width: 100%; height: auto; border-radius: 10px; }
  .prose figure { margin: 1.6em 0; }
  .prose figcaption, .prose caption { font-family: var(--sans); font-size: 13px; color: var(--text-muted); margin-top: 8px; }
  .prose code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--surface-2); padding: 1px 6px; border-radius: 4px; font-size: 0.82em; }
  .prose pre { background: var(--surface-2); padding: 14px 16px; border-radius: 8px; overflow-x: auto; font-size: 14px; }
  .prose sup { font-size: 0.7em; }

  .origin {
    margin: 64px auto 0; max-width: 720px; padding: 20px 0 0; border-top: 1px solid var(--border);
    font-size: 13px; color: var(--text-muted); display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  }
  .origin a { color: var(--text-2); }
  .origin span a { text-decoration: underline; text-underline-offset: 3px; text-decoration-color: var(--border-strong); }
  .origin a:hover { color: var(--accent-ink); }

  /* ---------- articles index ---------- */
  .page-head { max-width: 920px; margin: 0 auto 40px; padding: 24px 0 32px; border-bottom: 1px solid var(--border); }
  .page-head .section-title { margin-bottom: 18px; }
  .page-head .section-title::before { background: var(--blue); }
  .page-head .section-title::after { display: none; }
  h1.page-title { font-family: var(--serif); font-size: clamp(40px, 6vw, 64px); font-weight: 500; letter-spacing: -0.03em; line-height: 1; }
  .page-lead { color: var(--text-2); font-size: 17px; margin-top: 16px; max-width: 40em; }
  .page-count { margin-top: 12px; font-size: 13px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
  .list { max-width: 920px; margin: 0 auto; }
  .month {
    font-size: 11.5px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; color: var(--text-muted);
    padding: 36px 0 12px; display: flex; align-items: center; gap: 12px;
  }
  .month:first-child { padding-top: 0; }
  .month::after { content: ""; flex: 1; height: 1px; background: var(--border); }
  .item {
    display: grid; grid-template-columns: 150px minmax(0, 1fr); grid-template-areas: "meta title" "meta desc";
    column-gap: 32px; row-gap: 6px; padding: 22px 4px; border-bottom: 1px solid var(--border);
    transition: background-color 0.15s;
  }
  .item:hover h2 { color: var(--accent-ink); }
  .item .meta { grid-area: meta; flex-direction: column; align-items: flex-start; gap: 8px; padding-top: 4px; font-size: 12.5px; }
  .item h2 { grid-area: title; font-family: var(--serif); font-size: 22px; font-weight: 500; line-height: 1.28; letter-spacing: -0.012em; transition: color 0.15s; color: var(--text); }
  .item p { grid-area: desc; font-size: 14.5px; color: var(--text-muted); line-height: 1.6; max-width: 68ch; }

  @media (max-width: 700px) {
    main { padding: 28px 18px 24px; }
    .article-head { padding: 20px 0 24px; margin-bottom: 28px; }
    .prose { font-size: 18.5px; line-height: 1.68; }
    .prose h2 { font-size: 25px; }
    .prose table { font-size: 13.5px; }
    .item { grid-template-columns: 1fr; grid-template-areas: "meta" "title" "desc"; padding: 18px 2px; }
    .item .meta { flex-direction: row; align-items: center; padding-top: 0; }
    .item h2 { font-size: 20px; }
  }

  /* ---------- library search + filter ---------- */
  .lib-tools { max-width: 920px; margin: -12px auto 28px; display: flex; flex-direction: column; gap: 14px; }
  .lib-tools[hidden] { display: none; }
  .lib-search { position: relative; }
  .lib-search input {
    width: 100%; min-height: 46px; padding: 11px 16px 11px 42px; font: inherit; font-size: 16px;
    color: var(--text); background: var(--surface); border: 1px solid var(--border-strong);
    border-radius: 999px; outline: none; -webkit-appearance: none; appearance: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .lib-search input::placeholder { color: var(--text-muted); }
  .lib-search input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .lib-search svg { position: absolute; left: 16px; top: 50%; width: 16px; height: 16px; margin-top: -8px; color: var(--text-muted); pointer-events: none; }
  .lib-pills { display: flex; flex-wrap: wrap; gap: 6px; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px; min-height: 40px; padding: 0 15px;
    border: 1px solid var(--border); border-radius: 999px; background: var(--surface);
    font-size: 13px; font-weight: 600; color: var(--text-2); transition: background-color 0.15s, color 0.15s, border-color 0.15s;
  }
  .pill:hover { border-color: var(--border-strong); color: var(--text); }
  .pill[aria-pressed="true"],
  .pill[aria-current="page"] { background: var(--text); color: var(--surface); border-color: var(--text); }
  a.pill { text-decoration: none; color: inherit; }
  a.pill:hover { color: var(--text); }
  .pill .n { font-weight: 500; font-size: 11.5px; opacity: 0.65; font-variant-numeric: tabular-nums; }
  .lib-empty { max-width: 920px; margin: 8px auto 0; padding: 28px 4px; color: var(--text-muted); font-size: 15px; border-bottom: 1px solid var(--border); }
  .item[hidden], .month[hidden], .lib-empty[hidden] { display: none !important; }
  .cat-label {
    font-size: 10.5px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: var(--text-muted);
  }
  .meta a.cat-label:hover { color: var(--accent-ink); }

  /* ---------- related articles ---------- */
  .related { max-width: 720px; margin: 64px auto 0; }
  .related .section-title { margin-bottom: 8px; }
  .rel-item { display: block; padding: 18px 2px; border-bottom: 1px solid var(--border); }
  .rel-item .meta { font-size: 12px; margin-bottom: 6px; }
  .rel-item h3 { font-family: var(--serif); font-size: 20px; font-weight: 500; line-height: 1.3; letter-spacing: -0.01em; color: var(--text); transition: color 0.15s; }
  .rel-item:hover h3 { color: var(--accent-ink); }
  .related + .origin { margin-top: 40px; }

  /* ---------- subscribe ---------- */
  .subscribe { max-width: 720px; margin: 56px auto 0; }
  .subscribe-inner {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow); padding: 28px 32px; display: flex; flex-wrap: wrap;
    align-items: center; justify-content: space-between; gap: 18px 28px;
  }
  .subscribe h2 {
    font-family: var(--serif); font-size: 26px; font-weight: 500;
    letter-spacing: -0.02em; line-height: 1.2; margin-bottom: 8px; color: var(--text);
  }
  .subscribe p { color: var(--text-2); font-size: 15.5px; max-width: 36em; line-height: 1.5; }
  .btn-subscribe {
    display: inline-flex; align-items: center; justify-content: center; min-height: 44px;
    padding: 0 22px; border-radius: 999px; background: var(--accent); color: #fffdf9;
    font-weight: 600; font-size: 15px; white-space: nowrap;
    box-shadow: 0 6px 20px -8px rgba(156, 87, 41, 0.6);
    transition: background-color 0.2s, transform 0.2s;
  }
  .btn-subscribe:hover { background: var(--accent-ink); transform: translateY(-1px); }
  .subscribe-page .btn-subscribe { margin-top: 28px; }

  /* ---------- touch targets (phones/tablets) ---------- */
  @media (max-width: 900px) {
    nav a { display: inline-flex; align-items: center; min-height: 40px; }
    nav {
      -webkit-mask-image: linear-gradient(90deg, #000 85%, transparent);
      mask-image: linear-gradient(90deg, #000 85%, transparent);
    }
    .header-cta { display: inline-flex; align-items: center; min-height: 40px; }
    .logo, a.logo { min-height: 40px; }
    .footer-links a, .back, .origin a, .btn-subscribe { display: inline-flex; align-items: center; min-height: 40px; }
    .subscribe-inner { padding: 24px 20px; }
    .subscribe h2 { font-size: 23px; }
    .btn-subscribe { min-height: 44px; }
    .origin { align-items: center; }
    .meta a.cat-label { display: inline-block; padding: 12px 0; margin: -12px 0; }
  }
"""

HEADER = """<header>
  <div class="header-top">
    <a class="logo" href="/">RY<span>ZM</span></a>
    <nav>
      <a href="/#feed">Feed</a>
      <a href="/#breaking">Breaking</a>
      <a href="/articles/"{act_articles}>Articles</a>
      <a href="/articles/frameworks/">Frameworks</a>
      <a href="/notes/"{act_notes}>Notes</a>
      <a href="/#calendar">Calendar</a>
    </nav>
    <a class="header-cta" href="/subscribe/">Subscribe</a>
  </div>
</header>"""

FOOTER = """<footer>
  <div class="footer-inner">
    <div>RYZM · frameworks, not forecasts</div>
    <div class="footer-links">
      <a href="/about/">About</a>
      <a href="/subscribe/">Subscribe</a>
    </div>
  </div>
</footer>"""

SUBSCRIBE = """<aside class="subscribe" aria-label="Email subscription">
  <div class="subscribe-inner">
    <div>
      <h2>Get the weekly RYZM brief</h2>
      <p>New frameworks and the week’s key changes, by email. Free.</p>
    </div>
    <a class="btn-subscribe" href="/subscribe/">Subscribe</a>
  </div>
</aside>"""


def korea_based(text):
    """Keep About wording Korea-based until the WordPress source is updated."""
    return (text or "").replace("Seoul-based", "Korea-based").replace("Seoul based", "Korea-based")


def rewrite_subscribe_hrefs(text):
    """Point in-site copy at /subscribe/; the WordPress URL stays only on /subscribe/ itself."""
    text = (text or "").replace(WP_SUBSCRIBE, "/subscribe/")
    text = text.replace('<a href="/subscribe/" target="_blank" rel="noopener">', '<a href="/subscribe/">')
    return text


def rewrite_cat_query_hrefs(text):
    """Point leftover /articles/?cat=Name links at the static /articles/<slug>/ pages."""
    def repl(m):
        slug = cat_slug(html.unescape(m.group(1)))
        if not slug or slug == "all":
            return 'href="/articles/"'
        return 'href="%s"' % cat_href(slug)
    return re.sub(r'href="/articles/\?cat=([^"&#]+)"', repl, text or "")


def update_home_chrome(path=None):
    """Keep the hand-written home page in sync: PNG share card, icons, and category URLs."""
    path = path or os.path.join(ROOT, "index.html")
    with open(path, encoding="utf-8") as f:
        s = f.read()
    s2 = s
    s2 = re.sub(
        r'(property="og:image" content="https://ryzm\.app)/og-image\.svg"',
        r'\1/og-image.png"', s2)
    s2 = re.sub(
        r'(name="twitter:image" content="https://ryzm\.app)/og-image\.svg"',
        r'\1/og-image.png"', s2)
    if 'href="/favicon-32.png"' not in s2 and 'href="/favicon.svg"' in s2:
        s2 = s2.replace(
            '<link rel="icon" type="image/svg+xml" href="/favicon.svg">',
            '<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
            '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">\n'
            '<link rel="apple-touch-icon" href="/apple-touch-icon.png">',
            1)
    elif 'href="/favicon.svg"' not in s2:
        s2 = s2.replace('<link rel="alternate" type="text/markdown"',
                        FAVICON_LINKS + '\n<link rel="alternate" type="text/markdown"', 1)
    s2 = rewrite_cat_query_hrefs(s2)
    s2 = s2.replace("/articles/?cat=frameworks", "/articles/frameworks/")
    s2 = s2.replace("RYZM · Frameworks for Markets", "RYZM · Frameworks for markets")
    if 'name="twitter:title"' not in s2:
        s2 = s2.replace(
            '<meta name="twitter:image" content="https://ryzm.app/og-image.png">',
            '<meta name="twitter:image" content="https://ryzm.app/og-image.png">\n'
            '<meta name="twitter:title" content="RYZM · Frameworks for markets">',
            1)
    old_h1 = ('  <h1>RYZM maps how US rates, dollar funding and FX move into Korean markets.'
              '<br><em>Frameworks, not forecasts.</em></h1>')
    new_h1 = ('  <h1>Frameworks for markets<br><em>Not forecasts</em></h1>\n'
              '  <p class="intro-lead">RYZM maps how US rates, dollar funding and FX move into Korean markets. Frameworks, not forecasts.</p>')
    s2 = s2.replace(old_h1, new_h1)
    if s2 != s:
        with open(path, encoding="utf-8") as f_now:
            if f_now.read() == s:
                return write_index_html(path, s2)
    return False


def page(title, desc, canonical, og_type, extra_meta, ld, body, active="articles",
         og_image=None):
    if og_image:
        og_tags = (
            '<meta property="og:image" content="%s">\n'
            '<meta property="og:image:width" content="1200">\n'
            '<meta property="og:image:height" content="630">\n'
            '<meta name="twitter:image" content="%s">\n' % (og_image, og_image)
        )
    else:
        og_tags = OG_TAGS
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{canonical}">
<meta name="robots" content="index, follow, max-snippet:-1">
<meta property="og:type" content="{og_type}">
<meta property="og:site_name" content="RYZM">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{canonical}">
{og_tags}{extra_meta}<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
{favicons}
<meta name="theme-color" content="#f6f2ec" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#12100e" media="(prefers-color-scheme: dark)">
<link rel="alternate" type="text/markdown" title="llms.txt" href="/llms.txt">
<script type="application/ld+json">
{ld}
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400&display=swap" rel="stylesheet">
<style>{css}
  /* ---------- design pass 2 (2026-11-24) ---------- */
  .b-time, .cal-time, .card-meta .time, .ticker-inner {{ font-family: var(--mono); letter-spacing: 0; }}
  .prose {{ letter-spacing: 0; }}
  @media (max-width: 900px) {{
    nav {{
      -webkit-mask-image: linear-gradient(90deg, #000 80%, transparent);
      mask-image: linear-gradient(90deg, #000 80%, transparent);
    }}
  }}
</style>
</head>
<body>

{header}

{body}

{footer}
</body>
</html>
""".format(title=esc(title), desc=esc(desc), canonical=canonical, og_type=og_type,
           extra_meta=extra_meta, favicons=FAVICON_LINKS, og_tags=og_tags, ld=ld, css=CSS,
           header=HEADER.format(act_articles=' class="active"' if active == "articles" else "",
                                act_notes=' class="active"' if active == "notes" else ""),
           body=body, footer=FOOTER)


ORG = {"@type": "Organization", "@id": SITE + "/#org", "name": "RYZM", "url": SITE + "/"}


def fix_wp_link_text(body):
    """In-site links whose visible text is still the old WordPress URL show the ryzm.app URL instead."""
    return re.sub(r'(<a href="(/[^"]*)">)https?://%s/[^<]*(</a>)' % re.escape(WP_SITE),
                  lambda m: m.group(1) + SITE + m.group(2) + m.group(3), body)


def render_related(rel):
    if not rel:
        return ""
    rows = []
    for b in rel:
        rows.append("""    <a class="rel-item" href="{path}">
      <div class="meta"><span class="cat-label">{cat}</span><span class="dot"></span><time datetime="{iso}">{date}</time></div>
      <h3>{title}</h3>
    </a>""".format(path=b["path"], cat=esc(b["category"]), iso=b["date"], date=fmt_date(b["date"]),
                   title=esc(b["title"])))
    return """
  <aside class="related" aria-label="Related articles">
    <div class="section-title">Related articles</div>
%s
  </aside>""" % "\n".join(rows)


def render_article(a, back_href="/articles/", back_short="All articles", back_long="Back to Articles",
                   active="articles", related=None):
    url = SITE + a["path"]
    wp_url = a.get("wp_url") or ""
    from_wp = bool(wp_url) and WP_SITE in wp_url
    ld_obj = {
        "@context": "https://schema.org",
        "@type": "Article",
        "@id": url + "#article",
        "headline": a["title"][:110],
        "description": a["desc"],
        "url": url,
        "mainEntityOfPage": url,
        "datePublished": a["date"],
        "dateModified": a["modified"],
        "inLanguage": "en-US",
        "articleSection": a["type"],
        "keywords": a["tags"],
        "author": ORG,
        "publisher": ORG,
    }
    if from_wp:
        ld_obj["isBasedOn"] = wp_url
    og_image = None
    if a.get("image"):
        img = a["image"]
        og_image = img if img.startswith("http") else (SITE + img)
        ld_obj["image"] = og_image
    ld = jsonld(ld_obj)
    extra = ('<meta property="article:published_time" content="%s">\n'
             '<meta property="article:modified_time" content="%s">\n' % (a["date"], a["modified"]))
    words = len(plain(a["html"]).split())
    origin_span = ""
    if from_wp:
        origin_span = (
            '\n    <span>Originally published on <a href="{wp}" rel="noopener" target="_blank">'
            "ryzm9.wordpress.com</a></span>".format(wp=esc(wp_url))
        )
    body = """<main>
  <div class="article-wrap">
  <a class="back" href="{back_href}">&larr; {back_short}</a>
  <article>
    <div class="article-head">
      <div class="meta"><span class="tag">{type}</span>{cat}<time datetime="{date}">{date_h}</time><span class="dot"></span><span>{mins} min read</span></div>
      <h1 class="title">{title}</h1>
    </div>
    <div class="prose">
{content}
    </div>
  </article>
  </div>{related}
  {subscribe}
  <div class="origin">
    <a href="{back_href}">&larr; {back_long}</a>{origin_span}
  </div>
</main>""".format(date=a["date"], date_h=fmt_date(a["date"]), type=esc(a["type"]),
                  mins=max(1, round(words / 230)), back_href=back_href, back_short=back_short,
                  back_long=back_long, origin_span=origin_span,
                  title=esc(a["title"]), content=fix_wp_link_text(a["html"]),
                  cat=('<a class="cat-label" href="%s">%s</a>' % (cat_href(a["category"]), esc(a["category"]))
                       if a.get("category") else ""),
                  related=render_related(related), subscribe=SUBSCRIBE)
    return page("%s · RYZM" % a["title"], a["desc"], url, "article", extra, ld, body, active,
                og_image=og_image)


def render_brief(a, related=None):
    return render_article(a, back_href="/notes/", back_short="All daily briefs",
                          back_long="Back to Daily Briefs", active="notes", related=related)


def brief_related(briefs, a, n=3):
    others = [b for b in briefs if b["slug"] != a["slug"]]
    return [{"path": b["path"], "category": b.get("type") or "Daily Brief",
             "date": b["date"], "title": b["title"]} for b in others[:n]]


def short_brief_title(title):
    """List-card title without the series prefix and trailing date already shown in the meta row."""
    t = re.sub(r"^RYZM (Daily|Weekend) Brief\s*\|\s*", "", title or "")
    t = re.sub(r"\s*\|\s*(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}$", "", t)
    return t or title


def render_notes_index(briefs):
    url = SITE + "/notes/"
    desc = "RYZM Daily Briefs: dated, sourced morning notes on rates, funding, FX, and Korea."
    rows, month = [], None
    for a in briefs:
        m = datetime.fromisoformat(a["date"]).strftime("%B %Y")
        if m != month:
            rows.append('    <div class="month">%s</div>' % m)
            month = m
        rows.append("""    <a class="item" href="{path}">
      <div class="meta"><time datetime="{iso}">{date}</time><span class="tag">{type}</span></div>
      <h2>{title}</h2>
      <p>{desc}</p>
    </a>""".format(path=a["path"], iso=a["date"], date=fmt_date(a["date"]), type=esc(a["type"]),
                   title=esc(short_brief_title(a["title"])), desc=esc(a["desc"])))
    ld = jsonld({
        "@context": "https://schema.org", "@type": "CollectionPage", "@id": url, "url": url,
        "name": "RYZM Daily Briefs", "description": desc, "inLanguage": "en-US",
        "isPartOf": {"@id": SITE + "/#website"}, "publisher": ORG,
        "dateModified": max(a["modified"] for a in briefs) if briefs else None,
        "mainEntity": {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "url": SITE + a["path"], "name": a["title"]}
            for i, a in enumerate(briefs)]},
    })
    body = """<main>
  <div class="page-head">
    <div class="section-title">Notes</div>
    <h1 class="page-title">Daily Briefs</h1>
    <div class="page-count">%d briefs</div>
  </div>
  <div class="list">
%s
  </div>
</main>""" % (len(briefs), "\n".join(rows))
    return page("Daily Briefs · RYZM", desc, url, "website", "", ld, body, "notes")


def render_about(pg, url_map):
    url = SITE + "/about/"
    title = korea_based(plain(pg["title"]))
    content = pg["content"] if pg.get("ready") else sanitize(pg["content"], url_map)
    content = re.sub(r"^\s*<h2>\s*%s\s*</h2>\s*" % re.escape(esc(title)), "", content)
    content = rewrite_subscribe_hrefs(korea_based(content))
    desc = korea_based(description({"content": content, "excerpt": pg.get("excerpt") or pg.get("desc") or ""}))
    ld = jsonld({
        "@context": "https://schema.org", "@type": "AboutPage", "@id": url, "url": url,
        "name": title, "description": desc, "inLanguage": "en-US",
        "datePublished": pg["date"], "dateModified": pg["modified"],
        "isPartOf": {"@id": SITE + "/#website"}, "about": ORG, "publisher": ORG,
        "isBasedOn": pg["URL"],
    })
    body = """<main>
  <div class="article-wrap">
  <article>
    <div class="article-head">
      <div class="meta"><span class="tag">About</span></div>
      <h1 class="title">{title}</h1>
    </div>
    <div class="prose">
{content}
    </div>
  </article>
  </div>
{subscribe}
</main>""".format(title=esc(title), content=content, subscribe=SUBSCRIBE)
    return page("%s · RYZM" % title, desc, url, "website", "", ld, body, "")


# Client-side search + category filter for /articles/. Vanilla JS, no libraries.
# Without JS the tools stay hidden and every article is listed.
LIBRARY_JS = r"""(function(){
  var tools=document.getElementById('lib-tools'),q=document.getElementById('lib-q');
  if(!tools||!q||!document.querySelectorAll)return;
  var list=document.getElementById('lib-list'),empty=document.getElementById('lib-empty'),count=document.getElementById('lib-count');
  var kids=[].slice.call(list.children);
  var total=list.querySelectorAll('.item').length;
  var params={};(location.search||'').replace(/^\?/,'').split('&').forEach(function(kv){if(!kv)return;var i=kv.indexOf('=');var k=i<0?kv:kv.slice(0,i);try{params[k]=decodeURIComponent((i<0?'':kv.slice(i+1)).replace(/\+/g,' '));}catch(e){}});
  if(params.cat){
    var want=String(params.cat).toLowerCase();
    var dest=(want==='all'||!want)?'/articles/':'/articles/'+want+'/';
    var here=location.pathname.replace(/\/?$/,'/');
    if(here!==dest){location.replace(dest+(params.q?('?q='+encodeURIComponent(params.q)):''));return;}
  }
  function norm(v){return (v||'').toLowerCase().replace(/\u2019/g,"'").replace(/\s+/g,' ').trim();}
  function apply(push){
    var words=norm(q.value).split(' ').filter(Boolean),shown=0,month=null,monthShown=0;
    function closeMonth(){if(month)month.hidden=monthShown===0;}
    kids.forEach(function(el){
      if(el.classList.contains('month')){closeMonth();month=el;monthShown=0;return;}
      var hay=el.getAttribute('data-search')||'';
      var ok=words.every(function(w){return hay.indexOf(w)!==-1;});
      el.hidden=!ok;if(ok){shown++;monthShown++;}
    });
    closeMonth();
    empty.hidden=shown!==0;
    count.textContent=(shown===total?total+' articles':shown+' of '+total+' articles');
    if(push&&window.history&&history.replaceState){
      var ps=[];if(q.value.trim())ps.push('q='+encodeURIComponent(q.value.trim()));
      history.replaceState(null,'',location.pathname+(ps.length?'?'+ps.join('&'):''));
    }
  }
  if(params.q)q.value=params.q;
  q.addEventListener('input',function(){apply(true);});
  tools.hidden=false;
  apply(false);
})();"""


def render_pills(all_arts, current="All"):
    """Category pills as real links. Counts come from the full library."""
    counts = {c: sum(1 for a in all_arts if c in a["categories"]) for c in CATEGORY_ORDER}

    def pill(label, href, n, active):
        pressed = "true" if active else "false"
        current_attr = ' aria-current="page"' if active else ""
        return ('<a class="pill" href="%s" data-cat="%s"%s>%s <span class="n">%d</span></a>'
                % (href, esc(label), current_attr, esc(label), n))

    pills = [pill("All", "/articles/", len(all_arts), current == "All")]
    pills += [pill(c, cat_href(c), counts[c], current == c)
              for c in CATEGORY_ORDER if counts[c]]
    return pills


def render_index(arts, category=None, all_arts=None):
    """Library index, or a static /articles/<category>/ page listing only that category."""
    all_arts = all_arts if all_arts is not None else arts
    listed = [a for a in arts if (not category or category in a.get("categories", []))]
    if category:
        url = SITE + cat_href(category)
        name = "%s · RYZM" % category
        desc = CATEGORY_DESCRIPTIONS.get(
            category, "RYZM articles in %s." % category)
        title_h = category
        lead = {
            "Frameworks": "Reading frameworks, not forecasts. Full text on RYZM.",
            "Rates": "US rates, Treasuries, and the curve. Full text on RYZM.",
            "Funding": "Dollar funding, SOFR, reserves, and liquidity plumbing. Full text on RYZM.",
            "FX": "The dollar, the won, and FX hedging. Full text on RYZM.",
            "Korea": "Korean markets and the path from US rates and funding. Full text on RYZM.",
            "Credit": "Credit, spreads, and related market structure. Full text on RYZM.",
        }.get(category, "Research summaries, framework notes, and market closes. Full text on RYZM.")
        ld_name = "RYZM %s" % category
    else:
        url = SITE + "/articles/"
        name = "Articles · RYZM"
        desc = ("RYZM articles: research summaries, framework notes, and Korea and US market closes "
                "on rates, funding, FX, and Korea.")
        title_h = "Articles"
        lead = "Research summaries, framework notes, and market closes. Full text on RYZM."
        ld_name = "RYZM Articles"
    rows, month = [], None
    for a in listed:
        m = datetime.fromisoformat(a["date"]).strftime("%B %Y")
        if m != month:
            rows.append('    <div class="month">%s</div>' % m)
            month = m
        search = " ".join([a["title"], a["desc"], a["type"]] + a["categories"]).lower()
        search = re.sub(r"\s+", " ", search.replace("\u2019", "'"))
        rows.append("""    <a class="item" href="{path}" data-cat="{cats}" data-search="{search}">
      <div class="meta"><time datetime="{iso}">{date}</time><span class="cat-label">{cat}</span><span class="tag">{type}</span></div>
      <h2>{title}</h2>
      <p>{desc}</p>
    </a>""".format(path=a["path"], iso=a["date"], date=fmt_date(a["date"]), type=esc(a["type"]),
                   cats=esc(" ".join(a["categories"])), search=esc(search), cat=esc(a["category"]),
                   title=esc(a["title"]), desc=esc(a["desc"])))
    items = "\n".join(rows)
    pills = render_pills(all_arts, current=category or "All")
    ld = jsonld({
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "@id": url,
        "url": url,
        "name": ld_name,
        "description": desc,
        "inLanguage": "en-US",
        "isPartOf": {"@id": SITE + "/#website"},
        "publisher": ORG,
        "dateModified": max(a["modified"] for a in listed) if listed else None,
        "mainEntity": {
            "@type": "ItemList",
            "numberOfItems": len(listed),
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "url": SITE + a["path"], "name": a["title"]}
                for i, a in enumerate(listed)],
        },
    })
    body = """<main>
  <div class="page-head">
    <div class="section-title">Library</div>
    <h1 class="page-title">%s</h1>
    <p class="page-lead">%s</p>
    <div class="page-count" id="lib-count" aria-live="polite">%d articles</div>
  </div>
  <div class="lib-tools" id="lib-tools" hidden>
    <div class="lib-search" role="search">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.5-3.5"></path></svg>
      <input type="search" id="lib-q" placeholder="Search titles and summaries" aria-label="Search articles" autocomplete="off" spellcheck="false">
    </div>
    <div class="lib-pills" role="group" aria-label="Filter by category">
      %s
    </div>
  </div>
  <div class="list" id="lib-list">
%s
  </div>
  <p class="lib-empty" id="lib-empty" hidden>No articles match. Try another word or category.</p>
</main>
<script>
%s
</script>""" % (esc(title_h), esc(lead), len(listed), "\n      ".join(pills), items, LIBRARY_JS)
    return page(name, desc, url, "website", "", ld, body)


# ---------------------------------------------------------------- site files
def patch_links(text, url_map, is_html):
    """Point links to WordPress posts that have in-site pages at /articles/<slug>/."""
    def wp_to_site(m):
        u = m.group(0)
        p = url_map.get(norm_url(u))
        return SITE + p if p else u
    if is_html:
        def fix_a(m):
            tag = m.group(0)
            hm = re.search(r'href="([^"]+)"', tag)
            if not hm:
                return tag
            p = url_map.get(norm_url(hm.group(1)))
            if not p:
                return tag
            tag = tag.replace(hm.group(0), 'href="%s"' % p)
            tag = re.sub(r'\s+target="_blank"', "", tag)
            tag = re.sub(r'\s+rel="noopener"', "", tag)
            return tag
        text = re.sub(r"<a\s[^>]*>", fix_a, text)
    # remaining absolute URLs (JSON-LD, markdown)
    return re.sub(r"https?://%s/[^\s\"')<>\]]+" % re.escape(WP_SITE), wp_to_site, text)


KST_TIME = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{1,2}):(\d{2}) KST")


def _kst_key(block, now):
    """Sort key from the first 'Mon D, HH:MM KST' in a card. Year is inferred (cards are recent)."""
    m = KST_TIME.search(block)
    if not m:
        return datetime.min
    mon = datetime.strptime(m.group(1), "%b").month
    year = now.year - 1 if mon > now.month + 1 else now.year
    return datetime(year, mon, int(m.group(2)), int(m.group(3)), int(m.group(4)))


# Gemini / Apps Script owns these two live regions of the home page.
# This generator must never rewrite them.
BREAKING_LIST_RE = re.compile(r'<ul class="breaking-list">.*?</ul>', re.S)
FEED_DIV_RE = re.compile(r'<div class="feed">.*?\n      </div>', re.S)


def snapshot_live_index_sections(html_text):
    """Return the exact ul.breaking-list and div.feed strings, or None if absent."""
    br = BREAKING_LIST_RE.search(html_text or "")
    fe = FEED_DIV_RE.search(html_text or "")
    return (br.group(0) if br else None, fe.group(0) if fe else None)


def restore_live_index_sections(html_text, snap):
    """Put saved Breaking and Feed blocks back, leaving every other byte as in html_text."""
    breaking, feed = snap
    if breaking is not None:
        html_text, n = BREAKING_LIST_RE.subn(lambda _m: breaking, html_text, count=1)
        if n != 1:
            raise SystemExit("index.html is missing ul.breaking-list; refusing to write")
    if feed is not None:
        html_text, n = FEED_DIV_RE.subn(lambda _m: feed, html_text, count=1)
        if n != 1:
            raise SystemExit("index.html is missing div.feed; refusing to write")
    return html_text


def write_index_html(path, new_html):
    """Write index.html but keep ul.breaking-list and div.feed byte-for-byte from disk."""
    with open(path, encoding="utf-8") as f:
        current = f.read()
    new_html = restore_live_index_sections(new_html, snapshot_live_index_sections(current))
    if new_html != current:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_html)
        return True
    return False


def sort_home_cards(path=None):
    """No-op. Breaking (ul.breaking-list) and Feed (div.feed) are committed by
    Gemini via Apps Script. This generator must not edit those sections."""
    return False


def update_index_html(url_map):
    path = os.path.join(ROOT, "index.html")
    with open(path, encoding="utf-8") as f:
        s = f.read()
    s2 = patch_links(s, url_map, True)
    s2 = s2.replace('<a href="https://ryzm9.wordpress.com/about-2/" target="_blank" rel="noopener">About</a>',
                    '<a href="/about/">About</a>')
    s2 = rewrite_cat_query_hrefs(s2)
    if s2 != s:
        write_index_html(path, s2)
    update_home_chrome(path)


def update_llms(arts, url_map):
    path = os.path.join(ROOT, "llms.txt")
    with open(path, encoding="utf-8") as f:
        s = f.read()
    s = patch_links(s, url_map, False)
    for line, after in (("- About: %s/about/" % SITE, "- Articles (full text on this site): "),
                        ("- Daily briefs (full text on this site): %s/notes/" % SITE, "- About: ")):
        if line not in s:
            s = re.sub(r"^(%s.*)$" % re.escape(after), lambda m: m.group(1) + "\n" + line, s, count=1, flags=re.M)
    lines = ["## Articles", "",
             "Full text on ryzm.app. Index: %s/articles/" % SITE, ""]
    for a in arts:
        lines.append("- [%s](%s%s) (%s, %s): %s" % (a["title"], SITE, a["path"],
                                                    fmt_date(a["date"]), a["type"], a["desc"]))
    block = "\n".join(lines) + "\n\n"
    if re.search(r"^## Articles\b", s, re.M):
        s = re.sub(r"^## Articles\b.*?(?=^## |\Z)", lambda m: block, s, count=1, flags=re.M | re.S)
    else:
        s = s.rstrip("\n") + "\n\n" + block
    with open(path, "w", encoding="utf-8") as f:
        f.write(s.rstrip("\n") + "\n")


def write_section(out_dir, manifest, items, render_one, render_list, with_related=False):
    """Write <out_dir>/<slug>/index.html for each item plus <out_dir>/index.html; prune removed slugs."""
    os.makedirs(out_dir, exist_ok=True)
    old = []
    if os.path.exists(manifest):
        with open(manifest, encoding="utf-8") as f:
            old = json.load(f).get("slugs", [])
    new = [a["slug"] for a in items]
    for slug in set(old) - set(new):
        d = os.path.join(out_dir, slug)
        if os.path.isdir(d):
            shutil.rmtree(d)
    for a in items:
        d = os.path.join(out_dir, a["slug"])
        os.makedirs(d, exist_ok=True)
        html_out = render_one(a, related=brief_related(items, a)) if with_related else render_one(a)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(html_out)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_list(items))
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump({"source": API, "slugs": new}, f, indent=1)
        f.write("\n")


def write_sitemap(arts, briefs=(), about_lastmod=None):
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    newest = max([a["modified"][:10] for a in arts] + [today])
    urls = [(SITE + "/", today, "daily"), (SITE + "/articles/", newest, "daily")]
    for c in CATEGORY_ORDER:
        if any(c in a.get("categories", []) for a in arts):
            cat_mod = max(a["modified"][:10] for a in arts if c in a.get("categories", []))
            urls.append((SITE + cat_href(c), cat_mod, "weekly"))
    urls += [(SITE + "/llms.txt", today, None), (SITE + "/subscribe/", today, None)]
    if about_lastmod:
        urls.append((SITE + "/about/", about_lastmod, None))
    if briefs:
        urls.append((SITE + "/notes/", max(b["modified"][:10] for b in briefs), "daily"))
    urls += [(SITE + a["path"], a["modified"][:10], None) for a in arts]
    urls += [(SITE + b["path"], b["modified"][:10], None) for b in briefs]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lm, cf in urls:
        out.append("  <url>\n    <loc>%s</loc>\n    <lastmod>%s</lastmod>%s\n  </url>" % (
            loc, lm, "\n    <changefreq>%s</changefreq>" % cf if cf else ""))
    out.append("</urlset>")
    with open(os.path.join(ROOT, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def load_existing(slug, base=OUT):
    """Read the data back out of an already-built article page (for --retemplate)."""
    with open(os.path.join(base, slug, "index.html"), encoding="utf-8") as f:
        s = f.read()
    ld = json.loads(re.search(r'<script type="application/ld\+json">\n(.*?)\n</script>', s, re.S).group(1))
    title = html.unescape(re.search(r'<h1 class="title">(.*?)</h1>', s, re.S).group(1))
    start = s.index('<div class="prose">\n') + len('<div class="prose">\n')
    end = s.index('\n    </div>\n  </article>', start)
    image = None
    img_m = re.search(r'<meta property="og:image" content="([^"]+)"', s)
    if img_m:
        img_url = img_m.group(1)
        if img_url.rstrip("/") != SITE + "/og-image.png":
            image = img_url[len(SITE):] if img_url.startswith(SITE) else img_url
    out = {
        "slug": slug, "path": "/articles/%s/" % slug, "title": title,
        "date": ld["datePublished"], "modified": ld["dateModified"],
        "wp_url": ld.get("isBasedOn") or "",
        "type": ld["articleSection"], "desc": ld["description"], "tags": ld.get("keywords") or [],
        "html": s[start:end],
    }
    if image:
        out["image"] = image
    return out


def write_category_pages(arts):
    """Write /articles/<cat>/index.html for each category with articles; prune empty ones."""
    article_slugs = {a["slug"] for a in arts}
    collisions = sorted(article_slugs & set(CATEGORY_SLUGS))
    if collisions:
        raise SystemExit("Category slug collides with an article slug: %s" % ", ".join(collisions))
    for slug, name in CATEGORY_SLUGS.items():
        items = [a for a in arts if name in a.get("categories", [])]
        d = os.path.join(OUT, slug)
        if not items:
            if os.path.isdir(d):
                shutil.rmtree(d)
            continue
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_index(items, category=name, all_arts=arts))


def write_manifest(arts):
    """articles/manifest.json: "slugs" (kept for older tools) plus per-article "items" with categories."""
    items = []
    for a in arts:
        item = {"slug": a["slug"], "path": a["path"], "title": a["title"], "date": a["date"],
                "type": a["type"], "category": a["category"], "categories": a["categories"],
                "wp_categories": a.get("wp_cats") or []}
        if a.get("source") == "local":
            item["source"] = "local"
        items.append(item)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump({"source": API, "slugs": [a["slug"] for a in arts], "items": items},
                  f, indent=1, ensure_ascii=False)
        f.write("\n")



def featured_articles(arts, n=3):
    """Newest articles, preferring Frameworks or Korea so the home panel stays on-brand."""
    arts = sorted(arts, key=lambda a: a["date"], reverse=True)
    pref, rest = [], []
    for a in arts:
        cats = set(a.get("categories") or [])
        (pref if cats & {"Frameworks", "Korea"} else rest).append(a)
    return (pref + rest)[:n]


def write_rss(arts, briefs):
    """Write /rss.xml and an identical /feed.xml with the 30 newest articles and briefs."""
    items = list(arts) + list(briefs)
    items.sort(key=lambda a: a["date"], reverse=True)
    items = items[:30]

    def pub(iso):
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return email.utils.format_datetime(d)

    def cdata(text):
        return "<![CDATA[%s]]>" % (text or "").replace("]]>", "]]]]><![CDATA[>")

    last = max((a["date"] for a in items), default=datetime.now().isoformat())
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>RYZM</title>",
        "<link>%s/</link>" % SITE,
        "<description>%s</description>" % esc(
            "RYZM maps how US rates, dollar funding and FX move into Korean markets. Frameworks, not forecasts."),
        "<language>en-us</language>",
        "<lastBuildDate>%s</lastBuildDate>" % pub(last),
    ]
    for a in items:
        link = SITE + a["path"]
        out += [
            "<item>",
            "<title>%s</title>" % esc(a["title"]),
            "<link>%s</link>" % link,
            "<guid isPermaLink=\"true\">%s</guid>" % link,
            "<pubDate>%s</pubDate>" % pub(a["date"]),
            "<description>%s</description>" % cdata(a.get("desc") or ""),
            "</item>",
        ]
    out += ["</channel>", "</rss>", ""]
    xml = "\n".join(out)
    for name in ("rss.xml", "feed.xml"):
        with open(os.path.join(ROOT, name), "w", encoding="utf-8") as f:
            f.write(xml)


def write_subscribe():
    """In-site subscribe page; the email form itself stays on WordPress."""
    url = SITE + "/subscribe/"
    title = "Subscribe · RYZM"
    desc = "Get the weekly RYZM brief by email. New frameworks and the week's key changes. Free."
    ld = jsonld({
        "@context": "https://schema.org", "@type": "WebPage", "@id": url, "url": url,
        "name": "Subscribe to RYZM", "description": desc, "inLanguage": "en-US",
        "isPartOf": {"@id": SITE + "/#website"}, "publisher": ORG,
    })
    body = """<main>
  <div class="page-head subscribe-page">
    <div class="section-title">Subscribe</div>
    <h1 class="page-title">Get the weekly RYZM brief</h1>
    <p class="page-lead">New frameworks and the week’s key changes, by email. Free.</p>
    <p class="page-lead">The sign-up form is hosted on WordPress. RYZM does not collect email addresses on this site.</p>
    <a class="btn-subscribe" href="{wp}" target="_blank" rel="noopener">Subscribe by email</a>
  </div>
</main>""".format(wp=WP_SUBSCRIBE)
    out_dir = os.path.join(ROOT, "subscribe")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page(title, desc, url, "website", "", ld, body, ""))


def update_home_panels(arts, briefs, path=None):
    """Refresh Latest Brief and Featured Research on the home page. Safe to run every build."""
    path = path or os.path.join(ROOT, "index.html")
    with open(path, encoding="utf-8") as f:
        s = f.read()
    if not briefs and not arts:
        return False
    if briefs:
        b = sorted(briefs, key=lambda x: x["date"], reverse=True)[0]
        brief_html = """    <!-- home-latest-brief -->
    <article class="home-panel" aria-labelledby="latest-brief-title">
      <div class="section-title" id="latest-brief-title">Latest Brief</div>
      <a class="panel-feature" href="{path}">
        <div class="meta"><time datetime="{iso}">{date}</time></div>
        <h2>{title}</h2>
        <p>{desc}</p>
      </a>
      <a class="panel-more" href="/notes/">All briefs</a>
    </article>
    <!-- /home-latest-brief -->""".format(
            path=b["path"], iso=b["date"], date=fmt_date(b["date"]),
            title=esc(b["title"]), desc=esc(b.get("desc") or ""))
        s2, n = re.subn(r"    <!-- home-latest-brief -->.*?    <!-- /home-latest-brief -->",
                        brief_html, s, count=1, flags=re.S)
        if n:
            s = s2
    fw_all = sorted([a for a in arts if "Frameworks" in (a.get("categories") or [])],
                    key=lambda a: a["date"], reverse=True)
    fw_top_path = fw_all[0]["path"] if fw_all else None
    if arts:
        rows = []
        # The newest framework already headlines the Explore Frameworks panel; do not repeat it here.
        for a in featured_articles([x for x in arts if x["path"] != fw_top_path], 3):
            rows.append("""      <a class="feat-item" href="{path}">
        <div class="meta"><time datetime="{iso}">{date}</time><span class="cat-label">{cat}</span></div>
        <h3>{title}</h3>
        <p>{desc}</p>
      </a>""".format(path=a["path"], iso=a["date"], date=fmt_date(a["date"]),
                     cat=esc(a.get("category") or a.get("type") or ""),
                     title=esc(a["title"]), desc=esc(a.get("desc") or "")))
        feat_html = """    <!-- home-featured-research -->
    <section class="home-panel" aria-labelledby="featured-research-title">
      <div class="section-title" id="featured-research-title">Featured Research</div>
%s
      <a class="panel-more" href="/articles/">All articles</a>
    </section>
    <!-- /home-featured-research -->""" % "\n".join(rows)
        s2, n = re.subn(r"    <!-- home-featured-research -->.*?    <!-- /home-featured-research -->",
                        feat_html, s, count=1, flags=re.S)
        if n:
            s = s2
    fw = [a for a in arts if "Frameworks" in (a.get("categories") or [])]
    fw.sort(key=lambda a: a["date"], reverse=True)
    if fw:
        a = fw[0]
        fw_html = """    <!-- home-featured-framework -->
      <a class="panel-feature" href="{path}">
        <div class="meta"><time datetime="{iso}">{date}</time><span class="cat-label">Frameworks</span></div>
        <h2>{title}</h2>
        <p>{desc}</p>
      </a>
    <!-- /home-featured-framework -->""".format(
            path=a["path"], iso=a["date"], date=fmt_date(a["date"]),
            title=esc(a["title"]), desc=esc(a.get("desc") or ""))
        s2, n = re.subn(r"    <!-- home-featured-framework -->.*?    <!-- /home-featured-framework -->",
                        fw_html, s, count=1, flags=re.S)
        if n:
            s = s2
    if s != open(path, encoding="utf-8").read():
        return write_index_html(path, s)
    return False


# ---------------------------------------------------------------- local closes
# Daily Korea/US Close posts are written on the box by X Ryzm, not WordPress.
# Ingest a folder with --local-close / --local-closes-dir; persist under
# sources/closes/ so later WordPress-driven runs keep the pages.

FOLDER_RE = re.compile(r"^(korea|us)-close-(\d{8})$")
META_LINE_RE = re.compile(
    r"^\*\*(Meta description|Suggested categories|Suggested tags):\*\*", re.I)
FORBIDDEN_LABEL = re.compile(r"SavePick|세이브픽|@Goeun_6121", re.I)
META_ALIASES = {
    "title": "title",
    "slug": "slug",
    "description": "description",
    "meta description": "description",
    "meta_description": "description",
    "metadescription": "description",
    "tags": "tags",
    "date": "date",
    "time": "time",
    "modified": "modified",
    "kind": "kind",
    "type": "type",
}


def parse_cli(argv):
    retemplate = False
    folders, scan_dirs = [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--retemplate":
            retemplate = True
            i += 1
        elif a == "--local-close":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise SystemExit("Usage: --local-close <folder>")
            folders.append(argv[i + 1])
            i += 2
        elif a == "--local-closes-dir":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise SystemExit("Usage: --local-closes-dir <dir>")
            scan_dirs.append(argv[i + 1])
            i += 2
        else:
            raise SystemExit("Unknown argument: %s" % a)
    for d in scan_dirs:
        folders.extend(find_close_folders(d))
    return retemplate, folders


def find_close_folders(root):
    if not os.path.isdir(root):
        raise SystemExit("Not a directory: %s" % root)
    found = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p) and FOLDER_RE.match(name) and os.path.isfile(os.path.join(p, "post.md")):
            found.append(p)
    return found


def parse_meta_txt(text):
    """Parse key: value lines. Keys are case-insensitive; aliases are accepted."""
    out = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = META_ALIASES.get(k.strip().lower())
        if key:
            out[key] = v.strip()
    return out


def default_close_slug(kind, session):
    prefix = "korea-close" if kind == "korea-close" else "us-close"
    return "%s-%s-%d" % (prefix, session.strftime("%B").lower(), session.year)


def parse_close_datetime(meta, session):
    """Return a timezone-aware KST datetime. Does not invent a clock time.

    Prefer meta 'date' as ISO-8601 (with offset, or a bare YYYY-MM-DD plus
    optional 'time' in HH:MM or HH:MM:SS, always KST). If no date is given,
    use the folder session date at 00:00 KST.
    """
    raw = (meta or {}).get("date") or ""
    raw = raw.strip()
    if raw:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            d = datetime.fromisoformat(raw).date()
            t = ((meta or {}).get("time") or "00:00:00").replace("KST", "").strip()
            parts = [int(x) for x in t.split(":")]
            while len(parts) < 3:
                parts.append(0)
            return datetime(d.year, d.month, d.day, parts[0], parts[1], parts[2], tzinfo=KST)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt
    return datetime(session.year, session.month, session.day, 0, 0, tzinfo=KST)


def close_body_markdown(text):
    """Drop the leading H1 and **Meta description** / tags lines from post.md."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith("# ") or META_LINE_RE.match(s):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip("\n")


def safe_href(url):
    url = (url or "").strip()
    if not url or url.startswith("#") or url.lower().startswith("javascript:"):
        return None
    low = url.lower()
    if "x.com/" in low or "twitter.com/" in low or "goeun_6121" in low:
        return None
    return url


def inline_md(s):
    s = html.escape(s, quote=True)

    def link_sub(m):
        href = safe_href(html.unescape(m.group(2)))
        text = m.group(1)
        if not href:
            return text
        return '<a href="%s">%s</a>' % (html.escape(href, quote=True), text)

    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_sub, s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def md_to_html(md):
    """Small Markdown subset: headings, paragraphs, lists, links, bold, italic."""
    md = close_body_markdown(md)
    blocks, para, list_items, list_type = [], [], [], None

    def flush_para():
        if para:
            blocks.append("<p>%s</p>" % inline_md(" ".join(para)))
            para.clear()

    def flush_list():
        nonlocal list_type
        if list_items:
            blocks.append("<%s>\n%s\n</%s>" % (
                list_type,
                "\n".join("  <li>%s</li>" % inline_md(x) for x in list_items),
                list_type))
            list_items.clear()
            list_type = None

    ul_re = re.compile(r"^[-*+] (.+)$")
    ol_re = re.compile(r"^\d+\. (.+)$")
    for line in md.split("\n"):
        s = line.strip()
        if not s:
            flush_para()
            flush_list()
            continue
        if s.startswith("## "):
            flush_para()
            flush_list()
            blocks.append("<h2>%s</h2>" % inline_md(s[3:].strip()))
            continue
        if s.startswith("### "):
            flush_para()
            flush_list()
            blocks.append("<h3>%s</h3>" % inline_md(s[4:].strip()))
            continue
        if s.startswith("# "):
            flush_para()
            flush_list()
            continue
        m = ul_re.match(s)
        if m:
            flush_para()
            if list_type not in (None, "ul"):
                flush_list()
            list_type = "ul"
            list_items.append(m.group(1))
            continue
        m = ol_re.match(s)
        if m:
            flush_para()
            if list_type not in (None, "ol"):
                flush_list()
            list_type = "ol"
            list_items.append(m.group(1))
            continue
        flush_list()
        para.append(s)
    flush_para()
    flush_list()
    return "\n\n".join(blocks)


def assert_clean_html(html_body, slug):
    if FORBIDDEN_LABEL.search(html_body or ""):
        raise SystemExit("Refusing to publish %s: forbidden label (SavePick / @Goeun_6121)" % slug)
    for href in re.findall(r'href="([^"]*)"', html_body or ""):
        if href == "#" or href.startswith("#"):
            raise SystemExit("Refusing to publish %s: href=\"#\"" % slug)
        if re.search(r"(^|//)(x\.com|twitter\.com)/", href, re.I):
            raise SystemExit("Refusing to publish %s: x.com or twitter.com link" % slug)


def encode_featured_png(png_path, slug):
    data = open(png_path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("%s: featured.png is not a PNG" % png_path)
    w, h = struct.unpack(">II", data[16:24])
    if (w, h) != (1200, 630):
        print("warning: %s is %dx%d (expected 1200x630)" % (png_path, w, h))
    os.makedirs(ASSET_ARTICLES, exist_ok=True)
    rel = "assets/articles/%s.png.b64" % slug
    dest = os.path.join(ROOT, rel)
    wrapped = "\n".join(textwrap.wrap(base64.b64encode(data).decode("ascii"), 76)) + "\n"
    with open(dest, "w", encoding="ascii") as f:
        f.write(wrapped)
    return rel, "/assets/articles/%s.png" % slug


def write_local_record(article, markdown=None):
    os.makedirs(SOURCES, exist_ok=True)
    rec = {
        "slug": article["slug"],
        "title": article["title"],
        "date": article["date"],
        "modified": article["modified"],
        "type": article["type"],
        "desc": article["desc"],
        "tags": article["tags"],
        "wp_cats": article["wp_cats"],
        "kind": article.get("kind"),
        "source": "local",
        "image": article.get("image"),
        "image_b64": article.get("image_b64"),
        "html": article["html"],
    }
    if markdown is not None:
        rec["markdown"] = markdown
    path = os.path.join(SOURCES, article["slug"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1, ensure_ascii=False)
        f.write("\n")
    return path


def article_from_local_record(raw):
    a = {
        "slug": raw["slug"],
        "path": "/articles/%s/" % raw["slug"],
        "title": raw["title"],
        "date": raw["date"],
        "modified": raw.get("modified") or raw["date"],
        "wp_url": "",
        "type": raw.get("type") or "Market Close",
        "desc": raw.get("desc") or "",
        "tags": raw.get("tags") or [],
        "wp_cats": raw.get("wp_cats") or [],
        "html": raw["html"],
        "source": "local",
    }
    if raw.get("image"):
        a["image"] = raw["image"]
    if raw.get("kind"):
        a["kind"] = raw["kind"]
    apply_categories(a)
    return a


def load_local_closes():
    arts = []
    if not os.path.isdir(SOURCES):
        return arts
    for name in sorted(os.listdir(SOURCES)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(SOURCES, name), encoding="utf-8") as f:
            arts.append(article_from_local_record(json.load(f)))
    return arts


def merge_articles(wp_arts, local_arts):
    """Local Close posts win on slug collision; WordPress runs cannot drop them."""
    local_by = {a["slug"]: a for a in local_arts}
    out, seen = [], set()
    for a in wp_arts:
        if a["slug"] in local_by:
            print("note: local close %s overrides the WordPress post with the same slug" % a["slug"])
            continue
        out.append(a)
        seen.add(a["slug"])
    for a in local_arts:
        if a["slug"] not in seen:
            out.append(a)
            seen.add(a["slug"])
    out.sort(key=lambda a: a["date"], reverse=True)
    return out


def url_map_from_existing():
    """Map known WordPress and in-site URLs so Close bodies can reuse existing pages."""
    m = {}

    def add_from_manifest(manifest, fallback):
        if not os.path.exists(manifest):
            return
        with open(manifest, encoding="utf-8") as f:
            man = json.load(f)
        for it in man.get("items") or []:
            path = it.get("path") or (fallback % it["slug"])
            m[norm_url(SITE + path)] = path
        base = OUT if fallback.startswith("/articles/") else NOTES_OUT
        for sl in man.get("slugs") or []:
            try:
                ex = load_existing(sl, base)
            except (OSError, KeyError, ValueError, AttributeError, json.JSONDecodeError):
                continue
            if ex.get("wp_url"):
                m[norm_url(ex["wp_url"])] = ex["path"]
            m[norm_url(SITE + ex["path"])] = ex["path"]

    add_from_manifest(MANIFEST, "/articles/%s/")
    add_from_manifest(NOTES_MANIFEST, "/notes/%s/")
    return m


def ingest_close_folder(folder):
    """Read one X Ryzm Close folder, persist source + b64 image, return an article dict."""
    folder = os.path.abspath(folder)
    name = os.path.basename(folder.rstrip(os.sep))
    m = FOLDER_RE.match(name)
    if not m:
        raise SystemExit(
            "Close folder must be named korea-close-YYYYMMDD or us-close-YYYYMMDD: %s" % name)
    kind = "%s-close" % m.group(1)
    session = datetime.strptime(m.group(2), "%Y%m%d").date()
    post_md = os.path.join(folder, "post.md")
    featured = os.path.join(folder, "featured.png")
    if not os.path.isfile(post_md):
        raise SystemExit("Missing post.md in %s" % folder)
    if not os.path.isfile(featured):
        raise SystemExit("Missing featured.png in %s" % folder)
    markdown = open(post_md, encoding="utf-8").read()
    meta = {}
    meta_path = os.path.join(folder, "meta.txt")
    if os.path.isfile(meta_path):
        meta = parse_meta_txt(open(meta_path, encoding="utf-8").read())
    if not meta.get("title"):
        hm = re.search(r"^#\s+(.+)$", markdown, re.M)
        if hm:
            meta["title"] = hm.group(1).strip()
        else:
            meta["title"] = "%s Close | %s" % (
                "Korea" if kind == "korea-close" else "US",
                session.strftime("%B %-d, %Y"))
            print("warning: no title in meta.txt or post.md; using %s" % meta["title"])
    if not meta.get("description"):
        dm = re.search(r"^\*\*Meta description:\*\*\s*(.+)$", markdown, re.M | re.I)
        if dm:
            meta["description"] = dm.group(1).strip()
    if not meta.get("tags"):
        tm = re.search(r"^\*\*Suggested tags:\*\*\s*(.+)$", markdown, re.M | re.I)
        if tm:
            meta["tags"] = tm.group(1).strip()
    title = meta["title"]
    slug = meta.get("slug") or default_close_slug(kind, session)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise SystemExit("Invalid slug %r" % slug)
    if slug in CATEGORY_SLUGS:
        raise SystemExit("Slug collides with a category: %s" % slug)
    desc = meta.get("description") or ""
    if len(desc) > 200:
        desc = desc[:197].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    tags = meta.get("tags") or ""
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    dt = parse_close_datetime(meta, session)
    date_iso = dt.isoformat()
    modified = date_iso
    if meta.get("modified"):
        modified = parse_close_datetime({"date": meta["modified"]}, session).isoformat()
    html_body = md_to_html(markdown)
    if not desc:
        desc = description({"content": html_body, "excerpt": ""})
    html_body = sanitize(html_body, url_map_from_existing())
    assert_clean_html(html_body, slug)
    b64_rel, image_path = encode_featured_png(featured, slug)
    article = {
        "slug": slug,
        "path": "/articles/%s/" % slug,
        "title": title,
        "date": date_iso,
        "modified": modified,
        "wp_url": "",
        "type": "Market Close",
        "desc": desc,
        "tags": tags,
        "wp_cats": ["Korea Close"] if kind == "korea-close" else ["US Close"],
        "kind": kind,
        "html": html_body,
        "source": "local",
        "image_b64": b64_rel,
        "image": image_path,
    }
    apply_categories(article)
    write_local_record(article, markdown=close_body_markdown(markdown))
    ko = os.path.join(folder, "post_ko.md")
    if os.path.isfile(ko):
        shutil.copy2(ko, os.path.join(SOURCES, slug + ".ko.md"))
    return article


def retemplate():
    """Re-render existing article pages with the current template, without fetching WordPress."""
    with open(MANIFEST, encoding="utf-8") as f:
        man = json.load(f)
    slugs = man.get("slugs", [])
    known = {it["slug"]: it for it in man.get("items", [])}
    local_arts = load_local_closes()
    local_slugs = {a["slug"] for a in local_arts}
    arts = []
    for sl in slugs:
        if sl in local_slugs:
            continue
        page_path = os.path.join(OUT, sl, "index.html")
        if not os.path.isfile(page_path):
            print("warning: skipping missing article page %s" % sl)
            continue
        a = load_existing(sl)
        a["wp_cats"] = known.get(a["slug"], {}).get("wp_categories") or []
        apply_categories(a)
        arts.append(a)
    arts = merge_articles(arts, local_arts)
    for a in arts:
        d = os.path.join(OUT, a["slug"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_article(a, related=related_for(a, arts)))
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(arts))
    write_category_pages(arts)
    write_manifest(arts)
    update_home_chrome()
    briefs = []
    if os.path.exists(NOTES_MANIFEST):
        with open(NOTES_MANIFEST, encoding="utf-8") as f:
            nslugs = json.load(f).get("slugs", [])
        briefs = [load_existing(sl, NOTES_OUT) for sl in nslugs]
        for b in briefs:
            b["path"] = "/notes/%s/" % b["slug"]
        write_section(NOTES_OUT, NOTES_MANIFEST, briefs, render_brief, render_notes_index, with_related=True)
    about_path = os.path.join(ABOUT_OUT, "index.html")
    if os.path.exists(about_path):
        with open(about_path, encoding="utf-8") as f:
            about_html = f.read()
        ld_m = re.search(r'<script type="application/ld\+json">\n(.*?)\n</script>', about_html, re.S)
        title_m = re.search(r'<h1 class="title">(.*?)</h1>', about_html, re.S)
        start = about_html.index('<div class="prose">\n') + len('<div class="prose">\n')
        end = about_html.index('\n    </div>\n  </article>', start)
        ld = json.loads(ld_m.group(1)) if ld_m else {}
        about = {
            "title": html.unescape(title_m.group(1)) if title_m else "About RYZM",
            "content": about_html[start:end],
            "ready": True,
            "date": ld.get("datePublished", ""),
            "modified": ld.get("dateModified", ""),
            "URL": ld.get("isBasedOn") or ld.get("url") or (SITE + "/about/"),
        }
        with open(about_path, "w", encoding="utf-8") as f:
            f.write(render_about(about, {}))
        about_lastmod = about.get("modified", "")[:10]
    else:
        about_lastmod = None
    write_subscribe()
    write_rss(arts, briefs)
    update_llms(arts, url_map_from_existing())
    update_home_panels(arts, briefs)
    write_sitemap(arts, briefs, about_lastmod)
    print("Re-rendered %d articles and %d daily briefs with the current template" % (len(arts), len(briefs)))


def main():
    retemplate_flag, close_folders = parse_cli(sys.argv[1:])
    for folder in close_folders:
        a = ingest_close_folder(folder)
        print("Ingested local close %s -> %s (%s / %s)" % (
            folder, a["path"], a["type"], a["category"]))
    if retemplate_flag:
        return retemplate()
    posts = fetch_posts()
    chosen = sorted([p for p in posts if is_article(p)], key=lambda p: p["date"], reverse=True)
    brief_posts = sorted([p for p in posts if is_brief(p)], key=lambda p: p["date"], reverse=True)
    url_map = {norm_url(p["URL"]): "/articles/%s/" % p["slug"] for p in chosen}
    url_map.update({norm_url(p["URL"]): "/notes/%s/" % p["slug"] for p in brief_posts})
    about = fetch_page(ABOUT_SLUG)
    if about.get("URL"):
        url_map[norm_url(about["URL"])] = "/about/"
    arts = []
    for p in chosen:
        arts.append({
            "slug": p["slug"], "path": "/articles/%s/" % p["slug"],
            "title": plain(p["title"]), "date": p["date"], "modified": p["modified"],
            "wp_url": p["URL"], "type": type_label(p), "desc": description(p),
            "tags": [html.unescape(t) for t in (p.get("tags") or {})],
            "wp_cats": sorted(cats_of(p)),
            "html": sanitize(p["content"], url_map),
        })
    for a in arts:
        apply_categories(a)
    arts = merge_articles(arts, load_local_closes())

    os.makedirs(OUT, exist_ok=True)
    old = []
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            old = json.load(f).get("slugs", [])
    new = [a["slug"] for a in arts]
    collisions = sorted(set(new) & set(CATEGORY_SLUGS))
    if collisions:
        raise SystemExit("Category slug collides with an article slug: %s" % ", ".join(collisions))
    local_slugs = {a["slug"] for a in arts if a.get("source") == "local"}
    for slug in set(old) - set(new):
        if slug in CATEGORY_SLUGS or slug in local_slugs:
            continue
        d = os.path.join(OUT, slug)
        if os.path.isdir(d):
            shutil.rmtree(d)
    for a in arts:
        d = os.path.join(OUT, a["slug"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_article(a, related=related_for(a, arts)))
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(arts))
    write_category_pages(arts)
    write_manifest(arts)

    briefs = [{
        "slug": p["slug"], "path": "/notes/%s/" % p["slug"],
        "title": plain(p["title"]), "date": p["date"], "modified": p["modified"],
        "wp_url": p["URL"], "type": "Daily Brief", "desc": description(p),
        "tags": [html.unescape(t) for t in (p.get("tags") or {})],
        "html": sanitize(p["content"], url_map),
    } for p in brief_posts]
    write_section(NOTES_OUT, NOTES_MANIFEST, briefs, render_brief, render_notes_index, with_related=True)
    if about.get("content"):
        os.makedirs(ABOUT_OUT, exist_ok=True)
        with open(os.path.join(ABOUT_OUT, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_about(about, url_map))

    update_index_html(url_map)
    update_home_panels(arts, briefs)
    write_subscribe()
    write_rss(arts, briefs)
    update_llms(arts, url_map)
    write_sitemap(arts, briefs, about.get("modified", "")[:10] if about.get("content") else None)
    print("Built %d articles (from %d posts)" % (len(arts), len(posts)))
    for a in arts:
        print("  %s  %-16s %s" % (a["date"][:10], a["type"], a["path"]))


# ---------------------------------------------------------------- house style
# RYZM never publishes an em dash or en dash. WordPress sources sometimes contain
# them, so every build ends by rewriting the generated files:
#   spaced dash ("a - b" with an em or en dash) and any em dash -> ", "
#   en dash in a range ("8-9", "10:00-16:30", "$2.84-$2.89") -> "-"
DASH_FILES_EXT = (".html", ".xml", ".txt", ".json")
DASH_SKIP_DIRS = {".git", "scripts", "node_modules", ".vercel", "sources", "assets"}


def no_dashes(text):
    text = re.sub(r"[ \t]+[–—][ \t]+", ", ", text)
    text = re.sub(r"\s*—\s*", ", ", text)
    return text.replace("–", "-")


def strip_dashes_everywhere(root=None):
    root = root or ROOT
    changed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DASH_SKIP_DIRS]
        for name in filenames:
            if not name.endswith(DASH_FILES_EXT):
                continue
            path = os.path.join(dirpath, name)
            data = open(path, "rb").read()
            if b"\xe2\x80\x93" not in data and b"\xe2\x80\x94" not in data:
                continue
            text = data.decode("utf-8")
            fixed = no_dashes(text)
            if fixed != text:
                with open(path, "wb") as f:
                    f.write(fixed.encode("utf-8"))
                changed += 1
    return changed


if __name__ == "__main__":
    rc = main()
    n = strip_dashes_everywhere()
    if n:
        print("house style: removed dashes in %d file(s)" % n)
    sys.exit(rc)
