"""Tests for mirror.py behavior that does not need a browser."""

from __future__ import annotations

import json
import sys
import types

import mirror
import pytest
from mirror import (
    CHECK_CHANGES_EXIT_CODE,
    HttpStatusError,
    count_operations,
    fetch_api_metadata,
    fetch_api_spec,
    fetch_authenticated_json,
    is_api_url,
    looks_like_auth_html,
    open_protected_resource,
    plan_mirror,
    save_specs,
)


def artifact(name="example", **overrides):
    value = {
        "Name": name,
        "Type": "API",
        "Version": "v1",
        "State": "ACTIVE",
        "ModifiedAt": "/Date(1)/",
        "SubType": "REST",
    }
    value.update(overrides)
    return value


def write_mirror_files(output_dir, names):
    for child in ("specs", "metadata"):
        directory = output_dir / child
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / f"{name}.json").write_text("{}", encoding="utf-8")


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
        {
            "__raw": "<html><body>Log On</body></html>",
            "__content_type": "text/html",
            "__final_url": "https://accounts.sap.com/saml2/idp/sso",
        }
    )


def test_looks_like_auth_html_ignores_real_payload():
    assert not looks_like_auth_html({"d": {"results": []}})


def test_is_api_url_rejects_auth_and_lookalike_hosts():
    assert is_api_url("https://api.sap.com/odata/1.0/catalog.svc/")
    assert not is_api_url("https://accounts.sap.com/login")
    assert not is_api_url("https://api.sap.com.example.org/")


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

    def wait_for_load_state(self, state, timeout):
        self.load_state_calls.append({"state": state, "timeout": timeout})

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


def test_open_resource_tolerates_timeout_after_reaching_login(monkeypatch):
    monkeypatch.setattr(mirror, "PlaywrightTimeoutError", FakeTimeoutError)
    page = FakePage(
        [{"type": "timeout", "url": "https://accounts.sap.com/oauth2/authorize"}]
    )

    open_protected_resource(page, "https://api.sap.com/protected")

    assert len(page.goto_calls) == 1
    assert page.goto_calls[0]["wait_until"] == "domcontentloaded"
    assert page.waits == [1000]


def test_open_resource_tolerates_sap_authentication_host(monkeypatch):
    monkeypatch.setattr(mirror, "PlaywrightTimeoutError", FakeTimeoutError)
    page = FakePage(
        [
            {
                "type": "timeout",
                "url": "https://sappubliccatalog.authentication.eu10.hana.ondemand.com/oauth/authorize",
            }
        ]
    )

    open_protected_resource(page, "https://api.sap.com/protected")

    assert page.waits == [1000]


def test_open_resource_retries_after_unreachable_timeout(monkeypatch):
    monkeypatch.setattr(mirror, "PlaywrightTimeoutError", FakeTimeoutError)
    page = FakePage(
        [
            {"type": "timeout", "url": "about:blank"},
            {"type": "ok", "url": "https://api.sap.com/protected"},
        ]
    )

    open_protected_resource(page, "https://api.sap.com/protected")

    assert len(page.goto_calls) == 2
    assert page.waits[0] == 2000
    assert page.waits[-1] == 3000


def test_open_resource_raises_after_repeated_unreachable_timeouts(monkeypatch):
    monkeypatch.setattr(mirror, "PlaywrightTimeoutError", FakeTimeoutError)
    page = FakePage(
        [
            {"type": "timeout", "url": "about:blank"},
            {"type": "timeout", "url": "about:blank"},
        ]
    )

    with pytest.raises(FakeTimeoutError):
        open_protected_resource(page, "https://api.sap.com/protected")


def test_plan_mirror_skips_unchanged_complete_api(tmp_path):
    current = [artifact()]
    write_mirror_files(tmp_path, ["example"])

    assert plan_mirror(current, current, tmp_path) == ([], [])


def test_plan_mirror_detects_catalog_change(tmp_path):
    previous = [artifact()]
    current = [artifact(ModifiedAt="/Date(2)/")]
    write_mirror_files(tmp_path, ["example"])

    assert plan_mirror(current, previous, tmp_path) == (["example"], [])


def test_plan_mirror_repairs_missing_files(tmp_path):
    current = [artifact()]

    assert plan_mirror(current, current, tmp_path) == (["example"], [])


def test_plan_mirror_detects_removals(tmp_path):
    previous = [artifact("old"), artifact("kept")]
    current = [artifact("kept")]
    write_mirror_files(tmp_path, ["kept"])

    assert plan_mirror(current, previous, tmp_path) == ([], ["old"])


def test_plan_mirror_force_updates_every_api(tmp_path):
    current = [artifact("b"), artifact("a")]
    write_mirror_files(tmp_path, ["a", "b"])

    assert plan_mirror(current, current, tmp_path, force=True) == (["a", "b"], [])


def test_authenticated_fetch_uses_request_context_not_page_evaluate():
    class Response:
        ok = True
        status = 200
        url = "https://api.sap.com/spec"
        headers = {"content-type": "application/json"}

        @staticmethod
        def text():
            return '{"openapi": "3.0.0"}'

    class RequestContext:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    class Page:
        def evaluate(self, *_args, **_kwargs):
            raise AssertionError("DOM evaluation must not be used for API downloads")

    request = RequestContext()
    result = fetch_authenticated_json(request, Page(), "https://api.sap.com/spec")

    assert result == {"openapi": "3.0.0"}
    assert request.calls[0][1]["fail_on_status_code"] is False


def test_authenticated_fetch_refreshes_session_after_login_html(monkeypatch):
    class Response:
        ok = True
        status = 200
        url = "https://api.sap.com/metadata"

        def __init__(self, body, content_type):
            self._body = body
            self.headers = {"content-type": content_type}

        def text(self):
            return self._body

    class RequestContext:
        def __init__(self):
            self.responses = iter(
                [
                    Response("<html><body>Log On</body></html>", "text/html"),
                    Response('{"d": {"Name": "example"}}', "application/json"),
                ]
            )
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return next(self.responses)

    request = RequestContext()
    page = object()
    refreshes = []
    monkeypatch.setattr(
        mirror,
        "refresh_session",
        lambda actual_page, url: refreshes.append((actual_page, url)),
    )

    result = fetch_authenticated_json(request, page, "https://api.sap.com/metadata")

    assert result == {"d": {"Name": "example"}}
    assert request.calls == 2
    assert refreshes == [(page, "https://api.sap.com/metadata")]


def test_fetch_api_metadata_uses_authenticated_session(monkeypatch):
    request = object()
    page = object()
    calls = []

    def authenticated_fetch(actual_request, actual_page, url):
        calls.append((actual_request, actual_page, url))
        return {"d": {"Name": "example"}}

    monkeypatch.setattr(mirror, "fetch_authenticated_json", authenticated_fetch)

    assert fetch_api_metadata(request, page, "example") == {"Name": "example"}
    assert calls == [
        (
            request,
            page,
            "https://api.sap.com/odata/1.0/catalog.svc/"
            "APIContent.APIs('example')?$select=*&$format=json",
        )
    ]


def test_fetch_api_spec_treats_not_found_as_unavailable(monkeypatch):
    def not_found(*_args, **_kwargs):
        raise HttpStatusError(404, "https://api.sap.com/spec")

    monkeypatch.setattr(mirror, "fetch_authenticated_json", not_found)

    assert fetch_api_spec(object(), object(), "retired") is None


def test_save_specs_removes_a_spec_that_is_no_longer_available(tmp_path):
    write_mirror_files(tmp_path, ["example"])

    save_specs([artifact()], {"example": None}, {}, tmp_path)

    assert not (tmp_path / "specs" / "example.json").exists()


def test_check_mode_is_credential_free_when_mirror_is_current(tmp_path, monkeypatch):
    current = [artifact()]
    (tmp_path / "artifacts.json").write_text(json.dumps(current), encoding="utf-8")
    write_mirror_files(tmp_path, ["example"])
    monkeypatch.setattr(mirror, "fetch_artifact_list", lambda: current)
    monkeypatch.setattr(mirror, "SAP_USER", "")
    monkeypatch.setattr(mirror, "SAP_PASS", "")
    monkeypatch.setattr(mirror, "SAP_ACCOUNT", "")
    monkeypatch.setattr(
        sys,
        "argv",
        ["mirror.py", "--check", "--output-dir", str(tmp_path)],
    )

    assert mirror.main() == 0


def test_removal_only_run_needs_no_credentials_or_playwright(tmp_path, monkeypatch):
    previous = [artifact("kept"), artifact("removed")]
    current = [artifact("kept")]
    (tmp_path / "artifacts.json").write_text(json.dumps(previous), encoding="utf-8")
    write_mirror_files(tmp_path, ["kept"])

    monkeypatch.setattr(mirror, "fetch_artifact_list", lambda: current)
    monkeypatch.setattr(mirror, "SAP_USER", "")
    monkeypatch.setattr(mirror, "SAP_PASS", "")
    monkeypatch.setattr(mirror, "SAP_ACCOUNT", "")
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.setattr(mirror, "generate_summary", lambda *_args: None)
    monkeypatch.setattr(mirror, "generate_collection_files", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mirror.py", "--output-dir", str(tmp_path)],
    )

    assert mirror.main() == 0
    saved = json.loads((tmp_path / "artifacts.json").read_text(encoding="utf-8"))
    assert saved == current


def test_main_logs_in_before_fetching_metadata(tmp_path, monkeypatch):
    current = [artifact()]
    events = []

    class Page:
        url = "https://api.sap.com/"

        def screenshot(self, **_kwargs):
            raise AssertionError("The successful path must not take a screenshot")

    class Context:
        request = object()

        @staticmethod
        def new_page():
            return Page()

    class Browser:
        @staticmethod
        def new_context(**_kwargs):
            return Context()

        @staticmethod
        def close():
            events.append("close")

    class Chromium:
        @staticmethod
        def launch(**_kwargs):
            return Browser()

    class Playwright:
        chromium = Chromium()

    class PlaywrightManager:
        def __enter__(self):
            return Playwright()

        def __exit__(self, *_args):
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    setattr(sync_api, "sync_playwright", lambda: PlaywrightManager())
    playwright = types.ModuleType("playwright")
    playwright.__path__ = []
    setattr(playwright, "sync_api", sync_api)
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    monkeypatch.setattr(mirror, "fetch_artifact_list", lambda: current)
    monkeypatch.setattr(mirror, "SAP_USER", "user")
    monkeypatch.setattr(mirror, "SAP_PASS", "password")
    monkeypatch.setattr(mirror, "SAP_ACCOUNT", "account")
    monkeypatch.setattr(mirror, "login", lambda *_args: events.append("login"))
    monkeypatch.setattr(
        mirror,
        "fetch_api_metadata",
        lambda *_args: events.append("metadata") or {"Name": "example"},
    )
    monkeypatch.setattr(
        mirror,
        "fetch_api_spec",
        lambda *_args: events.append("spec") or {"openapi": "3.0.0", "paths": {}},
    )
    monkeypatch.setattr(mirror.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(mirror, "save_specs", lambda *_args: None)
    monkeypatch.setattr(mirror, "generate_summary", lambda *_args: None)
    monkeypatch.setattr(mirror, "generate_collection_files", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mirror.py", "--output-dir", str(tmp_path)],
    )

    assert mirror.main() == 0
    assert events[:3] == ["login", "metadata", "spec"]


def test_check_mode_signals_when_browser_run_is_needed(tmp_path, monkeypatch):
    current = [artifact()]
    monkeypatch.setattr(mirror, "fetch_artifact_list", lambda: current)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mirror.py", "--check", "--output-dir", str(tmp_path)],
    )

    assert mirror.main() == CHECK_CHANGES_EXIT_CODE
