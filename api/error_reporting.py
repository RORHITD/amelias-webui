"""Best-effort crash/error reporting for the Hermes WebUI desktop local server.

Scope: the Agents feature (Amelia Bot Teams' computer runner, ``api/amelia_bots.py``)
runs a tool-calling loop unattended, on a background thread, fetching and
handling media, driving MCP tools — every one of those failure surfaces used
to be silent: caught, logged into the run's own chat transcript as a short
system line, and never seen by anyone who isn't staring at that run right
now. This module is the "escalation path" the rest of the server was missing
(see ``api/crash_visibility.py``'s own docstring, which already diagnosed the
identical problem for main-thread/daemon-thread crashes but only ever wrote
the result to a local log file).

── Off by default, and never a DSN ─────────────────────────────────────────
Every function here is a no-op unless ``AMELIA_ERROR_URL`` is set. This
mirrors amelia-accounts' ``src/hosted.ts`` pattern for its sandboxed cloud
agent (``AMELIA_ERROR_URL``, never a raw Sentry DSN handed to a process that
runs code nobody at Amelia wrote) rather than the alternative this repo
explicitly rejected elsewhere: embedding a Sentry SDK + DSN in a process that
ships onto a stranger's laptop. This server has no DSN to leak in the first
place, and a self-hoster (or anyone who never pairs with Amelia's backend)
sends nothing, to anyone, ever — ``AMELIA_ERROR_URL`` is unset for them by
construction, not by a promise this file has to keep re-earning.

── What is sent, and nothing else ──────────────────────────────────────────
    kind      exception class name — "KeyError", "ConnectionResetError"
    where     a fixed label from the call site, never free text
    message   str(exception), capped, with every URL's query string removed
              and every secret-shaped value redacted through this repo's own
              credential redactor (``api.helpers``' ``_redact_fn_cached`` —
              the same function that scrubs WebUI API responses) and the
              user's home directory replaced with ``<home>``
    stack     "file:line in function" for OUR files only (see ``_OURS``)
    tags      a small, allowlisted bag of non-content identifiers — surface,
              feature, runner, bot_id, run_id, workspace_id, model, provider,
              tool, step, posture — never a free-form kwarg a caller invents

── What is NOT sent, structurally rather than by promise ──────────────────
No prompt/message/instruction content, no file contents, no tokens/keys
(redacted before anything else happens), no screenshots or other media
bytes, no local variables, no source lines, and no path under the user's
home directory.

── What is not an error ────────────────────────────────────────────────────
An agent honestly declining, a denied approval, a plan/budget limit, or a
user cancellation is a product event, not an exception — callers must never
route those here. ``NoLocalModelError`` (no local model reachable) and
``ValidationError`` (a tool call refused for an expected, user-facing
reason — an SSRF-blocked host, a size cap, a bad URL) are exactly that shape
in ``api/amelia_bots.py`` and are deliberately excluded at every call site
that reports there.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import urllib.request
from pathlib import Path

_ERROR_URL_ENV = "AMELIA_ERROR_URL"

# Files whose frames may appear in a reported stack. A frame from anywhere
# else — a provider SDK, a third-party package, a user's own MCP server, an
# extension — is dropped rather than included, the same guarantee
# amelia-accounts' sandboxed agent_server.py makes for its own stack filter
# (see its ``_OURS``). Keep in sync with the agent-runner-adjacent modules as
# they're added; this is deliberately a short, explicit allowlist rather than
# "everything under api/" so a stray frame from routes.py's 20k lines never
# becomes stack content in a crash report about the bot runner.
_OURS = frozenset({
    "amelia_bots.py",
    "amelia_media_extractor.py",
    "mcp_server.py",
    "connect.py",
    "crash_visibility.py",
    "runner_client.py",
    "agent_health.py",
    "agent_runtime.py",
    "agent_sessions.py",
    "error_reporting.py",
    "server.py",
})

_HOME = str(Path.home())

# A URL with a query string, spotted anywhere inside free text — not just in
# a field we already know is a URL. Mirrors amelia-accounts'
# observability.ts URL_WITH_QUERY: a signed download link or a callback URL
# embedded in an exception message ("could not reach https://x?token=...")
# is exactly where a secret hides in THIS codebase's tool-call failures
# (fetch_media, read_page).
_URL_WITH_QUERY = re.compile(r"\b(?:https?|wss?|ws)://[^\s\"'<>]*\?[^\s\"'<>]*")


def _bare_url(match: "re.Match[str]") -> str:
    text = match.group(0)
    q = text.find("?")
    return text if q < 0 else text[: q + 1] + "[redacted]"


def _redact(text: str) -> str:
    """Strip query strings from any URL, replace the home directory, then run
    this repo's own credential redactor — the same hard boundary
    ``api.helpers`` already applies to WebUI API responses, kept active here
    regardless of the user's ``api_redact_enabled`` setting: a crash report
    is not a session transcript the user asked to see unredacted."""
    if not text:
        return text
    out = _URL_WITH_QUERY.sub(_bare_url, text)
    if _HOME and _HOME in out:
        out = out.replace(_HOME, "<home>")
    # Deliberately NOT wrapped in its own try/except: a redactor that cannot
    # run must not become a reporter that ships unredacted text instead. Any
    # failure here propagates to report_error's own outer try/except, which
    # drops the whole report rather than risk that — "drop rather than
    # leak", the same rule amelia-accounts' scrub() follows on its own
    # exception path.
    from api.helpers import _redact_fn_cached
    return _redact_fn_cached(out)


_MAX_MESSAGE = 500
_MAX_TAG_VALUE = 120

# Sliding-window rate limit, per `where` label. This is a long-running
# desktop process (unlike the one-shot sandboxed agent this pattern is
# borrowed from), so a single lifetime cap would mean "one bad afternoon
# permanently silences reporting until the app restarts" — instead, a
# wedged loop that fires the same failure every second is capped, and a
# genuinely rare failure a week later is still reported.
_RATE_WINDOW_SECONDS = 600.0
_RATE_MAX_PER_WHERE = 5
_rate_lock = threading.Lock()
_rate_state: dict[str, list[float]] = {}

# De-dupes one exception object reported twice by two different except
# blocks on the way up the stack (the exact bug amelia-accounts'
# agent_server.py documents finding: an import failure filed once as
# "import:playbooks" and again as "uncaught"). Marked directly ON the
# exception instance rather than in an id()-keyed set: CPython reuses a
# freed object's memory address immediately (refcounting, not
# generation-deferred GC), so two DIFFERENT exceptions raised moments apart
# in a tight loop can share an id() — an id()-keyed table would then treat
# the second, unrelated failure as a duplicate of the first and silently
# drop it forever. An attribute on the live object has no such collision:
# it can only ever be read back on the SAME object, for as long as that
# object is reachable.
_REPORTED_ATTR = "_amelia_error_reported"

_ALLOWED_TAGS = frozenset({
    "surface", "feature", "runner", "bot_id", "run_id", "workspace_id",
    "session_id", "model", "provider", "tool", "step", "posture",
})


def error_url() -> str:
    return os.environ.get(_ERROR_URL_ENV, "").strip()


def error_reporting_enabled() -> bool:
    """Is anything actually being reported? Exposed so status/health surfaces
    can answer that truthfully instead of a caller having to reason about
    ``AMELIA_ERROR_URL`` themselves."""
    return bool(error_url())


def _rate_limited(where: str) -> bool:
    now = time.time()
    with _rate_lock:
        hits = _rate_state.setdefault(where, [])
        hits[:] = [t for t in hits if now - t < _RATE_WINDOW_SECONDS]
        if len(hits) >= _RATE_MAX_PER_WHERE:
            return True
        hits.append(now)
        return False


def _dedupe(exc: BaseException) -> bool:
    """True if *exc* was already reported (by this exact object) and should
    be skipped again. See ``_REPORTED_ATTR`` for why this is an attribute on
    the object rather than an id()-keyed table."""
    try:
        if getattr(exc, _REPORTED_ATTR, False):
            return True
        setattr(exc, _REPORTED_ATTR, True)
        return False
    except Exception:
        # Some builtin exception subtypes (rare) can refuse an extra
        # attribute; failing "not a duplicate" is the safe direction — a
        # false negative here means at worst one duplicate report, not a
        # dropped one.
        return False


def report_error(where: str, exc: BaseException, tags: dict | None = None) -> None:
    """File a crash. Never raises, never blocks the caller for long past the
    request timeout, and does nothing at all unless ``AMELIA_ERROR_URL`` is
    configured.

    ``where`` is a fixed label from the call site (e.g. ``"bots:runner"``),
    never free text built from user input. ``tags`` is filtered to
    ``_ALLOWED_TAGS`` so a caller cannot widen what leaves this process by
    adding a new kwarg without also updating the allowlist here.
    """
    url = error_url()
    if not url:
        return
    try:
        if _dedupe(exc):
            return
        if _rate_limited(where):
            return
        frames = []
        tb = getattr(exc, "__traceback__", None)
        if tb is not None:
            for frame in traceback.extract_tb(tb):
                name = os.path.basename(frame.filename)
                if name in _OURS:
                    frames.append(f"{name}:{frame.lineno} in {frame.name}")

        clean_tags: dict[str, str] = {"surface": "desktop"}
        for key, value in (tags or {}).items():
            if key not in _ALLOWED_TAGS or value is None:
                continue
            clean_tags[key] = _redact(str(value))[:_MAX_TAG_VALUE]

        payload = {
            "kind": type(exc).__name__[:120],
            "where": str(where)[:60],
            "message": _redact(str(exc))[:_MAX_MESSAGE],
            # Newest last, the way a traceback reads. Bounded, because a
            # runaway recursion produces thousands of identical lines.
            "stack": "\n".join(frames[-25:]),
            "tags": clean_tags,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        # Best effort, in the strongest sense: a reporter that can itself
        # raise or block is a worse bug than the one it exists to catch.
        pass
