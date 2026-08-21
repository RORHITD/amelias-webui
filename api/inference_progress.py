"""Live inference progress for local model servers that only log it.

At Hermes's working context (60k+), most of a turn is spent *ingesting* the
prompt before a single token streams back, so the UI looks frozen for minutes.
The MLX server knows exactly how far along it is -- it logs
``Prompt processing progress: 40960/44494`` -- but does not expose that over
the OpenAI-compatible API.

Rather than leave the user staring at nothing, this reads the tail of the
server's log and reconstructs the current phase. It is deliberately:

* **opt-in** -- ``inference_progress.log_path`` in config.yaml; no path, no
  feature, no cost.
* **tail-only** -- reads the last few KB, never the whole file, so it stays
  cheap no matter how long the server has been up.
* **fail-soft** -- any problem returns ``{"phase": "unknown"}`` rather than
  raising. Progress is a nicety; it must never break a chat.

The coupling to another process's log is real and acknowledged: if the server's
log format changes, this degrades to "unknown" instead of breaking.
"""

from __future__ import annotations

import os
import re

# Only the tail matters -- enough to span one request's lifecycle.
_TAIL_BYTES = 16_384

_RE_QUEUED = re.compile(r"Generation queued: request=(\S+).*?prompt_tokens=(\d+)")
_RE_PREFILL_START = re.compile(r"Prefill started: request=(\S+).*?prompt_tokens=(\d+)")
# The server emits two shapes depending on backend/version:
#   "Prompt processing progress: 40960/44494"
#   "Prefill progress: request=X tokens=2048/27937 (7.3%)"
_RE_PROGRESS = re.compile(
    r"(?:Prompt processing progress:\s*|Prefill progress:.*?tokens=)(\d+)/(\d+)"
)
_RE_PREFILL_DONE = re.compile(r"Prefill completed: request=(\S+).*?rate=([\d.]+) tok/s")
_RE_DECODE_START = re.compile(r"Decode started: request=(\S+).*?time_to_first_token=([\d.]+)s")
_RE_REQUEST_DONE = re.compile(r"Request completed: .*?in_flight=(\d+)")


def _configured_log_path() -> str | None:
    try:
        from api.config import get_config

        cfg = get_config() or {}
        path = str((cfg.get("inference_progress") or {}).get("log_path") or "").strip()
    except Exception:
        return None
    return path or None


def _read_tail(path: str) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - _TAIL_BYTES))
        return fh.read().decode("utf-8", errors="replace")


def inference_progress() -> dict:
    """Current inference phase for the configured local server.

    Returns a dict with ``phase`` in {idle, prefill, decode, unknown} plus,
    when known, ``prompt_tokens``, ``processed_tokens``, ``percent`` and
    ``rate_tok_s``.
    """
    path = _configured_log_path()
    if not path:
        return {"phase": "unknown", "reason": "not_configured"}
    try:
        tail = _read_tail(path)
    except Exception:
        return {"phase": "unknown", "reason": "log_unreadable"}

    lines = tail.splitlines()

    # Anchor on the NEWEST request, then read only the lines that follow it.
    # Scanning backwards without an anchor conflates requests: the previous
    # request's "Request completed" sits just above the current request's
    # "Generation queued" and would flip an in-flight turn back to idle.
    anchor = None
    prompt_tokens = None
    for i in range(len(lines) - 1, -1, -1):
        m = _RE_QUEUED.search(lines[i]) or _RE_PREFILL_START.search(lines[i])
        if m:
            anchor = i
            prompt_tokens = int(m.group(2))
            break

    if anchor is None:
        return {"phase": "idle"}

    phase = "prefill"
    processed = total = rate = None
    for line in lines[anchor + 1:]:
        m = _RE_PROGRESS.search(line)
        if m:
            processed, total = int(m.group(1)), int(m.group(2))
            phase = "prefill"
            continue
        m = _RE_PREFILL_DONE.search(line)
        if m:
            rate = float(m.group(2))
            phase = "decode"
            continue
        if _RE_DECODE_START.search(line):
            phase = "decode"
            continue
        if _RE_REQUEST_DONE.search(line):
            phase = "idle"

    out: dict = {"phase": phase}
    if prompt_tokens is not None:
        out["prompt_tokens"] = prompt_tokens
    if processed is not None and total:
        out["processed_tokens"] = processed
        out["total_tokens"] = total
        out["percent"] = round(100.0 * processed / total, 1)
    if rate is not None:
        out["rate_tok_s"] = rate
    return out
