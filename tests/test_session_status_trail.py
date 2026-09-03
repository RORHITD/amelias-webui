"""/api/session/status names what the running turn is doing.

The phone app shows the last entry of `recent` and fades the rest behind it,
the way the hosted sandbox's status is drawn. A machine of the person's own
answered only with counters, so a turn on a local model was a spinner and a
clock. The tool calls were in STREAM_LIVE_TOOL_CALLS the whole time.
"""
from api.step_labels import build_trail, describe_tool, THINKING


def test_tool_calls_become_words_a_person_recognises():
    assert describe_tool("write_file", {"path": "src/App.tsx"}) == "Writing src/App.tsx"
    assert describe_tool("read_file", {"path": "README.md"}) == "Reading README.md"
    assert describe_tool("terminal", {"command": "ls -la"}) == "Running ls -la"
    assert describe_tool("execute_code", {"code": "print(1)"}) == "Running print(1)"
    assert describe_tool("web_search", {"query": "expo router tabs"}) == "Searching the web for expo router tabs"
    assert describe_tool("browser_navigate", {"url": "https://example.com"}) == "Opening https://example.com"
    # Never the function name for a known tool; a humanised name for unknown ones.
    assert "execute_code" not in describe_tool("execute_code", {"code": "x"})
    assert describe_tool("some_new_tool", {}) == "Some new tool"


def test_a_long_command_is_cut_not_dumped():
    label = describe_tool("terminal", {"command": "x" * 200})
    assert len(label) < 70 and label.endswith("…")


def test_trail_reads_in_order_with_thinking_where_the_model_is():
    assert build_trail([], running=True) == [THINKING]
    assert build_trail([], running=False) == []
    calls = [
        {"name": "read_file", "args": {"path": "a.py"}, "done": True},
        {"name": "terminal", "args": {"command": "pytest"}, "done": False},
    ]
    assert build_trail(calls, running=True) == ["Reading a.py", "Running pytest"]
    calls[-1]["done"] = True
    assert build_trail(calls, running=True) == ["Reading a.py", "Running pytest", THINKING]


def test_trail_is_bounded_to_the_last_eight():
    calls = [{"name": "read_file", "args": {"path": f"f{i}"}, "done": True} for i in range(20)]
    trail = build_trail(calls, running=True)
    assert len(trail) == 8 and trail[-1] == THINKING and trail[0] == "Reading f13"


def test_status_carries_the_trail_for_a_live_stream(monkeypatch):
    from api import session_ops
    from api import config

    class S:  # the handful of attributes session_status reads
        session_id = "s1"; title = "t"; model = "m"; workspace = "/w"; personality = None
        messages = []; created_at = 1.0; updated_at = 2.0; input_tokens = 0; output_tokens = 0
        estimated_cost = None; profile = "default"; pending_started_at = 100.0; active_stream_id = "st1"

    monkeypatch.setattr(session_ops, "get_session", lambda sid: S())
    monkeypatch.setattr(session_ops, "_live_active_stream_id", lambda s: "st1")
    monkeypatch.setattr(session_ops.time, "time", lambda: 130.0)
    monkeypatch.setitem(config.STREAM_LIVE_TOOL_CALLS, "st1",
                        [{"name": "write_file", "args": {"path": "index.html"}, "done": True}])
    st = session_ops.session_status("s1")
    assert st["agent_running"] is True
    assert st["recent"] == ["Writing index.html", THINKING]
    assert st["step"] == THINKING and st["step_count"] == 1
    assert st["elapsed"] == 30.0

    # Idle: nothing invented.
    monkeypatch.setattr(session_ops, "_live_active_stream_id", lambda s: None)
    st = session_ops.session_status("s1")
    assert st["recent"] == [] and st["step"] == "" and st["elapsed"] == 0
