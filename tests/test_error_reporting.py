"""Tests for the Sentry-equivalent error reporting wired into the Agents
feature (Amelia Bot Teams' computer runner) and this server's process-wide
crash hooks.

Two halves, matching the amelia-accounts scripts/check-sentry.mjs model this
was written against:

  * Static (always): source-text assertions that the wiring exists and the
    privacy-critical shape is right — proves the code says the right thing,
    not that a report reaches the wire.
  * Live (always, since there's no SDK/subprocess to boot here — this is
    plain urllib against a local sink): boots api.error_reporting for real
    against a local HTTP server standing in for Amelia's backend, and
    asserts a report contains no secret, no home-directory path, and no
    bare query string — and that it is silent when AMELIA_ERROR_URL is
    unset, and silent for the product-event exception types that must never
    be reported.

Run: ./scripts/test.sh tests/test_error_reporting.py -v
"""
from __future__ import annotations

import ast
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent


def _code(path: str) -> str:
    """Source with docstrings and comments blanked, code formatting left
    untouched — a regex guard that only matched a comment or docstring
    EXPLAINING the behavior would be green forever without the behavior
    existing (this file is full of exactly that kind of prose, and so is
    every file it reads). Triple-quoted strings are dropped first, then any
    `# ...` trailing a line of real code. Best-effort rather than a full
    tokenizer: a `#` immediately after a quote character is left alone (the
    one shape this can miss), which only makes an assertion MORE
    conservative, never falsely green."""
    src = (REPO / path).read_text(encoding="utf-8")
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    lines = []
    for line in src.split("\n"):
        if re.match(r"^\s*#", line):
            continue
        lines.append(re.sub(r"(?<!['\"])#.*$", "", line))
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
#  Static: api/error_reporting.py itself
# ═══════════════════════════════════════════════════════════════════════════

def test_error_reporting_is_off_by_default():
    src = _code("api/error_reporting.py")
    assert "AMELIA_ERROR_URL" in src
    assert re.search(r"def error_url.*os\.environ\.get\(\s*_ERROR_URL_ENV", src, re.S)
    # report_error must bail out immediately when no URL is configured —
    # "off by default" has to be enforced at the top of the function, not
    # merely true because nothing calls it.
    assert re.search(r"def report_error.*if not url:\s*return", src, re.S)


def test_error_reporting_never_sends_a_sentry_dsn():
    """The "own endpoint, never a bare DSN" rule from amelia-accounts'
    src/hosted.ts, checked by NOT importing a Sentry SDK anywhere — the
    module's own docstring is allowed to explain the decision in prose
    (it does, at length), so this checks imports and the dependency
    manifests rather than banning the word."""
    src = _code("api/error_reporting.py")
    assert "sentry_sdk" not in src
    assert not re.search(r"^\s*(import|from)\s+sentry", src, re.M)
    assert "sentry-sdk" not in (REPO / "requirements.txt").read_text().lower()
    assert "sentry-sdk" not in (REPO / "requirements-dev.txt").read_text().lower()


def test_error_reporting_redacts_urls_and_secrets_and_home():
    src = _code("api/error_reporting.py")
    assert "_URL_WITH_QUERY" in src and "_bare_url" in src
    assert "_redact_fn_cached" in src  # reuses the repo's own credential redactor
    assert "_HOME" in src and "<home>" in src


def test_error_reporting_tags_are_allowlisted():
    """A caller must not be able to widen what leaves this process merely by
    passing a new kwarg — report_error has to filter against a fixed set."""
    src = _code("api/error_reporting.py")
    assert "_ALLOWED_TAGS" in src
    assert re.search(r"if key not in _ALLOWED_TAGS", src)


def test_error_reporting_caps_and_dedupes():
    src = _code("api/error_reporting.py")
    assert "_rate_limited" in src and "_RATE_MAX_PER_WHERE" in src
    assert "_dedupe" in src and "_REPORTED_ATTR" in src


def test_error_reporting_module_only_reports_our_files_in_the_stack():
    src = _code("api/error_reporting.py")
    assert "_OURS" in src
    assert re.search(r"if name in _OURS", src)


# ═══════════════════════════════════════════════════════════════════════════
#  Static: api/amelia_bots.py — the Agents runner loop and its tool calls
# ═══════════════════════════════════════════════════════════════════════════

def test_bots_runner_backstop_reports_unexpected_runner_exceptions():
    """start_run's `_work` wrapper is the last line of defense for the
    background thread the runner actually executes on — see start_run()."""
    src = _code("api/amelia_bots.py")
    m = re.search(r"def _work\(\):.*?_POOL\.submit", src, re.S)
    assert m, "start_run's _work wrapper not found"
    assert "_report_bot_error" in m.group(0)


def test_bots_tool_call_failures_are_reported():
    src = _code("api/amelia_bots.py")
    assert re.search(r'_report_bot_error\(\s*"bots:model_call"', src)
    assert re.search(r'_report_bot_error\(\s*"bots:tool:read_page"', src)
    assert re.search(r'_report_bot_error\(\s*"bots:tool:fetch_media"', src)


def _function_span(src: str, name: str, next_def: str) -> str:
    """Everything from `def {name}(` up to (not including) `\\ndef {next_def}`
    — avoids matching mid-body on the first "):" substring, which a
    signature with a return-type annotation (`-> None:`) or an `isinstance`
    call inside the body can both produce."""
    start = src.index(f"def {name}(")
    end = src.index(f"\ndef {next_def}", start)
    return src[start:end]


def test_bots_error_reporting_excludes_product_events():
    """ValidationError (an expected, user-facing tool refusal — SSRF-blocked
    host, size cap, bad URL, paused) and NoLocalModelError (no local model
    reachable) are product events, not bugs, and must never reach the
    reporter — see api/error_reporting.py's own docstring."""
    src = _code("api/amelia_bots.py")
    body = _function_span(src, "_report_bot_error", "run_bot_steps")
    assert re.search(r"isinstance\(\s*exc\s*,\s*\(\s*ValidationError\s*,\s*NoLocalModelError\s*\)\s*\)", body)


def test_bots_error_reporting_tags_feature_and_runner():
    src = _code("api/amelia_bots.py")
    body = _function_span(src, "_report_bot_error", "run_bot_steps")
    assert '"feature": "agents"' in body
    assert '"runner": "computer"' in body


def test_bots_never_imports_a_process_spawning_module():
    """Unrelated to error reporting, but the invariant this file's own
    module docstring makes ("the model can never get an arbitrary command
    executed through this runner") must not have been weakened by this
    change — re-asserted here as a guard against a future edit accidentally
    importing subprocess/os.exec* while wiring in more reporting call
    sites."""
    tree = ast.parse((REPO / "api/amelia_bots.py").read_text(encoding="utf-8"))
    banned = {"subprocess", "pty", "multiprocessing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module


# ═══════════════════════════════════════════════════════════════════════════
#  Static: api/crash_visibility.py — the process-wide backstop
# ═══════════════════════════════════════════════════════════════════════════

def test_crash_visibility_reports_thread_and_main_hooks():
    src = _code("api/crash_visibility.py")
    assert "_report_uncaught" in src
    thread_hook = re.search(r"def thread_excepthook\(args\).*?(?=\ndef )", src, re.S).group(0)
    main_hook = re.search(r"def main_excepthook\(.*?\).*?(?=\ndef )", src, re.S).group(0)
    assert "_report_uncaught(" in thread_hook
    assert "_report_uncaught(" in main_hook


def test_crash_visibility_report_hook_never_raises():
    src = _code("api/error_reporting.py")
    # The final except in report_error must swallow rather than propagate —
    # a reporter that can raise inside an excepthook becomes a second silent
    # death, which is the exact failure class this module exists to fix.
    tail = src[src.rfind("except Exception:"):]
    assert re.search(r"except Exception:\s*pass", tail)


# ═══════════════════════════════════════════════════════════════════════════
#  Static: mcp_server.py — the MCP tool-call chokepoint
# ═══════════════════════════════════════════════════════════════════════════

def test_mcp_call_tool_wraps_handlers_and_reports():
    pytest.importorskip("mcp", reason="mcp package not installed (optional MCP server dep)")
    src = _code("mcp_server.py")
    m = re.search(r"async def call_tool\(.*?(?=\nasync def main)", src, re.S)
    assert m, "call_tool dispatcher not found"
    body = m.group(0)
    assert "try:" in body
    assert "except Exception as exc:" in body
    assert "report_error(" in body
    assert '"mcp:call_tool"' in body


# ═══════════════════════════════════════════════════════════════════════════
#  Static: connect.py — the relay pairing path (standalone, stdlib-only)
# ═══════════════════════════════════════════════════════════════════════════

def test_connect_has_its_own_stdlib_only_reporter():
    src = _code("connect.py")
    assert "def report_error(" in src
    assert "/v1/machine-error" in src
    # connect.py's own promise: "Nothing but the standard library is used on
    # purpose" — the reporter must not import api.error_reporting (which
    # pulls in api.helpers and that module's dependency graph).
    assert "import api" not in src
    assert "from api" not in src


def test_connect_reconnect_loop_reports_lost_connections():
    src = _code("connect.py")
    idx = src.find("Lost the connection")
    assert idx > 0, "reconnect except block not found"
    nearby = src[idx:idx + 300]
    assert "report_error(" in nearby


def test_connect_reporter_redacts_query_strings_and_never_sends_the_token_in_the_message():
    src = _code("connect.py")
    m = re.search(r"def report_error\(.*?(?=\ndef |\nclass |\Z)", src, re.S)
    assert m
    body = m.group(0)
    assert "re.sub(" in body and "redacted" in body
    # The payload must carry the token as its own authenticated field, never
    # interpolated into the free-text message.
    assert '"token": token' in body


# ═══════════════════════════════════════════════════════════════════════════
#  Live: what actually reaches the wire
# ═══════════════════════════════════════════════════════════════════════════

class _Sink(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        _Sink.bodies.append(raw.decode("utf-8", errors="replace"))
        resp = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, fmt, *args):
        pass


@pytest.fixture
def sink(monkeypatch):
    _Sink.bodies = []
    srv = HTTPServer(("127.0.0.1", 0), _Sink)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/report"
    monkeypatch.setenv("AMELIA_ERROR_URL", url)

    import api.error_reporting as er
    er._rate_state.clear()

    try:
        yield url
    finally:
        srv.shutdown()
        thread.join(timeout=5)


def test_report_error_is_silent_when_unconfigured(monkeypatch):
    monkeypatch.delenv("AMELIA_ERROR_URL", raising=False)
    import api.error_reporting as er
    calls = []
    monkeypatch.setattr(er.urllib.request, "urlopen", lambda *a, **kw: calls.append(1))
    er.report_error("test:unconfigured", RuntimeError("should never be sent"))
    assert calls == []


def test_report_error_reaches_the_sink_without_secrets_or_home_or_query(sink):
    import api.error_reporting as er

    fake_secret = "sk-" + "a" * 40  # shaped like an OpenAI/Anthropic/OpenRouter key
    home = str(Path.home())
    try:
        raise ConnectionError(
            f"could not reach https://example.com/x?token={fake_secret} under {home}/workspace/proj/file.py"
        )
    except ConnectionError as exc:
        er.report_error("bots:tool:fetch_media", exc, {"feature": "agents", "runner": "computer", "bot_id": "b1"})

    assert len(_Sink.bodies) == 1
    wire = _Sink.bodies[0]
    payload = json.loads(wire)

    assert fake_secret not in wire
    assert home not in wire
    assert "?token=" not in wire  # the bare query string must not survive
    assert payload["kind"] == "ConnectionError"
    assert payload["where"] == "bots:tool:fetch_media"
    assert payload["tags"]["feature"] == "agents"
    assert payload["tags"]["runner"] == "computer"
    assert payload["tags"]["bot_id"] == "b1"
    # Structural guarantee, not just "this test didn't include it": the
    # payload has exactly these top-level keys, so a future edit that adds
    # e.g. a raw `prompt` field fails this assertion instead of silently
    # shipping content.
    assert set(payload.keys()) == {"kind", "where", "message", "stack", "tags"}


def test_report_error_deduplicates_the_same_exception_object(sink):
    import api.error_reporting as er

    try:
        raise ValueError("boom")
    except ValueError as exc:
        er.report_error("test:dedupe", exc)
        er.report_error("test:dedupe-again", exc)  # same object, different label

    assert len(_Sink.bodies) == 1


def test_report_error_rate_limits_per_where(sink):
    import api.error_reporting as er

    for i in range(er._RATE_MAX_PER_WHERE + 5):
        er.report_error("test:flood", ValueError(f"boom {i}"))

    assert len(_Sink.bodies) == er._RATE_MAX_PER_WHERE


def test_report_bot_error_excludes_validation_and_no_local_model_errors(sink):
    import api.amelia_bots as bots

    req = {"bot": {"id": "b1"}, "run_id": "r1", "posture": "balanced", "model": None}
    bots._report_bot_error("bots:tool:fetch_media", bots.ValidationError("bad url"), req)
    bots._report_bot_error("bots:model_call", bots.NoLocalModelError("no local model"), req)
    assert _Sink.bodies == []

    bots._report_bot_error("bots:tool:fetch_media", RuntimeError("a real bug"), req)
    assert len(_Sink.bodies) == 1
    payload = json.loads(_Sink.bodies[0])
    assert payload["kind"] == "RuntimeError"
    assert payload["tags"]["bot_id"] == "b1"
    assert payload["tags"]["run_id"] == "r1"
