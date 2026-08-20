"""Discover skills across every agent's directory, not just Hermes'.

The Skills screen used to list only ``~/.hermes/skills``. That is one of at
least four places skills actually live on a working machine, so the screen
showed a fraction of what the developer had written and none of what they used
day to day:

    ~/.hermes/skills                    Hermes' own (writable)
    ~/.claude/skills                    Claude Code, user level
    ~/.claude/plugins/*/skills          plugin-provided
    <repo>/.claude/skills               project-scoped

This module reads all of them, labels each skill with where it came from, and
can *link* the external ones into the Hermes skills directory so an agent that
only reads that one directory can still run them. Linking uses symlinks rather
than copies: edit the original and every agent sees the change, and removing a
link never destroys a real skill.

Nothing here writes to a directory it does not own. External roots are strictly
read-only; the only mutation is creating/removing symlinks under
``<hermes>/skills/<source-slug>/``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

# Directory names that never contain a skill definition worth listing.
_EXCLUDED = {".git", "node_modules", "__pycache__", ".venv", "venv", "references", "assets"}

# Where linked external skills land inside the Hermes skills dir. Kept distinct
# from hand-written categories so a sync can be reversed by deleting these and
# nothing else.
LINK_DIR_PREFIX = "linked-"

_CACHE: dict = {"at": 0.0, "value": None}
_CACHE_TTL_SECONDS = 30

# Canonical home for skills shared between agents, matching the layout an
# existing third-party `skills` CLI already established here. Its own manifest
# (.skill-lock.json) belongs to that tool and is never written by this module;
# links we create are tracked separately in LINK_MANIFEST.
SHARED_SKILLS_DIR = Path(os.path.expanduser("~")) / ".agents" / "skills"
LINK_MANIFEST = Path(os.path.expanduser("~")) / ".agents" / ".hermes-webui-links.json"

# Agent skill directories a shared skill should be visible from.
def agent_skill_dirs() -> list[tuple[str, Path]]:
    return [("hermes", _hermes_skills_dir()), ("claude", _home() / ".claude" / "skills")]


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _hermes_skills_dir() -> Path:
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home()) / "skills"
    except Exception:
        return Path(os.getenv("HERMES_HOME", str(_home() / ".hermes"))).expanduser() / "skills"


def _project_roots() -> list[Path]:
    """Repos whose ``.claude/skills`` should be scanned.

    Defaults to the active workspace only. Listing every repo on the machine
    would bury the user-level skills under dozens of project-scoped ones, so
    broader scanning is opt-in via HERMES_WEBUI_SKILL_PROJECT_ROOTS
    (colon-separated).
    """
    raw = os.getenv("HERMES_WEBUI_SKILL_PROJECT_ROOTS")
    if raw:
        return [Path(p).expanduser() for p in raw.split(":") if p.strip()]
    try:
        from api.models import get_last_workspace

        ws = get_last_workspace()
        return [Path(str(ws))] if ws else []
    except Exception:
        return []


def discover_sources() -> list[dict]:
    """Every skills root on this machine, with a human label and writability."""
    sources: list[dict] = []

    hermes = _hermes_skills_dir()
    sources.append({"slug": "hermes", "label": "Hermes", "root": hermes, "writable": True})

    # The machine already has a cross-agent convention: a canonical skill in
    # ~/.agents/skills with a relative symlink from each agent's own dir. That
    # is what "usable by any AI" already looks like here, so it is a first-class
    # source rather than something this module invents.
    sources.append({"slug": "shared", "label": "Shared", "root": SHARED_SKILLS_DIR,
                    "writable": True})

    claude_user = _home() / ".claude" / "skills"
    sources.append({"slug": "claude", "label": "Claude Code", "root": claude_user, "writable": False})

    plugins = _home() / ".claude" / "plugins"
    if plugins.is_dir():
        for entry in sorted(plugins.iterdir()):
            if not entry.is_dir() or entry.name in _EXCLUDED:
                continue
            # Plugins nest one level (plugins/<name>/skills) and marketplaces
            # nest two (plugins/marketplaces/<name>/skills).
            candidates = [entry / "skills"]
            if entry.name == "marketplaces":
                candidates = [c / "skills" for c in sorted(entry.iterdir()) if c.is_dir()]
            for cand in candidates:
                if cand.is_dir():
                    owner = cand.parent.name
                    sources.append({
                        "slug": f"plugin-{owner}",
                        "label": f"Plugin: {owner}",
                        "root": cand,
                        "writable": False,
                    })

    for repo in _project_roots():
        cand = repo / ".claude" / "skills"
        if cand.is_dir():
            sources.append({
                "slug": f"project-{repo.name}",
                "label": f"Project: {repo.name}",
                "root": cand,
                "writable": False,
            })

    return [s for s in sources if s["root"].is_dir()]


def _parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter reader for the ``name``/``description`` keys.

    Deliberately self-contained rather than importing the agent's parser: this
    module is also used by tests and by the sync CLI, where the agent package
    may not be importable.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict = {}
    key = None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            key = m.group(1).strip()
            out[key] = m.group(2).strip().strip('"').strip("'")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out.get(key, "") + " " + line.strip()).strip()
    return out


def _iter_skill_files(root: Path, max_depth: int = 4):
    """Yield SKILL.md paths under ``root`` without walking the whole tree."""
    root = Path(root)
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            try:
                if entry.is_symlink() and entry.is_dir():
                    # Follow one level of link (that is how synced skills look)
                    # but never recurse into a loop.
                    pass
                if entry.is_dir():
                    if entry.name in _EXCLUDED or entry.name.startswith("."):
                        continue
                    stack.append((entry, depth + 1))
                elif entry.name == "SKILL.md":
                    yield entry
            except (OSError, PermissionError):
                continue


def aggregate_skills(disabled: set | None = None, use_cache: bool = True) -> dict:
    """All skills from every source, newest label wins on a name collision."""
    now = time.time()
    if use_cache and _CACHE["value"] is not None and (now - _CACHE["at"]) < _CACHE_TTL_SECONDS:
        cached = _CACHE["value"]
    else:
        cached = _scan_all()
        _CACHE["value"] = cached
        _CACHE["at"] = now

    disabled = disabled or set()
    skills = []
    for item in cached:
        entry = dict(item)
        entry["disabled"] = entry["name"] in disabled
        skills.append(entry)
    skills.sort(key=lambda s: (s.get("source_label") or "", (s.get("name") or "").lower()))
    return {
        "success": True,
        "skills": skills,
        "sources": sorted({s.get("source_label") for s in skills if s.get("source_label")}),
        "count": len(skills),
    }


def _scan_all() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for source in discover_sources():
        root = source["root"]
        for skill_md in _iter_skill_files(root):
            # A linked skill would otherwise appear twice: once at its origin
            # and once through the Hermes link that points back at it.
            try:
                real = skill_md.resolve()
            except OSError:
                continue
            key = str(real)
            if key in seen:
                continue
            seen.add(key)
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
            except (OSError, UnicodeDecodeError):
                continue
            fm = _parse_frontmatter(text)
            name = (fm.get("name") or skill_md.parent.name)[:64]
            description = fm.get("description") or ""
            if not description:
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith(("#", "-", "`")):
                        description = line
                        break
            if len(description) > 400:
                description = description[:397] + "..."
            try:
                rel = skill_md.parent.relative_to(root)
                category = rel.parts[0] if len(rel.parts) > 1 else None
            except ValueError:
                category = None
            out.append({
                "name": name,
                "description": description,
                "category": category,
                "source": source["slug"],
                "source_label": source["label"],
                "writable": source["writable"],
                "path": str(skill_md.parent),
            })
    return out


def invalidate_cache() -> None:
    _CACHE["value"] = None
    _CACHE["at"] = 0.0


# ── Making a skill usable by every agent ─────────────────────────────────────
#
# "Available to any AI" here means: the skill's directory is reachable from each
# agent's own skills directory. Symlinks rather than copies, so editing the
# original updates every agent at once and there is exactly one source of truth.
#
# Two invariants make this safe to run repeatedly and safe to undo:
#   * a real directory is never replaced -- only a missing entry or a symlink we
#     already own is written, so a hand-written skill can't be clobbered;
#   * every link created is recorded, so unlinking removes only our own links.


def _read_manifest() -> dict:
    import json

    try:
        return json.loads(LINK_MANIFEST.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _write_manifest(data: dict) -> None:
    import json

    LINK_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = LINK_MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(LINK_MANIFEST)


def _relative_link_target(target: Path, link_parent: Path) -> str:
    """Relative target when both live under $HOME, else absolute.

    The existing links are relative (``../../.agents/skills/brand``); matching
    that keeps the tree portable if the home directory ever moves.
    """
    try:
        return os.path.relpath(target.resolve(), link_parent.resolve())
    except (OSError, ValueError):
        return str(target)


def link_skill(name: str) -> dict:
    """Expose one already-discovered skill to every agent. Idempotent."""
    name = str(name or "").strip().strip("/")
    if not name or "/" in name or name.startswith("."):
        return {"success": False, "error": "invalid skill name"}

    match = next((s for s in _scan_all() if s["name"] == name), None)
    if match is None:
        return {"success": False, "error": f"unknown skill: {name}"}
    origin = Path(match["path"])

    linked, skipped = [], []
    manifest = _read_manifest()
    for agent, agent_dir in agent_skill_dirs():
        dest = agent_dir / origin.name
        try:
            if _is_available_to(origin, agent_dir) and not dest.is_symlink():
                skipped.append(f"{agent} (already in this agent's tree)")
                continue
            if dest.is_symlink():
                if dest.resolve() == origin.resolve():
                    skipped.append(f"{agent} (already linked)")
                else:
                    skipped.append(f"{agent} (points elsewhere)")
                continue
            if dest.exists():
                # A real directory lives here. Never replace it.
                skipped.append(f"{agent} (real directory present)")
                continue
            agent_dir.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(_relative_link_target(origin, agent_dir))
            manifest.setdefault(name, {})[agent] = str(dest)
            linked.append(agent)
        except OSError as exc:
            skipped.append(f"{agent} ({exc.strerror or exc})")

    if linked:
        _write_manifest(manifest)
        invalidate_cache()
    return {"success": True, "skill": name, "linked": linked, "skipped": skipped,
            "origin": str(origin)}


def unlink_skill(name: str) -> dict:
    """Remove only the links this module created for ``name``."""
    manifest = _read_manifest()
    entry = manifest.get(name)
    if not entry:
        return {"success": False, "error": f"no links recorded for {name}"}
    removed, kept = [], []
    for agent, path in list(entry.items()):
        p = Path(path)
        # Guard hard: only ever unlink a symlink we recorded. A real directory
        # at this path means something replaced our link; leave it alone.
        if p.is_symlink():
            try:
                p.unlink()
                removed.append(agent)
                entry.pop(agent, None)
            except OSError as exc:
                kept.append(f"{agent} ({exc.strerror or exc})")
        else:
            kept.append(f"{agent} (not a symlink)")
            entry.pop(agent, None)
    if entry:
        manifest[name] = entry
    else:
        manifest.pop(name, None)
    _write_manifest(manifest)
    invalidate_cache()
    return {"success": True, "skill": name, "removed": removed, "kept": kept}


def _is_available_to(origin: Path, agent_dir: Path) -> bool:
    """Whether ``origin`` is already reachable from ``agent_dir``.

    Containment, not a flat name check: Hermes nests its skills in category
    directories (skills/media/gif-search), so testing only for
    ``agent_dir/<name>`` reported skills as missing from the very agent that
    owns them.
    """
    try:
        origin.resolve().relative_to(agent_dir.resolve())
        return True
    except (OSError, ValueError):
        pass
    dest = agent_dir / origin.name
    try:
        return dest.is_symlink() and dest.resolve() == origin.resolve()
    except OSError:
        return False


def sync_all(dry_run: bool = False) -> dict:
    """Make every discovered skill reachable from every agent's directory."""
    results, would = [], []
    handled: set[str] = set()
    for item in _scan_all():
        name = item["name"]
        origin = Path(item["path"])
        # Linking resolves by NAME, so two skills sharing one name (e.g. a
        # plugin's frontend-design and the user-level one) both map to the same
        # link. Without this guard the second copy is reported pending on every
        # run and the sync never converges.
        if name in handled:
            continue
        handled.add(name)
        missing = [
            agent for agent, agent_dir in agent_skill_dirs()
            if not _is_available_to(origin, agent_dir)
        ]
        if not missing:
            continue
        if dry_run:
            would.append({"skill": name, "missing_from": missing, "origin": str(origin)})
        else:
            results.append(link_skill(name))
    if dry_run:
        return {"success": True, "dry_run": True, "pending": would, "count": len(would)}
    linked = [r for r in results if r.get("linked")]
    return {"success": True, "synced": len(linked), "results": results}
