"""Tests for spec_paths — recovering the real request path from a spec."""

from __future__ import annotations

from spec_paths import full_path, join_path, service_prefix

# sapdme_assembly — prefix lives only in the x-servers extension
X_SERVERS_SPEC = {
    "swagger": "2.0",
    "host": "hostname",
    "basePath": "/",
    "x-servers": [{"url": "https://api.{regionHost}/assembly/v1"}],
    "paths": {"/autoAssemble": {"post": {"summary": "Auto assemble"}}},
}


def test_service_prefix_from_x_servers():
    assert service_prefix(X_SERVERS_SPEC) == "/assembly/v1"


def test_service_prefix_from_openapi3_servers():
    spec = {"openapi": "3.0.0", "servers": [{"url": "https://api.{regionHost}/ebr/v1"}]}
    assert service_prefix(spec) == "/ebr/v1"


def test_service_prefix_server_url_wins_over_duplicate_base_path():
    # sapdmi_vishleshki ships basePath '/oee/' alongside an '/oee' server URL —
    # honouring basePath as well would yield '/oee/oee/v1/...'
    spec = {"basePath": "/oee/", "x-servers": [{"url": "https://api.host/oee"}]}
    assert service_prefix(spec) == "/oee"


def test_service_prefix_falls_back_to_base_path():
    assert service_prefix({"swagger": "2.0", "basePath": "/v1"}) == "/v1"


def test_service_prefix_ignores_placeholder_base_path():
    assert service_prefix({"swagger": "2.0", "host": "hostname", "basePath": "/"}) == ""


def test_service_prefix_empty_when_nothing_to_prepend():
    # sapdme_quantityConfirmation — prefix already spelled out in the path keys
    spec = {"basePath": "/", "x-servers": [{"url": "https://api.{regionHost}"}]}
    assert service_prefix(spec) == ""


def test_service_prefix_missing_fields():
    assert service_prefix({}) == ""
    assert service_prefix({"servers": [{}]}) == ""


def test_service_prefix_adds_leading_slash():
    assert service_prefix({"basePath": "v1"}) == "/v1"


def test_join_path_prepends_prefix():
    assert join_path("/assembly/v1", "/autoAssemble") == "/assembly/v1/autoAssemble"


def test_join_path_without_prefix_is_identity():
    assert join_path("", "/autoAssemble") == "/autoAssemble"


def test_join_path_does_not_double_an_existing_prefix():
    # sappqm_aiml_scenarios mixes relative and already-prefixed path keys
    assert join_path("/aiml/v1", "/aiml/v1/inspectionLogsForContext") == (
        "/aiml/v1/inspectionLogsForContext"
    )
    assert join_path("/aiml/v1", "/aiml/v1") == "/aiml/v1"


def test_join_path_prefix_match_must_be_on_a_segment_boundary():
    # '/assembly/v1beta' does not sit under the '/assembly/v1' prefix
    assert join_path("/assembly/v1", "/v1beta/x") == "/assembly/v1/v1beta/x"


def test_full_path_combines_lookup_and_join():
    assert full_path(X_SERVERS_SPEC, "/autoAssemble") == "/assembly/v1/autoAssemble"
