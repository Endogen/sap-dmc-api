#!/usr/bin/env python3
"""Recover the real request path of an operation from a SAP DMC API spec.

SAP ships these specs with placeholder standard fields — `host: hostname` and
`basePath: "/"` — and keeps the actual base URL in the non-standard `x-servers`
extension (OpenAPI 3 specs use `servers`). The path keys under `paths` are
relative to that URL, so anything that renders a request path has to put the two
back together or it emits `/autoAssemble` instead of `/assembly/v1/autoAssemble`.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def service_prefix(spec: dict) -> str:
    """Return the service path prefix a spec's path keys are relative to.

    The server URL wins over `basePath`, which in these specs is either `/` or a
    duplicate of the prefix the server URL already carries; `basePath` is only
    the fallback for a plain Swagger 2.0 spec.

    Returns "" when neither yields a path — those specs spell the full prefix
    out in their path keys already.
    """
    servers = spec.get("x-servers") or spec.get("servers") or []
    prefix = urlsplit(servers[0].get("url", "")).path.rstrip("/") if servers else ""
    # No path on the server URL — fall back to the standard Swagger 2.0 field.
    if not prefix:
        prefix = (spec.get("basePath") or "").rstrip("/")
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    return prefix


def join_path(prefix: str, path: str) -> str:
    """Join a service prefix with an operation path key.

    A few SAP specs are internally inconsistent and already spell the prefix
    out in some path keys (e.g. `/aiml/v1/inspectionLogsForContext` sitting
    beside a relative `/inspectionLog`), so skip the prefix when it is already
    there rather than doubling it.
    """
    if not prefix or path == prefix or path.startswith(prefix + "/"):
        return path
    return prefix + path


def full_path(spec: dict, path: str) -> str:
    """Return `path` as the full request path, prefix included."""
    return join_path(service_prefix(spec), path)
