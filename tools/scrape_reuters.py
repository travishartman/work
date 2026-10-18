#!/usr/bin/env python3
"""Refresh the "Recent Work" links in index.html from Travis Hartman's Reuters byline.

Reuters bot-walls the author page (DataDome), so discovery uses Google News RSS
(keyless), resolving each result to its real reuters.com URL, plus a curated
fallback list (data/links.txt). Each candidate is validated and enriched with its
Open Graph share metadata (og:title / og:description), ranked newest-first, and
the top links are written into the Recent Work block. The file is only rewritten
when all checks pass.

Usage: python3 tools/scrape_reuters.py [--limit 4] [--dry-run]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(ROOT, "index.html")
LINKS_PATH = os.path.join(ROOT, "data", "links.txt")

AUTHOR_NAME = "Travis Hartman"
SEARCH_QUERY = f'"{AUTHOR_NAME}" reuters'
GNEWS_SEARCH = "https://news.google.com/rss/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Matches the Recent Work block (NOT the identical Prior Work block).
RECENT_BLOCK_RE = re.compile(
    r'(Recent Work</a>\s*</h1>\s*<div class="text">)(.*?)(</div>)', re.S
)


@dataclass
class Item:
    url: str
    title: str | None = None
    description: str | None = None
    published: datetime | None = None
    sources: set[str] = field(default_factory=set)


def canonical(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/") + "/"


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_date_rfc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def looks_like_article(url: str) -> bool:
    path = urlparse(url).path
    if not path or path == "/":
        return False
    skip = ("/authors/", "/sitemap", "/info-pages/", "/subscribe", "/newsletters",
            "/tag/", "/account/")
    return "reuters.com" in url and not any(path.startswith(s) for s in skip)


def fetch(url: str, retries: int = 3) -> tuple[int, str | None]:
    """GET with browser headers and simple backoff. Returns (status, html|None)."""
    last = 0
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
            last = r.status_code
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.status_code, r.text
            return r.status_code, None
        except requests.RequestException:
            last = 0
        time.sleep(1.5 * (attempt + 1))
    return last, None


def _gnews_resolve(session: requests.Session, gnews_url: str) -> str | None:
    """Resolve a Google News RSS article link to its real target via batchexecute."""
    m = re.search(r"/articles/([A-Za-z0-9_\-]+)", gnews_url)
    if not m:
        return None
    aid = m.group(1)
    try:
        page = session.get(f"https://news.google.com/rss/articles/{aid}", timeout=30)
        sig = re.search(r'data-n-a-sg="([^"]+)"', page.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
        if not (sig and ts):
            return None
        inner = [
            "garturlreq",
            [["en-US", "US", ["FINANCE_TOP_INDICES", "WEB_TEST_1_0_0"],
              None, None, 1, 1, "US:en", None, 180, None, None, None, None, None,
              0, None, None, [1]],
             "en-US", "US", 1, [2, 4, 8], 1, 1, None, 0, 0, None, 0],
            aid, int(ts.group(1)), sig.group(1),
        ]
        payload = "f.req=" + quote(
            json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])
        )
        resp = session.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            timeout=30,
        )
        hit = re.search(r'https?://www\.reuters\.com/[^\\"]+', resp.text)
        return hit.group(0) if hit else None
    except (requests.RequestException, ValueError):
        return None


def google_news(query: str, want: int = 12) -> list[Item]:
    """Discover byline articles via Google News RSS (keyless), resolved to real URLs."""
    items: list[Item] = []
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        r = session.get(
            GNEWS_SEARCH,
            params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  [gnews] HTTP {r.status_code}", file=sys.stderr)
            return items
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as exc:
        print(f"  [gnews] error: {exc}", file=sys.stderr)
        return items

    for node in root.findall(".//item")[:want]:
        source = (node.findtext("{*}source") or node.findtext("source") or "").strip()
        if source and source.lower() != "reuters":
            continue
        real = _gnews_resolve(session, (node.findtext("link") or "").strip())
        if not real:
            continue
        real = canonical(real)
        if not looks_like_article(real):
            continue
        title = _clean(node.findtext("title"))
        if title:
            title = re.sub(r"\s*-\s*Reuters\s*$", "", title)
        items.append(
            Item(url=real, title=title,
                 published=parse_date_rfc(node.findtext("pubDate")), sources={"gnews"})
        )
    return items


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\s+", " ", html.unescape(text)).strip() or None


def read_links_file(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    urls = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(canonical(line))
    return urls


def read_existing_recent(index_html: str) -> dict[str, tuple[str, str]]:
    """Map url -> (title, data-label) from the current Recent Work block."""
    out: dict[str, tuple[str, str]] = {}
    m = RECENT_BLOCK_RE.search(index_html)
    if not m:
        return out
    for a in re.finditer(
        r'<a[^>]*href="([^"]+)"[^>]*data-label="([^"]*)"[^>]*>(.*?)</a>', m.group(2), re.S
    ):
        url, label, title = a.group(1), a.group(2), a.group(3)
        out[canonical(url)] = (_clean(title) or "", _clean(html.unescape(label)) or "")
    return out


def parse_meta(page_html: str) -> tuple[str | None, str | None, datetime | None]:
    soup = BeautifulSoup(page_html, "lxml")

    def meta(*keys: str) -> str | None:
        for key in keys:
            tag = soup.find("meta", property=key) or soup.find("meta", attrs={"name": key})
            if tag and tag.get("content"):
                return tag["content"]
        return None

    title = meta("og:title", "twitter:title")
    desc = meta("og:description", "twitter:description", "description")
    published = parse_date(meta("article:published_time"))
    if not published:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (ValueError, TypeError):
                continue
            for node in data if isinstance(data, list) else [data]:
                if isinstance(node, dict) and node.get("datePublished"):
                    published = parse_date(node["datePublished"])
                    break
            if published:
                break
    return _clean(title), _clean(desc), published


def is_open_section(url: str) -> bool:
    return urlparse(url).path.startswith("/graphics/")


def validate(status: int, url: str) -> bool:
    """True if the link is reachable by a human.

    Open /graphics/ pages must return 200 (dead ones return 403/404).
    Bot-walled sections return 401 for both live and dead URLs, so those are
    trusted via their presence in a search index / the curated list.
    """
    if status in (404, 410):
        return False
    if is_open_section(url):
        return status == 200
    return status in (200, 401, 403)


def resolve(item: Item, existing: dict[str, tuple[str, str]]) -> bool:
    """Validate + enrich one item in place. Returns False if the link is broken."""
    status, page = fetch(item.url)
    if not validate(status, item.url):
        print(f"  BROKEN ({status}) {item.url}", file=sys.stderr)
        return False

    if page and is_open_section(item.url):
        title, desc, published = parse_meta(page)
        item.title = title or item.title
        item.description = desc or item.description
        item.published = published or item.published

    if (not item.title or not item.description) and item.url in existing:
        ex_title, ex_desc = existing[item.url]
        item.title = item.title or ex_title
        item.description = item.description or ex_desc

    if not item.title:
        print(f"  NO TITLE {item.url}", file=sys.stderr)
        return False
    if not item.description:
        item.description = item.title
    print(f"  OK ({status}) {item.url}")
    return True


def render_block(items: list[Item]) -> str:
    lines = ["", ""]
    for it in items:
        title = html.escape(it.title, quote=False)
        label = html.escape(it.description, quote=True)
        href = html.escape(it.url, quote=True)
        lines.append(
            f'\t\t\t\t\t\t\t\t\t\t<p>&#8640; '
            f'<a class="link" href="{href}" data-label="{label}">{title}</a></p>'
        )
        lines.append("\t\t\t\t\t")
    lines.append("                ")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--query", default=SEARCH_QUERY)
    args = ap.parse_args()

    with open(INDEX_PATH, encoding="utf-8") as fh:
        index_html = fh.read()
    if not RECENT_BLOCK_RE.search(index_html):
        print("ERROR: Recent Work block not found in index.html", file=sys.stderr)
        return 1
    existing = read_existing_recent(index_html)

    # Build candidate pool: Google News RSS first, curated list as floor.
    pool: dict[str, Item] = {}
    print("Discovery: Google News RSS")
    try:
        for it in google_news(args.query, want=12):
            pool.setdefault(it.url, it)
    except Exception as exc:  # discovery must never break the fail-safe rewrite
        print(f"  [gnews] unexpected error: {exc}", file=sys.stderr)
    print(f"  discovered {len(pool)} article(s)")

    for url in read_links_file(LINKS_PATH):
        pool.setdefault(url, Item(url=url, sources={"curated"}))

    if not pool:
        print("ERROR: no candidate URLs", file=sys.stderr)
        return 1

    # Validate + enrich, then rank newest-first (undated items last).
    resolved = [it for it in pool.values() if resolve(it, existing)]
    resolved.sort(
        key=lambda it: it.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    chosen = resolved[: args.limit]

    if len(chosen) < args.limit:
        print(f"ERROR: only {len(chosen)} valid links, need {args.limit}; "
              "leaving index.html unchanged", file=sys.stderr)
        return 1

    print("\nRecent Work ->")
    for it in chosen:
        when = it.published.date().isoformat() if it.published else "n/a"
        print(f"  [{when}] {it.title}\n      {it.url}\n      {it.description}")

    new_html = RECENT_BLOCK_RE.sub(
        lambda m: m.group(1) + render_block(chosen) + m.group(3), index_html, count=1
    )
    if new_html == index_html:
        print("\nNo change — index.html already up to date.")
        return 0
    if args.dry_run:
        print("\n--dry-run: index.html NOT written.")
        return 0

    with open(INDEX_PATH, "w", encoding="utf-8") as fh:
        fh.write(new_html)
    print("\nindex.html updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
