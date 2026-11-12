#!/bin/bash
cd "$(dirname "$0")"
cd "$(pwd -P)"
if [ ! -x ".venv/bin/python" ]; then
  osascript -e 'display dialog "找不到 Python 環境（.venv）。請先請家人幫你完成安裝。" buttons {"好"} default button 1 with title "Tabby"' >/dev/null
  exit 1
fi
exec .venv/bin/python -m tabbybot gui
