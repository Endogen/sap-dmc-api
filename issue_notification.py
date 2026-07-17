#!/usr/bin/env python3
"""Build a concise GitHub issue from an API diff report."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

MAX_BREAKING_DETAILS = 50


@dataclass(frozen=True)
class IssueNotification:
    """Rendered issue content and its stable deduplication identifier."""

    title: str
    body: str
    report_id: str


def _report_id(diff: dict[str, Any]) -> str:
    canonical = json.dumps(
        diff,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _count(summary: dict[str, Any], key: str) -> int:
    value = summary.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _code(value: Any) -> str:
    return f"<code>{html.escape(str(value))}</code>"


def _text(value: Any) -> str:
    text = html.escape(str(value))
    for character in ("\\", "`", "*", "_", "[", "]", "|", "#"):
        text = text.replace(character, f"\\{character}")
    return text


def _api_label(api_change: dict[str, Any]) -> str:
    name = api_change.get("api", "unknown")
    display_name = api_change.get("display_name") or name
    if display_name == name:
        return _code(name)
    return f"**{_text(display_name)}** ({_code(name)})"


def _describe_breaking_change(change: dict[str, Any]) -> str:
    kind = str(change.get("kind", "change"))
    endpoint = change.get("endpoint")
    schema = change.get("schema")
    field = change.get("field")

    if kind == "endpoint_removed":
        endpoint_label = f"{change.get('method', '')} {change.get('path', '')}".strip()
        return f"endpoint {_code(endpoint_label)} was removed"
    if kind == "param_added":
        return (
            f"required parameter {_code(change.get('param', 'unknown'))} was added "
            f"to {_code(endpoint or 'unknown endpoint')}"
        )
    if kind == "request_field_added":
        return (
            f"required request field {_code(field or 'unknown')} was added to "
            f"{_code(endpoint or 'unknown endpoint')}"
        )
    if kind == "request_type_changed":
        return (
            f"request field {_code(field or 'body')} on "
            f"{_code(endpoint or 'unknown endpoint')} changed from "
            f"{_code(change.get('old_type', 'unknown'))} to "
            f"{_code(change.get('new_type', 'unknown'))}"
        )
    if kind == "response_field_removed":
        return (
            f"response field {_code(field or 'unknown')} was removed from "
            f"{_code(endpoint or 'unknown endpoint')}"
        )
    if kind == "response_type_changed":
        return (
            f"response field {_code(field or 'body')} on "
            f"{_code(endpoint or 'unknown endpoint')} changed from "
            f"{_code(change.get('old_type', 'unknown'))} to "
            f"{_code(change.get('new_type', 'unknown'))}"
        )
    if kind == "schema_removed":
        return f"schema {_code(schema or 'unknown')} was removed"
    if kind == "schema_field_removed":
        return (
            f"field {_code(field or 'unknown')} was removed from schema "
            f"{_code(schema or 'unknown')}"
        )
    if kind == "schema_type_changed":
        return (
            f"field {_code(field or 'root')} in schema {_code(schema or 'unknown')} "
            f"changed from {_code(change.get('old_type', 'unknown'))} to "
            f"{_code(change.get('new_type', 'unknown'))}"
        )
    if kind == "schema_field_required":
        return (
            f"field {_code(field or 'unknown')} became required in schema "
            f"{_code(schema or 'unknown')}"
        )

    context = endpoint or schema
    description = kind.replace("_", " ")
    return f"{_text(description)}" + (f" in {_code(context)}" if context else "")


def build_issue_notification(
    diff: dict[str, Any],
    report_path: str,
    repository: str,
    ref: str = "main",
) -> IssueNotification:
    """Render a GitHub issue for one structural API diff report."""
    summary = diff.get("summary") if isinstance(diff.get("summary"), dict) else {}
    changes = diff.get("changes") if isinstance(diff.get("changes"), list) else []
    date = str(diff.get("date") or "unknown date")
    report_id = _report_id(diff)

    added = _count(summary, "apis_added")
    removed = _count(summary, "apis_removed")
    changed = _count(summary, "apis_changed")
    affected = added + removed + changed
    breaking = _count(summary, "breaking_changes")
    api_word = "API" if affected == 1 else "APIs"
    title = (
        f"API changes detected — {date} "
        f"({affected} {api_word}, {breaking} breaking) [{report_id}]"
    )

    report_url = (
        f"https://github.com/{repository.strip('/')}/blob/"
        f"{quote(ref, safe='/')}/{quote(report_path.lstrip('./'), safe='/')}"
    )
    lines = [
        "## Summary",
        "",
        f"The scheduled SAP DMC mirror detected structural API changes on **{_text(date)}**.",
        "",
        "| Metric | Count |",
        "|---|---:|",
        f"| Affected APIs | {affected} |",
        f"| APIs added | {added} |",
        f"| APIs removed | {removed} |",
        f"| APIs changed | {changed} |",
        f"| Endpoints added | {_count(summary, 'endpoints_added')} |",
        f"| Endpoints removed | {_count(summary, 'endpoints_removed')} |",
        f"| Breaking changes | **{breaking}** |",
        "",
        "## Affected APIs",
        "",
    ]

    if changes:
        for api_change in changes:
            if not isinstance(api_change, dict):
                continue
            change_type = str(api_change.get("type", "changed")).capitalize()
            details = api_change.get("changes")
            details = details if isinstance(details, list) else []
            breaking_count = sum(
                1
                for detail in details
                if isinstance(detail, dict) and detail.get("breaking") is True
            )
            suffix = ""
            if details:
                change_word = "change" if len(details) == 1 else "changes"
                suffix = (
                    f" — {len(details)} structural {change_word}, "
                    f"{breaking_count} breaking"
                )
            lines.append(f"- **{_text(change_type)}:** {_api_label(api_change)}{suffix}")
    else:
        lines.append("- No affected API details were included in the report.")

    breaking_details: list[str] = []
    for api_change in changes:
        if not isinstance(api_change, dict):
            continue
        if api_change.get("type") == "removed":
            breaking_details.append(f"{_api_label(api_change)} — API was removed")
        details = api_change.get("changes")
        if not isinstance(details, list):
            continue
        for detail in details:
            if isinstance(detail, dict) and detail.get("breaking") is True:
                breaking_details.append(
                    f"{_api_label(api_change)} — {_describe_breaking_change(detail)}"
                )

    lines.extend(["", "## Breaking changes", ""])
    if breaking_details:
        lines.extend(f"- {detail}" for detail in breaking_details[:MAX_BREAKING_DETAILS])
        if len(breaking_details) > MAX_BREAKING_DETAILS:
            omitted = len(breaking_details) - MAX_BREAKING_DETAILS
            lines.append(
                f"- _{omitted} more breaking changes are listed in the full report._"
            )
    else:
        lines.append("No breaking changes were classified.")

    lines.extend(
        [
            "",
            f"[View the full JSON change report]({report_url})",
            "",
            (
                f"<sub>Report ID: <code>{report_id}</code> · Generated "
                "automatically by the mirror workflow.</sub>"
            ),
            "",
        ]
    )
    return IssueNotification(title=title, body="\n".join(lines), report_id=report_id)


def _write_github_output(path: Path, notification: IssueNotification) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"title={notification.title}\n")
        output.write(f"report_id={notification.report_id}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("diff_path", type=Path, help="JSON diff report to render")
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--ref", default="main", help="Git ref used by the report link")
    parser.add_argument("--body-file", type=Path, required=True, help="Issue body output path")
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Optional GitHub Actions output file for the title and report ID",
    )
    args = parser.parse_args(argv)

    diff = json.loads(args.diff_path.read_text(encoding="utf-8"))
    notification = build_issue_notification(
        diff,
        report_path=args.diff_path.as_posix(),
        repository=args.repository,
        ref=args.ref,
    )
    args.body_file.parent.mkdir(parents=True, exist_ok=True)
    args.body_file.write_text(notification.body, encoding="utf-8")
    if args.github_output:
        _write_github_output(args.github_output, notification)
    else:
        print(notification.title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
