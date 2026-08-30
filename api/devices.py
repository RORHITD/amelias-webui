"""
Phones, emulators and simulators attached to THIS machine.

Ported from the sandbox agent (amelia-accounts src/agent_server.py), because
the promise runs the other way on a machine somebody owns: a sandbox we rent
has no USB ports and never will, while the laptop this server runs on may have
an Android and an iPhone sitting on the desk beside it. A real device — real
camera, real push delivery, real biometrics — is the one thing a rented
machine can never offer, and it is the reason to pair your own computer at
all. So the WebUI answers the same /api/devices contract the sandbox does,
and the phone app can show what is plugged in without caring which kind of
machine answered.

Discovery only. Nothing here starts, installs or taps anything: it reports
what is present and which tools exist to drive it, and the agent does the
rest through the shell it already has. A second, parallel way to run commands
on somebody's computer is not a feature, it is a second thing to get wrong.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run(argv: list[str], timeout: float = 6.0) -> str:
    """A command that is allowed to be missing. Absent tooling is not an error."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def attached_devices() -> dict:
    out: list[dict] = []

    # Android, physical and emulated. `adb devices -l` marks unauthorised
    # handsets, which is the commonest reason a plugged-in phone does not work.
    # Reported rather than silently omitted: "it is not in the list" sends
    # people hunting a cable problem they do not have.
    for line in _run(["adb", "devices", "-l"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = next((x.split(":", 1)[1] for x in parts if x.startswith("model:")), "")
        out.append({
            "platform": "android",
            "id": serial,
            "name": model.replace("_", " ") or serial,
            "kind": "emulator" if serial.startswith("emulator-") else "physical",
            "ready": state == "device",
            "note": "" if state == "device"
                    else "unauthorised — unlock the phone and accept the prompt"
                    if state == "unauthorized" else state,
        })

    # iOS, physical.
    #
    # `pairingState: paired` does NOT mean the phone is here. devicectl lists
    # every device this Mac has ever paired with, forever — measured on a real
    # machine, four iPhones came back "paired" with none of them plugged in.
    # `transportType` is the field that means present: None for every
    # remembered-but-absent device, and the link kind (wired / localNetwork)
    # for one that is actually reachable.
    dc = _run(["xcrun", "devicectl", "list", "devices", "--json-output", "-"], timeout=12)
    try:
        for d in (json.loads(dc).get("result", {}).get("devices", []) if dc else []):
            props = d.get("deviceProperties", {})
            hw = d.get("hardwareProperties", {})
            conn = d.get("connectionProperties", {})
            transport = conn.get("transportType")
            here = bool(transport) and conn.get("tunnelState") != "unavailable"
            out.append({
                "platform": "ios",
                "id": d.get("identifier", ""),
                "name": props.get("name") or hw.get("marketingName") or "iPhone",
                "kind": "physical",
                "ready": here,
                "note": "" if here
                        else "known to this Mac but not connected right now"
                        if conn.get("pairingState") == "paired"
                        else "not paired with this Mac",
            })
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass

    # Booted simulators. Useful, and deliberately labelled as simulators: a
    # result from one is not evidence about push, the camera or biometrics.
    sim = _run(["xcrun", "simctl", "list", "devices", "booted", "-j"], timeout=12)
    try:
        for _rt, devs in (json.loads(sim).get("devices", {}) if sim else {}).items():
            for d in devs:
                out.append({
                    "platform": "ios", "id": d.get("udid", ""),
                    "name": d.get("name", "Simulator"),
                    "kind": "simulator", "ready": d.get("state") == "Booted", "note": "",
                })
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass

    return {
        "devices": out,
        # What is available to DRIVE them. A device with no driver is one the
        # agent can see and not use, and naming the missing tool is a one-line
        # fix for the person rather than a mystery.
        "tools": {
            "adb": bool(_run(["adb", "version"], timeout=4)),
            "maestro": bool(_run(["maestro", "-v"], timeout=6)),
            "xcrun": bool(_run(["xcrun", "--version"], timeout=4)),
        },
    }


def device_check(device_id: str, app_id: str, platform: str) -> dict:
    """
    Does the app actually come up on a real phone.

    The agent can already drive a phone open-endedly; this exists because the
    question everybody has FIRST — "does it open" — has a yes-or-no answer, is
    the same every time, and asking a language model to run it costs a turn
    and returns prose. A pass proves the build installs and reaches its first
    screen without crashing, which is the failure that wastes the most time
    because it is invisible until somebody picks the phone up. It proves
    nothing about the rest of the app, and says so.
    """
    findings: list[dict] = []

    def say(sev: str, text: str) -> None:
        findings.append({"area": "reliability", "severity": sev,
                         "page": f"{platform} device {device_id}", "says": text})

    if not _run(["maestro", "-v"], timeout=8):
        return {
            "ok": False,
            "needs": "maestro",
            # Named, with the command, because "a tool is missing" that does
            # not say which one or how is a dead end dressed as an explanation.
            "says": "Maestro is not installed on this machine. It is the open-source "
                    "runner this uses to drive a real phone — install it with "
                    "`curl -fsSL https://get.maestro.mobile.dev | bash` and try again.",
            "findings": [],
        }

    flow = (
        "appId: " + app_id + "\n"
        "---\n"
        "- launchApp\n"
        # A real wait, not an assertion on a specific element: this has to work
        # against an app nobody described to us, so the only thing that can be
        # asserted is that something came up and stayed up.
        "- extendedWaitUntil:\n"
        "    visible: \"\"\n"
        "    timeout: 20000\n"
        "- takeScreenshot: /tmp/amelia-device-check\n"
    )
    path = Path("/tmp/amelia-device-check.yaml")
    try:
        path.write_text(flow, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "says": f"Could not write the flow: {e}", "findings": []}

    try:
        r = subprocess.run(
            ["maestro", "test", "--device", device_id, str(path)],
            capture_output=True, text=True, timeout=180,
        )
        output = (r.stdout or "") + (r.stderr or "")
        passed = r.returncode == 0
    except subprocess.TimeoutExpired:
        say("high", "the app did not finish starting within three minutes — on a real "
                    "device that is a hang, not slowness.")
        return {"ok": True, "passed": False, "findings": findings, "output": ""}
    except (FileNotFoundError, OSError) as e:
        return {"ok": False, "says": f"Could not run Maestro: {e}", "findings": []}

    if not passed:
        low = output.lower()
        if "failed to launch" in low or "request to open" in low or "not installed" in low:
            say("high", f"{app_id} would not open on that device — either it is not "
                         "installed or the bundle id is wrong. Install the build first.")
        else:
            say("high", "the app opened and then something went wrong before the first "
                        "screen settled. The runner output has the detail.")
        return {"ok": True, "passed": False, "findings": findings,
                "output": output[-2000:]}

    return {"ok": True, "passed": True, "findings": findings, "output": ""}
