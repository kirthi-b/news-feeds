from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

import feedparser  # type: ignore
import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
BUNDLES_MD = ROOT / "config" / "bundles.md"
OUT_JSON = ROOT / "docs" / "data.json"

HL = "en-US"
GL = "US"
CEID = "US:en"

MAX_ITEMS_PER_QUERY = 30
RETENTION_DAYS = 90
MAX_TOTAL_ITEMS = 6000

# How many publisher pages to fetch per run for blurbs (keeps Actions runtime sane)
BLURB_FETCH_LIMIT = 120

UA = "Mozilla/5.0 (compatible; ProjectFeedsBot/1.0)"


@dataclass
class Query:
    bundle: str
    q: str


def parse_bundles_md(text: str) -> List[Query]:
    """
    Accepts '-' or '*' bullets.

    ## Bundle Name
    - query
    * query
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    bundle: Optional[str] = None
    out: List[Query] = []

    for ln in lines:
        if not ln.strip():
            continue
        m = re.match(r"^\s*##\s+(.*\S)\s*$", ln)
        if m:
            bundle = m.group(1).strip()
            continue
        m = re.match(r"^\s*[-*]\s+(.*\S)\s*$", ln)
        if m and bundle:
            out.append(Query(bundle=bundle, q=m.group(1).strip()))
    return out


def google_news_rss_url(q: str) -> str:
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(q)}&hl={HL}&gl={GL}&ceid={CEID}"
    )


def to_ts(entry) -> int:
    if getattr(entry, "published_parsed", None):
        return int(time.mktime(entry.published_parsed))
    if getattr(entry, "updated_parsed", None):
        return int(time.mktime(entry.updated_parsed))
    return 0


def load_existing_items() -> List[Dict]:
    if not OUT_JSON.exists():
        return []
    try:
        data = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        items = data.get("items", [])
        return items if isinstance(items, list) else []
    except Exception:
        return []


def normalize_url(url: str) -> str:
    return (url or "").strip()


def canonical_key(it: Dict) -> str:
    cu = normalize_url(it.get("canonical_url") or "")
    if cu:
        return "canon::" + cu
    u = normalize_url(it.get("url") or "")
    if u:
        return "url::" + u
    title = (it.get("title") or "").strip().lower()
    source = (it.get("source") or "").strip().lower()
    h = hashlib.sha256(f"{title}::{source}".encode("utf-8")).hexdigest()[:20]
    return "ts::" + h


def extract_rss_blurb(entry) -> Optional[str]:
    # Google News RSS "summary" is often just title; keep only if it adds info.
    raw = getattr(entry, "summary", None)
    if not raw:
        return None
    txt = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    txt = " ".join(txt.split())
    title = (getattr(entry, "title", None) or "").strip()
    if not txt or (title and txt.lower() == title.lower()):
        return None
    return txt


def extract_publisher_url_from_param(url: str) -> str:
    """
    Some Google News links include a real URL as a query param (url=, u=).
    If present, use it.
    """
    try:
        p = urlparse(url)
        qs = parse_qs(p.query)
        for k in ("url", "u"):
            if k in qs and qs[k]:
                return unquote(qs[k][0]).strip()
    except Exception:
        pass
    return ""


def resolve_to_publisher(url: str) -> str:
    """
    Best-effort: turn Google News link into direct publisher URL.
    Strategy:
    1) If query param contains publisher URL, use it.
    2) Otherwise follow redirects with requests and use final URL if it’s not news.google.com.
    """
    url = normalize_url(url)
    if not url:
        return ""

    direct = extract_publisher_url_from_param(url)
    if direct and direct.startswith("http"):
        return direct

    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=12, allow_redirects=True)
        final = (r.url or url).strip()
        # If it still lands on Google News, treat as unresolved.
        if "news.google.com" in urlparse(final).netloc.lower():
            return ""
        return final
    except Exception:
        return ""


def first_sentence(text: str) -> str:
    text = " ".join((text or "").split()).strip()
    if not text:
        return ""
    # naive first-sentence split (good enough for a blurb)
    m = re.split(r"(?<=[.!?])\s+", text)
    return m[0].strip() if m else text[:240].strip()


def fetch_first_paragraph(url: str) -> Optional[str]:
    """
    Best-effort extraction of first substantive paragraph from publisher HTML.
    Falls back to meta description if needed.
    """
    url = normalize_url(url)
    if not url:
        return None

    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=12, allow_redirects=True)
        if not r.ok:
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text/html" not in ctype:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        # meta description fallback
        meta_desc = None
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            meta_desc = " ".join(str(md["content"]).split()).strip()

        # try article tag first
        article = soup.find("article")
        candidates = []
        if article:
            candidates = article.find_all("p")
        else:
            # fallback: any paragraphs
            candidates = soup.find_all("p")

        # pick first paragraph with decent length and not boilerplate
        for p in candidates:
            t = p.get_text(" ", strip=True)
            t = " ".join(t.split()).strip()
            if len(t) < 80:
                continue
            lowered = t.lower()
            if "subscribe" in lowered or "sign up" in lowered or "cookies" in lowered:
                continue
            return first_sentence(t)

        if meta_desc and len(meta_desc) >= 60:
            return first_sentence(meta_desc)

        return None
    except Exception:
        return None


def main() -> None:
    if not BUNDLES_MD.exists():
        raise SystemExit(f"Missing {BUNDLES_MD}")

    queries = parse_bundles_md(BUNDLES_MD.read_text(encoding="utf-8"))
    if not queries:
        raise SystemExit("No bundles/queries found in bundles.md")

    fresh: List[Dict] = []
    for qu in queries:
        feed = feedparser.parse(google_news_rss_url(qu.q))
        entries = getattr(feed, "entries", [])[:MAX_ITEMS_PER_QUERY]

        for e in entries:
            raw_url = (getattr(e, "link", None) or "").strip()
            title = (getattr(e, "title", None) or "").strip()
            if not raw_url and not title:
                continue

            source = ""
            if getattr(e, "source", None) and getattr(e.source, "title", None):
                source = (e.source.title or "").strip()

            ts = to_ts(e)
            blurb = extract_rss_blurb(e)

            publisher = resolve_to_publisher(raw_url) if raw_url else ""

            fresh.append(
                {
                    "bundle": qu.bundle,
                    "query": qu.q,
                    "title": title,
                    "source": source,
                    "url": raw_url,
                    "canonical_url": publisher or "",
                    "published_ts": ts,
                    "blurb": blurb,
                }
            )

    existing = load_existing_items()
    merged: Dict[str, Dict] = {}

    for it in existing:
        if isinstance(it, dict):
            merged[canonical_key(it)] = it

    for it in fresh:
        k = canonical_key(it)
        if k not in merged:
            merged[k] = it
            continue

        old = merged[k]
        old_ts = int(old.get("published_ts") or 0)
        new_ts = int(it.get("published_ts") or 0)

        chosen = it if new_ts >= old_ts else old
        other = old if chosen is it else it

        for field in ["canonical_url", "url", "source", "title", "blurb", "bundle", "query"]:
            if not chosen.get(field) and other.get(field):
                chosen[field] = other[field]

        merged[k] = chosen

    now_ts = int(datetime.now(timezone.utc).timestamp())
    cutoff = now_ts - (RETENTION_DAYS * 24 * 60 * 60)

    items: List[Dict] = []
    for it in merged.values():
        ts = int(it.get("published_ts") or 0)
        if ts > 0 and ts >= cutoff:
            items.append(it)

    items.sort(key=lambda x: int(x.get("published_ts") or 0), reverse=True)
    items = items[:MAX_TOTAL_ITEMS]

    # Fetch publisher first-paragraph blurbs (best-effort) for items missing blurbs.
    fetched = 0
    for it in items:
        if fetched >= BLURB_FETCH_LIMIT:
            break
        if it.get("blurb"):
            continue
        target = it.get("canonical_url") or ""
        if not target:
            continue
        b = fetch_first_paragraph(target)
        if b:
            it["blurb"] = b
        fetched += 1

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "retention_days": RETENTION_DAYS,
            "bundles_count": len({q.bundle for q in queries}),
            "queries_count": len(queries),
            "items_count": len(items),
        },
        "items": items,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
