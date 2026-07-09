"""Tests for scrape.py helpers that don't need a browser."""
from __future__ import annotations

import pytest

import scrape
from scrape import count_operations, looks_like_auth_html, open_package


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


class FakeTimeoutError(Exception):
    pass


class FakePage:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.url = ""
        self.goto_calls = []
        self.load_state_calls = []
        self.waits = []

    def goto(self, url, *, timeout, wait_until):
        self.goto_calls.append(
            {"url": url, "timeout": timeout, "wait_until": wait_until}
        )
        outcome = self._outcomes.pop(0)
        self.url = outcome["url"]
        if outcome["type"] == "timeout":
            raise FakeTimeoutError("timed out")
        return None

    def wait_for_load_state(self, state, timeout):
        self.load_state_calls.append({"state": state, "timeout": timeout})

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_open_package_tolerates_timeout_after_reaching_login(monkeypatch):
    monkeypatch.setattr(scrape, "PlaywrightTimeoutError", FakeTimeoutError)
    page = FakePage(
        [{"type": "timeout", "url": "https://accounts.sap.com/oauth2/authorize"}]
    )

    open_package(page)

    assert len(page.goto_calls) == 1
    assert page.goto_calls[0]["wait_until"] == "domcontentloaded"
    assert page.waits == [1000]


def test_open_package_retries_after_timeout_without_reaching_target(monkeypatch):
    monkeypatch.setattr(scrape, "PlaywrightTimeoutError", FakeTimeoutError)
    page = FakePage(
        [
            {"type": "timeout", "url": "about:blank"},
            {"type": "ok", "url": "https://api.sap.com/package/SAPDigitalManufacturingCloud/rest"},
        ]
    )

    open_package(page)

    assert len(page.goto_calls) == 2
    assert page.waits[0] == 2000
    assert page.waits[-1] == 3000


def test_open_package_raises_after_repeated_unreachable_timeouts(monkeypatch):
    monkeypatch.setattr(scrape, "PlaywrightTimeoutError", FakeTimeoutError)
    page = FakePage(
        [
            {"type": "timeout", "url": "about:blank"},
            {"type": "timeout", "url": "about:blank"},
        ]
    )

    with pytest.raises(FakeTimeoutError):
        open_package(page)
