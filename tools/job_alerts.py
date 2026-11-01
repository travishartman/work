#!/usr/bin/env python3
"""Email new remote-work postings from the RSS/Atom feeds listed in data/job-feeds.json.

Each run fetches every enabled feed, keeps postings that read as remote, drops
those already recorded in data/jobs-seen.json, and emails the rest in one
message with a link to each listing. State is saved only after the email is
sent, so a failed send is retried on the next run.

A feed with no saved state is seeded silently (its current postings are marked
seen, nothing is sent) so adding a feed never floods the inbox. Use
--include-backlog to email the current matches instead.

Environment (only needed when sending):
  SMTP_USER, SMTP_PASSWORD  SMTP login; for Gmail use an app password
  ALERT_TO                  recipient address
  SMTP_HOST, SMTP_PORT      optional, default smtp.gmail.com / 465

A feed may set "url_env" instead of "url" to read its URL from that environment
variable (used for private Google Alerts feeds); it is skipped when unset.

Usage: python3 tools/job_alerts.py [--dry-run] [--include-backlog] [--only FEED_ID]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse
from xml.etree import ElementTree as ET

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDS_PATH = os.path.join(ROOT, "data", "job-feeds.json")
STATE_PATH = os.path.join(ROOT, "data", "jobs-seen.json")

MAX_SEEN_PER_FEED = 500
MAX_SENT_URLS = 2000
TRACKING_PARAMS = re.compile(r"^(utm_.*|gclid|fbclid|ref|referrer|source|src)$", re.I)
SNIPPET_CHARS = 320
HEADERS = {"User-Agent": "job-alerts (+https://github.com/travishartman/work)"}

# Positive and negative signals are both needed: "not a remote or hybrid role"
# contains "remote" but must not match.
DEFAULT_INCLUDE = [
    r"\bremote(ly)?\b",
    r"\bwork(ing)? from (home|anywhere)\b",
    r"\bwfh\b",
    r"\btelecommut\w*",
    r"\btelework\w*",
    r"\bhome[- ]based\b",
    r"\blocation[- ]independent\b",
    r"\b(fully |100% )?distributed (team|company|workforce)\b",
    r"\bvirtual (position|role|job|team)\b",
    r"\banywhere in the (us|u\.s\.|united states|country)\b",
]
DEFAULT_EXCLUDE = [
    r"\bnot (a )?(fully )?remote\b",
    r"\bno remote\b",
    r"\bnon-remote\b",
    r"\bin[- ]person\b",
    r"\bon[- ]?site\b",
    r"\bin[- ]office\b",
    r"\bin the office\b",
    r"\bhybrid\b",
]


@dataclass
class Job:
    uid: str
    title: str
    url: str
    company: str
    location: str
    job_type: str
    published: datetime | None
    summary: str
    text: str
    feed_name: str
    also_in: list[str] = field(default_factory=list)

def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def strip_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def unwrap_redirect(url: str) -> str:
    """Google Alerts links go through google.com/url?url=<target>; return the target."""
    parts = urlparse(url)
    if parts.hostname in ("www.google.com", "google.com") and parts.path == "/url":
        target = parse_qs(parts.query).get("url", [""])[0]
        if target:
            return target
    return url


def is_blocked(url: str, domains: list[str]) -> bool:
    """Match a domain or a suffix such as ".shop" against the URL host."""
    host = (urlparse(url).hostname or "").lower()
    return any(host == d.lstrip(".") or host.endswith("." + d.lstrip(".")) for d in map(str.lower, domains))


def parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(content: bytes, feed_name: str) -> list[Job]:
    """Parse RSS 2.0 or Atom. Extra fields (location, company, job_type) are read by local name."""
    root = ET.fromstring(content)
    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    jobs: list[Job] = []
    for el in entries:
        fields: dict[str, str] = {}
        for child in el:
            name = local(child.tag)
            if name == "link" and child.get("href"):
                if child.get("rel") not in (None, "alternate"):
                    continue
                value = child.get("href", "")
            else:
                value = (child.text or "").strip()
            fields.setdefault(name, value)

        url = unwrap_redirect(fields.get("link", ""))
        if urlparse(url).scheme not in ("http", "https"):
            continue
        title = strip_html(fields.get("title", ""))
        if not title:
            continue
        body = strip_html(
            fields.get("encoded") or fields.get("content") or fields.get("description")
            or fields.get("summary") or ""
        )
        company = strip_html(fields.get("company", ""))
        location = strip_html(fields.get("location", ""))
        jobs.append(Job(
            uid=fields.get("guid") or fields.get("id") or url,
            title=title,
            url=url,
            company=company,
            location=location,
            job_type=strip_html(fields.get("job_type", "")),
            published=parse_when(fields.get("pubDate") or fields.get("published") or fields.get("updated")),
            summary=body[:SNIPPET_CHARS].rstrip() + ("..." if len(body) > SNIPPET_CHARS else ""),
            text=" ".join([title, company, location, body]),
            feed_name=feed_name,
        ))
    return jobs


def is_remote(text: str, include: list[str], exclude: list[str]) -> bool:
    if any(re.search(p, text, re.I) for p in exclude):
        return False
    return any(re.search(p, text, re.I) for p in include)


def matches_keywords(job: Job, keywords: list[str]) -> bool:
    """No keywords means no filter. Matches the title and summary, not the full body."""
    if not keywords:
        return True
    haystack = f"{job.title} {job.summary}"
    return any(re.search(p, haystack, re.I) for p in keywords)


def canonical_url(url: str) -> str:
    """Key for 'same destination': ignores scheme, www, trailing slash, fragment and tracking params."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k))
    return f"{host}{parts.path.rstrip('/')}" + (f"?{urlencode(query)}" if query else "")


def drop_duplicates(jobs: list[Job], already_sent: list[str]) -> list[Job]:
    """Keep the first posting per destination; skip any destination already emailed."""
    taken = set(already_sent)
    kept: dict[str, Job] = {}
    for j in jobs:
        key = canonical_url(j.url)
        if key in taken and key not in kept:
            continue
        if key in kept:
            if j.feed_name != kept[key].feed_name and j.feed_name not in kept[key].also_in:
                kept[key].also_in.append(j.feed_name)
            continue
        kept[key] = j
    return list(kept.values())


def select_new(
    jobs: list[Job], seen: list[str] | None, include_backlog: bool, matches: list[Job]
) -> tuple[list[Job], list[str], bool]:
    """Return (jobs to email, updated seen list, seeded).

    `seen` is None for a feed with no saved state. Every posting in the feed is
    recorded as seen, matching or not, so changing the filters later does not
    resend old postings.
    """
    current = [j.uid for j in jobs]
    if seen is None:
        new = list(matches) if include_backlog else []
        return new, current[:MAX_SEEN_PER_FEED], not include_backlog
    seen_set = set(seen)
    new = [j for j in matches if j.uid not in seen_set]
    kept = current + [u for u in seen if u not in set(current)]
    return new, kept[:MAX_SEEN_PER_FEED], False


def when_label(dt: datetime | None) -> str:
    return f"{dt:%b} {dt.day}, {dt.year}" if dt else ""


def source_label(j: Job) -> str:
    return f"Source: {j.feed_name}" + (f" (also in {', '.join(j.also_in)})" if j.also_in else "")


def render(jobs: list[Job]) -> tuple[str, str]:
    by_feed: dict[str, list[Job]] = {}
    for j in jobs:
        by_feed.setdefault(j.feed_name, []).append(j)

    plain: list[str] = []
    rich: list[str] = ['<div style="font-family:Georgia,serif;max-width:640px;line-height:1.45">']
    for feed_name, items in by_feed.items():
        plain.append(f"== {feed_name} ==\n")
        rich.append(f'<h2 style="font-size:16px;margin:24px 0 8px">{html.escape(feed_name)}</h2>')
        for j in items:
            meta = [m for m in (j.company, j.location, j.job_type, when_label(j.published)) if m]
            plain.append(f"{j.title}\n  {' | '.join(meta)}\n  {source_label(j)}\n  {j.url}\n  {j.summary}\n")
            rich.append(
                f'<p style="margin:0 0 18px"><a href="{html.escape(j.url, quote=True)}" '
                f'style="font-size:16px"><strong>{html.escape(j.title)}</strong></a><br>'
                f'<span style="color:#555;font-size:13px">{html.escape(" | ".join(meta))}</span><br>'
                f'<span style="color:#555;font-size:12px">{html.escape(source_label(j))}</span><br>'
                f'<span style="font-size:14px">{html.escape(j.summary)}</span><br>'
                f'<a href="{html.escape(j.url, quote=True)}" style="font-size:13px">View listing</a></p>'
            )
    rich.append("</div>")
    return "\n".join(plain), "\n".join(rich)


def build_message(jobs: list[Job], sender: str, to: str) -> EmailMessage:
    if len(jobs) == 1:
        subject = f"New remote data viz job: {jobs[0].title}"
    else:
        subject = f"{len(jobs)} new remote data viz jobs"
    plain, rich = render(jobs)
    msg = EmailMessage()
    msg["Subject"] = " ".join(subject.split())
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(plain)
    msg.add_alternative(rich, subtype="html")
    return msg


def send_message(msg: EmailMessage) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print results; send nothing, save nothing")
    ap.add_argument("--include-backlog", action="store_true", help="email current matches for unseeded feeds")
    ap.add_argument("--only", help="run a single feed id")
    args = ap.parse_args()

    config = load_json(FEEDS_PATH, {"feeds": []})
    feeds = [f for f in config.get("feeds", []) if f.get("enabled", True)]
    if args.only:
        feeds = [f for f in feeds if f.get("id") == args.only]
    if not feeds:
        print("ERROR: no enabled feeds", file=sys.stderr)
        return 1

    state = load_json(STATE_PATH, {"feeds": {}})
    state.setdefault("feeds", {})
    to_send: list[Job] = []
    errors = 0

    for feed in feeds:
        fid, name = feed["id"], feed.get("name", feed["id"])
        url = os.environ.get(feed["url_env"], "") if feed.get("url_env") else feed.get("url", "")
        if not url:
            print(f"[{fid}] skipped: no feed URL (set {feed.get('url_env')})")
            continue
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            jobs = parse_feed(resp.content, name)
        except (requests.RequestException, ET.ParseError) as exc:
            # Not str(exc): requests errors embed the URL, which may be a private feed.
            code = getattr(getattr(exc, "response", None), "status_code", None)
            print(f"[{fid}] ERROR: {type(exc).__name__}{f' HTTP {code}' if code else ''}", file=sys.stderr)
            errors += 1
            continue
        jobs = [j for j in jobs if not is_blocked(j.url, feed.get("block_domains", []))]

        keywords = feed.get("keywords", [])
        candidates = [j for j in jobs if matches_keywords(j, keywords)]
        if feed.get("remote_only", True):
            include = feed.get("include", DEFAULT_INCLUDE)
            exclude = feed.get("exclude", DEFAULT_EXCLUDE)
            matches = [j for j in candidates if is_remote(j.text, include, exclude)]
        else:
            matches = candidates

        prior = state["feeds"].get(fid, {}).get("seen")
        new, seen, seeded = select_new(jobs, prior, args.include_backlog, matches)
        state["feeds"][fid] = {"seen": seen}
        to_send.extend(new)
        note = " (seeded, nothing sent)" if seeded else ""
        print(f"[{fid}] {len(jobs)} postings, {len(matches)} remote, {len(new)} new{note}")

    to_send = drop_duplicates(to_send, state.get("sent_urls", []))
    to_send.sort(key=lambda j: j.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    if to_send:
        if args.dry_run:
            print("\nWould email:")
            for j in to_send:
                where = ", ".join(p for p in (j.company, j.location) if p)
                print(f"  - {j.title} ({where}) [{j.feed_name}]\n    {j.url}")
        else:
            missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD", "ALERT_TO") if not os.environ.get(k)]
            if missing:
                print(f"ERROR: missing environment: {', '.join(missing)}", file=sys.stderr)
                return 1
            try:
                send_message(build_message(to_send, os.environ["SMTP_USER"], os.environ["ALERT_TO"]))
            except (smtplib.SMTPException, OSError) as exc:
                print(f"ERROR: email not sent, state not saved: {exc}", file=sys.stderr)
                return 1
            print(f"\nEmailed {len(to_send)} posting(s).")
            sent = [canonical_url(j.url) for j in to_send] + state.get("sent_urls", [])
            state["sent_urls"] = sent[:MAX_SENT_URLS]

    if not args.dry_run:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
