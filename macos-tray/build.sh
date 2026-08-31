#!/bin/bash
# Build the menu-bar app. One file, no dependencies, ~2 seconds.
# Signing (optional, for handing the binary to someone else):
#   codesign --force --sign "Developer ID Application" AmeliaTray
set -euo pipefail
cd "$(dirname "$0")"
swiftc -O -parse-as-library main.swift -o AmeliaTray
echo "built: $(pwd)/AmeliaTray"
