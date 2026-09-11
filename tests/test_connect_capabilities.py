"""Tests for connect.py's capabilities() — see api/amelia_bots.py for the
route it reads, and connect.py's module docstring for the relay it feeds.

connect.py is a standalone, dependency-free script (by design — see its
module docstring), so these tests import it directly by path rather than as
a package, and use a tiny local HTTPServer fixture instead of a real Hermes
WebUI server: capabilities() only cares about the shape of whatever answers
GET /api/amelia/bots/status, not about the real server.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

REPO = pathlib.Path(__file__).parent.parent


def _load_connect():
    spec = importlib.util.spec_from_file_location("amelia_connect_under_test", REPO / "connect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


connect = _load_connect()


class _FakeLocalServer:
    """A tiny stand-in for the local Hermes WebUI's /api/amelia/bots/status."""

    def __init__(self, status_code: int, body: dict | None):
        self._status_code = status_code
        self._body = body
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/api/amelia/bots/status":
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = json.dumps(outer._body or {}).encode()
                self.send_response(outer._status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def local(self) -> str:
        return f"127.0.0.1:{self._server.server_address[1]}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def test_capabilities_reports_bots_true_with_max_parallel():
    srv = _FakeLocalServer(200, {"running": 0, "queued": 0, "max_parallel": 4, "local_models": ["ollama"], "paused": False})
    try:
        caps = connect.capabilities(srv.local)
    finally:
        srv.close()
    assert caps == {"bots": True, "max_parallel": 4}


def test_capabilities_reports_bots_true_without_max_parallel_when_absent():
    srv = _FakeLocalServer(200, {"running": 0, "queued": 0, "local_models": [], "paused": False})
    try:
        caps = connect.capabilities(srv.local)
    finally:
        srv.close()
    assert caps == {"bots": True}


def test_capabilities_reports_bots_false_on_non_200():
    srv = _FakeLocalServer(404, {"error": "not found"})
    try:
        caps = connect.capabilities(srv.local)
    finally:
        srv.close()
    assert caps == {"bots": False}


def test_capabilities_reports_bots_false_when_nothing_listens():
    # An older local server build without api/amelia_bots.py wired in yet —
    # or simply nothing running on that port — must degrade gracefully
    # rather than raising and breaking the pairing flow that calls this.
    caps = connect.capabilities("127.0.0.1:1")
    assert caps == {"bots": False}


def test_capabilities_ignores_a_non_integer_max_parallel():
    srv = _FakeLocalServer(200, {"max_parallel": "a lot"})
    try:
        caps = connect.capabilities(srv.local)
    finally:
        srv.close()
    assert caps == {"bots": True}
