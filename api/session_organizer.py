"""AI-assigned project labels for sessions, so the board can group by project.

Sessions carry no project of their own -- ``project_id`` is almost always NULL
and the workspace is a shared scratch dir. What a session is *about* lives in
its title (and first message). This module asks a model to file each session
under one of the user's known projects, caching the answer so the classify runs
at most once per (session, title).

Provider-agnostic by design: the model endpoint, model name, and project list
all come from ``config.yaml`` under ``session_organizer``, falling back to the
app's main ``model`` config. Point it at a local Ollama, a hosted API, or any
OpenAI-compatible endpoint -- "whatever AI they want". If the model is disabled
or unreachable, a keyword heuristic still assigns an obvious project rather than
dumping everything into one lane.

Nothing here can take the board down: every path is wrapped, and the worst case
is a session labelled ``Unsorted``.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import urllib.error


def _hermes_home() -> str:
    # Matches the env-fallback pattern used across the app (e.g. media_snapshots).
    return os.getenv("HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes"))


# --- cache -----------------------------------------------------------------

_CACHE_PATH = os.path.join(_hermes_home(), "session_projects.json")
_cache: dict | None = None
# Classify is best-effort. The first call to a cold model can be slow (load +
# first token); subsequent calls are ~1s. This only ever runs off the render
# path (background/cron), so a generous cap is fine.
_HTTP_TIMEOUT_SECONDS = 25


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
            _cache = json.load(fh)
    except Exception:
        _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_cache or {}, fh)
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        pass


def _cache_key(session_id: str, title: str) -> str:
    # Re-classify when the title changes; a renamed session may be a new topic.
    return f"{session_id}:{hash(title) & 0xFFFFFFFF}"


# --- config ----------------------------------------------------------------


def _organizer_config() -> dict:
    """Read ``session_organizer`` config, defaulting to the main model config.

    Shape (all optional)::

        session_organizer:
          enabled: true            # false -> heuristic only, no model calls
          base_url: http://…/v1    # defaults to model.base_url
          model: <name>            # defaults to model.default
          api_key_env: OPENAI_API_KEY
          projects: [ "Catholic Connect", "Woodlands Estates", … ]
    """
    try:
        from api.config import get_config

        cfg = get_config() or {}
    except Exception:
        cfg = {}

    org = dict(cfg.get("session_organizer") or {})
    model_cfg = cfg.get("model") or {}
    org.setdefault("base_url", model_cfg.get("base_url"))
    org.setdefault("model", model_cfg.get("default") or model_cfg.get("model"))
    org.setdefault("enabled", True)
    if not org.get("projects"):
        org["projects"] = _DEFAULT_PROJECTS
    return org


# Seed list of the user's real projects. Editable in config; the model is also
# free to answer "Unsorted" or propose a new name when nothing fits.
_DEFAULT_PROJECTS = [
    "Catholic Connect", "The Woodlands Estates", "ProxiClose", "VisitNote AI",
    "Star of the Sea NIL", "Bible Trivia", "Note Genie", "Mass Times Near Me",
    "WatchLive", "Zellaro Tile", "Daniel Dean", "OnPageSEO", "Amelias Agent",
    "501c3nonprofit", "Maps for Developers", "AI Phone 360", "Nancy's Bake House",
    "Smilux Dental", "SkillPay", "Houston IT Developers", "IoT / Devices",
    "Security Recon",
]

# Keyword fallback: substrings that strongly imply a project, matched against
# the lowered title. Kept small and unambiguous -- this only fires when the
# model is off or unreachable.
_KEYWORD_HINTS = [
    ("catholicconnect", "Catholic Connect"), ("catholic connect", "Catholic Connect"),
    ("woodlands", "The Woodlands Estates"), ("proxiclose", "ProxiClose"),
    ("visitnote", "VisitNote AI"), ("star of the sea", "Star of the Sea NIL"),
    ("sosnil", "Star of the Sea NIL"), ("bible", "Bible Trivia"),
    ("note genie", "Note Genie"), ("notegenie", "Note Genie"),
    ("mass time", "Mass Times Near Me"), ("watchlive", "WatchLive"),
    ("zellaro", "Zellaro Tile"), ("daniel dean", "Daniel Dean"),
    ("onpageseo", "OnPageSEO"), ("amelia", "Amelias Agent"), ("hermes", "Amelias Agent"),
    ("501c3", "501c3nonprofit"), ("mapsfordevelopers", "Maps for Developers"),
    ("aiphone", "AI Phone 360"), ("nancy", "Nancy's Bake House"),
    ("smilux", "Smilux Dental"), ("skillpay", "SkillPay"),
    ("vulnerab", "Security Recon"), ("pentest", "Security Recon"),
    ("x430", "IoT / Devices"), ("navimow", "IoT / Devices"), ("acre", "IoT / Devices"),
]

UNSORTED = "Unsorted"


def _heuristic_project(title: str) -> str:
    low = (title or "").lower()
    for needle, project in _KEYWORD_HINTS:
        if needle in low:
            return project
    return UNSORTED


# --- model call ------------------------------------------------------------


def _is_ollama(base_url: str, org: dict) -> bool:
    style = (org.get("api_style") or "auto").lower()
    if style == "ollama":
        return True
    if style == "openai":
        return False
    # Auto-detect: local Ollama serves on :11434. Anything else is treated as a
    # generic OpenAI-compatible endpoint.
    return ":11434" in base_url


def _classify_with_model(title: str, first_message: str, org: dict) -> str | None:
    base_url = (org.get("base_url") or "").rstrip("/")
    model = org.get("model")
    if not base_url or not model:
        return None

    projects = list(org.get("projects") or [])
    system = (
        "You file a work session under exactly one project. "
        "Reply with ONLY the project name, copied verbatim from the list, or the "
        "single word Unsorted if none clearly fit. No other words."
    )
    user = (
        "Projects:\n- " + "\n- ".join(projects) + "\n\n"
        f"Session title: {title}\n"
        + (f"First message: {first_message[:400]}\n" if first_message else "")
        + "\nWhich project? Answer with one name from the list, or Unsorted."
    )
    headers = {"Content-Type": "application/json"}
    key_env = org.get("api_key_env")
    api_key = os.environ.get(key_env) if key_env else None
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if _is_ollama(base_url, org):
        # Native Ollama endpoint: `think:false` actually silences Qwen/DeepSeek
        # reasoning (the OpenAI-compat /v1 route ignores it and returns empty
        # content while the model burns the token budget "thinking").
        host = base_url[:-3] if base_url.endswith("/v1") else base_url
        url = host + "/api/chat"
        payload = {
            "model": model,
            "think": False,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 24},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        content_path = lambda d: d["message"]["content"]
    else:
        url = base_url + "/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 24,
            "stream": False,
        }
        content_path = lambda d: d["choices"][0]["message"]["content"]

    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = content_path(data)
    except Exception:
        return None
    if not text:
        return None
    return _match_to_project(text, projects)


def _match_to_project(raw: str, projects: list) -> str:
    # Models like to add reasoning or punctuation; strip to the project it names.
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL | re.IGNORECASE)
    text = text.strip().strip(".\"' \n")
    low = text.lower()
    for p in projects:
        if p.lower() == low:
            return p
    for p in projects:
        if p.lower() in low:
            return p
    if "unsorted" in low:
        return UNSORTED
    return UNSORTED


# --- public API ------------------------------------------------------------


def project_for_session(session: dict, *, allow_model: bool = True) -> str:
    """Return the project label for one session, cached across calls.

    ``allow_model=False`` forces the heuristic (useful when rendering must stay
    instant and a cache miss shouldn't trigger a model round-trip inline).
    """
    if not isinstance(session, dict):
        return UNSORTED
    title = (session.get("title") or "").strip()
    if not title:
        return UNSORTED

    sid = str(session.get("session_id") or session.get("id") or "")
    key = _cache_key(sid, title)
    cache = _load_cache()
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("project"):
        return hit["project"]

    org = _organizer_config()
    project = None
    if allow_model and org.get("enabled"):
        first_msg = session.get("first_user_message") or ""
        project = _classify_with_model(title, first_msg, org)
    # Fall back to the keyword map when the model is off/unreachable (None) OR
    # when it hedged to Unsorted but a title keyword clearly names a project
    # (e.g. "X430" -> IoT). The model wins whenever it commits to a project.
    if not project or project == UNSORTED:
        heuristic = _heuristic_project(title)
        if heuristic != UNSORTED:
            project = heuristic
        elif not project:
            project = UNSORTED

    cache[key] = {"project": project, "at": int(time.time())}
    _save_cache()
    return project


# --- background warming -----------------------------------------------------

import threading

_warm_lock = threading.Lock()
_warming = False


def warm_async(sessions: list | None) -> None:
    """Classify any uncached sessions in a background thread.

    The board render path reads with ``allow_model=False`` so it never waits on
    the model; this fills the cache (with the AI's answer) shortly after, so the
    next render upgrades heuristic labels to AI ones. At most one warm thread
    runs at a time, and a model that's off/slow just leaves labels as-is.
    """
    global _warming
    if not sessions:
        return
    with _warm_lock:
        if _warming:
            return
        _warming = True

    def _run():
        global _warming
        try:
            for s in sessions:
                if not isinstance(s, dict) or s.get("is_automation"):
                    continue
                title = (s.get("title") or "").strip()
                if not title:
                    continue
                sid = str(s.get("session_id") or s.get("id") or "")
                if _cache_key(sid, title) in _load_cache():
                    continue
                try:
                    project_for_session(s, allow_model=True)
                except Exception:
                    pass
        finally:
            with _warm_lock:
                _warming = False

    threading.Thread(target=_run, name="session-organizer-warm", daemon=True).start()
