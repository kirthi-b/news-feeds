# News Feeds

This is a small, self-hosted tool I built to track news by **defined groups of keywords**, not by outlet or general topic. It’s meant to replicate the parts of Feedly / RSS.app, without accounts, ads, or manual curation.

It runs entirely on **GitHub Pages + GitHub Actions**. Anyone with the link can view it.

---

## What it does

- Pulls Google News RSS results based on **keywords defined in config/bundles.md**
- Groups those keywords into **project bundles**
- Deduplicates articles across runs
- Keeps a **rolling 3-month window**
- Publishes a searchable, filterable page
- Auto-updates **every day**

---

## How it’s structured

bundles.md  
→ define projects + keywords  

scripts/build.py  
→ fetches RSS, dedupes, writes data.json  

docs/  
├─ index.html — UI shell  
├─ styles.css — styles  
├─ app.js — UI logic  
└─ data.json — generated feed data  

.github/workflows/build.yml  
→ scheduled job  

GitHub Pages serves the `/docs` folder.

---

## Updates & data behavior

- The workflow runs **every day**
- Each article is hashed so the same story isn’t added twice
- Anything older than **90 days** is dropped on each run
- `docs/data.json` is regenerated cleanly every time

---

## What this is *not*

- A Feedly replacement for casual reading
- A scraper of full article content
- A long-term archive
- A real-time news tool

It’s a **project signal tracker** for custom use. 
