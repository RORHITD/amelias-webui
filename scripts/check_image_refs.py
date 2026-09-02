#!/usr/bin/env python3
"""Keep every reference to the WebUI container image naming the same image.

The image is named in three places that must agree:

  * docker-compose.two-container.yml / .three-container.yml — what a user pulls
  * static/ui.js — what the in-app update hint tells them to pull
  * .github/workflows/docker-smoke.yml — which re-tags the image it just built
    to that same name, so the compose files resolve during CI

Change one and the others do not follow. Two ways that has already bitten:

  * For a long time every one of them named `ghcr.io/nesquena/hermes-webui`,
    because this fork had zero tags and zero releases and therefore published
    no image at all. Anyone running Amelia in a container was running upstream's
    build, with none of our work in it. Nothing said so.
  * When the in-app hint was first repointed at our own image, that image did
    not exist yet — an anonymous manifest pull returned 403 — so the app would
    have handed customers a command that fails. Naming an image is a claim that
    it is pullable.

So this checks two things: that all the references agree, and that the image
they name is one WE publish rather than someone else's.

    python3 scripts/check_image_refs.py
    python3 scripts/check_image_refs.py --self-test
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files that name the image, and how many references each is expected to carry
# at minimum. Structure, not exact counts: a new compose service naming the
# image should not fail this, but a file that stops naming it entirely should.
SOURCES = (
    "docker-compose.two-container.yml",
    "docker-compose.three-container.yml",
    "static/ui.js",
    ".github/workflows/docker-smoke.yml",
)

# Any ghcr reference that looks like this project's WebUI image. Deliberately
# broad on the owner so a wrong owner is REPORTED rather than skipped — the
# whole point is to catch a reference pointing somewhere it should not.
IMAGE_RE = re.compile(r"ghcr\.io/[A-Za-z0-9._-]+/[A-Za-z0-9._-]*(?:webui|hermes-webui)[A-Za-z0-9._-]*")

OURS = "ghcr.io/rorhitd/amelias-webui"


def references(root: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for rel in SOURCES:
        path = root / rel
        if not path.is_file():
            continue
        hits = set(IMAGE_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
        if hits:
            found[rel] = hits
    return found


def check(root: Path, expected: str = OURS) -> list[str]:
    problems: list[str] = []
    found = references(root)

    missing = [rel for rel in SOURCES if (root / rel).is_file() and rel not in found]
    for rel in missing:
        problems.append(f"NO IMAGE   {rel} names no WebUI image at all — did a rename drop it?")

    for rel, hits in sorted(found.items()):
        for hit in sorted(hits):
            if hit != expected:
                problems.append(f"MISMATCH   {rel} names {hit}, expected {expected}")

    if not found and not missing:
        problems.append("NO SOURCES no file named a WebUI image; this guard is checking nothing")
    return problems


def self_test() -> int:
    fails: list[str] = []

    def expect(label: str, cond: bool) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            fails.append(label)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def write_all(image: str) -> None:
            (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
            (root / "static").mkdir(parents=True, exist_ok=True)
            for rel in SOURCES:
                (root / rel).write_text(f"image: {image}:latest\n", encoding="utf-8")

        write_all(OURS)
        expect("all references agreeing on our image passes", not check(root))

        # The original defect: everything quietly naming upstream's image.
        write_all("ghcr.io/nesquena/hermes-webui")
        broke = check(root)
        expect(
            "every reference pointing at UPSTREAM's image goes red",
            len(broke) == len(SOURCES) and all("MISMATCH" in p for p in broke),
        )

        # The subtler one: a single file left behind after a rename.
        write_all(OURS)
        (root / "static/ui.js").write_text("image: ghcr.io/someoneelse/hermes-webui:latest\n", encoding="utf-8")
        expect(
            "ONE file drifting to a different image goes red",
            any("static/ui.js" in p and "MISMATCH" in p for p in check(root)),
        )

        # A file that stops naming the image at all — a rename that deleted the
        # reference rather than updating it, which no equality check would see.
        write_all(OURS)
        (root / "docker-compose.two-container.yml").write_text("image: postgres:16\n", encoding="utf-8")
        expect(
            "a file that stops naming the image goes red",
            any("NO IMAGE" in p for p in check(root)),
        )

    print()
    if fails:
        print(f"check_image_refs self-test FAILED: {len(fails)} assertion(s) did not hold")
        return 1
    print("check_image_refs self-test passed: every sabotage was caught.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--expect", default=OURS, help="Image every reference must name")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    problems = check(REPO_ROOT, args.expect)
    if not problems:
        found = references(REPO_ROOT)
        total = sum(len(v) for v in found.values())
        print(f"check_image_refs: {total} reference(s) across {len(found)} file(s), all naming {args.expect}. OK")
        return 0

    print("check_image_refs: the container image is named inconsistently.\n")
    for p in problems:
        print(f"  {p}")
    print(
        "\nThese must move together: the compose files are what a user pulls, static/ui.js is what\n"
        "the in-app update hint tells them to pull, and docker-smoke.yml re-tags its build to that\n"
        "name so compose resolves in CI."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
