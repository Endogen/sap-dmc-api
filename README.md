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
  collections/       # Postman & Insomnia collection files
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

## Browsing APIs

A local web interface lets you browse and explore all API specs with Swagger UI — no build step or extra dependencies required.

```bash
python3 serve.py
# Opens http://localhost:8080
```

The landing page lists all APIs with search/filter, endpoint counts, and links to view each spec in Swagger UI or download the raw JSON.

Set a custom port with the `PORT` environment variable:

```bash
PORT=9090 python3 serve.py
```

## Postman & Insomnia Collections

Generate importable collections for Postman and Insomnia from the downloaded specs:

```bash
python3 generate_collections.py
```

This creates two files in `output/collections/`:

| File | Format | Import via |
|------|--------|------------|
| `postman_collection.json` | Postman v2.1 | Postman → Import → File |
| `insomnia_collection.json` | Insomnia Export v4 | Insomnia → Import → From File |

Both collections include:
- One folder per API, one request per endpoint
- Method, path, description, and request body examples (generated from schemas)
- `{{base_url}}` / `{{token}}` variables for host and auth configuration

## Re-scraping

Just run `python3 scrape.py` again. It overwrites all specs and regenerates the summary. Commit the diff to track what changed.

```bash
python3 scrape.py
git diff --stat output/
git add -A && git commit -m "Update specs $(date +%Y-%m-%d)"
```
