"""Prompt-ingestion progress parsed from a local model server's log.

At 60k context most of a turn is prefill, so without this the UI is frozen for
minutes with nothing to show. The parser must be resilient: two log formats
exist, requests interleave, and a missing/rotated log must degrade rather than
raise.
"""
import importlib


def _mod():
    return importlib.import_module("api.inference_progress")


def _write(tmp_path, lines):
    p = tmp_path / "server.err.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def _progress(monkeypatch, tmp_path, lines):
    m = _mod()
    path = _write(tmp_path, lines)
    monkeypatch.setattr(m, "_configured_log_path", lambda: path)
    return m.inference_progress()


def test_reports_prefill_percent_from_prefill_progress_format(monkeypatch, tmp_path):
    out = _progress(monkeypatch, tmp_path, [
        "12:22:39 - INFO - Generation queued: request=abc prompt_tokens=27937 max_tokens=25 images=0",
        "12:22:39 - INFO - Prefill started: request=abc backend=continuous_batching prompt_tokens=27937 images=0",
        "12:22:41 - INFO - Prefill progress: request=abc tokens=2048/27937 (7.3%)",
        "12:22:43 - INFO - Prefill progress: request=abc tokens=8192/27937 (29.3%)",
    ])
    assert out["phase"] == "prefill"
    assert out["processed_tokens"] == 8192
    assert out["total_tokens"] == 27937
    assert out["percent"] == 29.3
    assert out["prompt_tokens"] == 27937


def test_supports_the_other_progress_log_shape(monkeypatch, tmp_path):
    out = _progress(monkeypatch, tmp_path, [
        "11:09:00 - INFO - Prefill started: request=zzz backend=cb prompt_tokens=44494 images=0",
        "11:09:17 - INFO - Prompt processing progress: 40960/44494",
    ])
    assert out["phase"] == "prefill"
    assert out["processed_tokens"] == 40960


def test_previous_request_completion_does_not_mark_an_active_turn_idle(monkeypatch, tmp_path):
    """Regression: scanning backwards without anchoring on the newest request
    hit the PRIOR request's completion line and reported idle mid-prefill."""
    out = _progress(monkeypatch, tmp_path, [
        "12:00:00 - INFO - Request completed: endpoint=/chat/completions in_flight=0",
        "12:22:39 - INFO - Generation queued: request=new prompt_tokens=32228 max_tokens=25 images=0",
        "12:22:41 - INFO - Prefill progress: request=new tokens=2048/32228 (6.4%)",
    ])
    assert out["phase"] == "prefill"
    assert out["percent"] == 6.4


def test_decode_phase_after_prefill_completes(monkeypatch, tmp_path):
    out = _progress(monkeypatch, tmp_path, [
        "12:22:39 - INFO - Prefill started: request=abc backend=cb prompt_tokens=17 images=0",
        "12:22:39 - INFO - Prefill completed: request=abc prompt_tokens=17 cached_tokens=0 elapsed=0.2s rate=72.8 tok/s",
        "12:22:40 - INFO - Decode started: request=abc time_to_first_token=0.594s",
    ])
    assert out["phase"] == "decode"
    assert out["rate_tok_s"] == 72.8


def test_idle_after_request_completes(monkeypatch, tmp_path):
    out = _progress(monkeypatch, tmp_path, [
        "12:22:39 - INFO - Prefill started: request=abc backend=cb prompt_tokens=17 images=0",
        "12:22:40 - INFO - Request completed: endpoint=/chat/completions in_flight=0",
    ])
    assert out["phase"] == "idle"


def test_missing_log_degrades_instead_of_raising(monkeypatch, tmp_path):
    m = _mod()
    monkeypatch.setattr(m, "_configured_log_path", lambda: str(tmp_path / "nope.log"))
    assert m.inference_progress()["phase"] == "unknown"


def test_feature_is_opt_in(monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "_configured_log_path", lambda: None)
    out = m.inference_progress()
    assert out["phase"] == "unknown"
    assert out["reason"] == "not_configured"
