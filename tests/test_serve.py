"""Tests for serve.py — routing and path-traversal protection."""
from __future__ import annotations

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

import serve


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.APIHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


def get_raw(server, path: str) -> tuple[int, bytes]:
    """GET without client-side path normalization (unlike urllib/requests)."""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.putrequest("GET", path)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_index_served(server):
    status, body = get_raw(server, "/")
    assert status == 200
    assert b"SAP Digital Manufacturing Cloud" in body


def test_spec_served(server):
    name = next(serve.SPECS_DIR.glob("*.json")).name
    status, body = get_raw(server, f"/specs/{name}")
    assert status == 200
    assert body.lstrip().startswith(b"{")


def test_unknown_path_404(server):
    status, _ = get_raw(server, "/nope")
    assert status == 404


def test_traversal_blocked(server):
    # summary.json exists one level above specs/ — must not be reachable
    status, _ = get_raw(server, "/specs/../summary.json")
    assert status == 404


def test_encoded_traversal_blocked(server):
    status, _ = get_raw(server, "/specs/..%2F..%2Foutput%2Fsummary.json")
    assert status == 404


def test_collections_traversal_blocked(server):
    status, _ = get_raw(server, "/collections/../summary.json")
    assert status == 404
