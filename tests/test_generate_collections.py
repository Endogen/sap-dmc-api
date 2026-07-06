"""Tests for generate_collections — example bodies and collection structure."""
from __future__ import annotations

import json

from generate_collections import build_insomnia, build_postman, schema_to_example

SPEC = {
    "swagger": "2.0",
    "info": {"title": "Test API", "version": "1.0"},
    "host": "api.example.com",
    "basePath": "/v1",
    "paths": {
        "/widgets": {
            "get": {
                "summary": "List widgets",
                "parameters": [
                    {"name": "plant", "in": "query", "type": "string", "required": True},
                    {"name": "limit", "in": "query", "type": "integer"},
                ],
            },
            "post": {
                "summary": "Create widget",
                "parameters": [
                    {"name": "X-Trace", "in": "header", "type": "string"},
                    {"name": "body", "in": "body", "schema": {"$ref": "#/definitions/Widget"}},
                ],
            },
        },
    },
    "definitions": {
        "Widget": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "created": {"type": "string", "format": "date-time"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
            },
            "required": ["name", "count", "created", "tags"],
        },
    },
}

API_INFO = {"name": "test_api", "display_name": "Test API", "description": "A test API"}


def test_schema_to_example_resolves_ref_and_required_only():
    example = schema_to_example({"$ref": "#/definitions/Widget"}, SPEC)
    assert example == {
        "count": 0,
        "created": "2024-01-01T00:00:00Z",
        "name": "string",
        "tags": ["string"],
    }
    assert "note" not in example  # optional fields excluded


def test_schema_to_example_includes_all_props_when_no_required():
    schema = {"type": "object", "properties": {"a": {"type": "boolean"}}}
    assert schema_to_example(schema, SPEC) == {"a": True}


def test_schema_to_example_broken_ref_returns_empty():
    assert schema_to_example({"$ref": "#/definitions/Missing"}, SPEC) == {}


def test_build_postman_structure():
    collection = build_postman([(API_INFO, SPEC)])

    assert [f["name"] for f in collection["item"]] == ["Test API"]
    requests = collection["item"][0]["item"]
    assert len(requests) == 2

    by_method = {r["request"]["method"]: r["request"] for r in requests}

    # GET carries query params; required ones enabled
    query = {q["key"]: q for q in by_method["GET"]["url"]["query"]}
    assert query["plant"]["disabled"] is False
    assert query["limit"]["disabled"] is True

    # POST carries an example body built from the schema plus spec headers
    body = json.loads(by_method["POST"]["body"]["raw"])
    assert body["name"] == "string"
    header_keys = [h["key"] for h in by_method["POST"]["header"]]
    assert "Authorization" in header_keys
    assert "X-Trace" in header_keys


def test_build_insomnia_structure():
    export = build_insomnia([(API_INFO, SPEC)])
    by_type: dict[str, list] = {}
    for r in export["resources"]:
        by_type.setdefault(r["_type"], []).append(r)

    assert len(by_type["workspace"]) == 1
    assert len(by_type["environment"]) == 1
    assert len(by_type["request_group"]) == 1
    assert len(by_type["request"]) == 2

    post = next(r for r in by_type["request"] if r["method"] == "POST")
    assert json.loads(post["body"]["text"])["name"] == "string"
    # requests hang off the folder, folder off the workspace
    assert post["parentId"] == by_type["request_group"][0]["_id"]
    assert by_type["request_group"][0]["parentId"] == by_type["workspace"][0]["_id"]
