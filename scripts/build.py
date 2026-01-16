from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote_plus

import feedparser  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
BUNDLES_MD = ROOT / "config" / "bundles.md"
OUT_JSON = ROOT / "docs" / "data.json"

# Google News RSS search locale
HL = "en-US"
GL = "US"
CEID = "US:en"

MAX_ITEMS_PER_QUERY = 30   # per keyword query pull
MAX_TOTAL_ITEMS = 5000     # after merging + retention filter
RETENTION_DAYS = 90        # ~3 months rolling window


@dataclass
class Query:
    bundle: str
    q: str


def parse_bundles_md(text: str) -> List[Query]:
    """
    Format:
      ## Bundle Name
      - keyword query
      - another query
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    bundle = None
    out: List[Query] = []

    for ln in lines:
        if not ln.strip():
            continue
        m = re.match(r"^\s*##\s+(.*\S)\s*$", ln)
        if m:
            bundle = m.group(1).strip()
            continue
        m = re.match(r"^\s*-\s+(.*\S)\s*$", ln)
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
        # If corrupted, treat as empty rather than failing the run
        return []


def item_key(it: Dict) -> str:
    # Prefer URL; fall back to title+source to avoid losing items without a link
    url = (it.get("url") or "").strip()
    if url:
        return f"url::{url}"
    title = (it.get("title") or "").strip()
    source = (it.get("source") or "").strip()
    return f"ts::{title}::{source}"


def main() -> None:
    if not BUNDLES_MD.exists():
        raise SystemExit(f"Missing {BUNDLES_MD}")

    queries = parse_bundles_md(BUNDLES_MD.read_text(encoding="utf-8"))
    if not queries:
        raise SystemExit("No bundles/queries found in bundles.md")

    # Pull fresh items for current keywords
    fresh: List[Dict] = []
    for qu in queries:
        url = google_news_rss_url(qu.q)
        feed = feedparser.parse(url)
        entries = getattr(feed, "entries", [])[:MAX_ITEMS_PER_QUERY]

        for e in entries:
            link = (getattr(e, "link", None) or "").strip()
            title = (getattr(e, "title", None) or "").strip()

            source = ""
            if getattr(e, "source", None) and getattr(e.source, "title", None):
                source = (e.source.title or "").strip()

            ts = to_ts(e)

            # Drop items with no usable identifier
            if not link and not title:
                continue

            fresh.append(
                {
                    "bundle": qu.bundle,
                    "query": qu.q,
                    "title": title,
                    "source": source,
                    "url": link,
                    "published_ts": ts,
                }
            )

    # Merge with existing, keeping newest version per key
    existing = load_existing_items()
    merged: Dict[str, Dict] = {}

    # Load existing first
    for it in existing:
        if not isinstance(it, dict):
            continue
        k = item_key(it)
        if not k:
            continue
        merged[k] = it

    # Overlay fresh (and prefer fresher metadata if same key)
    for it in fresh:
        k = item_key(it)
        if not k:
            continue
        if k not in merged:
            merged[k] = it
            continue

        # If both exist, keep the one with the newer timestamp
        old_ts = int(merged[k].get("published_ts") or 0)
        new_ts = int(it.get("published_ts") or 0)

        if new_ts >= old_ts:
            merged[k] = it

    # Apply rolling retention window
    now_ts = int(datetime.now(timezone.utc).timestamp())
    cutoff = now_ts - (RETENTION_DAYS * 24 * 60 * 60)

    items = []
    for it in merged.values():
        ts = int(it.get("published_ts") or 0)
        if ts <= 0:
            continue
        if ts >= cutoff:
            items.append(it)

    # Sort newest first, cap
    items.sort(key=lambda x: int(x.get("published_ts") or 0), reverse=True)
    items = items[:MAX_TOTAL_ITEMS]

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "retention_days": RETENTION_DAYS,
            "bundles_count": len(set(q.bundle for q in queries)),
            "queries_count": len(queries),
            "items_count": len(items),
        },
        "items": items,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
