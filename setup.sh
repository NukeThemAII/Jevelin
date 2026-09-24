#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-cabbage.txt
.venv/bin/python -m cabbage doctor
