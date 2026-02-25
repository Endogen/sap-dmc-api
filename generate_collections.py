#!/usr/bin/env python3
"""Generate Postman and Insomnia collections from SAP DMC API specs."""
from __future__ import annotations

import json
import uuid
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPECS_DIR = ROOT / "output" / "specs"
SUMMARY_PATH = ROOT / "output" / "summary.json"
OUTPUT_DIR = ROOT / "output" / "collections"


# ---------------------------------------------------------------------------
# Schema → example value helpers
# ---------------------------------------------------------------------------

def example_for_type(prop_type: str, prop_format: str | None = None) -> object:
    """Return a sensible default value for a JSON Schema primitive type."""
    if prop_type == "string":
        if prop_format == "date-time":
            return "2024-01-01T00:00:00Z"
        if prop_format == "date":
            return "2024-01-01"
        return "string"
    if prop_type == "integer":
        return 0
    if prop_type == "number":
        return 0.0
    if prop_type == "boolean":
        return True
    if prop_type == "array":
        return []
    if prop_type == "object":
        return {}
    return None


def resolve_ref(ref: str, definitions: dict) -> dict | None:
    """Resolve a $ref like '#/definitions/Foo' into its schema dict."""
    parts = ref.lstrip("#/").split("/")
    node = definitions
    for p in parts:
        node = node.get(p) if isinstance(node, dict) else None
        if node is None:
            return None
    return node


def schema_to_example(schema: dict, definitions: dict, depth: int = 0) -> object:
    """Build a minimal example JSON body from a JSON Schema definition.

    Only includes required fields. Caps recursion at depth 3.
    """
    if depth > 3:
        return {}

    if "$ref" in schema:
        resolved = resolve_ref(schema["$ref"], definitions)
        if resolved is None:
            return {}
        return schema_to_example(resolved, definitions, depth)

    schema_type = schema.get("type", "object")

    if schema_type == "array":
        items = schema.get("items", {})
        return [schema_to_example(items, definitions, depth + 1)]

    if schema_type != "object":
        return example_for_type(schema_type, schema.get("format"))

    # Object — include required fields only
    props = schema.get("properties", {})
    required = set(schema.get("required", []))

    # If no required fields declared, include all properties (common in simple schemas)
    keys = required if required else set(props.keys())

    result = {}
    for key in sorted(keys):
        if key not in props:
            result[key] = "string"
            continue
        result[key] = schema_to_example(props[key], definitions, depth + 1)
    return result


# ---------------------------------------------------------------------------
# Read specs & summary
# ---------------------------------------------------------------------------

def load_specs() -> list[tuple[dict, dict]]:
    """Return list of (summary_entry, spec) pairs sorted by display name."""
    summary = json.loads(SUMMARY_PATH.read_text())
    pairs = []
    for api in summary:
        spec_path = SPECS_DIR / f"{api['name']}.json"
        if not spec_path.exists():
            continue
        spec = json.loads(spec_path.read_text())
        pairs.append((api, spec))
    pairs.sort(key=lambda p: p[0].get("display_name", ""))
    return pairs


def get_definitions(spec: dict) -> dict:
    """Get the definitions/schemas dict from a spec (Swagger 2.0 or OAS 3)."""
    if "definitions" in spec:
        return spec
    if "components" in spec and "schemas" in spec["components"]:
        # Wrap so $ref resolution works with '#/components/schemas/X'
        return spec
    return spec


def get_base_url(spec: dict) -> str:
    """Extract a usable base URL template from a spec."""
    # Swagger 2.0
    if spec.get("swagger") == "2.0":
        host = spec.get("host", "hostname")
        base_path = spec.get("basePath", "/").rstrip("/")
        schemes = spec.get("schemes", ["https"])
        scheme = schemes[0] if schemes else "https"
        return f"{scheme}://{host}{base_path}"
    # OpenAPI 3.x
    servers = spec.get("servers", [])
    if servers:
        return servers[0].get("url", "{{base_url}}")
    return "{{base_url}}"


def extract_body_example(operation: dict, definitions: dict) -> dict | None:
    """Extract a request body example from an operation."""
    # Swagger 2.0 — body parameter
    for param in operation.get("parameters", []):
        if param.get("in") == "body" and "schema" in param:
            return schema_to_example(param["schema"], definitions)

    # OpenAPI 3.x — requestBody
    req_body = operation.get("requestBody", {})
    content = req_body.get("content", {})
    for ct in ("application/json", "application/merge-patch+json"):
        if ct in content and "schema" in content[ct]:
            return schema_to_example(content[ct]["schema"], definitions)
    return None


def extract_query_params(operation: dict) -> list[dict]:
    """Extract query parameters from an operation."""
    params = []
    for p in operation.get("parameters", []):
        if p.get("in") == "query":
            params.append({
                "key": p.get("name", ""),
                "value": "",
                "description": p.get("description", ""),
                "disabled": not p.get("required", False),
            })
    return params


def extract_header_params(operation: dict) -> list[dict]:
    """Extract header parameters from an operation."""
    headers = []
    for p in operation.get("parameters", []):
        if p.get("in") == "header":
            headers.append({
                "key": p.get("name", ""),
                "value": "",
                "description": p.get("description", ""),
            })
    return headers


# ---------------------------------------------------------------------------
# Postman Collection (v2.1)
# ---------------------------------------------------------------------------

def build_postman(specs: list[tuple[dict, dict]]) -> dict:
    collection: dict = {
        "info": {
            "name": "SAP Digital Manufacturing Cloud APIs",
            "description": "Auto-generated collection of all SAP DMC REST APIs.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [
            {"key": "base_url", "value": "https://test.eu10.dmc.cloud.sap", "type": "string"},
            {"key": "token", "value": "", "type": "string"},
        ],
        "item": [],
    }

    for api_info, spec in specs:
        definitions = get_definitions(spec)
        base_url_raw = get_base_url(spec)

        folder: dict = {
            "name": api_info["display_name"],
            "description": api_info.get("description", ""),
            "item": [],
        }

        paths = spec.get("paths", {})
        for path, path_obj in paths.items():
            for method in ("get", "post", "put", "patch", "delete", "head", "options"):
                if method not in path_obj:
                    continue
                op = path_obj[method]
                summary = op.get("summary", "") or op.get("description", "") or f"{method.upper()} {path}"

                # Build URL
                url_raw = "{{base_url}}" + path
                # Split into host/path parts for Postman format
                path_parts = [p for p in path.split("/") if p]

                request: dict = {
                    "method": method.upper(),
                    "header": [
                        {"key": "Content-Type", "value": "application/json"},
                        {"key": "Authorization", "value": "Bearer {{token}}"},
                    ],
                    "url": {
                        "raw": url_raw,
                        "host": ["{{base_url}}"],
                        "path": path_parts,
                    },
                    "description": op.get("description", summary),
                }

                # Extra headers from spec
                for h in extract_header_params(op):
                    request["header"].append({
                        "key": h["key"],
                        "value": h["value"],
                        "description": h["description"],
                    })

                # Query params
                query_params = extract_query_params(op)
                if query_params:
                    request["url"]["query"] = query_params

                # Body
                body = extract_body_example(op, definitions)
                if body is not None and method in ("post", "put", "patch"):
                    request["body"] = {
                        "mode": "raw",
                        "raw": json.dumps(body, indent=2),
                        "options": {"raw": {"language": "json"}},
                    }

                folder["item"].append({
                    "name": summary,
                    "request": request,
                })

        collection["item"].append(folder)

    return collection


# ---------------------------------------------------------------------------
# Insomnia Export (v4)
# ---------------------------------------------------------------------------

def _insomnia_id() -> str:
    return f"req_{uuid.uuid4().hex[:24]}"


def build_insomnia(specs: list[tuple[dict, dict]]) -> dict:
    resources: list[dict] = []

    # Workspace
    ws_id = _insomnia_id()
    resources.append({
        "_id": ws_id,
        "_type": "workspace",
        "name": "SAP Digital Manufacturing Cloud APIs",
        "description": "Auto-generated workspace of all SAP DMC REST APIs.",
        "scope": "collection",
        "parentId": None,
    })

    # Base environment
    env_id = _insomnia_id()
    resources.append({
        "_id": env_id,
        "_type": "environment",
        "name": "Base Environment",
        "parentId": ws_id,
        "data": {
            "base_url": "https://test.eu10.dmc.cloud.sap",
            "token": "",
        },
    })

    for api_info, spec in specs:
        definitions = get_definitions(spec)

        # Folder per API
        folder_id = _insomnia_id()
        resources.append({
            "_id": folder_id,
            "_type": "request_group",
            "name": api_info["display_name"],
            "description": api_info.get("description", ""),
            "parentId": ws_id,
        })

        paths = spec.get("paths", {})
        for path, path_obj in paths.items():
            for method in ("get", "post", "put", "patch", "delete", "head", "options"):
                if method not in path_obj:
                    continue
                op = path_obj[method]
                summary = op.get("summary", "") or op.get("description", "") or f"{method.upper()} {path}"

                url = "{{ _.base_url }}" + path

                req: dict = {
                    "_id": _insomnia_id(),
                    "_type": "request",
                    "name": summary,
                    "description": op.get("description", summary),
                    "method": method.upper(),
                    "url": url,
                    "parentId": folder_id,
                    "headers": [
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": "Authorization", "value": "Bearer {{ _.token }}"},
                    ],
                    "parameters": [],
                    "authentication": {},
                }

                # Extra headers
                for h in extract_header_params(op):
                    req["headers"].append({
                        "name": h["key"],
                        "value": h["value"],
                        "description": h["description"],
                    })

                # Query params
                for p in extract_query_params(op):
                    req["parameters"].append({
                        "name": p["key"],
                        "value": p["value"],
                        "description": p["description"],
                        "disabled": p["disabled"],
                    })

                # Body
                body = extract_body_example(op, definitions)
                if body is not None and method in ("post", "put", "patch"):
                    req["body"] = {
                        "mimeType": "application/json",
                        "text": json.dumps(body, indent=2),
                    }

                resources.append(req)

    return {
        "_type": "export",
        "__export_format": 4,
        "__export_source": "sap-dmc-api-generator",
        "resources": resources,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not SPECS_DIR.exists() or not SUMMARY_PATH.exists():
        print("Error: output/specs/ or output/summary.json not found.", file=sys.stderr)
        print("Run scrape.py first, or check the output directory.", file=sys.stderr)
        sys.exit(1)

    print("Loading specs...")
    specs = load_specs()
    print(f"  {len(specs)} APIs loaded\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Postman
    print("Generating Postman collection...")
    postman = build_postman(specs)
    postman_path = OUTPUT_DIR / "postman_collection.json"
    postman_path.write_text(json.dumps(postman, indent=2))
    req_count = sum(len(f["item"]) for f in postman["item"])
    print(f"  {postman_path}  ({len(postman['item'])} folders, {req_count} requests)")

    # Insomnia
    print("Generating Insomnia collection...")
    insomnia = build_insomnia(specs)
    insomnia_path = OUTPUT_DIR / "insomnia_collection.json"
    insomnia_path.write_text(json.dumps(insomnia, indent=2))
    req_count_i = sum(1 for r in insomnia["resources"] if r["_type"] == "request")
    folder_count_i = sum(1 for r in insomnia["resources"] if r["_type"] == "request_group")
    print(f"  {insomnia_path}  ({folder_count_i} folders, {req_count_i} requests)")

    print("\nDone.")


if __name__ == "__main__":
    main()
