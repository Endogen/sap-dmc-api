"""Tests for scrape.py helpers that don't need a browser."""
from __future__ import annotations

from scrape import count_operations, looks_like_auth_html


def test_count_operations_skips_extensions_and_parameters():
    spec = {
        "paths": {
            "/a": {
                "get": {},
                "post": {},
                "parameters": [{"name": "id", "in": "path"}],
                "x-sap-ext": {},
            },
            "/b": {"delete": {}},
        }
    }
    assert count_operations(spec) == 3


def test_count_operations_empty_spec():
    assert count_operations({}) == 0


def test_looks_like_auth_html_detects_login_page():
    assert looks_like_auth_html(
        {"__raw": "<html><body>Log On</body></html>", "__content_type": "text/html",
         "__final_url": "https://accounts.sap.com/saml2/idp/sso"}
    )


def test_looks_like_auth_html_ignores_real_payload():
    assert not looks_like_auth_html({"d": {"results": []}})
