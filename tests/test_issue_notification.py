"""Tests for API-change GitHub issue rendering."""

from __future__ import annotations

import json

from issue_notification import build_issue_notification, main


def sample_diff() -> dict:
    return {
        "date": "2026-07-17",
        "summary": {
            "apis_added": 1,
            "apis_removed": 0,
            "apis_changed": 1,
            "endpoints_added": 2,
            "endpoints_removed": 1,
            "breaking_changes": 2,
        },
        "changes": [
            {
                "api": "new_api",
                "display_name": "New API",
                "type": "added",
            },
            {
                "api": "widget_api",
                "display_name": "Widget API",
                "type": "changed",
                "changes": [
                    {
                        "kind": "endpoint_removed",
                        "method": "DELETE",
                        "path": "/widgets/{id}",
                        "breaking": True,
                    },
                    {
                        "kind": "schema_field_required",
                        "schema": "Widget",
                        "field": "site",
                        "breaking": True,
                    },
                    {
                        "kind": "endpoint_added",
                        "method": "POST",
                        "path": "/widgets",
                        "breaking": False,
                    },
                ],
            },
        ],
    }


def test_build_issue_notification_summarizes_and_links_report():
    notification = build_issue_notification(
        sample_diff(),
        report_path="output/history/2026-07-17.json",
        repository="Endogen/sap-dmc-api",
        ref="main",
    )

    assert notification.report_id in notification.title
    assert "2 APIs, 2 breaking" in notification.title
    assert "| Endpoints added | 2 |" in notification.body
    assert "**Widget API**" in notification.body
    assert "endpoint <code>DELETE /widgets/{id}</code> was removed" in notification.body
    assert "field <code>site</code> became required" in notification.body
    assert (
        "https://github.com/Endogen/sap-dmc-api/blob/main/"
        "output/history/2026-07-17.json"
    ) in notification.body


def test_report_id_is_stable_across_dictionary_key_order():
    diff = sample_diff()
    reordered = json.loads(json.dumps(diff, sort_keys=True))

    first = build_issue_notification(diff, "report.json", "owner/repo")
    second = build_issue_notification(reordered, "report.json", "owner/repo")

    assert first.report_id == second.report_id


def test_cli_writes_body_and_github_outputs(tmp_path):
    diff_path = tmp_path / "output" / "history" / "report.json"
    diff_path.parent.mkdir(parents=True)
    diff_path.write_text(json.dumps(sample_diff()), encoding="utf-8")
    body_path = tmp_path / "issue.md"
    github_output = tmp_path / "github-output"

    result = main(
        [
            str(diff_path),
            "--repository",
            "owner/repo",
            "--body-file",
            str(body_path),
            "--github-output",
            str(github_output),
        ]
    )

    assert result == 0
    assert body_path.read_text(encoding="utf-8").startswith("## Summary\n")
    outputs = github_output.read_text(encoding="utf-8")
    assert "title=API changes detected" in outputs
    assert "report_id=" in outputs
