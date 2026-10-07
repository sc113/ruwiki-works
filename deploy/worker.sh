#!/bin/sh
set -eu
cd "$HOME/www/python/src"
export PYTHONUNBUFFERED=1
export PYTHONUTF8=1
exec "$HOME/www/python/venv/bin/python" -m toolforge_app worker --task "${1:-all}"
