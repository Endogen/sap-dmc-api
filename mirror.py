#!/usr/bin/env python3
"""
SAP Digital Manufacturing Cloud — REST API Mirror

Checks the public SAP catalog for changes, then logs into api.sap.com only
when protected OpenAPI/Swagger specifications need to be updated.

Re-run at any time to mirror changes published by SAP.

Usage:
    python3 mirror.py [--output-dir output] [--summary-only] [--min-specs N]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

if TYPE_CHECKING:
    from playwright.sync_api import APIRequestContext, Page

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover - used by credential-free installations

    class PlaywrightTimeoutError(Exception):
        """Fallback timeout error used when Playwright is not installed."""

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

ARTIFACT_CHANGE_FIELDS = (
    "Version",
    "State",
    "ModifiedAt",
    "SubType",
    "DisplayName",
    "Description",
    "reg_id",
)
CHECK_CHANGES_EXIT_CODE = 3
HTTP_TIMEOUT_SECONDS = 30
NAVIGATION_TIMEOUT_MS = 45_000
POST_NAVIGATION_PAUSE_MS = 3_000
USER_AGENT = "sap-dmc-api-mirror/1.0"

log = logging.getLogger("sap-mirror")


class SessionAuthError(RuntimeError):
    """Raised when SAP returns auth HTML instead of the requested JSON."""


class HttpStatusError(RuntimeError):
    """Raised when SAP returns a non-authentication HTTP error."""

    def __init__(self, status: int, url: str):
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} for {url}")


AUTH_HTML_MARKERS = (
    "accounts.sap.com",
    "oauth",
    "saml",
    "log on",
    "signin",
    "<html",
    "<!doctype html",
)


# ── Auth ────────────────────────────────────────────────────────────


def _click_submit(page: Page) -> None:
    button = page.wait_for_selector(
        "button[type='submit'], #logOnFormSubmit", timeout=10_000
    )
    button.click()


def is_api_url(url: str) -> bool:
    """Return whether a URL is on the authenticated API Hub origin."""
    return urlparse(url).hostname == "api.sap.com"


def _url_indicates_reachable_login(url: str) -> bool:
    """Return whether navigation reached SAP API Hub or its login service."""
    hostname = urlparse(url).hostname or ""
    return (
        is_api_url(url)
        or hostname == "accounts.sap.com"
        or ".authentication." in hostname
    )


def _settle_page(page: Page, pause_ms: int = POST_NAVIGATION_PAUSE_MS) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass
    page.wait_for_timeout(pause_ms)


def open_protected_resource(
    page: Page,
    protected_url: str,
    *,
    max_attempts: int = 2,
) -> None:
    """Open a protected SAP resource while tolerating slow login redirects."""
    for attempt in range(1, max_attempts + 1):
        try:
            page.goto(
                protected_url,
                timeout=NAVIGATION_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            _settle_page(page)
            return
        except PlaywrightTimeoutError:
            current_url = page.url or ""
            if _url_indicates_reachable_login(current_url):
                log.warning(
                    "Timed out opening %s, but reached %s; continuing",
                    protected_url,
                    current_url,
                )
                _settle_page(page, pause_ms=1_000)
                return
            if attempt == max_attempts:
                raise
            log.warning(
                "Timed out opening %s on attempt %d/%d (current URL: %s); retrying",
                protected_url,
                attempt,
                max_attempts,
                current_url or "<unknown>",
            )
            page.wait_for_timeout(2_000)


def login(page: Page, protected_url: str) -> None:
    """Perform SAP ID login and account selection."""
    log.info("Opening protected SAP resource to establish a session")
    open_protected_resource(page, protected_url)

    if is_api_url(page.url):
        log.info("Already logged in")
        return

    log.info("Logging in as %s", SAP_USER)

    # Email
    email = page.wait_for_selector("input[type='email'], #j_username", timeout=10_000)
    email.fill(SAP_USER)
    _click_submit(page)
    page.wait_for_timeout(3000)

    # Password
    pw = page.wait_for_selector("input[type='password']", timeout=10_000)
    pw.fill(SAP_PASS)
    _click_submit(page)
    page.wait_for_timeout(5000)

    # Account selection
    acct = page.query_selector(f"text={SAP_ACCOUNT}")
    if acct:
        log.info("Selecting account %s", SAP_ACCOUNT)
        acct.click()

    if not is_api_url(page.url):
        try:
            page.wait_for_url("**/api.sap.com/**", timeout=30_000)
        except Exception:
            pass
    page.wait_for_timeout(2000)

    if not is_api_url(page.url):
        raise SessionAuthError(
            f"SAP login did not return to api.sap.com (URL: {page.url})"
        )

    log.info("Login complete — URL: %s", page.url)


def refresh_session(page: Page, protected_url: str) -> None:
    """Reopen a protected resource to refresh the authenticated browser session."""
    log.info("Refreshing SAP session")
    login(page, protected_url)


def looks_like_auth_html(result: Any) -> bool:
    """Detect SAP login/redirect HTML returned in place of JSON."""
    if not isinstance(result, dict) or "__raw" not in result:
        return False

    raw = str(result.get("__raw", ""))
    content_type = str(result.get("__content_type", "")).lower()
    final_url = str(result.get("__final_url", "")).lower()
    text = f"{content_type}\n{final_url}\n{raw[:1000]}".lower()
    return any(marker in text for marker in AUTH_HTML_MARKERS)


def fetch_public_json(url: str) -> Any:
    """Fetch JSON from an endpoint that does not require a SAP session."""
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read().decode(
                response.headers.get_content_charset() or "utf-8"
            )
            content_type = response.headers.get("content-type", "")
            final_url = response.geturl()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        result = {
            "__raw": raw[:1000],
            "__url": url,
            "__final_url": final_url,
            "__content_type": content_type,
        }
        if looks_like_auth_html(result):
            raise SessionAuthError(
                f"SAP unexpectedly required login for public endpoint {url} "
                f"(final_url={final_url})"
            ) from exc
        raise RuntimeError(f"SAP returned non-JSON content for {url}") from exc


def fetch_authenticated_json(
    request: APIRequestContext,
    page: Page,
    url: str,
    *,
    max_attempts: int = 2,
) -> Any:
    """Fetch JSON with Playwright's cookie-sharing HTTP request context."""
    for attempt in range(1, max_attempts + 1):
        response = request.get(
            url,
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT_SECONDS * 1000,
            fail_on_status_code=False,
        )
        raw = response.text()
        try:
            result: Any = json.loads(raw)
        except json.JSONDecodeError:
            result = {
                "__raw": raw[:1000],
                "__url": url,
                "__final_url": response.url,
                "__content_type": response.headers.get("content-type", ""),
            }
        if not response.ok:
            result = {
                "__error": response.status,
                "__url": url,
                "__final_url": response.url,
                "__content_type": response.headers.get("content-type", ""),
                "__raw": raw[:1000],
            }

        if isinstance(result, dict) and "__error" in result:
            is_auth_error = result["__error"] in {401, 403} or looks_like_auth_html(
                result
            )
            if is_auth_error and attempt < max_attempts:
                log.warning(
                    "Auth failed for %s on attempt %d/%d, re-authenticating",
                    url,
                    attempt,
                    max_attempts,
                )
                refresh_session(page, url)
                continue
            if is_auth_error:
                raise SessionAuthError(
                    f"SAP returned auth/redirect content for {url} "
                    f"(status {result['__error']}, final_url={result.get('__final_url')})"
                )
            raise HttpStatusError(
                result["__error"],
                result.get("__final_url", result["__url"]),
            )

        if looks_like_auth_html(result):
            if attempt < max_attempts:
                log.warning(
                    "Got auth HTML instead of JSON for %s on attempt %d/%d, re-authenticating",
                    url,
                    attempt,
                    max_attempts,
                )
                refresh_session(page, url)
                continue
            raise SessionAuthError(
                f"SAP returned login/redirect HTML for {url} "
                f"(final_url={result.get('__final_url')})"
            )

        return result

    raise RuntimeError(f"Exhausted retries fetching {url}")


# ── Mirroring ───────────────────────────────────────────────────────


def fetch_artifact_list() -> list[dict]:
    """Get all API artifacts from the package."""
    log.info("Fetching artifact list...")
    data = fetch_public_json(ARTIFACTS_URL)
    artifacts = data.get("d", {}).get("results", [])
    apis = [a for a in artifacts if a.get("Type") == "API"]
    log.info("Found %d API artifacts", len(apis))
    return apis


def fetch_api_metadata(name: str) -> dict:
    """Fetch API metadata from OData catalog."""
    url = API_META_URL.format(name=name)
    data = fetch_public_json(url)
    return data.get("d", data)


def fetch_api_spec(
    request: APIRequestContext,
    page: Page,
    name: str,
) -> dict | None:
    """Fetch the OpenAPI/Swagger spec for an API."""
    url = API_SPEC_URL.format(name=name)
    try:
        spec = fetch_authenticated_json(request, page, url)
    except HttpStatusError as exc:
        if exc.status in {404, 410}:
            log.warning("Specification for %s is no longer available", name)
            return None
        raise
    if isinstance(spec, dict) and (
        "paths" in spec or "swagger" in spec or "openapi" in spec
    ):
        return spec
    keys = list(spec.keys())[:5] if isinstance(spec, dict) else type(spec)
    raise ValueError(f"Spec for {name} does not look like OpenAPI: {keys}")


def load_json_directory(directory: Path) -> dict[str, dict]:
    """Load a directory of JSON objects keyed by filename stem."""
    if not directory.is_dir():
        return {}
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in directory.glob("*.json")
    }


def load_existing_mirror(
    output_dir: Path,
) -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
    """Load the previous artifact index, specs, and metadata if present."""
    artifacts_path = output_dir / "artifacts.json"
    artifacts = (
        json.loads(artifacts_path.read_text(encoding="utf-8"))
        if artifacts_path.is_file()
        else []
    )
    specs = load_json_directory(output_dir / "specs")
    metadata = load_json_directory(output_dir / "metadata")
    return artifacts, specs, metadata


def plan_mirror(
    artifacts: list[dict],
    previous_artifacts: list[dict],
    output_dir: Path,
    *,
    force: bool = False,
) -> tuple[list[str], list[str]]:
    """Return API names that need downloading and names that were removed."""
    current = {artifact["Name"]: artifact for artifact in artifacts}
    previous = {artifact["Name"]: artifact for artifact in previous_artifacts}
    updates: set[str] = set()

    for name, artifact in current.items():
        old = previous.get(name)
        changed = old is None or any(
            artifact.get(field) != old.get(field) for field in ARTIFACT_CHANGE_FIELDS
        )
        missing_files = (
            not (output_dir / "specs" / f"{name}.json").is_file()
            or not (output_dir / "metadata" / f"{name}.json").is_file()
        )
        if force or changed or missing_files:
            updates.add(name)

    removals = set(previous) - set(current)
    return sorted(updates), sorted(removals)


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
        path = specs_dir / f"{name}.json"
        spec = specs.get(name)
        if spec:
            path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        elif name in specs and path.is_file():
            path.unlink()
            log.info("Removed unavailable specification %s", path)

        # Save metadata
        meta = metadata.get(name)
        if meta:
            path = meta_dir / f"{name}.json"
            path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # Delete files for APIs no longer in SAP's artifact list — otherwise
    # removed APIs linger on disk and never show up in the diff tracker.
    current = {a["Name"] for a in artifacts}
    for directory in (specs_dir, meta_dir):
        for f in directory.glob("*.json"):
            if f.stem not in current:
                f.unlink()
                log.info("Pruned stale file %s (API no longer published)", f)

    # Save artifact index
    index_path = output_dir / "artifacts.json"
    index_path.write_text(json.dumps(artifacts, indent=2), encoding="utf-8")

    log.info("Mirrored %d specs to %s", sum(1 for s in specs.values() if s), specs_dir)


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
            definitions = spec.get(
                "definitions", spec.get("components", {}).get("schemas", {})
            )

            entry["spec_version"] = spec.get("openapi", spec.get("swagger", ""))
            entry["api_version"] = info.get("version", "")
            entry["base_path"] = spec.get("basePath", "")
            entry["host"] = spec.get("host", "")

            # Enumerate endpoints (method-level operations)
            endpoints = []
            for path, methods in paths.items():
                for method, details in methods.items():
                    if method.startswith("x-") or method == "parameters":
                        continue
                    endpoints.append(
                        {
                            "method": method.upper(),
                            "path": path,
                            "summary": details.get("summary", ""),
                            "operation_id": details.get("operationId", ""),
                            "tags": details.get("tags", []),
                        }
                    )
            entry["endpoints"] = endpoints
            entry["endpoint_count"] = len(endpoints)
            entry["path_count"] = len(paths)
            entry["schema_count"] = len(definitions)
        else:
            entry["spec_version"] = None
            entry["endpoint_count"] = 0
            entry["path_count"] = 0
            entry["schema_count"] = 0
            entry["endpoints"] = []

        summary.append(entry)

    # Save summary JSON
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    total_endpoints = sum(e["endpoint_count"] for e in summary)
    total_schemas = sum(e["schema_count"] for e in summary)

    # Generate markdown overview
    md_lines = [
        "# SAP Digital Manufacturing Cloud — REST API Reference",
        "",
        f"*Mirrored: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
        f"**Package:** [{PACKAGE_ID}]({PACKAGE_URL})",
        "",
        f"**Total APIs:** {len(summary)}",
        f"**Total Endpoints:** {total_endpoints}",
        f"**Total Schemas:** {total_schemas}",
        "",
        "---",
        "",
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
        md_lines.append("")
        md_lines.append(f"**Slug:** `{entry['name']}`")
        if entry.get("base_path"):
            md_lines.append(f"**Base Path:** `{entry['base_path']}`")
        if entry.get("description"):
            md_lines.append(f"**Description:** {entry['description']}")
        md_lines.append("")
        md_lines.append("| Method | Path | Summary |")
        md_lines.append("|--------|------|---------|")
        for ep in entry["endpoints"]:
            md_lines.append(f"| `{ep['method']}` | `{ep['path']}` | {ep['summary']} |")
        md_lines.append("")

    readme_path = output_dir / "README.md"
    readme_path.write_text("\n".join(md_lines), encoding="utf-8")
    log.info("Generated summary: %s", readme_path)

    _update_repo_readme_stats(len(summary), total_endpoints, total_schemas)


def _update_repo_readme_stats(
    api_count: int, endpoint_count: int, schema_count: int
) -> None:
    """Rewrite the stats line in the repository README so it doesn't drift."""
    readme = Path(__file__).resolve().parent / "README.md"
    if not readme.is_file():
        return
    lines = readme.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("**Current stats:**"):
            lines[i] = (
                f"**Current stats:** {api_count} APIs · "
                f"{endpoint_count} endpoints · {schema_count:,} schemas"
            )
            readme.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("Updated stats line in %s", readme)
            return


def generate_collection_files(output_dir: Path) -> None:
    """Regenerate Postman/Insomnia collections so they never go stale."""
    try:
        from generate_collections import generate

        generate(output_dir)
    except Exception as e:
        log.warning("Collection generation failed: %s", e)


# ── Main ────────────────────────────────────────────────────────────


def count_operations(spec: dict) -> int:
    """Count method-level operations (path + HTTP method pairs) in a spec."""
    count = 0
    for methods in spec.get("paths", {}).values():
        if not isinstance(methods, dict):
            continue
        for method in methods:
            if method.startswith("x-") or method == "parameters":
                continue
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror SAP DMC REST APIs")
    parser.add_argument("--output-dir", default="output", help="Output directory")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Regenerate summary from existing specs",
    )
    parser.add_argument(
        "--min-specs",
        type=int,
        default=0,
        help="Abort without saving if the resulting mirror has fewer specs",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Check the public catalog without Playwright; exits 3 when a mirror "
            "run is needed"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Download every specification even if the catalog is unchanged",
    )
    args = parser.parse_args()

    if args.summary_only and (args.check or args.force):
        parser.error("--summary-only cannot be combined with --check or --force")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    output_dir = Path(args.output_dir)

    if args.summary_only:
        artifacts, specs, metadata = load_existing_mirror(output_dir)
        generate_summary(artifacts, specs, metadata, output_dir)
        generate_collection_files(output_dir)
        return 0

    artifacts = fetch_artifact_list()
    previous_artifacts, specs, metadata = load_existing_mirror(output_dir)
    update_names, removed_names = plan_mirror(
        artifacts,
        previous_artifacts,
        output_dir,
        force=args.force,
    )
    log.info(
        "Mirror plan: %d API(s) to update, %d API(s) to remove",
        len(update_names),
        len(removed_names),
    )

    if args.check:
        return CHECK_CHANGES_EXIT_CODE if update_names or removed_names else 0

    if not update_names and not removed_names:
        log.info("Catalog unchanged — mirror is current; browser not needed")
        return 0

    if update_names and (not SAP_USER or not SAP_PASS or not SAP_ACCOUNT):
        log.error(
            "Catalog changes require SAP_USER, SAP_PASS, and SAP_ACCOUNT (or use .env)"
        )
        return 1

    metadata_updates: dict[str, dict] = {}
    for i, name in enumerate(update_names, 1):
        log.info("[%d/%d] Fetching public metadata for %s", i, len(update_names), name)
        metadata_updates[name] = fetch_api_metadata(name)

    spec_updates: dict[str, dict | None] = {}
    if update_names:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            page = context.new_page()

            try:
                login(page, API_SPEC_URL.format(name=update_names[0]))
                request = context.request
                artifacts_by_name = {
                    artifact["Name"]: artifact for artifact in artifacts
                }
                for i, name in enumerate(update_names, 1):
                    artifact = artifacts_by_name[name]
                    display = artifact.get("DisplayName", name)
                    log.info("[%d/%d] %s (%s)", i, len(update_names), display, name)
                    spec = fetch_api_spec(request, page, name)
                    spec_updates[name] = spec
                    if spec:
                        log.info(
                            "  → %d endpoints, %d schemas",
                            count_operations(spec),
                            len(
                                spec.get(
                                    "definitions",
                                    spec.get("components", {}).get("schemas", {}),
                                )
                            ),
                        )
                    time.sleep(0.3)
            except Exception:
                try:
                    page.screenshot(path="login_failure.png", full_page=True)
                    log.error(
                        "Saved failure screenshot to login_failure.png (URL: %s)",
                        page.url,
                    )
                except Exception:
                    pass
                raise
            finally:
                browser.close()

    specs.update(spec_updates)
    metadata.update(metadata_updates)
    current_names = {artifact["Name"] for artifact in artifacts}
    specs = {name: spec for name, spec in specs.items() if name in current_names}
    metadata = {name: meta for name, meta in metadata.items() if name in current_names}

    spec_count = sum(1 for name in current_names if specs.get(name))
    if args.min_specs and spec_count < args.min_specs:
        log.error(
            "Resulting mirror has %d specs but --min-specs=%d — aborting "
            "without saving",
            spec_count,
            args.min_specs,
        )
        return 2

    save_specs(artifacts, specs, metadata, output_dir)
    generate_summary(artifacts, specs, metadata, output_dir)
    generate_collection_files(output_dir)

    # Print stats
    total_endpoints = sum(count_operations(s) for s in specs.values() if s)
    total_schemas = sum(
        len(s.get("definitions", s.get("components", {}).get("schemas", {})))
        for s in specs.values()
        if s
    )
    log.info("=" * 50)
    log.info(
        "APIs: %d | Specs: %d | Endpoints: %d | Schemas: %d",
        len(artifacts),
        spec_count,
        total_endpoints,
        total_schemas,
    )
    log.info("Output: %s", output_dir.resolve())

    # Diff detection
    try:
        from diff_tracker import (
            load_specs_from_git,
            load_specs_from_dir,
            diff_specs,
            save_diff,
            rebuild_changelog,
        )

        old_specs = load_specs_from_git("HEAD")
        new_specs = load_specs_from_dir(output_dir / "specs")
        diff = diff_specs(old_specs, new_specs)

        if diff:
            history_dir = output_dir / "history"
            history_dir.mkdir(exist_ok=True)
            save_diff(diff, history_dir)
            rebuild_changelog(history_dir, output_dir / "changelog.json")
            log.info(
                "Changes detected: %d APIs affected, %d breaking changes",
                diff["summary"]["apis_changed"]
                + diff["summary"]["apis_added"]
                + diff["summary"]["apis_removed"],
                diff["summary"]["breaking_changes"],
            )
        else:
            log.info("No spec changes detected")
    except Exception as e:
        log.warning("Diff detection skipped: %s", e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
