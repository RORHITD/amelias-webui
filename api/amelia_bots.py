"""Amelia Bot Teams — the desktop "computer runner" for bots.

Scope, read alongside ``SPEC.md`` §"How a bot runs → Computer runner": the
Amelia server (a separate service, not in this repo) decides a bot should run
on this machine and relays ``POST /api/amelia/bots/run`` to us through the
machine relay (``connect.py`` ``serve_one``, which proxies an authenticated
websocket frame from the paired relay session into a plain loopback HTTP
request — see ``connect.py`` and ``api/auth.py`` ``PUBLIC_PATHS`` for why this
path does not also require a browser session).

This module is the entire computer-runner: it validates the run request,
answers 202 immediately, and does the actual work — an at-most-8-step
tool-calling loop against a LOCAL model only (never a cloud model, regardless
of posture, unless the bot's posture explicitly allows the user's own
locally-configured API key) — on a background thread from a small bounded
pool. Progress and results are POSTed back to ``callback_url`` with the
per-run token; nothing here ever logs that token, and nothing here ever
*executes* a risky action (email, message, public post, spend, delete, run a
command) — those are always reported as ``approval_requests`` for the Amelia
server to evaluate and, if allowed, carry out itself.

State (pause flag, last capacity measurement) is persisted under this
module's own directory beneath ``STATE_DIR`` — see ``api/shares.py`` for the
atomic-write convention this mirrors.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from api.config import STATE_DIR, cfg

# ── constants ─────────────────────────────────────────────────────────────

BOTS_DIR = STATE_DIR / "amelia_bots"
_STATE_PATH = BOTS_DIR / "state.json"
_CAPACITY_PATH = BOTS_DIR / "capacity.json"
_STATE_LOCK = threading.RLock()

MAX_STEPS = 8
DEFAULT_MAX_PARALLEL = 2
RUN_TIMEOUT_SECONDS = 120.0
CALLBACK_MAX_ATTEMPTS = 4
CALLBACK_BACKOFF_BASE = 0.5

RISKY_ACTIONS = frozenset({
    "send_email", "send_message", "post_public", "spend_money", "delete", "run_command",
})
# Tools the computer runner is trusted to execute directly — never a risky action.
SAFE_TOOLS = frozenset({"read_page", "post_message", "update_task", "remember"})

READ_PAGE_MAX_BYTES = 200_000
READ_PAGE_TIMEOUT_SECONDS = 10.0

_FAKE_MODEL_ENV = "BOTS_FAKE_MODEL"


def fake_model_enabled() -> bool:
    return str(os.environ.get(_FAKE_MODEL_ENV, "")).strip() in ("1", "true", "yes")


_LOOPBACK_ADDRS = {"127.0.0.1", "::1"}


def is_loopback_client(handler) -> bool:
    """True only when the TCP peer is this machine.

    PUBLIC_PATHS drops the browser-session requirement for these routes (see
    api/auth.py), so this is the entire remaining trust boundary: whoever can
    open a loopback socket to this port. Both legitimate callers already
    satisfy it — connect.py's ``serve_one`` proxies over ``http.client`` to
    ``127.0.0.1`` (see connect.py), and the Tauri tray calls the local server
    directly — so this never has to reject a real caller, only a request that
    somehow arrived from off-machine (e.g. HERMES_WEBUI_HOST overridden to
    bind non-loopback). Fails closed: an address that can't be read is
    treated as non-loopback.
    """
    try:
        addr = handler.client_address[0]
    except Exception:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_loopback or addr in _LOOPBACK_ADDRS


# ── persisted state (pause flag + capacity result) ──────────────────────────
#
# Mirrors api/shares.py's tempfile+fsync+os.replace atomic-write pattern.
# Kept as a small local copy rather than a shared import — the repo's existing
# convention (see api/media_snapshots.py) is each module owns its own copy of
# this helper instead of a cross-module dependency for a few lines of I/O.

def _write_json_atomic(path: Path, payload: dict) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_state() -> dict:
    with _STATE_LOCK:
        return _read_json(_STATE_PATH)


def _save_state(state: dict) -> None:
    with _STATE_LOCK:
        _write_json_atomic(_STATE_PATH, state)


def is_paused() -> bool:
    return bool(_load_state().get("paused", False))


def set_paused(paused: bool) -> None:
    state = _load_state()
    state["paused"] = bool(paused)
    _save_state(state)


def load_capacity() -> dict | None:
    data = _read_json(_CAPACITY_PATH)
    return data or None


def _save_capacity(result: dict) -> None:
    _write_json_atomic(_CAPACITY_PATH, result)


def configured_max_parallel() -> int:
    """The pool size to use right now: a measured value, else the default."""
    cap = load_capacity()
    if cap and isinstance(cap.get("max_parallel"), int) and cap["max_parallel"] > 0:
        return cap["max_parallel"]
    return DEFAULT_MAX_PARALLEL


# ── bounded worker pool ──────────────────────────────────────────────────────
#
# Self-contained rather than reusing api/background_process.py's session-event
# machinery: that module is coupled to chat-session streaming/SSE state this
# runner has nothing to do with. A dedicated bounded semaphore keeps "max
# parallel bot runs" simple to reason about and simple to unit-test.

class _RunPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, dict] = {}
        self._queue: list[Callable[[], None]] = []

    def _capacity(self) -> int:
        return max(1, configured_max_parallel())

    def submit(self, run_id: str, fn: Callable[[], None]) -> None:
        with self._lock:
            self._running[run_id] = {"started_at": time.time()}

        def _wrapped():
            try:
                fn()
            finally:
                with self._lock:
                    self._running.pop(run_id, None)
                self._drain()

        with self._lock:
            if len(self._running) - 1 < self._capacity():  # slot already reserved above
                threading.Thread(target=_wrapped, daemon=True, name=f"bot-run-{run_id}").start()
                return
            # Over capacity: release the reservation, queue instead.
            self._running.pop(run_id, None)
            self._queue.append(lambda: self._start(run_id, fn))

    def _start(self, run_id: str, fn: Callable[[], None]) -> None:
        with self._lock:
            self._running[run_id] = {"started_at": time.time()}

        def _wrapped():
            try:
                fn()
            finally:
                with self._lock:
                    self._running.pop(run_id, None)
                self._drain()

        threading.Thread(target=_wrapped, daemon=True, name=f"bot-run-{run_id}").start()

    def _drain(self) -> None:
        with self._lock:
            if not self._queue or len(self._running) >= self._capacity():
                return
            nxt = self._queue.pop(0)
        nxt()

    def status(self) -> dict:
        with self._lock:
            return {"running": len(self._running), "queued": len(self._queue)}


_POOL = _RunPool()


# ── validation ───────────────────────────────────────────────────────────────

class ValidationError(ValueError):
    pass


def _require_str(body: dict, key: str) -> str:
    val = body.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValidationError(f"missing or invalid '{key}'")
    return val


def validate_run_request(body: dict) -> dict:
    """Validate the SPEC body for POST /api/amelia/bots/run.

    Body shape (SPEC.md "Computer runner"):
      {run_id, token, callback_url, bot:{...}, context:[...], prompt, tools,
       model_hint:{posture, model}}
    """
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    run_id = _require_str(body, "run_id")
    token = _require_str(body, "token")
    callback_url = _require_str(body, "callback_url")
    bot = body.get("bot")
    if not isinstance(bot, dict) or not bot.get("id"):
        raise ValidationError("missing or invalid 'bot'")
    prompt = body.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise ValidationError("'prompt' must be a string when present")
    context = body.get("context")
    if context is not None and not isinstance(context, list):
        raise ValidationError("'context' must be a list when present")
    tools = body.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise ValidationError("'tools' must be a list when present")
    model_hint = body.get("model_hint") or {}
    if not isinstance(model_hint, dict):
        raise ValidationError("'model_hint' must be an object when present")

    parsed = urllib.parse.urlsplit(callback_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("'callback_url' must be an http(s) URL")

    return {
        "run_id": run_id,
        "token": token,
        "callback_url": callback_url,
        "bot": bot,
        "prompt": prompt or "",
        "context": context or [],
        "tools": tools or [],
        "posture": str(model_hint.get("posture") or bot.get("posture") or "balanced"),
        "model": model_hint.get("model") or bot.get("model"),
    }


# ── SSRF-safe read_page ──────────────────────────────────────────────────────
#
# Local copy of the private-network guard, matching api/routes.py's
# `_tts_addr_is_blocked` / `_tts_host_is_blocked_target` / pin-and-resolve
# trio (module-local copies are this repo's existing convention rather than a
# generic shared helper — see api/media_snapshots.py's atomic-write copy).

def _addr_is_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_pinned_address(hostname: str, port: int | None) -> str:
    host = (hostname or "").strip().lower()
    if not host:
        raise ValidationError("invalid read_page host")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise ValidationError("could not resolve read_page host") from exc
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        pinned = str(sockaddr[0])
        if _addr_is_blocked(pinned):
            raise ValidationError("read_page target is not allowed (private network)")
        return pinned
    raise ValidationError("could not resolve read_page host")


def read_page(url: str) -> dict:
    """Local, size/time-capped fetch. http(s) only; refuses private-network
    addresses (including a hostname that resolves to one, DNS-rebinding
    style)."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValidationError("read_page url must be http(s)")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # Resolve-and-pin before connecting (TOCTOU-safe, matches the tts helper).
    _resolve_pinned_address(parsed.hostname, port)

    req = urllib.request.Request(url, headers={"User-Agent": "Amelia-Bot-Runner/1.0"})

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    start = time.time()
    with opener.open(req, timeout=READ_PAGE_TIMEOUT_SECONDS) as resp:
        raw = resp.read(READ_PAGE_MAX_BYTES + 1)
        truncated = len(raw) > READ_PAGE_MAX_BYTES
        raw = raw[:READ_PAGE_MAX_BYTES]
        text = raw.decode("utf-8", errors="replace")
    return {
        "url": url,
        "status": getattr(resp, "status", 200),
        "text": text,
        "truncated": truncated,
        "elapsed_ms": int((time.time() - start) * 1000),
    }


# ── local model discovery ────────────────────────────────────────────────────
#
# Uniform OpenAI-compatible surface (/v1/models, /v1/chat/completions) —
# Ollama, LM Studio, and mlx_lm.server all speak it, so one client covers all
# three rather than three bespoke wire formats.

_LOCAL_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("ollama", "http://127.0.0.1:11434"),
    ("lmstudio", "http://127.0.0.1:1234"),
    ("mlx", "http://127.0.0.1:8080"),
)


def _configured_base_url(provider_id: str) -> str | None:
    try:
        from api.config import _get_provider_base_url
        return _get_provider_base_url(provider_id)
    except Exception:
        return None


def _probe_openai_compat(base_url: str, timeout: float = 2.5) -> list[str] | None:
    """Return the model id list if base_url answers GET /v1/models, else None."""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/v1/models")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        ids = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
        return ids
    except Exception:
        return None


def discover_local_models() -> list[dict]:
    """Return every reachable local OpenAI-compatible server, each as
    {provider, base_url, models:[...]}. Read-only: never starts or stops a
    local model server."""
    found: list[dict] = []
    seen_urls: set[str] = set()
    for provider_id, default_base in _LOCAL_CANDIDATES:
        base = _configured_base_url(provider_id) or default_base
        if base in seen_urls:
            continue
        models = _probe_openai_compat(base)
        if models is not None:
            seen_urls.add(base)
            found.append({"provider": provider_id, "base_url": base, "models": models})
    return found


_OWN_KEY_DEFAULTS = (
    ("openai", "https://api.openai.com", "gpt-5.4-mini"),
    ("anthropic", "https://api.anthropic.com", "claude-haiku-3.5"),
)


def _own_api_key() -> dict | None:
    """A user-configured cloud API key usable locally, when posture allows it.

    Checked only for non-private postures, and only as a fallback when no
    local model is reachable — private posture never reaches this function
    (see choose_model_endpoint). Reuses api/providers.py's own credential
    lookup (env var, .env, credential pool, config.yaml — see
    ``_get_provider_api_key``) rather than re-deriving it here, so a key the
    user already configured through onboarding/settings is recognized the
    same way the rest of the app recognizes it.
    """
    try:
        from api.providers import provider_has_usable_credential, _get_provider_api_key
    except Exception:
        return None
    for provider_id, base_url, default_model in _OWN_KEY_DEFAULTS:
        try:
            if not provider_has_usable_credential(provider_id):
                continue
            key = _get_provider_api_key(provider_id)
        except Exception:
            continue
        if key:
            base = _configured_base_url(provider_id) or base_url
            return {"provider": provider_id, "base_url": base, "api_key": key, "model": default_model}
    return None


class NoLocalModelError(RuntimeError):
    pass


def choose_model_endpoint(posture: str, model_hint: str | None) -> dict:
    """Pick where bot-step model calls go: {provider, base_url, model, api_key?}.

    Private posture = local model only, ever. Every other posture prefers a
    local model too (it's free), and only falls back to the user's own
    configured key when no local model is reachable.
    """
    local = discover_local_models()
    if local:
        chosen = local[0]
        model = model_hint if (model_hint and model_hint in chosen["models"]) else (
            chosen["models"][0] if chosen["models"] else model_hint
        )
        if not model:
            raise NoLocalModelError("local server has no models loaded")
        return {"provider": chosen["provider"], "base_url": chosen["base_url"], "model": model, "api_key": None}

    if posture == "private":
        raise NoLocalModelError("no local model reachable and posture is private")

    own_key = _own_api_key()
    if own_key:
        return {
            "provider": own_key["provider"],
            "base_url": own_key["base_url"],
            "model": model_hint or own_key["model"],
            "api_key": own_key["api_key"],
        }
    raise NoLocalModelError("no local model reachable and no local API key configured")


# ── the fake model (BOTS_FAKE_MODEL=1) ───────────────────────────────────────
#
# Deterministic, no network, $0 — mirrors SPEC.md "Fake model for tests":
# a scripted reply can contain a tool call. `[[email:a@b.com]]` in the prompt
# makes the fake model call send_email; `[[mention:Writer]]` makes it post a
# message mentioning Writer.

_EMAIL_TRIGGER = re.compile(r"\[\[email:([^\]]+)\]\]")
_MENTION_TRIGGER = re.compile(r"\[\[mention:([^\]]+)\]\]")


def _fake_model_step(prompt: str, step: int, origin: str) -> dict:
    """Return {tool, args, done} for one fake-model step, scripted from the
    prompt's trigger markers so tests can exercise approvals and handoffs
    deterministically."""
    m = _EMAIL_TRIGGER.search(prompt)
    if m:
        return {
            "tool": "send_email",
            "args": {"to": m.group(1), "subject": "Fake model test email", "body": prompt},
            "origin": origin,
            "done": True,
        }
    m = _MENTION_TRIGGER.search(prompt)
    if m:
        return {
            "tool": "post_message",
            "args": {"text": f"@{m.group(1)} — fake model handoff"},
            "origin": "user",
            "done": True,
        }
    return {
        "tool": "post_message",
        "args": {"text": f"(fake model) step {step}: {prompt[:120]}"},
        "origin": "user",
        "done": True,
    }


def _call_local_model(endpoint: dict, messages: list[dict], timeout: float = 30.0) -> str:
    """One non-streaming OpenAI-compatible chat completion call."""
    url = endpoint["base_url"].rstrip("/") + "/v1/chat/completions"
    payload = {"model": endpoint["model"], "messages": messages, "stream": False}
    headers = {"Content-Type": "application/json"}
    if endpoint.get("api_key"):
        headers["Authorization"] = f"Bearer {endpoint['api_key']}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    choices = data.get("choices") or []
    if not choices:
        return ""
    return ((choices[0].get("message") or {}).get("content")) or ""


# ── the tool-calling loop ────────────────────────────────────────────────────

def run_bot_steps(req: dict, emit) -> None:
    """Run at most MAX_STEPS steps for one bot run, calling emit(kind, **kw)
    for every event, approval_request, and task update. Never executes a
    risky action — those are always emitted as approval_requests."""
    run_id = req["run_id"]
    prompt = req["prompt"] or "Say hello."
    posture = req["posture"]

    try:
        endpoint = None if fake_model_enabled() else choose_model_endpoint(posture, req.get("model"))
    except NoLocalModelError:
        emit("event", kind="status", body="no local model available", data={"bot_id": req["bot"].get("id"), "state": "idle"})
        emit("done", done=True, error="no_local_model")
        return

    emit("event", kind="status", body="working", data={"bot_id": req["bot"].get("id"), "state": "working"})

    context_had_content = any(
        isinstance(ev, dict) and ev.get("kind") in ("message", "result") and ev.get("author", {}).get("type") != "user"
        for ev in (req.get("context") or [])
    )
    origin = "content" if context_had_content else "user"

    for step in range(1, MAX_STEPS + 1):
        if fake_model_enabled():
            decision = _fake_model_step(prompt, step, origin)
        else:
            try:
                reply = _call_local_model(endpoint, [{"role": "user", "content": prompt}])
            except Exception as exc:
                emit("event", kind="system", body=f"model call failed: {exc}", data={})
                break
            decision = {"tool": "post_message", "args": {"text": reply}, "origin": origin, "done": True}

        tool = decision["tool"]
        args = decision["args"]
        step_origin = decision.get("origin", origin)

        if tool in RISKY_ACTIONS:
            emit(
                "approval_request",
                action=tool,
                summary=_summarize(tool, args),
                payload=args,
                origin=step_origin,
            )
        elif tool == "post_message":
            emit("event", kind="message", body=args.get("text", ""), data={})
        elif tool == "update_task":
            emit("task", title=args.get("title", ""), status=args.get("status", "todo"))
        elif tool == "remember":
            emit("event", kind="system", body=f"remembered: {args.get('note', '')}", data={})
        elif tool == "read_page":
            try:
                page = read_page(args.get("url", ""))
                emit("event", kind="system", body=f"read {page['url']} ({len(page['text'])} chars)", data={})
            except Exception as exc:
                emit("event", kind="system", body=f"read_page failed: {exc}", data={})
        else:
            emit("event", kind="system", body=f"unknown tool '{tool}' ignored", data={})

        if decision.get("done"):
            break

    emit("event", kind="status", body="idle", data={"bot_id": req["bot"].get("id"), "state": "idle"})
    emit("done", done=True)


def _summarize(action: str, args: dict) -> str:
    if action == "send_email":
        return f"Send an email to {args.get('to', '?')}: {args.get('subject', '')}"
    if action == "send_message":
        return f"Send a message to {args.get('to', '?')}"
    if action == "post_public":
        return f"Post publicly: {str(args.get('text', ''))[:80]}"
    if action == "spend_money":
        return f"Spend {args.get('amount_cents', '?')} cents"
    if action == "delete":
        return f"Delete {args.get('target', '?')}"
    if action == "run_command":
        return f"Run command: {args.get('command', '?')}"
    return f"{action}: {json.dumps(args)[:120]}"


# ── posting back to the Amelia server ────────────────────────────────────────
#
# Modeled on api/runner_client.py's HttpRunnerClient: Bearer auth, no-redirect
# opener (a redirect could otherwise smuggle the run token to another host),
# scheme validation, and — the one rule that matters most here — the token is
# NEVER included in any log line, exception message, or emitted event.

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def post_callback(callback_url: str, token: str, payload: dict) -> bool:
    """POST one batch to the runner callback. Retries with backoff. Returns
    True on any 2xx; never raises, never logs the token."""
    scheme = urllib.parse.urlsplit(callback_url).scheme.lower()
    if scheme not in ("http", "https"):
        return False
    body = dict(payload)
    body["token"] = token
    data = json.dumps(body).encode()
    delay = CALLBACK_BACKOFF_BASE
    for attempt in range(1, CALLBACK_MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                callback_url, data=data,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(req, timeout=15) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                return False  # not retryable — a 401/404 will not heal itself
        except Exception:
            pass
        if attempt < CALLBACK_MAX_ATTEMPTS:
            time.sleep(delay)
            delay *= 2
    return False


# ── the batching emitter used by run_bot_steps ───────────────────────────────

class _CallbackEmitter:
    """Collects events for one run and flushes them to callback_url. The
    'done' emit always flushes synchronously so a caller waiting on the run's
    terminal state (a test, or a poll) observes it promptly."""

    def __init__(self, callback_url: str, token: str, run_id: str) -> None:
        self.callback_url = callback_url
        self.token = token
        self.run_id = run_id
        self.events: list[dict] = []
        self.approval_requests: list[dict] = []
        self.tasks: list[dict] = []

    def __call__(self, emit_kind: str, **kw) -> None:
        if emit_kind == "event":
            self.events.append({"kind": kw["kind"], "body": kw.get("body", ""), "data": kw.get("data", {})})
            self._flush(done=False)
        elif emit_kind == "approval_request":
            self.approval_requests.append({
                "action": kw["action"], "summary": kw["summary"],
                "payload": kw["payload"], "origin": kw["origin"],
            })
            self._flush(done=False)
        elif emit_kind == "task":
            self.tasks.append({"title": kw["title"], "status": kw["status"]})
            self._flush(done=False)
        elif emit_kind == "done":
            self._flush(done=True, error=kw.get("error"))

    def _flush(self, *, done: bool, error: str | None = None) -> None:
        payload = {
            "events": self.events, "approval_requests": self.approval_requests,
            "tasks": self.tasks, "done": done,
        }
        if error:
            payload["error"] = error
        post_callback(self.callback_url, self.token, payload)
        self.events, self.approval_requests, self.tasks = [], [], []


def start_run(req: dict) -> None:
    """Submit a validated run request to the bounded pool."""
    emitter = _CallbackEmitter(req["callback_url"], req["token"], req["run_id"])

    def _work():
        try:
            run_bot_steps(req, emitter)
        except Exception as exc:
            emitter("event", kind="system", body=f"runner error: {type(exc).__name__}", data={})
            emitter("done", done=True, error="runner_exception")

    _POOL.submit(req["run_id"], _work)


# ── status / capacity ────────────────────────────────────────────────────────

def status_snapshot() -> dict:
    pool = _POOL.status()
    local = discover_local_models()
    cap = load_capacity()
    return {
        "running": pool["running"],
        "queued": pool["queued"],
        "max_parallel": configured_max_parallel(),
        "local_models": [m["provider"] for m in local],
        "paused": is_paused(),
    }


def measure_capacity(levels: tuple[int, ...] = (1, 2, 4, 8), samples_per_level: int = 3) -> dict:
    """Run short generations at increasing concurrency and pick max_parallel
    as the highest level whose p95 time-to-first-token stays <= 3s.

    Uses the fake model under BOTS_FAKE_MODEL=1 (deterministic, instant) so
    tests can assert the selection logic without real inference; otherwise
    calls the first reachable local model.
    """
    if fake_model_enabled():
        endpoint = None
    else:
        endpoint = choose_model_endpoint("balanced", None)

    results = []
    for n in levels:
        ttfts = []
        toks = []

        def _one_call():
            start = time.time()
            if fake_model_enabled():
                delay = float(os.environ.get("BOTS_FAKE_MODEL_LATENCY_MS", "50")) / 1000.0
                delay *= 1.0 + 0.15 * (n / max(levels))  # gentle, deterministic degradation with load
                time.sleep(delay)
                ttft = time.time() - start
                text = "ok " * 8
            else:
                t0 = time.time()
                text = _call_local_model(endpoint, [{"role": "user", "content": "Reply with one short word."}], timeout=20.0)
                ttft = time.time() - t0
            elapsed = max(time.time() - start, 1e-6)
            return ttft, len(text.split()) / elapsed

        for _ in range(samples_per_level):
            threads_results: list[tuple[float, float]] = []
            lock = threading.Lock()
            barrier_threads = []

            def _runner():
                r = _one_call()
                with lock:
                    threads_results.append(r)

            for _ in range(n):
                t = threading.Thread(target=_runner)
                barrier_threads.append(t)
                t.start()
            for t in barrier_threads:
                t.join()
            for ttft, tok_s in threads_results:
                ttfts.append(ttft)
                toks.append(tok_s)

        ttfts.sort()
        p95_idx = max(0, int(round(0.95 * (len(ttfts) - 1))))
        p95_ms = int(ttfts[p95_idx] * 1000) if ttfts else 0
        tok_s = round(sum(toks) / len(toks), 2) if toks else 0.0
        results.append({"n": n, "ttft_p95_ms": p95_ms, "tok_s": tok_s})

    max_parallel = 1
    for level in results:
        if level["ttft_p95_ms"] <= 3000:
            max_parallel = level["n"]
        else:
            break

    result = {
        "model": (endpoint or {}).get("model") if endpoint else "fake-model",
        "levels": results,
        "max_parallel": max_parallel,
        "measured_at": time.time(),
    }
    _save_capacity(result)
    return result


# ── HTTP glue for api/routes.py ──────────────────────────────────────────────
#
# Pure (status_code, payload) returns — matching the rest of the codebase's
# convention that route handlers own response writing (j()/bad()) while
# feature modules return data (see api/background.py's get_results()).

def handle_run_request(handler, body: dict) -> tuple[int, dict]:
    if not is_loopback_client(handler):
        return 403, {"error": "forbidden"}
    if is_paused():
        return 409, {"error": "paused"}
    try:
        req = validate_run_request(body)
    except ValidationError as exc:
        return 400, {"error": str(exc)}
    start_run(req)
    return 202, {"ok": True, "run_id": req["run_id"]}


def handle_status_request(handler) -> tuple[int, dict]:
    if not is_loopback_client(handler):
        return 403, {"error": "forbidden"}
    return 200, status_snapshot()


def handle_pause_request(handler, body: dict) -> tuple[int, dict]:
    if not is_loopback_client(handler):
        return 403, {"error": "forbidden"}
    if not isinstance(body, dict) or not isinstance(body.get("paused"), bool):
        return 400, {"error": "'paused' must be a boolean"}
    set_paused(body["paused"])
    return 200, {"paused": body["paused"]}


def handle_capacity_measure_request(handler, body: dict) -> tuple[int, dict]:
    if not is_loopback_client(handler):
        return 403, {"error": "forbidden"}
    try:
        result = measure_capacity()
    except NoLocalModelError as exc:
        return 200, {"error": "no_local_model", "message": str(exc)}
    return 200, result


def handle_capacity_get_request(handler) -> tuple[int, dict]:
    if not is_loopback_client(handler):
        return 403, {"error": "forbidden"}
    cap = load_capacity()
    if cap is None:
        return 200, {"model": None, "levels": [], "max_parallel": DEFAULT_MAX_PARALLEL, "measured_at": None}
    return 200, cap
