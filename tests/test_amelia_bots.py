"""Tests for the Amelia Bot Teams computer runner (api/amelia_bots.py).

Two layers:

* **Unit tests** (no server) exercise validation, the SSRF guard, model-
  endpoint selection, and the fake-model / capacity-selection logic directly
  against the module — fast, and they don't depend on anything network-ish.
* **HTTP tests** spin up a *dedicated* isolated server subprocess for this
  file only (own ephemeral port, own HERMES_HOME/state dir, BOTS_FAKE_MODEL=1
  so no real model or network is ever touched) rather than sharing
  conftest's session-scoped ``test_server`` — that shared server does not run
  with ``BOTS_FAKE_MODEL=1``, and toggling env on a subprocess already
  running would not do anything anyway. This file never binds 8787 and never
  touches ``~/.hermes``.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

REPO = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from tests.conftest import REPO_ROOT, SERVER_SCRIPT, VENV_PYTHON, WORKDIR, _auto_test_port, _wait_for_server

import api.amelia_bots as bots


# ── unit tests: validation ──────────────────────────────────────────────────

def test_validate_run_request_accepts_a_full_body():
    body = {
        "run_id": "run_1", "token": "tok", "callback_url": "http://127.0.0.1:9/cb",
        "bot": {"id": "bot_1", "posture": "balanced"}, "prompt": "hi",
        "context": [], "tools": [], "model_hint": {"posture": "balanced"},
    }
    req = bots.validate_run_request(body)
    assert req["run_id"] == "run_1"
    assert req["posture"] == "balanced"


@pytest.mark.parametrize("missing", ["run_id", "token", "callback_url", "bot"])
def test_validate_run_request_rejects_missing_field(missing):
    body = {
        "run_id": "run_1", "token": "tok", "callback_url": "http://127.0.0.1:9/cb",
        "bot": {"id": "bot_1"},
    }
    del body[missing]
    with pytest.raises(bots.ValidationError):
        bots.validate_run_request(body)


def test_validate_run_request_rejects_non_http_callback():
    body = {
        "run_id": "run_1", "token": "tok", "callback_url": "ftp://evil/cb",
        "bot": {"id": "bot_1"},
    }
    with pytest.raises(bots.ValidationError):
        bots.validate_run_request(body)


# ── unit tests: SSRF guard on read_page ─────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1/x",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata endpoint
    "http://10.1.2.3/x",
    "http://192.168.1.1/x",
    "http://[::1]/x",
])
def test_read_page_refuses_private_network_targets(url):
    with pytest.raises(bots.ValidationError):
        bots.read_page(url)


def test_read_page_refuses_non_http_scheme():
    with pytest.raises(bots.ValidationError):
        bots.read_page("file:///etc/passwd")


def test_read_page_fetches_a_real_public_style_target(monkeypatch):
    """Not a network test: proves the *allow* path by faking DNS resolution
    to a public-looking address and serving from a local fixture server, so
    the guard's allow-branch has real coverage without touching the network."""
    srv = HTTPServer(("127.0.0.1", 0), _StaticPageHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    try:
        # The guard itself is exercised by the dedicated refusal tests above;
        # this test proves the *allow* path actually fetches content
        # end-to-end, so it bypasses just the guard (not DNS/connection —
        # the URL is real loopback, so the real fetch is real).
        monkeypatch.setattr(bots, "_resolve_pinned_address", lambda host, port_: "127.0.0.1")
        page = bots.read_page(f"http://127.0.0.1:{port}/")
        assert "hello from fixture" in page["text"]
        assert page["status"] == 200
    finally:
        srv.shutdown()
        srv.server_close()


class _StaticPageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello from fixture"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


# ── unit tests: local model discovery / posture ────────────────────────────

@pytest.fixture(autouse=True)
def _no_real_ollama_ps_probe(monkeypatch):
    """Every test in this module that doesn't explicitly test
    `_ollama_loaded_models` itself should stay hermetic — this machine
    happens to have a real Ollama server on 11434, and without this a
    "unit" test would silently make a real network call and could pass or
    fail depending on what's loaded on the developer's box at the time."""
    monkeypatch.setattr(bots, "_ollama_loaded_models", lambda *a, **kw: set())


def test_choose_model_endpoint_prefers_local_over_own_key(monkeypatch):
    monkeypatch.setattr(bots, "discover_local_models", lambda: [
        {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "models": ["m1"]}
    ])
    monkeypatch.setattr(bots, "_own_api_key", lambda: {"provider": "openai", "base_url": "x", "api_key": "k", "model": "gpt"})
    endpoint = bots.choose_model_endpoint("balanced", None)
    assert endpoint["provider"] == "ollama"
    assert endpoint["api_key"] is None


def test_choose_model_endpoint_private_posture_never_uses_own_key(monkeypatch):
    """Private posture = local model only, ever — even when a usable cloud
    key is configured. This is the load-bearing privacy guarantee."""
    monkeypatch.setattr(bots, "discover_local_models", lambda: [])
    monkeypatch.setattr(bots, "_own_api_key", lambda: {"provider": "openai", "base_url": "x", "api_key": "k", "model": "gpt"})
    with pytest.raises(bots.NoLocalModelError):
        bots.choose_model_endpoint("private", None)


def test_choose_model_endpoint_balanced_falls_back_to_own_key_when_no_local(monkeypatch):
    monkeypatch.setattr(bots, "discover_local_models", lambda: [])
    monkeypatch.setattr(bots, "_own_api_key", lambda: {"provider": "openai", "base_url": "https://api.openai.com", "api_key": "k", "model": "gpt-5.4-mini"})
    endpoint = bots.choose_model_endpoint("balanced", None)
    assert endpoint["provider"] == "openai"
    assert endpoint["api_key"] == "k"


def test_choose_model_endpoint_no_local_no_key_raises(monkeypatch):
    monkeypatch.setattr(bots, "discover_local_models", lambda: [])
    monkeypatch.setattr(bots, "_own_api_key", lambda: None)
    with pytest.raises(bots.NoLocalModelError):
        bots.choose_model_endpoint("balanced", None)


def test_choose_model_endpoint_skips_provider_with_only_excluded_models(monkeypatch):
    """A reachable Ollama whose only models are uncensored/roleplay/heretic
    finetunes must be treated the same as 'no usable local model' — the
    exact regression this fix is for: the first real run on this box picked
    an abliterated 35B model because it was simply first in the list."""
    monkeypatch.setattr(bots, "discover_local_models", lambda: [
        {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "models": [
            "heretic-q4:latest", "qwen3.8-uncensored-orca:latest",
        ]},
    ])
    monkeypatch.setattr(bots, "_own_api_key", lambda: None)
    with pytest.raises(bots.NoLocalModelError, match="uncensored"):
        bots.choose_model_endpoint("private", None)


def test_choose_model_endpoint_falls_through_to_own_key_when_only_excluded(monkeypatch):
    monkeypatch.setattr(bots, "discover_local_models", lambda: [
        {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "models": ["heretic-q4:latest"]},
    ])
    monkeypatch.setattr(bots, "_own_api_key", lambda: {"provider": "openai", "base_url": "https://api.openai.com", "api_key": "k", "model": "gpt-5.4-mini"})
    endpoint = bots.choose_model_endpoint("balanced", None)
    assert endpoint["provider"] == "openai"


def test_choose_model_endpoint_picks_the_one_allowed_model_among_excluded(monkeypatch):
    """Regression proof against the real Ollama list on the dev box this
    feature was built on: qwen3.8-32k must be selectable even sitting next
    to a pile of excluded finetunes on the same server."""
    monkeypatch.setattr(bots, "discover_local_models", lambda: [
        {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "models": [
            "hf.co/PocketAiHub/Ornith-1.5-35B-A3B-Abliterated-GGUF:Q8_0",
            "heretic-q4:latest",
            "huihui_ai/qwen3.8-abliterated:27b-q8_0",
            "qwen3.8-uncensored-orca:latest",
            "qwen3.8-32k:latest",
            "qwen3-vl:latest",
            "nomic-embed-text:latest",
        ]},
    ])
    endpoint = bots.choose_model_endpoint("private", None)
    assert endpoint["model"] == "qwen3.8-32k:latest"


# ── unit tests: model selection policy (is_excluded_model / select_model) ──

@pytest.mark.parametrize("name", [
    "hf.co/PocketAiHub/Ornith-1.5-35B-A3B-Abliterated-GGUF:Q8_0",
    "heretic-q4:latest",
    "huihui_ai/qwen3.8-abliterated:27b-q8_0",
    "qwen3.8-uncensored-orca:latest",
    "some-nsfw-chat-model",
    "llama3-roleplay-8b",
    "mixtral-role-play-finetune",
    "RP-Mistral-7B",  # case-insensitive
    "nomic-embed-text:latest",
    "qwen3-vl:latest",
    "",
    None,
])
def test_is_excluded_model_matches_policy(name):
    assert bots.is_excluded_model(name) is True


@pytest.mark.parametrize("name", [
    "qwen3.8:27b", "qwen3.6:35b-a3b-mtp-q8_0", "gpt-oss-20b", "gemma-2-9b-it",
    "llama-3.1-8b-instruct", "glm-4-9b-chat", "deepseek-v3", "mistral-7b-instruct",
])
def test_is_excluded_model_allows_general_instruct_models(name):
    assert bots.is_excluded_model(name) is False


def test_select_model_excludes_even_when_it_would_sort_first():
    models = ["abliterated-aaa-model", "qwen3.8:27b"]
    assert bots.select_model(models) == "qwen3.8:27b"


def test_select_model_returns_none_when_everything_is_excluded():
    assert bots.select_model(["heretic-q4:latest", "nomic-embed-text:latest"]) is None


def test_select_model_prefers_an_already_loaded_model():
    models = ["qwen3.8:27b", "llama-3.1-8b-instruct"]
    assert bots.select_model(models, loaded={"llama-3.1-8b-instruct"}) == "llama-3.1-8b-instruct"


def test_select_model_family_order_breaks_ties_when_nothing_else_decides():
    # Neither has a parsed size or quant, so family rank is the only signal.
    assert bots.select_model(["mistral-latest", "qwen-latest"]) == "qwen-latest"


def test_select_model_prefers_mid_size_over_the_largest():
    models = ["qwen-7b", "qwen-30b", "qwen-235b"]
    assert bots.select_model(models) == "qwen-30b"


def test_select_model_override_wins_when_present_and_allowed():
    models = ["qwen-7b", "qwen-30b"]
    assert bots.select_model(models, override="qwen-7b") == "qwen-7b"


def test_select_model_ignores_an_excluded_override():
    """The coordinator's policy must hold even against a user's own
    override — see choose_model_endpoint's docstring."""
    models = ["qwen-30b", "qwen-abliterated-7b"]
    assert bots.select_model(models, override="qwen-abliterated-7b") == "qwen-30b"


def test_select_model_is_deterministic_across_repeated_calls():
    models = ["qwen-30b", "llama-30b", "qwen-31b"]
    results = {bots.select_model(models) for _ in range(20)}
    assert len(results) == 1


# ── unit tests: chosen_model override setting ───────────────────────────────

def test_chosen_model_override_round_trips(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(bots, "_STATE_PATH", pathlib.Path(tmp) / "state.json")
        assert bots.get_chosen_model_override() is None
        bots.set_chosen_model_override("qwen3.8:27b")
        assert bots.get_chosen_model_override() == "qwen3.8:27b"
        bots.set_chosen_model_override(None)
        assert bots.get_chosen_model_override() is None


def test_status_snapshot_reports_chosen_model(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(bots, "_STATE_PATH", pathlib.Path(tmp) / "state.json")
        monkeypatch.setattr(bots, "_CAPACITY_PATH", pathlib.Path(tmp) / "capacity.json")
        monkeypatch.setattr(bots, "discover_local_models", lambda: [
            {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "models": ["qwen3.8:27b", "heretic-q4:latest"]}
        ])
        snap = bots.status_snapshot()
        assert snap["local_models"] == ["ollama"]
        assert snap["chosen_model"] == "qwen3.8:27b"


def test_status_snapshot_chosen_model_none_when_nothing_usable(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(bots, "_STATE_PATH", pathlib.Path(tmp) / "state.json")
        monkeypatch.setattr(bots, "_CAPACITY_PATH", pathlib.Path(tmp) / "capacity.json")
        monkeypatch.setattr(bots, "discover_local_models", lambda: [])
        monkeypatch.setattr(bots, "_own_api_key", lambda: None)
        snap = bots.status_snapshot()
        assert snap["chosen_model"] is None


# ── unit tests: run_bot_steps against the fake model ────────────────────────

class _Collector:
    def __init__(self):
        self.calls = []

    def __call__(self, emit_kind, **kw):
        self.calls.append((emit_kind, kw))


def test_run_bot_steps_no_local_model_posts_friendly_error(monkeypatch):
    monkeypatch.delenv("BOTS_FAKE_MODEL", raising=False)
    monkeypatch.setattr(bots, "fake_model_enabled", lambda: False)
    monkeypatch.setattr(bots, "choose_model_endpoint", lambda *a, **kw: (_ for _ in ()).throw(bots.NoLocalModelError("none")))
    collector = _Collector()
    req = {"run_id": "r1", "prompt": "hi", "posture": "private", "bot": {"id": "b1"}, "context": [], "model": None}
    bots.run_bot_steps(req, collector)
    done = [kw for kind, kw in collector.calls if kind == "done"]
    assert done and done[0]["error"] == "no_local_model"


def test_run_bot_steps_risky_action_becomes_approval_request_not_executed(monkeypatch):
    monkeypatch.setenv("BOTS_FAKE_MODEL", "1")
    collector = _Collector()
    req = {
        "run_id": "r2", "prompt": "please [[email:a@b.com]] them", "posture": "balanced",
        "bot": {"id": "b1"}, "context": [], "model": None,
    }
    bots.run_bot_steps(req, collector)
    approvals = [kw for kind, kw in collector.calls if kind == "approval_request"]
    assert len(approvals) == 1
    assert approvals[0]["action"] == "send_email"
    assert approvals[0]["payload"]["to"] == "a@b.com"
    # The one rule that matters most: nothing in the emitted events ever
    # claims the email was sent — only that approval was requested.
    events = [kw for kind, kw in collector.calls if kind == "event"]
    assert not any("sent" in str(kw.get("body", "")).lower() for kw in events)


def test_run_bot_steps_run_command_is_only_ever_an_approval_request(monkeypatch):
    """A model that asks to run a command gets an approval_request and
    nothing else: no process is started, and no event claims it ran."""
    monkeypatch.setenv("BOTS_FAKE_MODEL", "1")
    monkeypatch.setattr(bots, "_fake_model_step", lambda prompt, step, origin: {
        "tool": "run_command", "args": {"command": "touch /tmp/should-never-exist"}, "origin": "user", "done": True,
    })
    started = []
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: started.append(a) or (_ for _ in ()).throw(AssertionError("Popen")))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: started.append(a) or (_ for _ in ()).throw(AssertionError("run")))
    monkeypatch.setattr(os, "system", lambda *a, **kw: started.append(a) or (_ for _ in ()).throw(AssertionError("system")))
    collector = _Collector()
    req = {"run_id": "rc1", "prompt": "run it", "posture": "balanced", "bot": {"id": "b1"}, "context": [], "model": None}
    bots.run_bot_steps(req, collector)
    approvals = [kw for kind, kw in collector.calls if kind == "approval_request"]
    assert [a["action"] for a in approvals] == ["run_command"]
    assert approvals[0]["payload"]["command"] == "touch /tmp/should-never-exist"
    assert started == []
    events = [kw for kind, kw in collector.calls if kind == "event"]
    assert not any("ran" in str(kw.get("body", "")).lower().split() for kw in events)


def test_runner_has_no_path_that_executes_a_command():
    """The computer runner never carries out a risky action itself. The
    server answers every callback with {approvals:[{id,status}]}; nothing in
    the runner may read that answer as permission to act, and the module has
    no way to start a process at all."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(bots))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [ast.alias(name=n.module or "")])}
    assert not imported & {"subprocess", "pty", "pexpect", "shlex"}
    calls = {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)}
    assert not calls & {"os.system", "os.popen", "os.execv", "os.execvp", "os.spawnv", "os.startfile"}
    # post_callback returns a bool; the server's approvals list is never handed back to the run loop.
    src = inspect.getsource(bots.post_callback)
    assert "approvals" not in src


def test_run_bot_steps_marks_content_origin_when_context_has_bot_content(monkeypatch):
    monkeypatch.setenv("BOTS_FAKE_MODEL", "1")
    collector = _Collector()
    req = {
        "run_id": "r3", "prompt": "[[email:a@b.com]]", "posture": "balanced", "bot": {"id": "b1"},
        "context": [{"kind": "message", "author": {"type": "bot", "id": "other"}, "body": "read this page: ..."}],
        "model": None,
    }
    bots.run_bot_steps(req, collector)
    approvals = [kw for kind, kw in collector.calls if kind == "approval_request"]
    assert approvals[0]["origin"] == "content"


def test_run_bot_steps_defaults_origin_to_user_with_empty_context(monkeypatch):
    monkeypatch.setenv("BOTS_FAKE_MODEL", "1")
    collector = _Collector()
    req = {
        "run_id": "r4", "prompt": "[[email:a@b.com]]", "posture": "balanced",
        "bot": {"id": "b1"}, "context": [], "model": None,
    }
    bots.run_bot_steps(req, collector)
    approvals = [kw for kind, kw in collector.calls if kind == "approval_request"]
    assert approvals[0]["origin"] == "user"


def test_run_bot_steps_stops_within_max_steps(monkeypatch):
    """A fake model that never sets done=True must still stop at MAX_STEPS."""
    monkeypatch.setenv("BOTS_FAKE_MODEL", "1")
    monkeypatch.setattr(bots, "_fake_model_step", lambda prompt, step, origin: {
        "tool": "post_message", "args": {"text": f"step {step}"}, "origin": origin, "done": False,
    })
    collector = _Collector()
    req = {"run_id": "r5", "prompt": "loop forever", "posture": "balanced", "bot": {"id": "b1"}, "context": [], "model": None}
    bots.run_bot_steps(req, collector)
    messages = [kw for kind, kw in collector.calls if kind == "event" and kw.get("kind") == "message"]
    assert len(messages) == bots.MAX_STEPS


# ── unit tests: capacity selection with deterministic fake latencies ───────

def test_measure_capacity_picks_highest_level_under_3s_p95(monkeypatch):
    monkeypatch.setenv("BOTS_FAKE_MODEL", "1")
    monkeypatch.setenv("BOTS_FAKE_MODEL_LATENCY_MS", "10")
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(bots, "_CAPACITY_PATH", pathlib.Path(tmp) / "capacity.json")
        result = bots.measure_capacity(levels=(1, 2, 4), samples_per_level=2)
        assert result["max_parallel"] in (1, 2, 4)
        for level in result["levels"]:
            assert level["ttft_p95_ms"] >= 0
        # Persisted for later GET /api/amelia/bots/capacity reads — checked
        # while _CAPACITY_PATH is still patched and the tempdir still exists.
        assert bots.load_capacity()["max_parallel"] == result["max_parallel"]


def test_measure_capacity_degrades_selection_when_latency_exceeds_budget(monkeypatch):
    """A model too slow at n=1 already (fake latency forced above 3s) must
    select max_parallel=1 — proves the p95<=3s cutoff is load-bearing, not
    just always picking the largest level tried."""
    monkeypatch.setenv("BOTS_FAKE_MODEL", "1")
    monkeypatch.setenv("BOTS_FAKE_MODEL_LATENCY_MS", "3200")
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(bots, "_CAPACITY_PATH", pathlib.Path(tmp) / "capacity.json")
        result = bots.measure_capacity(levels=(1, 2), samples_per_level=1)
    assert result["max_parallel"] == 1


def test_measure_capacity_warms_up_exactly_once_before_any_timed_sample(monkeypatch):
    """The regression this fix exists for: a cold model load landing inside
    the first timed sample instead of before it. Proven by call count/order
    rather than by sleeping for real in the test (fast, still deterministic)
    — see test_warm_model_calls_ollama_native_chat_non_streaming below for
    the real HTTP shape `_warm_model` sends."""
    monkeypatch.setenv("BOTS_FAKE_MODEL", "1")
    monkeypatch.setenv("BOTS_FAKE_MODEL_LATENCY_MS", "5")
    warm_calls = []

    def spy(endpoint):
        warm_calls.append(len(warm_calls))
        # Do NOT actually sleep the real (slow) warm-up delay here — the
        # point is call count/order, and a real sleep would just make the
        # suite slower without strengthening the assertion.

    monkeypatch.setattr(bots, "_warm_model", spy)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(bots, "_CAPACITY_PATH", pathlib.Path(tmp) / "capacity.json")
        result = bots.measure_capacity(levels=(1, 2, 4), samples_per_level=2)
    assert len(warm_calls) == 1, "warm-up must run exactly once per measurement, not once per level/sample"
    # And its (mocked-away) cost must not leak into any level's timing — every
    # level should still read the fast fake per-call latency, not a warm-up-
    # inflated one.
    for level in result["levels"]:
        assert level["ttft_p95_ms"] < 200


def test_warm_model_calls_ollama_native_chat_non_streaming(monkeypatch):
    """Pins the real request shape: Ollama's NATIVE /api/chat (not the
    OpenAI-compat surface, which reports neither eval_count nor
    eval_duration), non-streaming, think disabled, num_predict=1 — a
    minimal load-forcing call, not a real generation."""
    calls = []

    class _FakeResponse:
        def read(self):
            return b'{"done": true}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data))
        assert req.full_url.endswith("/api/chat")
        return _FakeResponse()

    monkeypatch.setattr(bots.urllib.request, "urlopen", fake_urlopen)
    bots._warm_model({"provider": "ollama", "base_url": "http://127.0.0.1:11434", "model": "qwen3.8:27b", "api_key": None})
    assert len(calls) == 1
    assert calls[0]["stream"] is False
    assert calls[0]["think"] is False
    assert calls[0]["options"]["num_predict"] == 1


# ── unit tests: pause / state persistence ───────────────────────────────────

def test_pause_state_round_trips(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(bots, "_STATE_PATH", pathlib.Path(tmp) / "state.json")
        assert bots.is_paused() is False
        bots.set_paused(True)
        assert bots.is_paused() is True
        bots.set_paused(False)
        assert bots.is_paused() is False


# ── unit test: the callback token is never logged ───────────────────────────

def test_post_callback_never_logs_the_token(monkeypatch, caplog):
    received = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            received["body"] = json.loads(self.rfile.read(n))
            resp = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, fmt, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    try:
        secret_token = "sekrit-run-token-xyz"
        with caplog.at_level("DEBUG"):
            ok = bots.post_callback(f"http://127.0.0.1:{port}/cb", secret_token, {"events": [], "done": True})
        assert ok is True
        assert received["body"]["token"] == secret_token  # the server DID receive it...
        assert secret_token not in caplog.text            # ...but it was never logged
    finally:
        srv.shutdown()
        srv.server_close()


def test_post_callback_retries_then_gives_up_gracefully():
    # Real backoff (small: CALLBACK_BACKOFF_BASE=0.5, 4 attempts, ~3.5s worst
    # case) rather than patching the global `time.sleep` — that function
    # object is shared process-wide, so patching it here would also freeze
    # any other thread's sleep for the duration of this test.
    ok = bots.post_callback("http://127.0.0.1:1/unreachable", "tok", {"done": True})
    assert ok is False  # never raises


# ── HTTP tests: a dedicated isolated fake-model server for this file ───────

@pytest.fixture(scope="module")
def bots_server(tmp_path_factory):
    port = _auto_test_port(REPO_ROOT)
    state_dir = tmp_path_factory.mktemp("amelia_bots_state")
    home_dir = tmp_path_factory.mktemp("amelia_bots_home")
    base = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY") or key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
    env.update({
        "HERMES_WEBUI_PORT": str(port),
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": str(state_dir),
        "HERMES_HOME": str(home_dir),
        "BOTS_FAKE_MODEL": "1",
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
    })
    log_path = state_dir / "server.log"
    with open(log_path, "w") as logf:
        proc = subprocess.Popen(
            [VENV_PYTHON, str(SERVER_SCRIPT)], cwd=WORKDIR, env=env,
            stdout=logf, stderr=subprocess.STDOUT,
        )
    ok, reason = _wait_for_server(base, timeout=45, proc=proc, log_path=log_path)
    if not ok:
        proc.kill()
        pytest.fail(f"amelia_bots test server did not start: {reason}")
    try:
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def fake_callback(tmp_path):
    events = []
    lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            with lock:
                events.append(data)
            resp = b'{"ok":true,"approvals":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, fmt, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/cb", events, lock
    finally:
        srv.shutdown()
        srv.server_close()


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, json.loads(r.read())


def _wait_for_done(events, lock, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            if any(e.get("done") for e in events):
                return True
        time.sleep(0.05)
    return False


def test_run_returns_202_and_completes_in_background(bots_server, fake_callback):
    callback_url, events, lock = fake_callback
    status, payload = _post(bots_server, "/api/amelia/bots/run", {
        "run_id": "http_run_1", "token": "tok1", "callback_url": callback_url,
        "bot": {"id": "bot_http", "posture": "balanced"}, "prompt": "hello",
    })
    assert status == 202
    assert payload["run_id"] == "http_run_1"
    assert _wait_for_done(events, lock), "background run never posted done:true to the callback"
    with lock:
        assert any(e.get("token") == "tok1" for e in events)


def test_run_rejects_invalid_body_with_400(bots_server):
    status, payload = _post(bots_server, "/api/amelia/bots/run", {"run_id": "x"})
    assert status == 400
    assert "error" in payload


def test_pause_then_run_returns_409(bots_server, fake_callback):
    callback_url, events, lock = fake_callback
    status, _ = _post(bots_server, "/api/amelia/bots/pause", {"paused": True})
    assert status == 200
    try:
        status, payload = _post(bots_server, "/api/amelia/bots/run", {
            "run_id": "http_run_paused", "token": "tok2", "callback_url": callback_url,
            "bot": {"id": "bot_http"}, "prompt": "hello",
        })
        assert status == 409
        assert payload["error"] == "paused"
    finally:
        _post(bots_server, "/api/amelia/bots/pause", {"paused": False})


def test_status_reports_paused_flag(bots_server):
    status, payload = _post(bots_server, "/api/amelia/bots/pause", {"paused": True})
    assert status == 200
    try:
        status, snap = _get(bots_server, "/api/amelia/bots/status")
        assert status == 200
        assert snap["paused"] is True
        assert "max_parallel" in snap
    finally:
        _post(bots_server, "/api/amelia/bots/pause", {"paused": False})


def test_approval_request_reaches_callback_and_is_never_executed(bots_server, fake_callback):
    callback_url, events, lock = fake_callback
    status, _ = _post(bots_server, "/api/amelia/bots/run", {
        "run_id": "http_run_approval", "token": "tok3", "callback_url": callback_url,
        "bot": {"id": "bot_http"}, "prompt": "please [[email:x@y.com]] them",
    })
    assert status == 202
    assert _wait_for_done(events, lock)
    with lock:
        all_approvals = [a for e in events for a in e.get("approval_requests", [])]
    assert any(a["action"] == "send_email" and a["payload"]["to"] == "x@y.com" for a in all_approvals)


def test_capacity_measure_endpoint_persists_and_get_reflects_it(bots_server):
    status, result = _post(bots_server, "/api/amelia/bots/capacity/measure", {})
    assert status == 200
    assert "max_parallel" in result
    status, cap = _get(bots_server, "/api/amelia/bots/capacity")
    assert status == 200
    assert cap["max_parallel"] == result["max_parallel"]


def test_status_reports_local_models_and_chosen_model_fields(bots_server):
    status, snap = _get(bots_server, "/api/amelia/bots/status")
    assert status == 200
    assert "local_models" in snap
    assert "chosen_model" in snap  # None is a valid value; the key must exist either way


def test_model_override_round_trips_through_status(bots_server):
    status, resp = _post(bots_server, "/api/amelia/bots/model", {"model": "qwen3.8:27b"})
    assert status == 200
    assert resp["chosen_model_override"] == "qwen3.8:27b"
    try:
        status, resp = _post(bots_server, "/api/amelia/bots/model", {"model": None})
        assert status == 200
        assert resp["chosen_model_override"] is None
    finally:
        _post(bots_server, "/api/amelia/bots/model", {"model": None})


def test_model_override_requires_the_field(bots_server):
    status, resp = _post(bots_server, "/api/amelia/bots/model", {})
    assert status == 400
    assert "error" in resp
