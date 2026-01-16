from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import quote_plus

import feedparser  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
BUNDLES_MD = ROOT / "config" / "bundles.md"
OUT_JSON = ROOT / "docs" / "data.json"

# Google News RSS search locale
HL = "en-US"
GL = "US"
CEID = "US:en"

MAX_ITEMS_PER_QUERY = 30  # you can change later
MAX_TOTAL_ITEMS = 600     # global cap


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
    # q is inserted as URL-encoded
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(q)}&hl={HL}&gl={GL}&ceid={CEID}"
    )


def to_ts(entry) -> int:
    # feedparser uses time.struct_time for published_parsed when available
    if getattr(entry, "published_parsed", None):
        return int(time.mktime(entry.published_parsed))
    if getattr(entry, "updated_parsed", None):
        return int(time.mktime(entry.updated_parsed))
    return 0


def main() -> None:
    if not BUNDLES_MD.exists():
        raise SystemExit(f"Missing {BUNDLES_MD}")

    queries = parse_bundles_md(BUNDLES_MD.read_text(encoding="utf-8"))
    if not queries:
        raise SystemExit("No bundles/queries found in bundles.md")

    seen: set[str] = set()
    items: List[Dict] = []

    for qu in queries:
        url = google_news_rss_url(qu.q)
        feed = feedparser.parse(url)

        # If feed parsing fails, skip quietly (common when rate-limited)
        entries = getattr(feed, "entries", [])[:MAX_ITEMS_PER_QUERY]
        for e in entries:
            link = getattr(e, "link", None) or ""
            title = getattr(e, "title", None) or ""
            source = ""
            if getattr(e, "source", None) and getattr(e.source, "title", None):
                source = e.source.title

            key = link.strip() or title.strip()
            if not key or key in seen:
                continue
            seen.add(key)

            ts = to_ts(e)
            items.append(
                {
                    "bundle": qu.bundle,
                    "query": qu.q,
                    "title": title,
                    "source": source,
                    "url": link,
                    "published_ts": ts,
                }
            )

    # Sort newest first, cap
    items.sort(key=lambda x: x.get("published_ts", 0), reverse=True)
    items = items[:MAX_TOTAL_ITEMS]

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
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
