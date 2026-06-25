# AGENTS.md

## What This Repo Is

Website mirror tool for `atinph.org`. Single Python script crawls the Squarespace-based site, rewrites URLs to relative paths, and outputs a static `site-mirror/` folder.

## Run

```bash
python mirror_site.py
```

No dependencies beyond Python 3 stdlib. No tests, no build, no lint.

## Key Facts

- `ROOT_URL` in script = crawl entry point. Change to mirror a different site.
- `ALLOWED_PAGE_HOSTS` / `ALLOWED_ASSET_HOSTS` whitelist what gets fetched. Squarespace CDN hosts hardcoded.
- Output goes to `site-mirror/` (deleted and recreated on each run).
- Failures logged to `site-mirror/mirror-failures.txt`.
- Script fetches HTML first (pages), then discovered assets (CSS, JS, images, fonts).
- CSS `url()` and `@import` references get rewritten to local asset paths.
- HTML attributes `href`, `src`, `data-src`, `data-image`, `poster`, `srcset`, inline `style` all get rewritten.
