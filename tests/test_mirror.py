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
    generate_summary,
    emit_github_output,
    fetch_public_json,
    record_changes,
    fetch_updates,
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


# ---------------------------------------------------------------------------
# Summary — endpoint paths must carry the service prefix SAP hides in x-servers
# ---------------------------------------------------------------------------


@pytest.fixture
def summarize(tmp_path, monkeypatch):
    """Run generate_summary over a single spec and return its summary entry.

    generate_summary also refreshes the stats line in the repository's own
    README; stub that out so the tests stay inside tmp_path.
    """
    monkeypatch.setattr(mirror, "_update_repo_readme_stats", lambda *a, **k: None)

    def run(spec, name="example"):
        generate_summary([artifact(name)], {name: spec}, {}, tmp_path)
        summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
        return summary[0]

    return run


def test_generate_summary_prefixes_endpoint_paths(summarize):
    entry = summarize(
        {
            "swagger": "2.0",
            "host": "hostname",
            "basePath": "/",
            "x-servers": [{"url": "https://api.{regionHost}/assembly/v1"}],
            "paths": {"/autoAssemble": {"post": {"summary": "Auto assemble"}}},
        },
    )

    assert entry["base_path"] == "/assembly/v1"
    assert [e["path"] for e in entry["endpoints"]] == ["/assembly/v1/autoAssemble"]


def test_generate_summary_base_path_not_placeholder_slash(summarize):
    entry = summarize(
        {
            "openapi": "3.0.0",
            "servers": [{"url": "https://api.{regionHost}/alerts"}],
            "paths": {"/v1/Alerts": {"get": {"summary": "List"}}},
        },
    )

    assert entry["base_path"] == "/alerts"
    assert [e["path"] for e in entry["endpoints"]] == ["/alerts/v1/Alerts"]


def test_generate_summary_keeps_already_prefixed_paths(summarize):
    entry = summarize(
        {
            "basePath": "/",
            "x-servers": [{"url": "https://api.{regionHost}"}],
            "paths": {"/quantityConfirmation/v1/confirm": {"post": {"summary": "C"}}},
        },
    )

    assert entry["base_path"] == ""
    assert [e["path"] for e in entry["endpoints"]] == [
        "/quantityConfirmation/v1/confirm"
    ]


def test_generate_summary_writes_lf_newlines(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror, "_update_repo_readme_stats", lambda *a, **k: None)
    spec = {"basePath": "/v1", "paths": {"/x": {"get": {"summary": "X"}}}}
    generate_summary([artifact()], {"example": spec}, {}, tmp_path)

    for name in ("summary.json", "README.md"):
        raw = (tmp_path / name).read_bytes()
        assert b"\r\n" not in raw


# ---------------------------------------------------------------------------
# One bad API must not cost the whole run
# ---------------------------------------------------------------------------


class StubPage:
    """Stands in for the Playwright page; fetch_updates never touches it."""

    url = "https://api.sap.com/"


def _spec(name):
    return {"swagger": "2.0", "paths": {"/" + name: {"get": {"summary": name}}}}


def fetch_updates_over(monkeypatch, names, failing=None, error=None):
    failing = failing or {}
    monkeypatch.setattr(mirror.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mirror, "fetch_api_metadata", lambda r, p, name: {"Name": name})

    def fake_spec(request, page, name):
        if name in failing:
            raise failing[name]
        return _spec(name)

    monkeypatch.setattr(mirror, "fetch_api_spec", fake_spec)
    return mirror.fetch_updates(
        None, StubPage(), list(names), {n: artifact(n) for n in names}
    )


def test_one_failing_api_does_not_abort_the_others(monkeypatch):
    metadata, specs, failures = fetch_updates_over(
        monkeypatch,
        ["good1", "bad", "good2"],
        failing={"bad": ValueError("does not look like OpenAPI")},
    )

    assert sorted(specs) == ["good1", "good2"]
    assert sorted(metadata) == ["good1", "good2"]
    assert failures == [("bad", "does not look like OpenAPI")]


def test_failed_api_is_left_out_so_its_previous_spec_survives(monkeypatch):
    _, specs, _ = fetch_updates_over(
        monkeypatch, ["bad"], failing={"bad": RuntimeError("boom")}
    )

    # None would mean "SAP dropped this API" and delete the file — absent means
    # "we could not refresh it", which keeps what was mirrored before.
    assert "bad" not in specs


def test_withdrawn_api_is_still_recorded_as_none(monkeypatch):
    monkeypatch.setattr(mirror.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mirror, "fetch_api_metadata", lambda r, p, name: {})
    monkeypatch.setattr(mirror, "fetch_api_spec", lambda r, p, name: None)

    _, specs, failures = mirror.fetch_updates(
        None, StubPage(), ["gone"], {"gone": artifact("gone")}
    )

    assert specs == {"gone": None}
    assert failures == []


def test_lost_session_still_aborts_the_run(monkeypatch):
    """Every remaining API would fail the same way — no point continuing."""
    with pytest.raises(mirror.SessionAuthError):
        fetch_updates_over(
            monkeypatch,
            ["a", "b"],
            failing={"a": mirror.SessionAuthError("session gone")},
        )


# ---------------------------------------------------------------------------
# The public catalog fetch retries
# ---------------------------------------------------------------------------


def _http_error(code):
    from urllib.error import HTTPError

    return HTTPError("https://api.sap.com/x", code, "boom", {}, None)


def public_fetch_returning(monkeypatch, results):
    """Drive fetch_public_json over a scripted list of outcomes."""
    calls = []

    def fake_read(url):
        outcome = results[len(calls)]
        calls.append(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, "application/json", url

    monkeypatch.setattr(mirror, "_read_public_url", fake_read)
    monkeypatch.setattr(mirror.time, "sleep", lambda *_: None)
    return calls


def test_public_fetch_retries_a_transient_server_error(monkeypatch):
    calls = public_fetch_returning(
        monkeypatch, [_http_error(503), '{"ok": true}']
    )

    assert fetch_public_json("https://api.sap.com/x") == {"ok": True}
    assert len(calls) == 2


def test_public_fetch_retries_a_network_error(monkeypatch):
    from urllib.error import URLError

    calls = public_fetch_returning(
        monkeypatch, [URLError("connection reset"), '{"ok": true}']
    )

    assert fetch_public_json("https://api.sap.com/x") == {"ok": True}
    assert len(calls) == 2


def test_public_fetch_does_not_retry_a_404(monkeypatch):
    calls = public_fetch_returning(monkeypatch, [_http_error(404)])

    with pytest.raises(RuntimeError, match="HTTP 404"):
        fetch_public_json("https://api.sap.com/x")
    assert len(calls) == 1


def test_public_fetch_gives_up_after_the_last_attempt(monkeypatch):
    calls = public_fetch_returning(
        monkeypatch, [_http_error(503)] * mirror.PUBLIC_FETCH_ATTEMPTS
    )

    with pytest.raises(RuntimeError, match="HTTP 503"):
        fetch_public_json("https://api.sap.com/x")
    assert len(calls) == mirror.PUBLIC_FETCH_ATTEMPTS


# ---------------------------------------------------------------------------
# Reporting back to the workflow
# ---------------------------------------------------------------------------


def test_emit_github_output_appends_key_values(tmp_path, monkeypatch):
    out = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    emit_github_output("history_file", "output/history/2026-09-15.json")
    emit_github_output("failed_apis", "a,b")

    assert out.read_text(encoding="utf-8").splitlines() == [
        "history_file=output/history/2026-09-15.json",
        "failed_apis=a,b",
    ]


def test_emit_github_output_is_a_no_op_outside_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    emit_github_output("history_file", "x")  # must not raise


# ---------------------------------------------------------------------------
# The failure screenshot must not publish the SAP account
# ---------------------------------------------------------------------------


class RecordingPage:
    url = "https://accounts.sap.com/saml2/idp/sso"

    def __init__(self, redact_fails=False):
        self.evaluated = []
        self.screenshots = []
        self.redact_fails = redact_fails

    def evaluate(self, script):
        if self.redact_fails:
            raise RuntimeError("page closed")
        self.evaluated.append(script)

    def screenshot(self, **kwargs):
        self.screenshots.append(kwargs)


def test_failure_screenshot_redacts_form_fields_first(tmp_path):
    page = RecordingPage()

    mirror._save_failure_screenshot(page, path=str(tmp_path / "shot.png"))

    assert len(page.screenshots) == 1
    assert page.evaluated, "inputs must be cleared before the capture"
    assert "field.value = ''" in page.evaluated[0]


def test_no_screenshot_is_written_if_redaction_fails(tmp_path):
    """Better no artifact at all than one showing the login form."""
    page = RecordingPage(redact_fails=True)

    mirror._save_failure_screenshot(page, path=str(tmp_path / "shot.png"))

    assert page.screenshots == []


# ---------------------------------------------------------------------------
# A broken changelog must fail loudly, not silently stop updating
# ---------------------------------------------------------------------------


def test_record_changes_returns_none_when_nothing_changed(tmp_path, monkeypatch):
    import diff_tracker

    monkeypatch.setattr(diff_tracker, "load_specs_from_git", lambda ref: {})
    monkeypatch.setattr(diff_tracker, "load_specs_from_dir", lambda d: {})
    monkeypatch.setattr(diff_tracker, "diff_specs", lambda old, new: None)

    assert record_changes(tmp_path) is None


def test_record_changes_writes_history_and_returns_its_path(tmp_path, monkeypatch):
    import diff_tracker

    diff = {
        "date": "2026-09-15",
        "summary": {
            "apis_added": 0, "apis_removed": 0, "apis_changed": 1,
            "endpoints_added": 1, "endpoints_removed": 0, "breaking_changes": 0,
        },
        "changes": [],
    }
    monkeypatch.setattr(diff_tracker, "load_specs_from_git", lambda ref: {})
    monkeypatch.setattr(diff_tracker, "load_specs_from_dir", lambda d: {})
    monkeypatch.setattr(diff_tracker, "diff_specs", lambda old, new: diff)

    history_file = record_changes(tmp_path)

    assert history_file is not None
    assert history_file.is_file()
    assert (tmp_path / "changelog.json").is_file()


def test_record_changes_lets_a_diff_failure_surface(tmp_path, monkeypatch):
    """Swallowing this is what hid the changelog and the issues for two months."""
    import diff_tracker

    def boom(old, new):
        raise KeyError("summary")

    monkeypatch.setattr(diff_tracker, "load_specs_from_git", lambda ref: {})
    monkeypatch.setattr(diff_tracker, "load_specs_from_dir", lambda d: {})
    monkeypatch.setattr(diff_tracker, "diff_specs", boom)

    with pytest.raises(KeyError):
        record_changes(tmp_path)
