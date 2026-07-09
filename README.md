# SAP Digital Manufacturing Cloud — API Scraper

Scrapes all REST API specs from the [SAP Digital Manufacturing Cloud](https://api.sap.com/package/SAPDigitalManufacturingCloud/rest) package on SAP Business Accelerator Hub.

Downloads complete Swagger 2.0 / OpenAPI specs for every API, including all endpoints, request/response schemas, and metadata.

**Browse the APIs online:** https://endogen.github.io/sap-dmc-api/ — search, Swagger UI, changelog, and collection downloads, rebuilt automatically after every scrape.

## What's Included

```
output/
  README.md          # Full API reference (table of all APIs + endpoints)
  summary.json       # Machine-readable index of all APIs & endpoints
  artifacts.json     # Raw artifact catalog from SAP
  changelog.json     # Combined changelog of all detected spec changes
  history/           # One diff report per scrape run that found changes
  specs/             # 88 Swagger JSON files (one per API)
  metadata/          # 88 API metadata files from OData catalog
  collections/       # Postman & Insomnia collection files
```

**Current stats:** 88 APIs · 477 endpoints · 1,281 schemas

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

# Abort without saving if fewer than N specs were fetched
# (guards against half-failed sessions; used by CI)
python3 scrape.py --min-specs 80

# Regenerate summary/README/collections from existing specs
# (no login or Playwright needed)
python3 scrape.py --summary-only
```

A full scrape takes about 50 seconds. The scraper:
1. Logs into api.sap.com via Playwright (headless Chromium)
2. Fetches the artifact list from the OData catalog API
3. Downloads the Swagger spec + metadata for each API (and prunes specs for APIs SAP removed)
4. Generates `summary.json`, a markdown reference (`output/README.md`), and Postman/Insomnia collections
5. Diffs against the last committed specs and updates `output/changelog.json`

On a failed login the scraper saves `login_failure.png` showing what the browser saw.

## Automation (GitHub Actions + Pages)

Everything also runs unattended on GitHub — no local machine needed:

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `scrape.yml` | daily at 05:17 UTC + manual | Scrapes SAP, commits changed specs/changelog to `main`, triggers the Pages deploy |
| `deploy-pages.yml` | push to `main` (output/templates/static) + manual | Assembles the static site and publishes it to GitHub Pages |
| `test.yml` | push / PR | Runs the pytest suite |

The live site is at **https://endogen.github.io/sap-dmc-api/**.

Required repository secrets (Settings → Secrets and variables → Actions): `SAP_USER`, `SAP_PASS`, `SAP_ACCOUNT`.

The scrape job runs with `--min-specs 80`, so a half-failed SAP session aborts instead of committing a gutted dataset. If the login fails, the run uploads a `login-failure` artifact with a screenshot of what SAP showed the headless browser.

## Browsing APIs

The same interface that runs on GitHub Pages can be served locally — no build step or extra dependencies required.

```bash
python3 serve.py
# Opens http://127.0.0.1:8080
```

The landing page lists all APIs with search/filter, endpoint counts, and links to view each spec in Swagger UI or download the raw JSON.

The server binds to localhost by default; use the `HOST`/`PORT` environment variables to change that:

```bash
PORT=9090 python3 serve.py
HOST=0.0.0.0 python3 serve.py   # expose on the network
```

## Postman & Insomnia Collections

Collections are regenerated automatically at the end of every scrape. To rebuild them manually from existing specs:

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

The daily GitHub Actions run takes care of this, but a manual run works the same way: `scrape.py` overwrites all specs, regenerates summary and collections, and records any changes in `output/changelog.json`. Commit the diff to keep the history.

```bash
python3 scrape.py
git diff --stat output/
git add -A && git commit -m "Update specs $(date +%Y-%m-%d)"
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Covers the diff tracker (change detection and breaking-change flags), collection generation, and the web server's routing/path-traversal protection.
