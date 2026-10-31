#!/usr/bin/env python3
"""Build data/work.json from a saved Reuters author-page HTML and refresh Recent Work.

Usage: python3 tools/build_work_db.py [--paste data/reuters-paste.txt] [--limit 6] [--dry-run]
Entries keep the order they appear on the page (newest first).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape_reuters as sr  # noqa: E402

PASTE_PATH = os.path.join(sr.ROOT, "data", "reuters-paste.txt")
DB_PATH = os.path.join(sr.ROOT, "data", "work.json")
BASE = "https://www.reuters.com"


def text(node) -> str | None:
    return sr._clean(node.get_text(" ")) if node else None


def parse_cards(page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "lxml")
    rows = []
    seen: set[str] = set()
    for card in soup.find_all(attrs={"data-testid": "StoryCard"}):
        link = card.find(attrs={"data-testid": "TitleLink"})
        if not link or not link.get("href"):
            continue
        if sr.canonical(urljoin(BASE, link["href"])) in seen:
            continue
        seen.add(sr.canonical(urljoin(BASE, link["href"])))
        kicker = card.find(attrs={"data-testid": "KickerLink"})
        when = card.find(attrs={"data-testid": "DateLineText"})
        published = sr.parse_date(when.get("datetime")) if when else None
        rows.append({
            "title": text(card.find(attrs={"data-testid": "TitleHeading"})),
            "url": sr.canonical(urljoin(BASE, link["href"])),
            "category": (kicker.contents[0].strip() if kicker and kicker.contents else None),
            "published": published.isoformat() if published else None,
            "description": text(card.find(attrs={"data-testid": "Description"})),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paste", default=PASTE_PATH)
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.paste, encoding="utf-8") as fh:
        rows = parse_cards(fh.read())
    if not rows:
        print("ERROR: no story cards found", file=sys.stderr)
        return 1

    with open(sr.INDEX_PATH, encoding="utf-8") as fh:
        index_html = fh.read()
    if not sr.RECENT_BLOCK_RE.search(index_html):
        print("ERROR: Recent Work block not found in index.html", file=sys.stderr)
        return 1

    # Translated editions live under a two-letter path such as /ar/.
    english = [r for r in rows if not re.match(r"^/[a-z]{2}/", urlparse(r["url"]).path)]
    chosen = [
        sr.Item(url=r["url"], title=r["title"], description=r["description"] or r["title"])
        for r in english[: args.limit]
    ]
    new_html = sr.RECENT_BLOCK_RE.sub(
        lambda m: m.group(1) + sr.render_block(chosen) + m.group(3), index_html, count=1
    )

    print(f"{len(rows)} entries parsed")
    for i, r in enumerate(rows, 1):
        print(f"{i:>2}. [{(r['published'] or 'n/a')[:10]}] {r['title']}")
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    with open(DB_PATH, "w", encoding="utf-8") as fh:
        json.dump(
            {"generated": datetime.now().astimezone().isoformat(timespec="seconds"),
             "source": os.path.basename(args.paste), "items": rows},
            fh, indent=2, ensure_ascii=False,
        )
        fh.write("\n")
    with open(sr.INDEX_PATH, "w", encoding="utf-8") as fh:
        fh.write(new_html)
    print(f"\nWrote {DB_PATH} and updated Recent Work with {len(chosen)} links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
