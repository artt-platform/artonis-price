#!/bin/zsh
set -e

cd "$(dirname "$0")"
echo "Starting Artonis Artist Price MVP..."
echo "Open http://127.0.0.1:8765 in your browser."
python3 artonis_price_mvp.py serve --host 127.0.0.1 --port 8765
