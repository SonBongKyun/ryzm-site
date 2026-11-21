#!/usr/bin/env python3
"""Build in-site article pages for ryzm.app from the RYZM WordPress.com blog.

Usage (from the repo root):  python3 scripts/build_articles.py
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
    --bg: #f7f3ee; --surface: #fffdf9; --surface-2: #f3eee8; --border: #e5ddd4;
    --text: #2c2520; --text-muted: #7a7068; --accent: #c47b4a;
    --accent-soft: rgba(196, 123, 74, 0.12); --blue: #4a7c9b;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg);
    background-image: radial-gradient(ellipse 90% 60% at 10% 10%, rgba(184, 92, 46, 0.08), transparent),
      radial-gradient(ellipse 70% 50% at 90% 80%, rgba(58, 106, 136, 0.06), transparent);
    color: var(--text); line-height: 1.5; min-height: 100vh;
    -webkit-font-smoothing: antialiased; letter-spacing: -0.011em;
  }
  a { color: inherit; text-decoration: none; }
  header { position: sticky; top: 0; z-index: 100; background: rgba(247, 243, 238, 0.96);
    backdrop-filter: blur(20px); border-bottom: 1px solid var(--border); }
  .header-top { max-width: 1200px; margin: 0 auto; min-height: 56px; display: flex; align-items: center;
    justify-content: space-between; padding: 0 20px; gap: 16px; }
  .logo { font-weight: 800; font-size: 20px; letter-spacing: -0.045em; color: #1a1512; }
  .logo span { color: var(--accent); font-weight: 800; }
  nav { display: flex; gap: 28px; font-size: 13px; color: var(--text-muted); font-weight: 500; }
  nav a:hover, nav a.active { color: var(--text); }
  nav a.active { font-weight: 600; box-shadow: inset 0 -2px 0 var(--accent); padding-bottom: 2px; }
  .header-cta { background: var(--accent); color: #fffdf9; padding: 7px 14px; border-radius: 6px;
    font-size: 13px; font-weight: 600; white-space: nowrap; }
  @media (max-width: 900px) {
    .header-top { flex-wrap: wrap; padding: 8px 16px; }
    nav { order: 3; width: 100%; gap: 20px; overflow-x: auto; white-space: nowrap; padding: 4px 0 6px;
      -webkit-overflow-scrolling: touch; scrollbar-width: none; }
    nav::-webkit-scrollbar { display: none; }
  }
  main { max-width: 760px; margin: 0 auto; padding: 40px 20px 72px; }
  .back { display: inline-block; font-size: 13px; color: var(--text-muted); margin-bottom: 24px; }
  .back:hover { color: var(--text); }
  .section-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.1em;
    color: var(--text-muted); margin-bottom: 16px; display: flex; align-items: center; gap: 10px; }
  .section-title::before { content: ''; width: 18px; height: 3px; background: var(--blue); border-radius: 1.5px; }
  .meta { font-size: 12.5px; color: var(--text-muted); margin-bottom: 10px; }
  .tag { background: var(--accent-soft); color: var(--accent); padding: 2px 7px; border-radius: 4px;
    font-weight: 600; font-size: 11px; letter-spacing: 0.02em; margin-left: 4px; }
  h1.title { font-size: 34px; line-height: 1.15; letter-spacing: -0.03em; font-weight: 650; padding-bottom: 22px;
    border-bottom: 1px solid var(--border); margin-bottom: 28px; }
  .prose { font-size: 16.5px; line-height: 1.75; color: var(--text); }
  .prose > * + * { margin-top: 1.05em; }
  .prose h2 { font-size: 22px; line-height: 1.3; letter-spacing: -0.02em; font-weight: 650; margin-top: 2em; }
  .prose h3 { font-size: 18px; line-height: 1.35; font-weight: 650; margin-top: 1.6em; }
  .prose h4, .prose h5, .prose h6 { font-size: 16px; font-weight: 650; margin-top: 1.4em; }
  .prose a { color: var(--accent); text-decoration: underline; text-underline-offset: 2px;
    text-decoration-thickness: 1px; overflow-wrap: anywhere; }
  .prose ul, .prose ol { padding-left: 1.4em; }
  .prose li + li { margin-top: 0.4em; }
  .prose blockquote { border-left: 3px solid var(--accent); padding: 4px 0 4px 16px; color: var(--text-muted); }
  .prose table { width: 100%; border-collapse: collapse; font-size: 14.5px; display: block; overflow-x: auto; }
  .prose th, .prose td { border: 1px solid var(--border); padding: 8px 10px; text-align: left; }
  .prose th { background: var(--surface-2); font-weight: 600; }
  .prose img { max-width: 100%; height: auto; border-radius: 10px; }
  .prose figcaption { font-size: 13px; color: var(--text-muted); margin-top: 6px; }
  .prose code { background: var(--surface-2); padding: 1px 5px; border-radius: 4px; font-size: 0.92em; }
  .origin { margin-top: 48px; padding-top: 18px; border-top: 1px solid var(--border); font-size: 13px;
    color: var(--text-muted); display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .origin a { text-decoration: underline; text-underline-offset: 2px; }
  .list { display: flex; flex-direction: column; gap: 8px; }
  .item { display: block; background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
    padding: 16px 18px; transition: all 0.2s; box-shadow: 0 2px 6px rgba(44, 37, 32, 0.03); }
  .item:hover { background: var(--surface-2); transform: translateY(-2px); box-shadow: 0 6px 16px rgba(44, 37, 32, 0.08); }
  .item .meta { margin-bottom: 4px; font-size: 11.5px; }
  .item h2 { font-size: 16px; font-weight: 600; line-height: 1.4; margin-bottom: 4px; }
  .item p { font-size: 13.5px; color: var(--text-muted); }
  .page-lead { color: var(--text-muted); font-size: 15px; margin: -4px 0 24px; }
  footer { border-top: 1px solid var(--border); padding: 24px 20px; margin-top: 40px; }
  .footer-inner { max-width: 1200px; margin: 0 auto; display: flex; justify-content: space-between;
    font-size: 12px; color: var(--text-muted); gap: 16px; flex-wrap: wrap; }
  .footer-links { display: flex; gap: 18px; }
  .footer-links a:hover { color: var(--text); }
  @media (max-width: 600px) { h1.title { font-size: 27px; } .prose { font-size: 16px; } main { padding-top: 28px; } }
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
<link rel="alternate" type="text/markdown" title="llms.txt" href="/llms.txt">
<script type="application/ld+json">
{ld}
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;650&display=swap" rel="stylesheet">
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
    body = """<main>
  <a class="back" href="/articles/">&larr; Back to Articles</a>
  <article>
    <div class="meta"><time datetime="{date}">{date_h}</time> · {type}</div>
    <h1 class="title">{title}</h1>
    <div class="prose">
{content}
    </div>
  </article>
  <div class="origin">
    <a href="/articles/">&larr; Back to Articles</a>
    <span>Originally published on <a href="{wp}" rel="noopener" target="_blank">ryzm9.wordpress.com</a></span>
  </div>
</main>""".format(date=a["date"], date_h=fmt_date(a["date"]), type=esc(a["type"]),
                  title=esc(a["title"]), content=a["html"], wp=esc(a["wp_url"]))
    return page("%s · RYZM" % a["title"], a["desc"], url, "article", extra, ld, body)


def render_index(arts):
    url = SITE + "/articles/"
    desc = ("RYZM articles: research summaries, framework notes, and Korea and US market closes "
            "on rates, funding, FX, and Korea.")
    items = "\n".join(
        """    <a class="item" href="{path}">
      <div class="meta">{date} · {type}</div>
      <h2>{title}</h2>
      <p>{desc}</p>
    </a>""".format(path=a["path"], date=fmt_date(a["date"]), type=esc(a["type"]),
                   title=esc(a["title"]), desc=esc(a["desc"])) for a in arts)
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
  <div class="section-title">Articles</div>
  <p class="page-lead">Research summaries, framework notes, and market closes. Full text on RYZM.</p>
  <div class="list">
%s
  </div>
</main>""" % items
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


def main():
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
