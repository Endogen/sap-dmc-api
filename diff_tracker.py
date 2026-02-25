#!/usr/bin/env python3
"""
API Diff Tracker — Detect structural changes between SAP DMC API scrape runs.

Uses git history to compare the current specs against the last committed version.
Generates per-run diff reports and a combined changelog.

Usage:
    python3 diff_tracker.py                  # Compare current specs against HEAD
    python3 diff_tracker.py --ref HEAD~3     # Compare against a specific git ref
    python3 diff_tracker.py --rebuild-changelog  # Rebuild changelog from history
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("diff-tracker")

SPECS_SUBDIR = "output/specs"
HISTORY_SUBDIR = "output/history"
CHANGELOG_PATH = "output/changelog.json"

# Fields to ignore when comparing specs
IGNORE_TOP_LEVEL = {"host", "externalDocs", "servers"}
IGNORE_PREFIXES = ("x-sap-",)

# Maximum depth for recursive schema comparison
MAX_DEPTH = 5


# ── Git helpers ──────────────────────────────────────────────────────

def _git_available() -> bool:
    """Check if git is available and we're in a repo."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, check=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _git_show(ref: str, path: str) -> str | None:
    """Get file contents from a git ref. Returns None if not found."""
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _git_ls_tree(ref: str, path: str) -> list[str]:
    """List files in a directory at a git ref."""
    try:
        result = subprocess.run(
            ["git", "ls-tree", "--name-only", ref, f"{path}/"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []


# ── Spec loading ─────────────────────────────────────────────────────

def load_specs_from_git(ref: str = "HEAD") -> dict[str, dict]:
    """Load all specs from a git ref using git show."""
    specs: dict[str, dict] = {}
    files = _git_ls_tree(ref, SPECS_SUBDIR)
    for filepath in files:
        if not filepath.endswith(".json"):
            continue
        name = Path(filepath).stem
        content = _git_show(ref, filepath)
        if content:
            try:
                specs[name] = json.loads(content)
            except json.JSONDecodeError:
                log.warning("Failed to parse %s from %s", filepath, ref)
    return specs


def load_specs_from_dir(specs_dir: Path) -> dict[str, dict]:
    """Load all specs from the filesystem."""
    specs: dict[str, dict] = {}
    if not specs_dir.is_dir():
        return specs
    for f in sorted(specs_dir.glob("*.json")):
        try:
            specs[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load %s: %s", f, e)
    return specs


# ── Normalization ────────────────────────────────────────────────────

def _normalize_spec(spec: dict) -> dict:
    """Normalize a spec for comparison: remove ignored fields."""
    normalized = {}
    for key, value in spec.items():
        if key in IGNORE_TOP_LEVEL:
            continue
        if any(key.startswith(p) for p in IGNORE_PREFIXES):
            continue
        normalized[key] = value
    return normalized


def _get_definitions(spec: dict) -> dict:
    """Get schema definitions regardless of spec version."""
    return spec.get("definitions", spec.get("components", {}).get("schemas", {}))


def _resolve_ref(ref_str: str, spec: dict) -> dict | None:
    """Resolve a $ref string to the actual schema."""
    if not ref_str.startswith("#/"):
        return None
    parts = ref_str[2:].split("/")
    obj: Any = spec
    for part in parts:
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj if isinstance(obj, dict) else None


def _resolve_schema(schema: dict | None, spec: dict, depth: int = 0) -> dict | None:
    """Resolve a schema, following $ref if present."""
    if schema is None or depth > MAX_DEPTH:
        return schema
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], spec)
        return resolved
    return schema


def _extract_structural_fields(schema: dict | None, spec: dict, depth: int = 0) -> dict:
    """Extract structural information from a schema for comparison."""
    if schema is None or depth > MAX_DEPTH:
        return {}

    schema = _resolve_schema(schema, spec, depth)
    if schema is None:
        return {}

    result: dict[str, Any] = {}
    if "type" in schema:
        result["type"] = schema["type"]
    if "required" in schema:
        result["required"] = sorted(schema["required"]) if isinstance(schema["required"], list) else schema["required"]
    if "enum" in schema:
        result["enum"] = sorted(schema["enum"]) if isinstance(schema["enum"], list) else schema["enum"]

    if "properties" in schema and depth < MAX_DEPTH:
        props = {}
        for prop_name, prop_val in schema["properties"].items():
            props[prop_name] = _extract_structural_fields(prop_val, spec, depth + 1)
        result["properties"] = props

    if "items" in schema and depth < MAX_DEPTH:
        result["items"] = _extract_structural_fields(schema["items"], spec, depth + 1)

    if "additionalProperties" in schema and isinstance(schema["additionalProperties"], dict) and depth < MAX_DEPTH:
        result["additionalProperties"] = _extract_structural_fields(schema["additionalProperties"], spec, depth + 1)

    return result


# ── Endpoint comparison ──────────────────────────────────────────────

def _get_endpoints(spec: dict) -> dict[str, dict]:
    """Extract all endpoints as {METHOD /path: details} dict."""
    endpoints: dict[str, dict] = {}
    for path, methods in spec.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            if method.startswith("x-") or method == "parameters" or not isinstance(details, dict):
                continue
            key = f"{method.upper()} {path}"
            endpoints[key] = details
    return endpoints


def _get_params(endpoint: dict) -> dict[str, dict]:
    """Extract parameters from an endpoint as {name: info} dict."""
    params: dict[str, dict] = {}
    for p in endpoint.get("parameters", []):
        if not isinstance(p, dict):
            continue
        name = p.get("name", "")
        if name:
            params[name] = {
                "in": p.get("in", ""),
                "required": p.get("required", False),
                "type": p.get("type", p.get("schema", {}).get("type", "")),
            }
    return params


def _compare_schemas(old_schema: dict, new_schema: dict, old_spec: dict, new_spec: dict) -> list[dict]:
    """Compare two schemas structurally. Returns list of field-level changes."""
    changes: list[dict] = []
    old_struct = _extract_structural_fields(old_schema, old_spec)
    new_struct = _extract_structural_fields(new_schema, new_spec)

    # Type change
    if old_struct.get("type") != new_struct.get("type") and (old_struct.get("type") or new_struct.get("type")):
        changes.append({
            "kind": "type_changed",
            "old_type": old_struct.get("type", ""),
            "new_type": new_struct.get("type", ""),
        })

    # Compare properties
    old_props = old_struct.get("properties", {})
    new_props = new_struct.get("properties", {})
    old_required = set(old_struct.get("required", []))
    new_required = set(new_struct.get("required", []))

    for field in sorted(set(old_props) | set(new_props)):
        if field in new_props and field not in old_props:
            changes.append({
                "kind": "field_added",
                "field": field,
                "type": new_props[field].get("type", ""),
                "required": field in new_required,
            })
        elif field in old_props and field not in new_props:
            changes.append({
                "kind": "field_removed",
                "field": field,
                "type": old_props[field].get("type", ""),
            })
        elif field in old_props and field in new_props:
            old_type = old_props[field].get("type", "")
            new_type = new_props[field].get("type", "")
            if old_type != new_type and (old_type or new_type):
                changes.append({
                    "kind": "field_type_changed",
                    "field": field,
                    "old_type": old_type,
                    "new_type": new_type,
                })

    # Required changes (without field addition/removal)
    for field in new_required - old_required:
        if field in old_props and field in new_props:
            changes.append({
                "kind": "field_made_required",
                "field": field,
            })

    return changes


# ── Single API diff ──────────────────────────────────────────────────

def diff_single_api(old_spec: dict, new_spec: dict) -> dict | None:
    """Compare two versions of a single API spec. Returns changes or None."""
    old_norm = _normalize_spec(old_spec)
    new_norm = _normalize_spec(new_spec)

    changes: list[dict] = []

    # Version change
    old_version = old_norm.get("info", {}).get("version", "")
    new_version = new_norm.get("info", {}).get("version", "")
    if old_version != new_version:
        changes.append({
            "kind": "version_changed",
            "old_version": old_version,
            "new_version": new_version,
            "breaking": False,
        })

    # Endpoints
    old_endpoints = _get_endpoints(old_norm)
    new_endpoints = _get_endpoints(new_norm)

    for key in sorted(set(old_endpoints) | set(new_endpoints)):
        method, path = key.split(" ", 1)
        if key in new_endpoints and key not in old_endpoints:
            changes.append({
                "kind": "endpoint_added",
                "method": method,
                "path": path,
                "summary": new_endpoints[key].get("summary", ""),
                "breaking": False,
            })
        elif key in old_endpoints and key not in new_endpoints:
            changes.append({
                "kind": "endpoint_removed",
                "method": method,
                "path": path,
                "summary": old_endpoints[key].get("summary", ""),
                "breaking": True,
            })
        else:
            # Endpoint exists in both — compare params and schemas
            old_ep = old_endpoints[key]
            new_ep = new_endpoints[key]
            ep_label = f"{method} {path}"

            # Parameter changes
            old_params = _get_params(old_ep)
            new_params = _get_params(new_ep)
            for pname in sorted(set(old_params) | set(new_params)):
                if pname in new_params and pname not in old_params:
                    is_required = new_params[pname].get("required", False)
                    changes.append({
                        "kind": "param_added",
                        "endpoint": ep_label,
                        "param": pname,
                        "required": is_required,
                        "breaking": is_required,
                    })
                elif pname in old_params and pname not in new_params:
                    changes.append({
                        "kind": "param_removed",
                        "endpoint": ep_label,
                        "param": pname,
                        "breaking": False,
                    })

            # Request body comparison
            old_body = _get_request_body_schema(old_ep, old_spec)
            new_body = _get_request_body_schema(new_ep, new_spec)
            if old_body and new_body:
                body_changes = _compare_schemas(old_body, new_body, old_spec, new_spec)
                for bc in body_changes:
                    if bc["kind"] == "field_added":
                        changes.append({
                            "kind": "request_field_added",
                            "endpoint": ep_label,
                            "field": bc["field"],
                            "type": bc.get("type", ""),
                            "required": bc.get("required", False),
                            "breaking": bc.get("required", False),
                        })
                    elif bc["kind"] == "field_removed":
                        changes.append({
                            "kind": "request_field_removed",
                            "endpoint": ep_label,
                            "field": bc["field"],
                            "breaking": False,
                        })
                    elif bc["kind"] in ("field_type_changed", "type_changed"):
                        changes.append({
                            "kind": "request_type_changed",
                            "endpoint": ep_label,
                            "field": bc.get("field", ""),
                            "old_type": bc.get("old_type", ""),
                            "new_type": bc.get("new_type", ""),
                            "breaking": True,
                        })

            # Response body comparison (use 200/201 success response)
            old_resp = _get_response_schema(old_ep, old_spec)
            new_resp = _get_response_schema(new_ep, new_spec)
            if old_resp and new_resp:
                resp_changes = _compare_schemas(old_resp, new_resp, old_spec, new_spec)
                for rc in resp_changes:
                    if rc["kind"] == "field_added":
                        changes.append({
                            "kind": "response_field_added",
                            "endpoint": ep_label,
                            "field": rc["field"],
                            "type": rc.get("type", ""),
                            "breaking": False,
                        })
                    elif rc["kind"] == "field_removed":
                        changes.append({
                            "kind": "response_field_removed",
                            "endpoint": ep_label,
                            "field": rc["field"],
                            "breaking": True,
                        })
                    elif rc["kind"] in ("field_type_changed", "type_changed"):
                        changes.append({
                            "kind": "response_type_changed",
                            "endpoint": ep_label,
                            "field": rc.get("field", ""),
                            "old_type": rc.get("old_type", ""),
                            "new_type": rc.get("new_type", ""),
                            "breaking": True,
                        })

    # Schema definitions comparison
    old_defs = _get_definitions(old_norm)
    new_defs = _get_definitions(new_norm)

    for schema_name in sorted(set(old_defs) | set(new_defs)):
        if schema_name in new_defs and schema_name not in old_defs:
            changes.append({
                "kind": "schema_added",
                "schema": schema_name,
                "breaking": False,
            })
        elif schema_name in old_defs and schema_name not in new_defs:
            changes.append({
                "kind": "schema_removed",
                "schema": schema_name,
                "breaking": True,
            })
        elif schema_name in old_defs and schema_name in new_defs:
            old_def = old_defs[schema_name]
            new_def = new_defs[schema_name]
            if not isinstance(old_def, dict) or not isinstance(new_def, dict):
                continue
            schema_changes = _compare_schemas(old_def, new_def, old_spec, new_spec)
            for sc in schema_changes:
                if sc["kind"] == "field_added":
                    changes.append({
                        "kind": "schema_field_added",
                        "schema": schema_name,
                        "field": sc["field"],
                        "type": sc.get("type", ""),
                        "required": sc.get("required", False),
                        "breaking": False,
                    })
                elif sc["kind"] == "field_removed":
                    changes.append({
                        "kind": "schema_field_removed",
                        "schema": schema_name,
                        "field": sc["field"],
                        "breaking": True,
                    })
                elif sc["kind"] in ("field_type_changed", "type_changed"):
                    changes.append({
                        "kind": "schema_type_changed",
                        "schema": schema_name,
                        "field": sc.get("field", ""),
                        "old_type": sc.get("old_type", ""),
                        "new_type": sc.get("new_type", ""),
                        "breaking": True,
                    })
                elif sc["kind"] == "field_made_required":
                    changes.append({
                        "kind": "schema_field_required",
                        "schema": schema_name,
                        "field": sc["field"],
                        "breaking": False,
                    })

    return {"changes": changes} if changes else None


def _get_request_body_schema(endpoint: dict, spec: dict) -> dict | None:
    """Extract request body schema from an endpoint (Swagger 2.0 or OpenAPI 3.x)."""
    # Swagger 2.0: body parameter
    for p in endpoint.get("parameters", []):
        if isinstance(p, dict) and p.get("in") == "body" and "schema" in p:
            return _resolve_schema(p["schema"], spec)

    # OpenAPI 3.x: requestBody
    rb = endpoint.get("requestBody", {})
    if isinstance(rb, dict):
        content = rb.get("content", {})
        for media_type in ("application/json", "application/merge-patch+json", "*/*"):
            if media_type in content:
                schema = content[media_type].get("schema")
                if schema:
                    return _resolve_schema(schema, spec)
    return None


def _get_response_schema(endpoint: dict, spec: dict) -> dict | None:
    """Extract success response schema from an endpoint."""
    responses = endpoint.get("responses", {})
    for code in ("200", "201", "202"):
        resp = responses.get(code)
        if not isinstance(resp, dict):
            continue
        # Swagger 2.0
        if "schema" in resp:
            return _resolve_schema(resp["schema"], spec)
        # OpenAPI 3.x
        content = resp.get("content", {})
        for media_type in ("application/json", "*/*"):
            if media_type in content:
                schema = content[media_type].get("schema")
                if schema:
                    return _resolve_schema(schema, spec)
    return None


# ── Multi-API diff ───────────────────────────────────────────────────

def diff_specs(old_specs: dict[str, dict], new_specs: dict[str, dict]) -> dict | None:
    """Compare two sets of specs. Returns diff dict or None if no meaningful changes."""
    api_changes: list[dict] = []

    all_apis = sorted(set(old_specs) | set(new_specs))

    for api_name in all_apis:
        old = old_specs.get(api_name)
        new = new_specs.get(api_name)

        display_name = ""
        if new:
            display_name = new.get("info", {}).get("title", api_name)
        elif old:
            display_name = old.get("info", {}).get("title", api_name)

        if new and not old:
            api_changes.append({
                "api": api_name,
                "display_name": display_name,
                "type": "added",
            })
        elif old and not new:
            api_changes.append({
                "api": api_name,
                "display_name": display_name,
                "type": "removed",
            })
        elif old and new:
            result = diff_single_api(old, new)
            if result and result["changes"]:
                api_changes.append({
                    "api": api_name,
                    "display_name": display_name,
                    "type": "changed",
                    "changes": result["changes"],
                })

    if not api_changes:
        return None

    # Build summary
    summary = {
        "apis_added": sum(1 for c in api_changes if c["type"] == "added"),
        "apis_removed": sum(1 for c in api_changes if c["type"] == "removed"),
        "apis_changed": sum(1 for c in api_changes if c["type"] == "changed"),
        "endpoints_added": 0,
        "endpoints_removed": 0,
        "breaking_changes": 0,
    }

    for ac in api_changes:
        if ac["type"] == "removed":
            summary["breaking_changes"] += 1
        for change in ac.get("changes", []):
            if change["kind"] == "endpoint_added":
                summary["endpoints_added"] += 1
            elif change["kind"] == "endpoint_removed":
                summary["endpoints_removed"] += 1
            if change.get("breaking"):
                summary["breaking_changes"] += 1

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "summary": summary,
        "changes": api_changes,
    }


# ── Save / Changelog ────────────────────────────────────────────────

def save_diff(diff: dict, history_dir: Path) -> Path:
    """Save a diff report to the history directory. Returns the file path."""
    history_dir.mkdir(parents=True, exist_ok=True)
    date_str = diff["date"]
    filepath = history_dir / f"{date_str}.json"

    # If a file already exists for today, append a counter
    if filepath.exists():
        counter = 2
        while True:
            filepath = history_dir / f"{date_str}_{counter}.json"
            if not filepath.exists():
                break
            counter += 1

    filepath.write_text(json.dumps(diff, indent=2), encoding="utf-8")
    log.info("Saved diff to %s", filepath)
    return filepath


def generate_changelog(history_dir: Path) -> list[dict]:
    """Read all history files and combine into a sorted changelog (newest first)."""
    entries: list[dict] = []
    if not history_dir.is_dir():
        return entries
    for f in sorted(history_dir.glob("*.json"), reverse=True):
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
            entries.append(entry)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read history file %s: %s", f, e)
    return entries


def rebuild_changelog(history_dir: Path, changelog_path: Path) -> None:
    """Rebuild the changelog.json from all history files."""
    entries = generate_changelog(history_dir)
    changelog_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    log.info("Rebuilt changelog with %d entries → %s", len(entries), changelog_path)


# ── CLI ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="API Diff Tracker")
    parser.add_argument("--ref", default="HEAD", help="Git ref to compare against (default: HEAD)")
    parser.add_argument("--output-dir", default="output", help="Output directory")
    parser.add_argument("--rebuild-changelog", action="store_true", help="Rebuild changelog from history files")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    output_dir = Path(args.output_dir)
    history_dir = output_dir / "history"
    changelog_path = output_dir / "changelog.json"

    if args.rebuild_changelog:
        rebuild_changelog(history_dir, changelog_path)
        return

    if not _git_available():
        log.error("Git is not available or not in a git repository")
        return

    log.info("Loading specs from git ref '%s'...", args.ref)
    old_specs = load_specs_from_git(args.ref)
    log.info("Loaded %d specs from %s", len(old_specs), args.ref)

    specs_dir = output_dir / "specs"
    log.info("Loading specs from %s...", specs_dir)
    new_specs = load_specs_from_dir(specs_dir)
    log.info("Loaded %d specs from filesystem", len(new_specs))

    diff = diff_specs(old_specs, new_specs)

    if diff:
        history_dir.mkdir(parents=True, exist_ok=True)
        save_diff(diff, history_dir)
        rebuild_changelog(history_dir, changelog_path)

        s = diff["summary"]
        total = s["apis_added"] + s["apis_removed"] + s["apis_changed"]
        log.info(
            "Changes detected: %d APIs affected, %d breaking changes",
            total, s["breaking_changes"],
        )
        log.info("  Added: %d APIs, %d endpoints", s["apis_added"], s["endpoints_added"])
        log.info("  Removed: %d APIs, %d endpoints", s["apis_removed"], s["endpoints_removed"])
        log.info("  Changed: %d APIs", s["apis_changed"])
    else:
        log.info("No spec changes detected")


if __name__ == "__main__":
    main()
