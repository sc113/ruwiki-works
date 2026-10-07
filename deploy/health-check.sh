#!/bin/sh
set -eu
cd "$HOME/www/python/src"
export PYTHONUTF8=1
exec "$HOME/www/python/venv/bin/python" -m toolforge_app check-health "$1"
