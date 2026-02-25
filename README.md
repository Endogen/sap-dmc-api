# SAP Digital Manufacturing Cloud — API Scraper

Scrapes all REST API specs from the [SAP Digital Manufacturing Cloud](https://api.sap.com/package/SAPDigitalManufacturingCloud/rest) package on SAP Business Accelerator Hub.

Downloads complete Swagger 2.0 / OpenAPI specs for every API, including all endpoints, request/response schemas, and metadata.

## What's Included

```
output/
  README.md          # Full API reference (table of all APIs + endpoints)
  summary.json       # Machine-readable index of all APIs & endpoints
  artifacts.json     # Raw artifact catalog from SAP
  specs/             # 88 Swagger JSON files (one per API)
  metadata/          # 88 API metadata files from OData catalog
```

**Current stats:** 88 APIs · 344 endpoints · 1,288 schemas

## Setup

Requires Python 3.10+ and a Chromium browser (installed by Playwright).

```bash
# Install dependencies
pip install -r requirements.txt

# Install browser for Playwright
playwright install chromium

# Configure credentials
cp .env.example .env
# Edit .env with your SAP credentials
```

### Credentials

You need an SAP account with access to the SAP Digital Manufacturing Cloud package. Set these in `.env`:

```
SAP_USER=your.email@company.com
SAP_PASS=your-password
SAP_ACCOUNT=S00xxxxxxxx
```

The account ID (`SAP_ACCOUNT`) is the S-number shown on the SAP ID account selection screen after login.

## Scraping

```bash
# Full scrape (login + download all 88 API specs)
python3 scrape.py

# Custom output directory
python3 scrape.py --output-dir my-output

# Regenerate summary/README from existing specs (no login needed)
python3 scrape.py --summary-only
```

A full scrape takes about 50 seconds. The scraper:
1. Logs into api.sap.com via Playwright (headless Chromium)
2. Fetches the artifact list from the OData catalog API
3. Downloads the Swagger spec + metadata for each API
4. Generates `summary.json` and a markdown reference (`output/README.md`)

## Re-scraping

Just run `python3 scrape.py` again. It overwrites all specs and regenerates the summary. Commit the diff to track what changed.

```bash
python3 scrape.py
git diff --stat output/
git add -A && git commit -m "Update specs $(date +%Y-%m-%d)"
```
