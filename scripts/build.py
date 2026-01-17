from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

import feedparser  # type: ignore
import requests

ROOT = Path(__file__).resolve().parents[1]
BUNDLES_MD = ROOT / "config" / "bundles.md"
OUT_JSON = ROOT / "docs" / "data.json"

HL = "en-US"
GL = "US"
CEID = "US:en"

RETENTION_DAYS = 180
MAX_ITEMS_PER_QUERY = 50
UA = "Mozilla/5.0 (compatible; ProjectFeedsBot/1.1)"


@dataclass
class QuerySpec:
    bundle: str
    include: str
    exclude: List[str] = field(default_factory=list)

    def to_google_query(self) -> str:
        parts = [self.include.strip()]
        for ex in self.exclude:
            ex = ex.strip()
            if not ex:
                continue
            # If exclusion already starts with -, keep it; else add -
            if ex.startswith("-"):
                parts.append(ex)
            else:
                parts.append(f"-{ex}")
        return " ".join(parts).strip()


def parse_bundles_md(text: str) -> List[QuerySpec]:
    """
    Format:
    ## Bundle
    + include query
    - exclude term
    - "exclude phrase"

    Multiple + blocks allowed per bundle.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    bundle: Optional[str] = None
    current: Optional[QuerySpec] = None
    out: List[QuerySpec] = []

    for ln in lines:
        if not ln.strip():
            continue

        m = re.match(r"^\s*##\s+(.*\S)\s*$", ln)
        if m:
            bundle = m.group(1).strip()
            current = None
            continue

        if not bundle:
            continue

        m = re.match(r"^\s*\+\s+(.*\S)\s*$", ln)
        if m:
            # start a new query block
            if current:
                out.append(current)
            current = QuerySpec(bundle=bundle, include=m.group(1).strip(), exclude=[])
            continue

        m = re.match(r"^\s*-\s+(.*\S)\s*$", ln)
        if m and current:
            current.exclude.append(m.group(1).strip())
            continue

        # Back-compat: if user still uses * or - bullets, treat as include-only query
        m = re.match(r"^\s*[*]\s+(.*\S)\s*$", ln)
        if m:
            if current:
                out.append(current)
                current = None
            out.append(QuerySpec(bundle=bundle, include=m.group(1).strip(), exclude=[]))
            continue

        # Plain dash bullets historically meant includes; if no current + block, treat as include query
        m = re.match(r"^\s*-\s+(.*\S)\s*$", ln)
        if m and not current:
            out.append(QuerySpec(bundle=bundle, include=m.group(1).strip(), exclude=[]))
            continue

    if current:
        out.append(current)

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
    out = dict(existing)
    out["id"] = existing.get("id") or incoming.get("id")

    ex_ts = int(out.get("published_ts") or 0)
    in_ts = int(incoming.get("published_ts") or 0)
    if ex_ts == 0 and in_ts:
        out["published_ts"] = in_ts

    for k in ("title", "source", "bundle", "query", "url", "canonical_url", "guid"):
        if not safe_str(out.get(k)):
            out[k] = incoming.get(k) or out.get(k)

    if safe_str(incoming.get("canonical_url")) and not safe_str(existing.get("canonical_url")):
        out["canonical_url"] = incoming["canonical_url"]

    return out


def main() -> None:
    if not BUNDLES_MD.exists():
        raise SystemExit(f"Missing {BUNDLES_MD}")

    specs = parse_bundles_md(BUNDLES_MD.read_text(encoding="utf-8"))
    if not specs:
        raise SystemExit("No bundles/queries found in bundles.md")

    existing_items = load_existing_items()
    by_id: Dict[str, Dict] = {}

    for it in existing_items:
        if not isinstance(it, dict):
            continue
        it_id = safe_str(it.get("id"))
        if not it_id:
            it_id = stable_id_for_item(it)
            it["id"] = it_id
        by_id[it_id] = it

    # Fetch new RSS items
    for spec in specs:
        google_q = spec.to_google_query()
        feed = feedparser.parse(google_news_rss_url(google_q))
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
                "bundle": spec.bundle,
                # store the human-readable query you wrote, not the expanded operator string
                "query": spec.include,
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

    now_ts = int(datetime.now(timezone.utc).timestamp())
    cutoff = now_ts - (RETENTION_DAYS * 86400)

    items = []
    for it in by_id.values():
        ts = int(it.get("published_ts") or 0)
        if ts == 0:
            continue
        if ts >= cutoff:
            items.append(it)

    items.sort(key=lambda x: int(x.get("published_ts") or 0), reverse=True)

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "retention_days": RETENTION_DAYS,
            "bundles_count": len({s.bundle for s in specs}),
            "queries_count": len(specs),
            "items_count": len(items),
        },
        "items": items,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
