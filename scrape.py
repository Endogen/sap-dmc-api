#!/usr/bin/env python3
"""
SAP Digital Manufacturing Cloud — REST API Scraper

Logs into api.sap.com, downloads all OpenAPI/Swagger specs for the
SAP Digital Manufacturing Cloud package, and generates summary files.

Re-run at any time to update specs when SAP publishes changes.

Usage:
    python3 scrape.py [--output-dir specs] [--summary]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os

from playwright.sync_api import sync_playwright, Page

# ── Config ──────────────────────────────────────────────────────────

PACKAGE_ID = "SAPDigitalManufacturingCloud"
PACKAGE_URL = f"https://api.sap.com/package/{PACKAGE_ID}/rest"

# Credentials — set via environment or .env file
SAP_USER = os.environ.get("SAP_USER", "")
SAP_PASS = os.environ.get("SAP_PASS", "")
SAP_ACCOUNT = os.environ.get("SAP_ACCOUNT", "")

ARTIFACTS_URL = (
    f"https://api.sap.com/odata/1.0/catalog.svc/"
    f"ContentEntities.ContentPackages('{PACKAGE_ID}')/Artifacts?$format=json"
)
API_META_URL = (
    "https://api.sap.com/odata/1.0/catalog.svc/"
    "APIContent.APIs('{name}')?$select=*&$format=json"
)
API_SPEC_URL = (
    "https://api.sap.com/odata/1.0/catalog.svc/"
    "APIContent.APIs('{name}')/$value?type=json"
)

log = logging.getLogger("sap-scraper")


# ── Auth ────────────────────────────────────────────────────────────

def login(page: Page) -> None:
    """Perform SAP ID login and account selection."""
    log.info("Navigating to %s", PACKAGE_URL)
    page.goto(PACKAGE_URL, timeout=30_000)
    page.wait_for_timeout(3000)

    if "accounts.sap.com" not in page.url:
        log.info("Already logged in")
        return

    log.info("Logging in as %s", SAP_USER)

    # Email
    email = page.wait_for_selector(
        "input[type='email'], #j_username", timeout=10_000
    )
    email.fill(SAP_USER)
    page.query_selector("button[type='submit'], #logOnFormSubmit").click()
    page.wait_for_timeout(3000)

    # Password
    pw = page.wait_for_selector("input[type='password']", timeout=10_000)
    pw.fill(SAP_PASS)
    page.query_selector("button[type='submit'], #logOnFormSubmit").click()
    page.wait_for_timeout(5000)

    # Account selection
    acct = page.query_selector(f"text={SAP_ACCOUNT}")
    if acct:
        log.info("Selecting account %s", SAP_ACCOUNT)
        acct.click()
        try:
            page.wait_for_url("**/api.sap.com/**", timeout=30_000)
        except Exception:
            pass
        page.wait_for_timeout(5000)

    log.info("Login complete — URL: %s", page.url)


def fetch_json(page: Page, url: str) -> Any:
    """Fetch JSON via the browser's authenticated session."""
    result = page.evaluate(
        """async (url) => {
            const resp = await fetch(url);
            if (!resp.ok) return {__error: resp.status, __url: url};
            const text = await resp.text();
            try { return JSON.parse(text); }
            catch { return {__raw: text.substring(0, 500), __url: url}; }
        }""",
        url,
    )
    if isinstance(result, dict) and "__error" in result:
        raise RuntimeError(f"HTTP {result['__error']} for {result['__url']}")
    return result


# ── Scraping ────────────────────────────────────────────────────────

def fetch_artifact_list(page: Page) -> list[dict]:
    """Get all API artifacts from the package."""
    log.info("Fetching artifact list...")
    data = fetch_json(page, ARTIFACTS_URL)
    artifacts = data.get("d", {}).get("results", [])
    apis = [a for a in artifacts if a.get("Type") == "API"]
    log.info("Found %d API artifacts", len(apis))
    return apis


def fetch_api_metadata(page: Page, name: str) -> dict:
    """Fetch API metadata from OData catalog."""
    url = API_META_URL.format(name=name)
    data = fetch_json(page, url)
    return data.get("d", data)


def fetch_api_spec(page: Page, name: str) -> dict | None:
    """Fetch the OpenAPI/Swagger spec for an API."""
    url = API_SPEC_URL.format(name=name)
    try:
        spec = fetch_json(page, url)
        if isinstance(spec, dict) and ("paths" in spec or "swagger" in spec or "openapi" in spec):
            return spec
        log.warning("Spec for %s doesn't look like OpenAPI: %s", name, list(spec.keys())[:5] if isinstance(spec, dict) else type(spec))
        return spec
    except Exception as e:
        log.error("Failed to fetch spec for %s: %s", name, e)
        return None


# ── Output ──────────────────────────────────────────────────────────

def save_specs(
    artifacts: list[dict],
    specs: dict[str, dict | None],
    metadata: dict[str, dict],
    output_dir: Path,
) -> None:
    """Save all specs and metadata to disk."""
    specs_dir = output_dir / "specs"
    meta_dir = output_dir / "metadata"
    specs_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    for artifact in artifacts:
        name = artifact["Name"]

        # Save spec
        spec = specs.get(name)
        if spec:
            path = specs_dir / f"{name}.json"
            path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

        # Save metadata
        meta = metadata.get(name)
        if meta:
            path = meta_dir / f"{name}.json"
            path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # Save artifact index
    index_path = output_dir / "artifacts.json"
    index_path.write_text(json.dumps(artifacts, indent=2), encoding="utf-8")

    log.info("Saved %d specs to %s", sum(1 for s in specs.values() if s), specs_dir)


def generate_summary(
    artifacts: list[dict],
    specs: dict[str, dict | None],
    metadata: dict[str, dict],
    output_dir: Path,
) -> None:
    """Generate human-readable summary files."""
    summary: list[dict[str, Any]] = []

    for artifact in sorted(artifacts, key=lambda a: a.get("DisplayName", a["Name"])):
        name = artifact["Name"]
        spec = specs.get(name)
        meta = metadata.get(name)

        entry: dict[str, Any] = {
            "name": name,
            "display_name": artifact.get("DisplayName", name),
            "description": artifact.get("Description", ""),
            "version": artifact.get("Version", ""),
            "status": meta.get("RegistrationStatus", "") if meta else "",
        }

        if spec:
            info = spec.get("info", {})
            paths = spec.get("paths", {})
            definitions = spec.get("definitions", spec.get("components", {}).get("schemas", {}))

            entry["spec_version"] = spec.get("openapi", spec.get("swagger", ""))
            entry["api_version"] = info.get("version", "")
            entry["base_path"] = spec.get("basePath", "")
            entry["host"] = spec.get("host", "")
            entry["endpoint_count"] = len(paths)
            entry["schema_count"] = len(definitions)

            # Enumerate endpoints
            endpoints = []
            for path, methods in paths.items():
                for method, details in methods.items():
                    if method.startswith("x-") or method == "parameters":
                        continue
                    endpoints.append({
                        "method": method.upper(),
                        "path": path,
                        "summary": details.get("summary", ""),
                        "operation_id": details.get("operationId", ""),
                        "tags": details.get("tags", []),
                    })
            entry["endpoints"] = endpoints
        else:
            entry["spec_version"] = None
            entry["endpoint_count"] = 0
            entry["schema_count"] = 0
            entry["endpoints"] = []

        summary.append(entry)

    # Save summary JSON
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Generate markdown overview
    md_lines = [
        f"# SAP Digital Manufacturing Cloud — REST API Reference",
        f"",
        f"*Scraped: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*",
        f"",
        f"**Package:** [{PACKAGE_ID}]({PACKAGE_URL})",
        f"",
        f"**Total APIs:** {len(summary)}",
        f"**Total Endpoints:** {sum(e['endpoint_count'] for e in summary)}",
        f"**Total Schemas:** {sum(e['schema_count'] for e in summary)}",
        f"",
        f"---",
        f"",
    ]

    # Table of contents
    md_lines.append("## APIs\n")
    md_lines.append("| # | API | Endpoints | Schemas | Version | Status |")
    md_lines.append("|---|-----|-----------|---------|---------|--------|")
    for i, entry in enumerate(summary, 1):
        status = entry.get("status", "")
        md_lines.append(
            f"| {i} | [{entry['display_name']}](specs/{entry['name']}.json) "
            f"| {entry['endpoint_count']} | {entry['schema_count']} "
            f"| {entry.get('api_version', '')} | {status} |"
        )

    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")

    # Detailed endpoint listing
    md_lines.append("## Endpoint Details\n")
    for entry in summary:
        if not entry["endpoints"]:
            continue
        md_lines.append(f"### {entry['display_name']}")
        md_lines.append(f"")
        md_lines.append(f"**Slug:** `{entry['name']}`")
        if entry.get("base_path"):
            md_lines.append(f"**Base Path:** `{entry['base_path']}`")
        if entry.get("description"):
            md_lines.append(f"**Description:** {entry['description']}")
        md_lines.append(f"")
        md_lines.append(f"| Method | Path | Summary |")
        md_lines.append(f"|--------|------|---------|")
        for ep in entry["endpoints"]:
            md_lines.append(f"| `{ep['method']}` | `{ep['path']}` | {ep['summary']} |")
        md_lines.append("")

    readme_path = output_dir / "README.md"
    readme_path.write_text("\n".join(md_lines), encoding="utf-8")
    log.info("Generated summary: %s", readme_path)


# ── Main ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape SAP DMC REST APIs")
    parser.add_argument("--output-dir", default="output", help="Output directory")
    parser.add_argument("--summary-only", action="store_true", help="Regenerate summary from existing specs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    output_dir = Path(args.output_dir)

    if not args.summary_only and (not SAP_USER or not SAP_PASS or not SAP_ACCOUNT):
        log.error("Set SAP_USER, SAP_PASS, SAP_ACCOUNT env vars (or use .env file)")
        sys.exit(1)

    if args.summary_only:
        # Regenerate from existing data
        artifacts = json.loads((output_dir / "artifacts.json").read_text())
        specs = {}
        metadata = {}
        for f in (output_dir / "specs").glob("*.json"):
            specs[f.stem] = json.loads(f.read_text())
        for f in (output_dir / "metadata").glob("*.json"):
            metadata[f.stem] = json.loads(f.read_text())
        generate_summary(artifacts, specs, metadata, output_dir)
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        # Login
        login(page)

        # Fetch artifact list
        artifacts = fetch_artifact_list(page)

        # Fetch specs and metadata for each API
        specs: dict[str, dict | None] = {}
        metadata: dict[str, dict] = {}
        total = len(artifacts)

        for i, artifact in enumerate(artifacts, 1):
            name = artifact["Name"]
            display = artifact.get("DisplayName", name)
            log.info("[%d/%d] %s (%s)", i, total, display, name)

            # Metadata
            try:
                meta = fetch_api_metadata(page, name)
                metadata[name] = meta
            except Exception as e:
                log.error("  Metadata failed: %s", e)

            # Spec
            spec = fetch_api_spec(page, name)
            specs[name] = spec
            if spec:
                paths = spec.get("paths", {})
                defs = spec.get("definitions", spec.get("components", {}).get("schemas", {}))
                log.info("  → %d endpoints, %d schemas", len(paths), len(defs))
            else:
                log.warning("  → No spec available")

            # Small delay to be polite
            time.sleep(0.3)

        browser.close()

    # Save everything
    save_specs(artifacts, specs, metadata, output_dir)
    generate_summary(artifacts, specs, metadata, output_dir)

    # Print stats
    spec_count = sum(1 for s in specs.values() if s)
    total_endpoints = sum(
        len(s.get("paths", {})) for s in specs.values() if s
    )
    total_schemas = sum(
        len(s.get("definitions", s.get("components", {}).get("schemas", {})))
        for s in specs.values() if s
    )
    log.info("=" * 50)
    log.info("APIs: %d | Specs: %d | Endpoints: %d | Schemas: %d",
             len(artifacts), spec_count, total_endpoints, total_schemas)
    log.info("Output: %s", output_dir.resolve())

    # Diff detection
    try:
        from diff_tracker import (
            load_specs_from_git, load_specs_from_dir, diff_specs,
            save_diff, rebuild_changelog,
        )

        old_specs = load_specs_from_git("HEAD")
        new_specs = load_specs_from_dir(output_dir / "specs")
        diff = diff_specs(old_specs, new_specs)

        if diff:
            history_dir = output_dir / "history"
            history_dir.mkdir(exist_ok=True)
            save_diff(diff, history_dir)
            rebuild_changelog(history_dir, output_dir / "changelog.json")
            log.info("Changes detected: %d APIs affected, %d breaking changes",
                     diff["summary"]["apis_changed"] + diff["summary"]["apis_added"] + diff["summary"]["apis_removed"],
                     diff["summary"]["breaking_changes"])
        else:
            log.info("No spec changes detected")
    except Exception as e:
        log.warning("Diff detection skipped: %s", e)


if __name__ == "__main__":
    main()
