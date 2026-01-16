from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urlparse

import feedparser  # type: ignore
import requests
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
BUNDLES_MD = ROOT / "config" / "bundles.md"
OUT_JSON = ROOT / "docs" / "data.json"
PDF_DIR = ROOT / "docs" / "pdfs"

HL = "en-US"
GL = "US"
CEID = "US:en"

MAX_ITEMS_PER_QUERY = 30
MAX_TOTAL_ITEMS = 5000
RETENTION_DAYS = 90

# Limit how many PDFs to generate per run (keeps Actions fast)
MAX_PDFS_PER_RUN = 80

UA = "Mozilla/5.0 (compatible; ProjectFeedsBot/1.0)"


@dataclass
class Query:
    bundle: str
    q: str


def parse_bundles_md(text: str) -> List[Query]:
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


def item_key(it: Dict) -> str:
    url = (it.get("url") or "").strip()
    if url:
        return f"url::{url}"
    title = (it.get("title") or "").strip()
    source = (it.get("source") or "").strip()
    return f"ts::{title}::{source}"


def stable_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def extract_from_rss(entry) -> tuple[Optional[str], Optional[str]]:
    """
    Best-effort image + blurb from RSS fields.
    """
    blurb = None
    if getattr(entry, "summary", None):
        blurb = BeautifulSoup(entry.summary, "lxml").get_text(" ", strip=True)

    image = None
    # feedparser sometimes exposes media_thumbnail / media_content
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


def fetch_og(url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Best-effort Open Graph scrape for og:image and og:description.
    """
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=12)
        if not r.ok or "text/html" not in (r.headers.get("Content-Type") or ""):
            return None, None
        soup = BeautifulSoup(r.text, "lxml")

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


def make_pdf_clipping(pdf_path: Path, item: Dict) -> None:
    """
    Generates a one-page "clipping" PDF: title, source, date, blurb, link.
    No full-article reproduction.
    """
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(pdf_path), pagesize=letter)
    w, h = letter

    title = (item.get("title") or "").strip()
    source = (item.get("source") or "").strip()
    url = (item.get("url") or "").strip()
    blurb = (item.get("blurb") or "").strip()
    ts = int(item.get("published_ts") or 0)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "—"

    x = 0.85 * inch
    y = h - 0.9 * inch

    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y, title[:120])
    y -= 0.28 * inch

    c.setFont("Helvetica", 10)
    c.drawString(x, y, f"{source}  •  {dt}")
    y -= 0.25 * inch

    c.setFont("Helvetica", 9)
    # Wrap blurb
    if blurb:
        blurb = blurb[:700]
        words = blurb.split()
        line = ""
        for word in words:
            if len(line) + len(word) + 1 > 95:
                c.drawString(x, y, line)
                y -= 0.18 * inch
                if y < 1.2 * inch:
                    break
                line = word
            else:
                line = (line + " " + word).strip()
        if y >= 1.2 * inch and line:
            c.drawString(x, y, line)
            y -= 0.25 * inch

    c.setFont("Helvetica-Oblique", 9)
    # Keep link printable
    safe_url = url if len(url) <= 140 else url[:137] + "..."
    c.drawString(x, 0.9 * inch, safe_url)

    c.showPage()
    c.save()


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
            link = (getattr(e, "link", None) or "").strip()
            title = (getattr(e, "title", None) or "").strip()
            if not link and not title:
                continue

            source = ""
            if getattr(e, "source", None) and getattr(e.source, "title", None):
                source = (e.source.title or "").strip()

            ts = to_ts(e)

            img, blurb = extract_from_rss(e)

            fresh.append(
                {
                    "bundle": qu.bundle,
                    "query": qu.q,
                    "title": title,
                    "source": source,
                    "url": link,
                    "published_ts": ts,
                    "image_url": img,
                    "blurb": blurb,
                    "pdf_path": None,
                }
            )

    existing = load_existing_items()
    merged: Dict[str, Dict] = {}

    for it in existing:
        if isinstance(it, dict):
            merged[item_key(it)] = it

    for it in fresh:
        k = item_key(it)
        if k not in merged:
            merged[k] = it
        else:
            old_ts = int(merged[k].get("published_ts") or 0)
            new_ts = int(it.get("published_ts") or 0)
            if new_ts >= old_ts:
                # preserve prior enrichments if new is missing
                if not it.get("image_url") and merged[k].get("image_url"):
                    it["image_url"] = merged[k]["image_url"]
                if not it.get("blurb") and merged[k].get("blurb"):
                    it["blurb"] = merged[k]["blurb"]
                if merged[k].get("pdf_path"):
                    it["pdf_path"] = merged[k]["pdf_path"]
                merged[k] = it

    now_ts = int(datetime.now(timezone.utc).timestamp())
    cutoff = now_ts - (RETENTION_DAYS * 24 * 60 * 60)

    items: List[Dict] = []
    for it in merged.values():
        ts = int(it.get("published_ts") or 0)
        if ts > 0 and ts >= cutoff:
            items.append(it)

    items.sort(key=lambda x: int(x.get("published_ts") or 0), reverse=True)
    items = items[:MAX_TOTAL_ITEMS]

    # Enrich missing image/blurb via Open Graph for top items only (keeps runtime sane)
    for it in items[:200]:
        if (not it.get("image_url") or not it.get("blurb")) and it.get("url"):
            og_img, og_desc = fetch_og(it["url"])
            if not it.get("image_url") and og_img:
                it["image_url"] = og_img
            if not it.get("blurb") and og_desc:
                it["blurb"] = og_desc

    # Generate/update clipping PDFs for newest items (and only if missing)
    pdfs_made = 0
    for it in items:
        if pdfs_made >= MAX_PDFS_PER_RUN:
            break
        url = (it.get("url") or "").strip()
        if not url:
            continue

        pid = stable_id(url)
        rel = f"pdfs/{pid}.pdf"
        out = PDF_DIR / f"{pid}.pdf"

        if it.get("pdf_path") == rel and out.exists():
            continue

        make_pdf_clipping(out, it)
        it["pdf_path"] = rel
        pdfs_made += 1

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
