from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote_plus

import feedparser  # type: ignore
import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
BUNDLES_MD = ROOT / "config" / "bundles.md"
OUT_JSON = ROOT / "docs" / "data.json"

# Google News RSS search locale
HL = "en-US"
GL = "US"
CEID = "US:en"

# Controls
MAX_ITEMS_PER_QUERY = 30        # how many items to read from each keyword feed
RETENTION_DAYS = 90             # ~3 months rolling window
MAX_TOTAL_ITEMS = 6000          # cap after merge+retention
OG_ENRICH_LIMIT = 120           # only try OG scrape on top N items to keep runtime sane

UA = "Mozilla/5.0 (compatible; ProjectFeedsBot/1.0)"


@dataclass
class Query:
    bundle: str
    q: str


def parse_bundles_md(text: str) -> List[Query]:
    """
    Accepts either '-' or '*' bullets.

    Format:
      ## Bundle Name
      - keyword query
      * keyword query
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
            continue

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
    url = (url or "").strip()
    if not url:
        return ""
    return url


def canonical_key(it: Dict) -> str:
    """
    Dedup key across runs:
    Prefer canonical_url; else fallback to url; else hashed title+source.
    """
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


def extract_from_rss(entry) -> tuple[Optional[str], Optional[str]]:
    blurb = None
    if getattr(entry, "summary", None):
        blurb = BeautifulSoup(entry.summary, "html.parser").get_text(" ", strip=True)

    image = None
    if getattr(entry, "media_thumbnail", None):
        try:
            image = entry.media_thumbnail[0].get("url")
        except Exception:
            pass
    if not image and getattr(entry, "media_content", None):
        try:
            image = entry.media_content[0].get("url")
        except Exception:
            pass

    return image, blurb


def resolve_final_url(url: str) -> str:
    """
    Follow redirects to get the publisher URL, so OG image isn't Google News branding.
    """
    url = normalize_url(url)
    if not url:
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=12, allow_redirects=True)
        final = (r.url or url).strip()
        return final
    except Exception:
        return url


def fetch_og(url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Best-effort Open Graph scrape for og:image and og:description.
    """
    url = normalize_url(url)
    if not url:
        return None, None
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=12, allow_redirects=True)
        if not r.ok:
            return None, None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text/html" not in ctype:
            return None, None

        soup = BeautifulSoup(r.text, "html.parser")

        def meta(prop: str) -> Optional[str]:
            tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content"):
                return str(tag["content"]).strip()
            return None

        img = meta("og:image")
        desc = meta("og:description") or meta("description")
        if desc:
            desc = " ".join(desc.split())
        return img, desc
    except Exception:
        return None, None


def main() -> None:
    if not BUNDLES_MD.exists():
        raise SystemExit(f"Missing {BUNDLES_MD}")

    queries = parse_bundles_md(BUNDLES_MD.read_text(encoding="utf-8"))
    if not queries:
        raise SystemExit("No bundles/queries found in bundles.md")

    # Pull fresh items
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
            img, blurb = extract_from_rss(e)

            # Resolve publisher URL (fixes Google News logo thumbnails)
            canonical = resolve_final_url(raw_url) if raw_url else ""

            fresh.append(
                {
                    "bundle": qu.bundle,
                    "query": qu.q,
                    "title": title,
                    "source": source,
                    "url": raw_url,
                    "canonical_url": canonical or raw_url,
                    "published_ts": ts,
                    "image_url": img,
                    "blurb": blurb,
                }
            )

    # Merge with existing items (rolling window) with dedupe across runs
    existing = load_existing_items()
    merged: Dict[str, Dict] = {}

    # Load existing first
    for it in existing:
        if not isinstance(it, dict):
            continue
        k = canonical_key(it)
        merged[k] = it

    # Overlay fresh items (prefer newer ts, and fill missing fields)
    for it in fresh:
        k = canonical_key(it)
        if k not in merged:
            merged[k] = it
            continue

        old = merged[k]
        old_ts = int(old.get("published_ts") or 0)
        new_ts = int(it.get("published_ts") or 0)

        # Always prefer the record with the newer timestamp
        chosen = it if new_ts >= old_ts else old
        other = old if chosen is it else it

        # Preserve enrichments if chosen is missing them
        for field in ["image_url", "blurb", "canonical_url", "url", "source", "title"]:
            if not chosen.get(field) and other.get(field):
                chosen[field] = other[field]

        merged[k] = chosen

    # Apply retention window
    now_ts = int(datetime.now(timezone.utc).timestamp())
    cutoff = now_ts - (RETENTION_DAYS * 24 * 60 * 60)

    items: List[Dict] = []
    for it in merged.values():
        ts = int(it.get("published_ts") or 0)
        if ts <= 0:
            continue
        if ts >= cutoff:
            items.append(it)

    # Sort newest first and cap
    items.sort(key=lambda x: int(x.get("published_ts") or 0), reverse=True)
    items = items[:MAX_TOTAL_ITEMS]

    # OG enrich for top items that lack image/blurb using publisher canonical_url
    for it in items[:OG_ENRICH_LIMIT]:
        target = it.get("canonical_url") or it.get("url") or ""
        if not target:
            continue
        need_img = not it.get("image_url")
        need_desc = not it.get("blurb")
        if not (need_img or need_desc):
            continue
        og_img, og_desc = fetch_og(target)
        if need_img and og_img:
            it["image_url"] = og_img
        if need_desc and og_desc:
            it["blurb"] = og_desc

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
