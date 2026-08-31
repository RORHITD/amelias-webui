# Amelia for Mac (menu bar)

Not another chat window — the phone and web have those. This is the Mac
*being the machine*: start Amelia, hold the relay line, show the pairing
code, all without Terminal.

```
./build.sh          # swiftc, one file, no dependencies
./AmeliaTray        # menu bar: leaf icon, top right
```

The menu shows a green dot when the server answers, the six-digit pairing
code the first time (type it into the phone app: Profile → Connect your own
computer), and one-click "Keep running after restarts" (connect.py --install
under the hood — launchd keeps it alive and respawns it).

`./AmeliaTray --probe` runs the pairing pipeline headless and prints
`CODE=nnnnnn` — used by tests, handy for debugging.

Signing for distribution: `codesign --force --sign "Developer ID Application" AmeliaTray`
(notarization needed before Gatekeeper lets strangers run it: `xcrun notarytool`).
