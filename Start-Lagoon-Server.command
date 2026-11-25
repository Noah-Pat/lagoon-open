#!/bin/bash
# Lagoon Open LAN server launcher for macOS (double-click in Finder).
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
  python3 lagoon-server.py
else
  echo "Python 3 was not found."
  echo "Open Terminal and run:  xcode-select --install   (installs Apple's free command line tools, which include python3)"
fi
echo
read -n 1 -s -r -p "Press any key to close this window"
