"""What the agent is doing right now, in words a person recognises.

The phone app polls /api/session/status every 1.5 seconds and shows the last
entry of a `recent` trail, with the ones before it fading behind — the same
contract the hosted sandbox speaks. A machine of the person's own answered
that poll with token counters and a bool, so a five-minute turn on a local
model was a spinner and a clock: "it is working but not showing me what it's
doing". The tool calls were being recorded the whole time
(STREAM_LIVE_TOOL_CALLS); nothing read them back for the status.

Deliberately not the tool's function name: "execute_code" tells a developer
what happened and tells everyone else nothing. The file or command is the
part that makes it feel like work is happening.
"""
from __future__ import annotations

from typing import Any

THINKING = "Thinking"


def _short(value: Any, cap: int = 48) -> str:
    s = str(value or "").strip().replace("\n", " ")
    return s[:cap] + ("…" if len(s) > cap else "")


def _path(args: dict) -> str:
    for key in ("path", "file_path", "filename", "file", "target"):
        if args.get(key):
            return _short(args[key], 64)
    return "a file"


def describe_tool(name: str, args: Any) -> str:
    """One tool call as a step label."""
    a = args if isinstance(args, dict) else {}
    n = str(name or "").strip()
    if n in ("write_file",):
        return f"Writing {_path(a)}"
    if n in ("read_file",):
        return f"Reading {_path(a)}"
    if n in ("patch",):
        return f"Editing {_path(a)}"
    if n in ("search_files",):
        return f"Searching the files for {_short(a.get('pattern') or a.get('query') or a.get('regex'), 40)}"
    if n in ("terminal", "execute_code", "process"):
        cmd = a.get("command") or a.get("cmd") or a.get("code") or ""
        return f"Running {_short(cmd)}" if cmd else "Running a command"
    if n in ("read_terminal", "read_window_below", "focus_pane"):
        return "Checking the command's output"
    if n in ("web_search", "x_search", "session_search"):
        q = a.get("query") or a.get("q") or ""
        return f"Searching the web for {_short(q, 40)}" if q else "Searching the web"
    if n in ("web_extract",):
        return f"Reading {_short(a.get('url') or a.get('urls') or 'a page', 56)}"
    if n.startswith("browser_"):
        if n == "browser_navigate":
            return f"Opening {_short(a.get('url') or 'a page', 56)}"
        return "Working in the browser"
    if n in ("open_preview", "read_preview", "annotate_preview", "close_preview", "drive_preview"):
        return "Checking the preview"
    if n in ("delegate_task",):
        return "Handing part of the work to a helper"
    if n in ("clarify",):
        return "Asking you a question"
    if n in ("todo",):
        return "Planning the steps"
    if n in ("memory",):
        return "Remembering something for later"
    if n.startswith("kanban_"):
        return "Updating the board"
    if n.startswith("project_"):
        return "Switching project"
    if n in ("image_generate", "video_generate"):
        return "Generating media"
    if n in ("vision_analyze", "video_analyze"):
        return "Looking at what you sent"
    if n in ("skill_view", "skills_list", "skill_manage"):
        return "Reading a skill"
    return n.replace("_", " ").capitalize() if n else THINKING


def build_trail(live_tool_calls: Any, *, running: bool, limit: int = 8) -> list[str]:
    """The `recent` list: every tool call so far in words, with Thinking where
    the model is between tools. Order is the order things happened."""
    calls = list(live_tool_calls or [])
    labels: list[str] = []
    if running and not calls:
        return [THINKING]
    for tc in calls:
        if not isinstance(tc, dict):
            continue
        labels.append(describe_tool(tc.get("name"), tc.get("args")))
    # A finished tool means the model is reading its result and deciding —
    # that is the moment a poll almost always lands on, and it deserves a name.
    if running and calls and isinstance(calls[-1], dict) and calls[-1].get("done"):
        labels.append(THINKING)
    return labels[-limit:]
