#!/usr/bin/env python3
"""Fail the build when the Hermes Agent engine stops honouring what we import.

Two independent checks, either of which can be run alone:

  --mode symbols  (default)  Every name this WebUI imports out of the engine
                             still exists in the engine's source tree.
  --mode version             The installed engine's version is inside the
                             range recorded in UPSTREAM_TESTED_ENGINE.
  --mode all                 Both.

Why this exists
---------------
`scripts/audit_agent_source_dependencies.py` already *reports* how deeply the
WebUI reaches into the engine — 245 findings across 7 classes at the time this
was written. A report is not a gate. It cannot fail, so it cannot tell you that
an engine upgrade just removed something you depend on; you find that out when a
customer's server dies on import.

The coupling is real and some of it has no stability contract at all. We import
leading-underscore names — `_get_auxiliary_task_config`, `_read_main_provider`,
`_estimate_msg_budget_tokens`, `_exhausted_ttl`, `_lookup_supports_vision` —
which upstream is free to rename in any commit without it being a breaking
change on their side. That is a defensible trade (they move fast and the
alternative is reimplementing their model catalogue), but it is only defensible
if something checks.

How it checks
-------------
Statically, never by importing. Importing a module to see whether a symbol
exists runs that module's side effects — which has bitten this codebase before —
and would also require the engine's venv. So both sides are parsed with `ast`:

  * every `from agent.x import y` / `import agent.x` in the WebUI is collected
    by walking the AST, which means comments and strings that merely *mention*
    an import cannot produce a false positive;
  * the engine module is parsed and its module-level bindings collected
    (defs, classes, assignments, and re-exports).

The symbol list is DERIVED, never hardcoded. A hardcoded list rots silently the
first time someone adds an import, and then reports "all clear" about a set that
no longer matches reality.

Usage
-----
    python3 scripts/check_engine_contract.py
    python3 scripts/check_engine_contract.py --engine-dir /path/to/hermes-agent
    python3 scripts/check_engine_contract.py --mode version
    python3 scripts/check_engine_contract.py --self-test

`--engine-dir` is how you evaluate a candidate upgrade without installing it
over the engine you rely on daily: clone the tag somewhere scratch and point
this at it.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Top-level module names that belong to the engine rather than to the WebUI.
# Kept in step with scripts/audit_agent_source_dependencies.py's AGENT_MODULE_ROOTS.
ENGINE_ROOTS = (
    "agent",
    "cron",
    "hermes_cli",
    "hermes_constants",
    "hermes_state",
    "run_agent",
    "tools",
)

# WebUI directories worth scanning. Tests are deliberately excluded: they patch
# and monkeypatch engine internals that production code does not depend on, and
# a test's stale import should not be able to block a release.
WEBUI_SCAN_DIRS = ("api", "scripts")
WEBUI_SCAN_FILES = ("bootstrap.py", "server.py", "mcp_server.py", "connect.py")

PIN_FILE = "UPSTREAM_TESTED_ENGINE"
VERSION_FILE = Path("hermes_cli") / "__init__.py"
OVERRIDE_ENV = "HERMES_WEBUI_ALLOW_UNTESTED_ENGINE"


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def default_engine_dir() -> Path | None:
    """Best-effort locate of the installed engine, mirroring bootstrap.py's search."""
    env = os.getenv("HERMES_WEBUI_AGENT_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    hermes_home = Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))
    candidates = [
        hermes_home / "hermes-agent",
        Path.home() / "hermes-agent",
        REPO_ROOT.parent / "hermes-agent",
        Path("/usr/local/lib/hermes-agent"),
    ]
    for c in candidates:
        if (c / "run_agent.py").exists():
            return c
    return None


def _iter_webui_sources(root: Path):
    for name in WEBUI_SCAN_FILES:
        p = root / name
        if p.is_file():
            yield p
    for d in WEBUI_SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p


def _engine_root_of(module: str) -> str | None:
    head = module.split(".", 1)[0]
    return head if head in ENGINE_ROOTS else None


# --------------------------------------------------------------------------
# what the WebUI asks of the engine
# --------------------------------------------------------------------------

# Exceptions that, when caught around an import, mean the author has already
# decided the symbol is allowed to be missing.
_ABSENCE_TOLERANT = {"ImportError", "ModuleNotFoundError", "AttributeError", "Exception", "BaseException"}


def _handles_absence(node: ast.Try) -> bool:
    """True when this try/except is prepared for the import not resolving."""
    for handler in node.handlers:
        if handler.type is None:  # bare `except:`
            return True
        for sub in ast.walk(handler.type):
            if isinstance(sub, ast.Name) and sub.id in _ABSENCE_TOLERANT:
                return True
            if isinstance(sub, ast.Attribute) and sub.attr in _ABSENCE_TOLERANT:
                return True
    return False


def collect_requirements(root: Path) -> dict[str, dict[str, bool]]:
    """Map engine module -> {imported name: is_required}.

    `is_required` is False when the import sits inside a try/except that catches
    ImportError (or broader). Upstream uses that pattern deliberately — see the
    `CompressionSnapshotStaleError` import in api/streaming.py, whose own comment
    says the fallback "keeps a rolling WebUI/Agent upgrade actionable". Treating
    those as hard failures would make this guard cry wolf, and a guard that cries
    wolf is a guard that gets skipped.

    A bare `import agent.foo` records the module with no names, which still
    asserts the module must exist.
    """
    reqs: dict[str, dict[str, bool]] = {}

    def record(module: str, names, required: bool) -> None:
        bucket = reqs.setdefault(module, {})
        for alias in names:
            if alias.name == "*":
                continue
            # Required wins: if any call site needs it unguarded, it is required.
            bucket[alias.name] = bucket.get(alias.name, False) or required

    def visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, ast.Try):
            body_guarded = guarded or _handles_absence(node)
            for child in node.body:
                visit(child, body_guarded)
            # Handler / else / finally bodies are not protected by this try.
            for group in (node.orelse, node.finalbody):
                for child in group:
                    visit(child, guarded)
            for handler in node.handlers:
                for child in handler.body:
                    visit(child, guarded)
            return
        if isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import — WebUI-internal, not the engine.
            if not node.level and node.module and _engine_root_of(node.module):
                record(node.module, node.names, not guarded)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _engine_root_of(alias.name):
                    reqs.setdefault(alias.name, {})
            return
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    for path in _iter_webui_sources(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError:
            # A file we cannot parse is a different failure; do not mask it here.
            continue
        visit(tree, False)
    return reqs


# --------------------------------------------------------------------------
# what the engine actually provides
# --------------------------------------------------------------------------

def _module_file(engine_dir: Path, module: str) -> Path | None:
    rel = Path(*module.split("."))
    for candidate in (engine_dir / f"{rel}.py", engine_dir / rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _is_package_dir(engine_dir: Path, module: str) -> bool:
    return (engine_dir / Path(*module.split("."))).is_dir()


def module_bindings(path: Path) -> set[str]:
    """Names bound at module top level, without executing anything."""
    names: set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except SyntaxError:
        return names
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # Re-exports count: `from .x import y` makes `y` importable from here.
            for alias in node.names:
                if alias.name == "*":
                    continue
                names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.Try):
            # Optional-dependency shims bind names inside try/except at top level.
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        for leaf in ast.walk(target):
                            if isinstance(leaf, ast.Name):
                                names.add(leaf.id)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias in sub.names:
                        if alias.name != "*":
                            names.add(alias.asname or alias.name.split(".", 1)[0])
    return names


def check_symbols(root: Path, engine_dir: Path) -> tuple[list[str], list[str], int, int]:
    """Return (failures, tolerated, modules_checked, symbols_checked).

    `tolerated` are symbols that are missing but whose import site already
    handles absence — informational, never a build failure.
    """
    reqs = collect_requirements(root)
    failures: list[str] = []
    tolerated: list[str] = []
    symbols = 0
    for module in sorted(reqs):
        path = _module_file(engine_dir, module)
        if path is None:
            if _is_package_dir(engine_dir, module):
                # Namespace package with no __init__; nothing to introspect.
                continue
            failures.append(f"MODULE GONE     {module}  (imported by the WebUI)")
            continue
        provided = module_bindings(path)
        for name, required in sorted(reqs[module].items()):
            symbols += 1
            if name in provided:
                continue
            # A submodule import — `from agent import model_metadata` — is
            # satisfied by the file existing, not by a binding in __init__.
            if _module_file(engine_dir, f"{module}.{name}") is not None:
                continue
            private = " (private, no stability contract)" if name.startswith("_") else ""
            if required:
                failures.append(f"SYMBOL GONE     {module}.{name}{private}")
            else:
                tolerated.append(f"absent, handled  {module}.{name}{private}")
    return failures, tolerated, len(reqs), symbols


# --------------------------------------------------------------------------
# version pin
# --------------------------------------------------------------------------

_VER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VER_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def read_pin(root: Path) -> tuple[tuple[int, int, int] | None, tuple[int, int, int] | None]:
    path = root / PIN_FILE
    if not path.is_file():
        return None, None
    lo = hi = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key == "min":
            lo = _parse_version(value)
        elif key == "max":
            hi = _parse_version(value)
    return lo, hi


def engine_version(engine_dir: Path) -> tuple[int, int, int] | None:
    path = engine_dir / VERSION_FILE
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("__version__"):
            return _parse_version(line)
    return None


# A verdict we could not reach is not a violation. "The pin is missing" and "this
# engine layout has no readable __version__" both mean *unknown*, and refusing to
# start on unknown would block every machine whose engine is laid out differently
# from ours — a pip install, a distro package, a test harness pointing at a
# scratch directory. Only a version we actually read and found out of range is a
# refusal. main() maps these to a distinct exit code so callers can tell the two
# apart instead of inferring it from the message text.
INDETERMINATE = ("NO PIN", "NO VERSION")


def check_version(root: Path, engine_dir: Path) -> list[str]:
    lo, hi = read_pin(root)
    if lo is None and hi is None:
        return [f"NO PIN          {PIN_FILE} is missing or has no min/max"]
    found = engine_version(engine_dir)
    if found is None:
        return [f"NO VERSION      could not read __version__ from {engine_dir / VERSION_FILE}"]
    dotted = ".".join(str(p) for p in found)
    if lo and found < lo:
        return [f"ENGINE TOO OLD  {dotted} is below the tested minimum {'.'.join(map(str, lo))}"]
    if hi and found > hi:
        return [
            f"ENGINE UNTESTED {dotted} is above the tested maximum {'.'.join(map(str, hi))}",
            f"                Run the suite against it and widen `max` in {PIN_FILE},",
            f"                or set {OVERRIDE_ENV}=1 to proceed deliberately.",
        ]
    return []


# --------------------------------------------------------------------------
# self-test — prove the guard goes red before trusting it green
# --------------------------------------------------------------------------

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def self_test() -> int:
    """Build throwaway trees and assert each check fails when it should.

    Fixtures are bound to STRUCTURE, not to any literal that exists in the real
    repo — a fixture pinned to a real symbol name goes stale the moment that
    symbol is renamed, and then quietly stops proving anything.
    """
    failures: list[str] = []

    def expect(label: str, condition: bool) -> None:
        print(f"  {'PASS' if condition else 'FAIL'}  {label}")
        if not condition:
            failures.append(label)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # --- a healthy pair: engine provides everything the WebUI asks for ---
        web = tmp_path / "web"
        eng = tmp_path / "engine"
        _write(web / "api" / "uses.py", "from agent.thing import public_fn, _private_fn\n")
        _write(eng / "agent" / "__init__.py", "")
        _write(
            eng / "agent" / "thing.py",
            "def public_fn():\n    return 1\n\n\ndef _private_fn():\n    return 2\n",
        )
        _write(eng / "run_agent.py", "class AIAgent:\n    pass\n")
        _write(eng / "hermes_cli" / "__init__.py", '__version__ = "0.20.5"\n')
        _write(web / PIN_FILE, "min = 0.20.5\nmax = 0.20.6\n")

        clean, _tol, mods, syms = check_symbols(web, eng)
        expect(f"healthy tree passes (modules={mods} symbols={syms})", not clean and syms == 2)
        expect("healthy version passes", not check_version(web, eng))

        # --- sabotage 1: a public symbol disappears ---
        _write(eng / "agent" / "thing.py", "def _private_fn():\n    return 2\n")
        broke, _, _, _ = check_symbols(web, eng)
        expect("removing a PUBLIC symbol goes red", any("public_fn" in f for f in broke))

        # --- sabotage 2: a private symbol disappears, and is labelled as such ---
        _write(eng / "agent" / "thing.py", "def public_fn():\n    return 1\n")
        broke, _, _, _ = check_symbols(web, eng)
        expect(
            "removing a PRIVATE symbol goes red and is flagged private",
            any("_private_fn" in f and "private" in f for f in broke),
        )

        # --- sabotage 3: the whole module disappears ---
        (eng / "agent" / "thing.py").unlink()
        broke, _, _, _ = check_symbols(web, eng)
        expect("removing the MODULE goes red", any("MODULE GONE" in f for f in broke))

        # --- the false-positive that would make this guard untrustworthy ---
        # An import the author already wrapped in try/except ImportError is a
        # deliberate optional dependency, not a broken contract.
        _write(eng / "agent" / "thing.py", "def public_fn():\n    return 1\n")
        _write(
            web / "api" / "defensive.py",
            "try:\n"
            "    from agent.thing import maybe_absent\n"
            "except ImportError:\n"
            "    maybe_absent = None\n",
        )
        soft, tol, _, _ = check_symbols(web, eng)
        expect(
            "a try/except ImportError import is TOLERATED, not failed",
            not any("maybe_absent" in f for f in soft) and any("maybe_absent" in t for t in tol),
        )

        # ...but the same name imported bare somewhere else must still fail,
        # because one guarded call site does not protect an unguarded one.
        _write(web / "api" / "bare.py", "from agent.thing import maybe_absent\n")
        hard, _, _, _ = check_symbols(web, eng)
        expect(
            "the same symbol imported UNGUARDED elsewhere still goes red",
            any("maybe_absent" in f for f in hard),
        )
        (web / "api" / "bare.py").unlink()
        (web / "api" / "defensive.py").unlink()

        # --- sabotage 4: engine version drifts above the pin ---
        _write(eng / "hermes_cli" / "__init__.py", '__version__ = "0.21.0"\n')
        expect(
            "an engine above the tested max goes red",
            any("UNTESTED" in f for f in check_version(web, eng)),
        )
        _write(eng / "hermes_cli" / "__init__.py", '__version__ = "0.19.0"\n')
        expect(
            "an engine below the tested min goes red",
            any("TOO OLD" in f for f in check_version(web, eng)),
        )

        # --- indeterminate must NOT be treated as a violation ---
        # Shipped as a bug once: an engine directory with no readable
        # __version__ made bootstrap REFUSE TO START. Every test harness
        # pointing at a scratch dir hit it, and so would any machine whose
        # engine is laid out differently (a pip install, a distro package).
        # "We could not tell" and "we checked and it is wrong" are different
        # answers and only the second may stop a server.
        (eng / "hermes_cli" / "__init__.py").unlink()
        unreadable = check_version(web, eng)
        expect(
            "an engine with no readable version reports, but as INDETERMINATE",
            bool(unreadable) and all(p.startswith(INDETERMINATE) for p in unreadable),
        )
        _write(eng / "hermes_cli" / "__init__.py", '__version__ = "0.21.0"\n')
        expect(
            "...while a version we CAN read and is out of range is not indeterminate",
            not all(p.startswith(INDETERMINATE) for p in check_version(web, eng)),
        )

        # --- sabotage 5: a comment that merely MENTIONS an import must not count ---
        # This guard reads its own documentation, so it has to parse code, not text.
        _write(
            web / "api" / "comments_only.py",
            '"""Docstring mentioning from agent.ghost import nothing_here."""\n'
            "# from agent.ghost import also_nothing\n"
            "X = 1\n",
        )
        clean2, _, _, _ = check_symbols(web, eng)
        expect(
            "an import named only in a comment or docstring is NOT collected",
            not any("ghost" in f for f in clean2),
        )

    print()
    if failures:
        print(f"self-test FAILED: {len(failures)} assertion(s) did not hold")
        return 1
    print("self-test passed: every sabotage was caught, and no false positive fired")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("symbols", "version", "all"), default="all")
    ap.add_argument("--engine-dir", help="Engine checkout to check against (default: the installed one)")
    ap.add_argument("--repo-root", default=str(REPO_ROOT))
    ap.add_argument("--self-test", action="store_true", help="Prove the guard goes red, then exit")
    ap.add_argument("--warn-only", action="store_true", help="Report but always exit 0")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    root = Path(args.repo_root).resolve()
    engine_dir = Path(args.engine_dir).expanduser().resolve() if args.engine_dir else default_engine_dir()

    if engine_dir is None or not engine_dir.is_dir():
        # No engine present is not this guard's failure to report — bootstrap
        # installs one. Say so and get out of the way.
        print("engine contract: no engine checkout found; nothing to check")
        return 0

    print(f"engine contract: checking {engine_dir}")
    problems: list[str] = []
    if args.mode in ("symbols", "all"):
        found, tolerated, mods, syms = check_symbols(root, engine_dir)
        print(f"  symbols: {syms} name(s) across {mods} engine module(s)")
        for line in tolerated:
            print(f"  note: {line}")
        problems += found
    if args.mode in ("version", "all"):
        problems += check_version(root, engine_dir)

    if not problems:
        print("  OK")
        return 0

    print()
    for line in problems:
        print(f"  {line}")
    print()

    if args.warn_only:
        return 0
    if os.getenv(OVERRIDE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"  {OVERRIDE_ENV} is set — proceeding anyway.")
        return 0
    if all(p.startswith(INDETERMINATE) for p in problems):
        # Nothing was contradicted; we just could not tell. Exit 3 so bootstrap
        # can carry on while CI, which passes --mode symbols, still fails loudly
        # on a real breach.
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main())
