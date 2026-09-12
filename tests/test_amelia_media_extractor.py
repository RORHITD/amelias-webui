"""Tests for api/amelia_media_extractor.py — the yt-dlp shim used by
api/amelia_bots.py's fetch_media for page URLs (see that module's docstring
for why the one legitimate subprocess call in this feature lives here rather
than in api/amelia_bots.py itself).

All subprocess calls are mocked; nothing here ever actually invokes a real
yt-dlp binary (this machine may or may not have one installed), so these
tests are fast and hermetic regardless of what's on PATH.
"""
from __future__ import annotations

import json
import subprocess

import pytest

import api.amelia_media_extractor as extractor


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_extractor_available_true_when_which_finds_a_binary(monkeypatch):
    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")
    assert extractor.extractor_available() is True


def test_extractor_available_false_when_missing(monkeypatch):
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    assert extractor.extractor_available() is False


def test_extract_media_raises_when_binary_vanishes_between_check_and_call(monkeypatch, tmp_path):
    monkeypatch.setattr(extractor.shutil, "which", lambda name: None)
    with pytest.raises(extractor.ExtractorError, match="not on PATH"):
        extractor.extract_media("https://example.com/v", tmp_path, filename_stem=None, timeout=5.0)


def test_extract_media_argv_is_code_constructed_url_after_separator(monkeypatch, tmp_path):
    """The url is the LAST argv element, preceded by a bare `--` separator,
    so a url that happens to start with `-` can never be parsed as a flag —
    and nothing here is ever run through a shell."""
    captured = {}
    out_file = tmp_path / "clip.mp4"
    out_file.write_bytes(b"videobytes")

    def fake_run(cmd, capture_output, text, timeout, check):
        captured["cmd"] = cmd
        assert capture_output is True
        assert text is True
        assert check is False
        info = {"filepath": str(out_file), "duration": 42.0}
        return _FakeCompletedProcess(returncode=0, stdout=json.dumps(info))

    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")
    monkeypatch.setattr(extractor.subprocess, "run", fake_run)

    url = "-not-a-flag-but-looks-like-one"
    result = extractor.extract_media(url, tmp_path, filename_stem="clip", timeout=5.0)

    cmd = captured["cmd"]
    assert cmd[0] == "/usr/local/bin/yt-dlp"
    assert cmd[-2:] == ["--", url]
    assert result["path"] == str(out_file)
    assert result["filename"] == "clip.mp4"
    assert result["bytes"] == len(b"videobytes")
    assert result["duration"] == 42.0
    assert result["mime"] == "video/mp4"


def test_extract_media_raises_on_nonzero_exit_with_stderr_reason(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, text, timeout, check):
        return _FakeCompletedProcess(returncode=1, stdout="", stderr="ERROR: Unsupported URL: foo\n")

    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")
    monkeypatch.setattr(extractor.subprocess, "run", fake_run)
    with pytest.raises(extractor.ExtractorError, match="Unsupported URL"):
        extractor.extract_media("https://example.com/v", tmp_path, filename_stem=None, timeout=5.0)


def test_extract_media_raises_on_timeout(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, text, timeout, check):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")
    monkeypatch.setattr(extractor.subprocess, "run", fake_run)
    with pytest.raises(extractor.ExtractorError, match="timed out"):
        extractor.extract_media("https://example.com/v", tmp_path, filename_stem=None, timeout=1.5)


def test_extract_media_raises_when_no_parseable_json_line(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, text, timeout, check):
        return _FakeCompletedProcess(returncode=0, stdout="not json at all\n")

    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")
    monkeypatch.setattr(extractor.subprocess, "run", fake_run)
    with pytest.raises(extractor.ExtractorError, match="no parseable output"):
        extractor.extract_media("https://example.com/v", tmp_path, filename_stem=None, timeout=5.0)


def test_extract_media_raises_when_output_file_is_missing(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, text, timeout, check):
        info = {"filepath": str(tmp_path / "never-written.mp4")}
        return _FakeCompletedProcess(returncode=0, stdout=json.dumps(info))

    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")
    monkeypatch.setattr(extractor.subprocess, "run", fake_run)
    with pytest.raises(extractor.ExtractorError, match="output file is missing"):
        extractor.extract_media("https://example.com/v", tmp_path, filename_stem=None, timeout=5.0)


def test_extract_media_raises_on_oserror_running_binary(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, text, timeout, check):
        raise OSError("no such file or directory")

    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")
    monkeypatch.setattr(extractor.subprocess, "run", fake_run)
    with pytest.raises(extractor.ExtractorError, match="could not run yt-dlp"):
        extractor.extract_media("https://example.com/v", tmp_path, filename_stem=None, timeout=5.0)


def test_extract_media_duration_defaults_to_none_when_not_reported(monkeypatch, tmp_path):
    out_file = tmp_path / "song.mp3"
    out_file.write_bytes(b"mp3bytes")

    def fake_run(cmd, capture_output, text, timeout, check):
        info = {"filepath": str(out_file)}
        return _FakeCompletedProcess(returncode=0, stdout=json.dumps(info))

    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/local/bin/yt-dlp")
    monkeypatch.setattr(extractor.subprocess, "run", fake_run)
    result = extractor.extract_media("https://example.com/v", tmp_path, filename_stem=None, timeout=5.0)
    assert result["duration"] is None
