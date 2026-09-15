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


# ---------------------------------------------------------------------------
# Service prefix — shown in the changelog, but never used as the compare key
# ---------------------------------------------------------------------------


def prefixed_spec(url="https://api.{regionHost}/assembly/v1", paths=None):
    spec = make_spec(paths=paths if paths is not None else copy.deepcopy(BASE_PATHS))
    spec["basePath"] = "/"
    spec["x-servers"] = [{"url": url}]
    return spec


def test_added_endpoint_reports_the_full_request_path():
    old = prefixed_spec()
    new = prefixed_spec()
    new["paths"]["/brandNew"] = {"post": {"summary": "New thing"}}

    changes = diff_single_api(old, new)["changes"]
    added = [c for c in changes if c["kind"] == "endpoint_added"]
    assert [c["path"] for c in added] == ["/assembly/v1/brandNew"]


def test_removed_endpoint_reports_the_full_request_path():
    old = prefixed_spec()
    new = prefixed_spec(paths={})

    changes = diff_single_api(old, new)["changes"]
    removed = [c for c in changes if c["kind"] == "endpoint_removed"]
    assert [c["path"] for c in removed] == ["/assembly/v1/widgets"]


def test_param_change_labels_the_endpoint_with_its_full_path():
    old = prefixed_spec()
    new = prefixed_spec()
    new["paths"]["/widgets"]["get"]["parameters"].append(
        {"name": "limit", "in": "query", "type": "integer", "required": True}
    )

    changes = diff_single_api(old, new)["changes"]
    added = [c for c in changes if c["kind"] == "param_added"]
    assert [c["endpoint"] for c in added] == ["GET /assembly/v1/widgets"]


def test_base_url_move_reports_one_entry_not_every_endpoint():
    """The whole point: moving the base URL must not churn the changelog."""
    old = prefixed_spec()
    new = prefixed_spec(url="https://api.{regionHost}/assembly/v2")

    changes = diff_single_api(old, new)["changes"]

    assert [c["kind"] for c in changes] == ["base_url_changed"]
    assert changes[0]["old_path"] == "/assembly/v1"
    assert changes[0]["new_path"] == "/assembly/v2"
    assert changes[0]["breaking"] is True


def test_base_url_move_does_not_re_key_endpoints():
    old = prefixed_spec()
    new = prefixed_spec(url="https://api.{regionHost}/assembly/v2")

    kinds = [c["kind"] for c in diff_single_api(old, new)["changes"]]
    assert "endpoint_added" not in kinds
    assert "endpoint_removed" not in kinds


def test_unchanged_prefixed_spec_still_produces_no_diff():
    spec = prefixed_spec()
    assert diff_single_api(spec, copy.deepcopy(spec)) is None


def test_segment_moved_out_of_the_server_url_is_not_a_change():
    """The sapdme_nonconformance case: same URLs, different split.

    SAP moved '/v1' from the server URL into the path keys. Every request URL
    stayed the same, so the changelog should stay quiet.
    """
    old = prefixed_spec(
        url="https://api.{regionHost}/nonconformance/v1",
        paths={"/nonconformances": {"get": {"summary": "List"}}},
    )
    new = prefixed_spec(
        url="https://api.{regionHost}/nonconformance",
        paths={"/v1/nonconformances": {"get": {"summary": "List"}}},
    )

    changes = diff_single_api(old, new)["changes"]
    kinds = [c["kind"] for c in changes]
    assert "endpoint_added" not in kinds
    assert "endpoint_removed" not in kinds


def test_segment_shuffle_still_reports_a_genuinely_removed_endpoint():
    old = prefixed_spec(
        url="https://api.{regionHost}/nonconformance/v1",
        paths={
            "/nonconformances": {"get": {"summary": "List"}},
            "/legacy": {"get": {"summary": "Old"}},
        },
    )
    new = prefixed_spec(
        url="https://api.{regionHost}/nonconformance",
        paths={"/v1/nonconformances": {"get": {"summary": "List"}}},
    )

    changes = diff_single_api(old, new)["changes"]
    removed = [c for c in changes if c["kind"] == "endpoint_removed"]
    assert [c["path"] for c in removed] == ["/nonconformance/v1/legacy"]
    assert not [c for c in changes if c["kind"] == "endpoint_added"]


def test_base_url_move_still_compares_endpoint_internals():
    """Rebasing must pair endpoints up, not just silence them."""
    old = prefixed_spec()
    new = prefixed_spec(url="https://api.{regionHost}/assembly/v2")
    new["paths"]["/widgets"]["get"]["parameters"].append(
        {"name": "limit", "in": "query", "type": "integer", "required": True}
    )

    changes = diff_single_api(old, new)["changes"]
    params = [c for c in changes if c["kind"] == "param_added"]
    assert [c["endpoint"] for c in params] == ["GET /assembly/v2/widgets"]
