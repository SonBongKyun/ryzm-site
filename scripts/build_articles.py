#!/usr/bin/env python3
"""Build in-site article pages for ryzm.app from the RYZM WordPress.com blog.

Usage (from the repo root):  python3 scripts/build_articles.py
                             python3 scripts/build_articles.py --retemplate
                             (re-render existing pages with the current template; no fetch)
Python 3 standard library only. Idempotent: safe to run every day.

What it does
  1. Fetches every post from the WordPress.com public API (ryzm9.wordpress.com).
  2. Keeps long-form market articles (research summaries, framework notes,
     Korea/US close, key issues, rates/FX/funding analysis). Skips Daily Briefs,
     Market Wire news notes, off-topic series, and very short posts.
  3. Writes /articles/<slug>/index.html for each article and /articles/index.html.
  4. Rewrites links to those WordPress posts in index.html and llms.txt
     (including the JSON-LD block) so they point to the in-site pages.
  5. Regenerates sitemap.xml and the "## Articles" section of llms.txt.
"""
import html
import json
import os
import re
import shutil
import sys
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

SITE = "https://ryzm.app"
WP_SITE = "ryzm9.wordpress.com"
API = "https://public-api.wordpress.com/rest/v1.1/sites/%s/posts/" % WP_SITE
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "articles")
MANIFEST = os.path.join(OUT, "manifest.json")

INCLUDE_CATS = {"Key Issues", "Framework Note", "Korea Close", "US Close",
                "Rates & Bonds", "FX", "Funding & Liquidity", "Korea Bridge", "Credit"}
EXCLUDE_CATS = {"Daily Brief", "Market Wire", "Young Trends Daily", "Science & Tech Daily",
                "Short Reflections", "Tech Idea", "Everyday Explainers"}
MIN_TEXT_CHARS = 900

FAVICON_LINKS = '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'


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


def cats_of(p):
    return {html.unescape(c) for c in (p.get("categories") or {})}


def plain(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def is_article(p):
    cats = cats_of(p)
    if cats & EXCLUDE_CATS or not cats & INCLUDE_CATS:
        return False
    return len(plain(p.get("content"))) >= MIN_TEXT_CHARS


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
    --sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
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
    font-weight: 800;
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
  .logo span { color: var(--accent); font-weight: 800; }
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
"""

HEADER = """<header>
  <div class="header-top">
    <a class="logo" href="/">RY<span>ZM</span></a>
    <nav>
      <a href="/#feed">Feed</a>
      <a href="/#breaking">Breaking</a>
      <a href="/articles/" class="active">Articles</a>
      <a href="/#frameworks">Frameworks</a>
      <a href="/#notes">Notes</a>
      <a href="/#calendar">Calendar</a>
    </nav>
    <a class="header-cta" href="https://ryzm9.wordpress.com/subscribe/" target="_blank" rel="noopener">Get alerts</a>
  </div>
</header>"""

FOOTER = """<footer>
  <div class="footer-inner">
    <div>RYZM · frameworks, not forecasts</div>
    <div class="footer-links">
      <a href="https://ryzm9.wordpress.com/about-2/" target="_blank" rel="noopener">About</a>
      <a href="https://ryzm9.wordpress.com/subscribe/" target="_blank" rel="noopener">Alerts</a>
    </div>
  </div>
</footer>"""


def page(title, desc, canonical, og_type, extra_meta, ld, body):
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
{extra_meta}<meta name="twitter:card" content="summary">
{favicons}
<meta name="theme-color" content="#f6f2ec" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#12100e" media="(prefers-color-scheme: dark)">
<link rel="alternate" type="text/markdown" title="llms.txt" href="/llms.txt">
<script type="application/ld+json">
{ld}
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>

{header}

{body}

{footer}
</body>
</html>
""".format(title=esc(title), desc=esc(desc), canonical=canonical, og_type=og_type,
           extra_meta=extra_meta, favicons=FAVICON_LINKS, ld=ld, css=CSS,
           header=HEADER, body=body, footer=FOOTER)


ORG = {"@type": "Organization", "@id": SITE + "/#org", "name": "RYZM", "url": SITE + "/"}


def render_article(a):
    url = SITE + a["path"]
    ld = jsonld({
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
        "isBasedOn": a["wp_url"],
    })
    extra = ('<meta property="article:published_time" content="%s">\n'
             '<meta property="article:modified_time" content="%s">\n' % (a["date"], a["modified"]))
    words = len(plain(a["html"]).split())
    body = """<main>
  <div class="article-wrap">
  <a class="back" href="/articles/">&larr; All articles</a>
  <article>
    <div class="article-head">
      <div class="meta"><span class="tag">{type}</span><time datetime="{date}">{date_h}</time><span class="dot"></span><span>{mins} min read</span></div>
      <h1 class="title">{title}</h1>
    </div>
    <div class="prose">
{content}
    </div>
  </article>
  </div>
  <div class="origin">
    <a href="/articles/">&larr; Back to Articles</a>
    <span>Originally published on <a href="{wp}" rel="noopener" target="_blank">ryzm9.wordpress.com</a></span>
  </div>
</main>""".format(date=a["date"], date_h=fmt_date(a["date"]), type=esc(a["type"]),
                  mins=max(1, round(words / 230)),
                  title=esc(a["title"]), content=a["html"], wp=esc(a["wp_url"]))
    return page("%s · RYZM" % a["title"], a["desc"], url, "article", extra, ld, body)


def render_index(arts):
    url = SITE + "/articles/"
    desc = ("RYZM articles: research summaries, framework notes, and Korea and US market closes "
            "on rates, funding, FX, and Korea.")
    rows, month = [], None
    for a in arts:
        m = datetime.fromisoformat(a["date"]).strftime("%B %Y")
        if m != month:
            rows.append('    <div class="month">%s</div>' % m)
            month = m
        rows.append("""    <a class="item" href="{path}">
      <div class="meta"><time datetime="{iso}">{date}</time><span class="tag">{type}</span></div>
      <h2>{title}</h2>
      <p>{desc}</p>
    </a>""".format(path=a["path"], iso=a["date"], date=fmt_date(a["date"]), type=esc(a["type"]),
                   title=esc(a["title"]), desc=esc(a["desc"])))
    items = "\n".join(rows)
    ld = jsonld({
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "@id": url,
        "url": url,
        "name": "RYZM Articles",
        "description": desc,
        "inLanguage": "en-US",
        "isPartOf": {"@id": SITE + "/#website"},
        "publisher": ORG,
        "dateModified": max(a["modified"] for a in arts) if arts else None,
        "mainEntity": {
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "url": SITE + a["path"], "name": a["title"]}
                for i, a in enumerate(arts)],
        },
    })
    body = """<main>
  <div class="page-head">
    <div class="section-title">Library</div>
    <h1 class="page-title">Articles</h1>
    <p class="page-lead">Research summaries, framework notes, and market closes. Full text on RYZM.</p>
    <div class="page-count">%d articles</div>
  </div>
  <div class="list">
%s
  </div>
</main>""" % (len(arts), items)
    return page("Articles · RYZM", desc, url, "website", "", ld, body)


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


def update_index_html(url_map):
    path = os.path.join(ROOT, "index.html")
    with open(path, encoding="utf-8") as f:
        s = f.read()
    s2 = patch_links(s, url_map, True)
    if 'href="/favicon.svg"' not in s2:
        s2 = s2.replace('<link rel="alternate" type="text/markdown"',
                        FAVICON_LINKS + '\n<link rel="alternate" type="text/markdown"', 1)
    if s2 != s:
        with open(path, "w", encoding="utf-8") as f:
            f.write(s2)


def update_llms(arts, url_map):
    path = os.path.join(ROOT, "llms.txt")
    with open(path, encoding="utf-8") as f:
        s = f.read()
    s = patch_links(s, url_map, False)
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


def write_sitemap(arts):
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    newest = max([a["modified"][:10] for a in arts] + [today])
    urls = [(SITE + "/", today, "daily"), (SITE + "/articles/", newest, "daily"),
            (SITE + "/llms.txt", today, None)]
    urls += [(SITE + a["path"], a["modified"][:10], None) for a in arts]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lm, cf in urls:
        out.append("  <url>\n    <loc>%s</loc>\n    <lastmod>%s</lastmod>%s\n  </url>" % (
            loc, lm, "\n    <changefreq>%s</changefreq>" % cf if cf else ""))
    out.append("</urlset>")
    with open(os.path.join(ROOT, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def load_existing(slug):
    """Read the data back out of an already-built article page (for --retemplate)."""
    with open(os.path.join(OUT, slug, "index.html"), encoding="utf-8") as f:
        s = f.read()
    ld = json.loads(re.search(r'<script type="application/ld\+json">\n(.*?)\n</script>', s, re.S).group(1))
    title = html.unescape(re.search(r'<h1 class="title">(.*?)</h1>', s, re.S).group(1))
    start = s.index('<div class="prose">\n') + len('<div class="prose">\n')
    end = s.index('\n    </div>\n  </article>', start)
    return {
        "slug": slug, "path": "/articles/%s/" % slug, "title": title,
        "date": ld["datePublished"], "modified": ld["dateModified"], "wp_url": ld["isBasedOn"],
        "type": ld["articleSection"], "desc": ld["description"], "tags": ld.get("keywords") or [],
        "html": s[start:end],
    }


def retemplate():
    """Re-render existing article pages with the current template, without fetching WordPress."""
    with open(MANIFEST, encoding="utf-8") as f:
        slugs = json.load(f).get("slugs", [])
    arts = [load_existing(sl) for sl in slugs]
    for a in arts:
        with open(os.path.join(OUT, a["slug"], "index.html"), "w", encoding="utf-8") as f:
            f.write(render_article(a))
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(arts))
    print("Re-rendered %d articles with the current template" % len(arts))


def main():
    if "--retemplate" in sys.argv[1:]:
        return retemplate()
    posts = fetch_posts()
    chosen = sorted([p for p in posts if is_article(p)], key=lambda p: p["date"], reverse=True)
    url_map = {norm_url(p["URL"]): "/articles/%s/" % p["slug"] for p in chosen}
    arts = []
    for p in chosen:
        arts.append({
            "slug": p["slug"], "path": "/articles/%s/" % p["slug"],
            "title": plain(p["title"]), "date": p["date"], "modified": p["modified"],
            "wp_url": p["URL"], "type": type_label(p), "desc": description(p),
            "tags": [html.unescape(t) for t in (p.get("tags") or {})],
            "html": sanitize(p["content"], url_map),
        })

    os.makedirs(OUT, exist_ok=True)
    old = []
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            old = json.load(f).get("slugs", [])
    new = [a["slug"] for a in arts]
    for slug in set(old) - set(new):
        d = os.path.join(OUT, slug)
        if os.path.isdir(d):
            shutil.rmtree(d)
    for a in arts:
        d = os.path.join(OUT, a["slug"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_article(a))
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(arts))
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump({"source": API, "slugs": new}, f, indent=1)
        f.write("\n")

    update_index_html(url_map)
    update_llms(arts, url_map)
    write_sitemap(arts)
    print("Built %d articles (from %d posts)" % (len(arts), len(posts)))
    for a in arts:
        print("  %s  %-16s %s" % (a["date"][:10], a["type"], a["path"]))


if __name__ == "__main__":
    sys.exit(main())
