"""Tests for diff_tracker — structural spec comparison and breaking-change flags."""
from __future__ import annotations

import copy

from diff_tracker import diff_single_api, diff_specs


def make_spec(paths=None, definitions=None, version="1.0", host="api.example.com"):
    return {
        "swagger": "2.0",
        "info": {"title": "Test API", "version": version},
        "host": host,
        "paths": paths or {},
        "definitions": definitions or {},
    }


BASE_PATHS = {
    "/widgets": {
        "get": {
            "summary": "List widgets",
            "parameters": [{"name": "plant", "in": "query", "type": "string"}],
        },
    },
}

BASE_DEFS = {
    "Widget": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer"},
        },
        "required": ["name"],
    },
}


def change_kinds(diff, api="api1"):
    for entry in diff["changes"]:
        if entry["api"] == api:
            return {c["kind"]: c for c in entry.get("changes", [])}
    return {}


def test_identical_specs_produce_no_diff():
    spec = make_spec(BASE_PATHS, BASE_DEFS)
    assert diff_specs({"api1": spec}, {"api1": copy.deepcopy(spec)}) is None


def test_ignored_top_level_fields_do_not_diff():
    old = make_spec(BASE_PATHS, BASE_DEFS, host="old.example.com")
    new = make_spec(BASE_PATHS, BASE_DEFS, host="new.example.com")
    assert diff_specs({"api1": old}, {"api1": new}) is None


def test_endpoint_added_is_not_breaking():
    old = make_spec(BASE_PATHS, BASE_DEFS)
    new = make_spec(copy.deepcopy(BASE_PATHS), BASE_DEFS)
    new["paths"]["/widgets"]["post"] = {"summary": "Create widget"}

    diff = diff_specs({"api1": old}, {"api1": new})
    kinds = change_kinds(diff)
    assert kinds["endpoint_added"]["breaking"] is False
    assert kinds["endpoint_added"]["method"] == "POST"
    assert diff["summary"]["endpoints_added"] == 1
    assert diff["summary"]["breaking_changes"] == 0


def test_endpoint_removed_is_breaking():
    old = make_spec(copy.deepcopy(BASE_PATHS), BASE_DEFS)
    old["paths"]["/widgets"]["delete"] = {"summary": "Delete widget"}
    new = make_spec(BASE_PATHS, BASE_DEFS)

    diff = diff_specs({"api1": old}, {"api1": new})
    kinds = change_kinds(diff)
    assert kinds["endpoint_removed"]["breaking"] is True
    assert diff["summary"]["endpoints_removed"] == 1
    assert diff["summary"]["breaking_changes"] == 1


def test_required_param_added_is_breaking():
    old = make_spec(copy.deepcopy(BASE_PATHS), BASE_DEFS)
    new = make_spec(copy.deepcopy(BASE_PATHS), BASE_DEFS)
    new["paths"]["/widgets"]["get"]["parameters"].append(
        {"name": "site", "in": "query", "type": "string", "required": True}
    )

    diff = diff_specs({"api1": old}, {"api1": new})
    kinds = change_kinds(diff)
    assert kinds["param_added"]["param"] == "site"
    assert kinds["param_added"]["breaking"] is True


def test_schema_field_removed_is_breaking():
    old = make_spec(BASE_PATHS, copy.deepcopy(BASE_DEFS))
    new = make_spec(BASE_PATHS, copy.deepcopy(BASE_DEFS))
    del new["definitions"]["Widget"]["properties"]["count"]

    diff = diff_specs({"api1": old}, {"api1": new})
    kinds = change_kinds(diff)
    assert kinds["schema_field_removed"]["field"] == "count"
    assert kinds["schema_field_removed"]["breaking"] is True


def test_schema_field_made_required_is_breaking():
    old = make_spec(BASE_PATHS, copy.deepcopy(BASE_DEFS))
    new = make_spec(BASE_PATHS, copy.deepcopy(BASE_DEFS))
    new["definitions"]["Widget"]["required"] = ["name", "count"]

    diff = diff_specs({"api1": old}, {"api1": new})
    kinds = change_kinds(diff)
    assert kinds["schema_field_required"]["field"] == "count"
    assert kinds["schema_field_required"]["breaking"] is True


def test_schema_field_type_change_is_breaking():
    old = make_spec(BASE_PATHS, copy.deepcopy(BASE_DEFS))
    new = make_spec(BASE_PATHS, copy.deepcopy(BASE_DEFS))
    new["definitions"]["Widget"]["properties"]["count"] = {"type": "string"}

    diff = diff_specs({"api1": old}, {"api1": new})
    kinds = change_kinds(diff)
    assert kinds["schema_type_changed"]["old_type"] == "integer"
    assert kinds["schema_type_changed"]["new_type"] == "string"
    assert kinds["schema_type_changed"]["breaking"] is True


def test_version_change_is_not_breaking():
    old = make_spec(BASE_PATHS, BASE_DEFS, version="Beta")
    new = make_spec(BASE_PATHS, BASE_DEFS, version="v1")

    result = diff_single_api(old, new)
    kinds = {c["kind"]: c for c in result["changes"]}
    assert kinds["version_changed"]["old_version"] == "Beta"
    assert kinds["version_changed"]["new_version"] == "v1"
    assert kinds["version_changed"]["breaking"] is False


def test_api_added_and_removed():
    spec = make_spec(BASE_PATHS, BASE_DEFS)

    diff = diff_specs({}, {"api1": spec})
    assert diff["summary"]["apis_added"] == 1
    assert diff["summary"]["breaking_changes"] == 0

    diff = diff_specs({"api1": spec}, {})
    assert diff["summary"]["apis_removed"] == 1
    assert diff["summary"]["breaking_changes"] == 1


def test_request_body_field_via_ref():
    """Body schema changes behind a $ref are detected on the endpoint."""
    paths = {
        "/widgets": {
            "post": {
                "summary": "Create widget",
                "parameters": [
                    {"name": "body", "in": "body", "schema": {"$ref": "#/definitions/Widget"}}
                ],
            },
        },
    }
    old = make_spec(copy.deepcopy(paths), copy.deepcopy(BASE_DEFS))
    new = make_spec(copy.deepcopy(paths), copy.deepcopy(BASE_DEFS))
    new["definitions"]["Widget"]["properties"]["color"] = {"type": "string"}

    diff = diff_specs({"api1": old}, {"api1": new})
    kinds = change_kinds(diff)
    assert kinds["request_field_added"]["field"] == "color"
    assert kinds["request_field_added"]["breaking"] is False
