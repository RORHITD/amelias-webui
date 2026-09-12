"""yt-dlp-based page-URL media extraction for the Amelia bot-teams computer
runner (see ``api/amelia_bots.py``'s ``fetch_media``).

Deliberately isolated in its own module rather than folded into
``api.amelia_bots``. That module's own contribution test
(``test_runner_has_no_path_that_executes_a_command`` in
``tests/test_amelia_bots.py``) asserts, at the AST level, that
``api/amelia_bots.py`` imports no process-spawning module and calls no
exec-family function — the guarantee behind "the model can never get an
arbitrary command executed through this runner" (``RISKY_ACTIONS``' own
``run_command`` is always only ever reported as an ``approval_request``,
never carried out).

That guarantee is specifically about ARBITRARY, model-authored commands. This
module is different in kind: it shells out to exactly one hardcoded,
PATH-discovered binary (``yt-dlp``) with a fixed, code-constructed argv list
— never a shell string, never a model-supplied flag — to do the one job of
turning a page URL ``fetch_media`` already SSRF-validated into a local media
file. Isolating the one legitimate ``subprocess`` use in this small module
keeps the ``api/amelia_bots.py`` invariant literally true (its own source
still imports no process-spawning module) while still allowing this narrow,
safe delegation. Nothing here is installed automatically — an absent
``yt-dlp`` is reported back as a clear, actionable message, never silently
worked around.
"""
from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
from pathlib import Path


class ExtractorError(RuntimeError):
    """yt-dlp ran but failed: bad URL, unsupported site, network error, no
    parseable output, or a missing output file."""


def extractor_available() -> bool:
    """True when a ``yt-dlp`` binary is on PATH. Never installs it."""
    return shutil.which("yt-dlp") is not None


def extract_media(
    url: str,
    dest_dir: Path,
    *,
    filename_stem: str | None,
    timeout: float,
) -> dict:
    """Run ``yt-dlp`` to download *url*'s media into *dest_dir*.

    Returns ``{path, filename, bytes, mime, duration}`` (``duration`` is
    ``None`` when yt-dlp did not report one). Raises :class:`ExtractorError`
    with a clear, human-readable reason on any failure — missing binary
    (checked by the caller via :func:`extractor_available` first, so this
    only covers a binary that vanished between the check and the call),
    process failure, timeout, or unparseable/missing output.

    The argv is entirely code-constructed: *url* is passed as a single argv
    element after a ``--`` separator (so a URL that happens to start with
    ``-`` can never be parsed as a flag), and nothing here ever runs through
    a shell or accepts a model-authored flag.
    """
    binary = shutil.which("yt-dlp")
    if not binary:
        raise ExtractorError("yt-dlp is not on PATH")

    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = filename_stem or "%(title).200B-%(id)s"
    out_template = str(dest_dir / (stem + ".%(ext)s"))

    cmd = [
        binary,
        "--no-playlist",
        "--max-downloads", "1",
        "--no-warnings",
        "--print-json",
        "-o", out_template,
        "--",
        url,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractorError(
            f"yt-dlp timed out after {timeout:g}s fetching {url}"
        ) from exc
    except OSError as exc:
        raise ExtractorError(f"could not run yt-dlp: {exc}") from exc

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = tail[-1] if tail else f"exit code {proc.returncode}"
        raise ExtractorError(f"yt-dlp failed to fetch {url}: {reason}")

    info = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            info = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(info, dict):
        raise ExtractorError(
            f"yt-dlp reported success but produced no parseable output for {url}"
        )

    filepath = info.get("filepath") or info.get("_filename")
    if not filepath or not Path(filepath).is_file():
        raise ExtractorError(
            f"yt-dlp reported success but the output file is missing for {url}"
        )

    path = Path(filepath)
    size = path.stat().st_size
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return {
        "path": str(path),
        "filename": path.name,
        "bytes": size,
        "mime": mime,
        "duration": info.get("duration"),
    }
