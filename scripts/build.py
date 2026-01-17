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

ROOT = Path(__file__).resolve().parents[1]
BUNDLES_MD = ROOT / "config" / "bundles.md"
OUT_JSON = ROOT / "docs" / "data.json"

# Google News RSS params
HL = "en-US"
GL = "US"
CEID = "US:en"

# Retention policy (6 months)
RETENTION_DAYS = 180

# Fetch sizes
MAX_ITEMS_PER_QUERY = 50  # per query, per run (RSS itself often limits anyway)

UA = "Mozilla/5.0 (compatible; ProjectFeedsBot/1.0)"


@dataclass
class Query:
    bundle: str
    q: str


def parse_bundles_md(text: str) -> List[Query]:
    """
    Format:
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
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl={HL}&gl={GL}&ceid={CEID}"


def to_ts(entry) -> int:
    if getattr(entry, "published_parsed", None):
        return int(time.mktime(entry.published_parsed))
    if getattr(entry, "updated_parsed", None):
        return int(time.mktime(entry.updated_parsed))
    return 0


def safe_str(x) -> str:
    return (x or "").strip()


def load_existing_items() -> List[Dict]:
    if not OUT_JSON.exists():
        return []
    try:
        data = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        items = data.get("items", [])
        return items if isinstance(items, list) else []
    except Exception:
        return []


def extract_publisher_url_from_param(url: str) -> str:
    """
    Sometimes Google News link includes a real URL as a query param.
    """
    try:
        p = urlparse(url)
        qs = parse_qs(p.query)
        for k in ("url", "u"):
            if k in qs and qs[k]:
                v = unquote(qs[k][0]).strip()
                if v.startswith("http"):
                    return v
    except Exception:
        pass
    return ""


def resolve_to_publisher(url: str) -> str:
    """
    Best-effort canonical publisher URL:
    - if url= param exists, use it
    - else follow redirects; if final host != news.google.com, use it
    """
    url = safe_str(url)
    if not url:
        return ""

    direct = extract_publisher_url_from_param(url)
    if direct:
        return direct

    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=12, allow_redirects=True)
        final = safe_str(r.url or "")
        if final and "news.google.com" not in urlparse(final).netloc.lower():
            return final
    except Exception:
        pass

    return ""


def stable_id_for_item(it: Dict) -> str:
    """
    Stable across runs. Prefer canonical_url; else url; else guid; else title/source/ts.
    """
    canon = safe_str(it.get("canonical_url"))
    if canon:
        base = f"canon::{canon}"
    else:
        url = safe_str(it.get("url"))
        guid = safe_str(it.get("guid"))
        if url:
            base = f"url::{url}"
        elif guid:
            base = f"guid::{guid}"
        else:
            title = safe_str(it.get("title")).lower()
            source = safe_str(it.get("source")).lower()
            ts = str(int(it.get("published_ts") or 0))
            base = f"ts::{title}::{source}::{ts}"

    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def merge_item(existing: Dict, incoming: Dict) -> Dict:
    """
    Keep existing, fill gaps with incoming; update fields that can improve over time
    (canonical_url is allowed to appear later; source/title may improve).
    """
    out = dict(existing)

    # Always keep id stable
    out["id"] = existing.get("id") or incoming.get("id")

    # Prefer earlier published_ts if existing has it; otherwise take incoming
    ex_ts = int(out.get("published_ts") or 0)
    in_ts = int(incoming.get("published_ts") or 0)
    if ex_ts == 0 and in_ts:
        out["published_ts"] = in_ts

    # Upgradable fields: fill blanks
    for k in ("title", "source", "bundle", "query", "url", "canonical_url", "guid"):
        if not safe_str(out.get(k)):
            out[k] = incoming.get(k) or out.get(k)

    # If canonical_url becomes available later, upgrade it
    if safe_str(incoming.get("canonical_url")) and not safe_str(existing.get("canonical_url")):
        out["canonical_url"] = incoming["canonical_url"]

    # Keep bundle/query as first-seen by default; but if missing, fill
    return out


def main() -> None:
    if not BUNDLES_MD.exists():
        raise SystemExit(f"Missing {BUNDLES_MD}")

    queries = parse_bundles_md(BUNDLES_MD.read_text(encoding="utf-8"))
    if not queries:
        raise SystemExit("No bundles/queries found in bundles.md")

    existing_items = load_existing_items()
    by_id: Dict[str, Dict] = {}

    # Load existing into map keyed by stable id (recomputed to be robust)
    for it in existing_items:
        if not isinstance(it, dict):
            continue
        it_id = safe_str(it.get("id"))
        if not it_id:
            it_id = stable_id_for_item(it)
            it["id"] = it_id
        by_id[it_id] = it

    # Fetch new RSS items
    for qu in queries:
        feed = feedparser.parse(google_news_rss_url(qu.q))
        entries = getattr(feed, "entries", [])[:MAX_ITEMS_PER_QUERY]

        for e in entries:
            title = safe_str(getattr(e, "title", None))
            url = safe_str(getattr(e, "link", None))
            guid = safe_str(getattr(e, "guid", None)) or safe_str(getattr(e, "id", None))

            if not (title or url or guid):
                continue

            source = ""
            if getattr(e, "source", None) and getattr(e.source, "title", None):
                source = safe_str(e.source.title)

            ts = to_ts(e)

            canonical_url = resolve_to_publisher(url) if url else ""

            incoming = {
                "bundle": qu.bundle,
                "query": qu.q,
                "title": title,
                "source": source,
                "url": url,
                "canonical_url": canonical_url,
                "guid": guid,
                "published_ts": ts,
            }
            incoming["id"] = stable_id_for_item(incoming)

            iid = incoming["id"]
            if iid in by_id:
                by_id[iid] = merge_item(by_id[iid], incoming)
            else:
                by_id[iid] = incoming

    # Retain only last 6 months
    now_ts = int(datetime.now(timezone.utc).timestamp())
    cutoff = now_ts - (RETENTION_DAYS * 86400)

    items = []
    for it in by_id.values():
        ts = int(it.get("published_ts") or 0)
        # keep undated items (ts==0) only if they are newly added; safest is to drop them
        if ts == 0:
            continue
        if ts >= cutoff:
            items.append(it)

    # Sort newest-first for UI
    items.sort(key=lambda x: int(x.get("published_ts") or 0), reverse=True)

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
