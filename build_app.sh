#!/usr/bin/env bash
# Builds "MacBook Optimizer.app" into ./dist using PyInstaller.
# Run from the project folder, with your virtual environment active.
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This script builds a macOS app, so it has to run on a Mac."; exit 1
fi

PY="${PYTHON:-python3}"

if ! "$PY" -c "import tkinter" 2>/dev/null; then
  echo "Tkinter isn't available in $PY. Install Python from python.org and recreate your .venv."; exit 1
fi
TKV="$("$PY" -c 'import tkinter; print(tkinter.TkVersion)')"
if [[ "$TKV" == 8.5* ]]; then
  echo "This Python uses Tk $TKV, which draws blank windows on modern macOS."
  echo "Install Python from python.org (Tk 8.6) and recreate your .venv."; exit 1
fi

echo "==> Installing dependencies"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt -r requirements-build.txt

echo "==> Generating icon"
"$PY" make_icon.py

echo "==> Building app (this takes a minute or two)"
rm -rf build dist
"$PY" -m PyInstaller --noconfirm --clean MacBookOptimizer.spec

echo
echo "Done:  dist/MacBook Optimizer.app"
echo "Put it on your Desktop with:   cp -R \"dist/MacBook Optimizer.app\" ~/Desktop/"
echo "or drag it into your Applications folder."
